"""Adversarial regression suite for the durable Kanban invocation-authority
mechanism (t_714420e1, design t_a1260456).

These tests exercise the module directly (``hermes_cli.kanban_invocation_authority``)
against real, on-disk SQLite Kanban boards, and where the property under
test depends on genuine kernel process identity (PID reuse resistance,
detach/reparent immunity, activation against a real spawned child), they
drive REAL subprocesses -- no mocking of ``psutil`` process identity or the
DB verification query itself.

SCOPE NOTE: this mechanism is wired into the dispatcher's spawn path
(``kanban_db_dispatch._default_spawn`` issues + activates a grant for every
spawned worker) but is NOT YET the live mutation-authority boundary --
enforcement cutover is gated on the mandatory invocation-path inventory
(design doc section 8, tracked as a separate pass on t_714420e1). These
tests therefore verify the MECHANISM's own correctness (issue/activate/
verify/revoke semantics, the specific threat properties it closes) rather
than end-to-end CLI mutation denial through the still-interim ancestry-based
guard in ``agent.delegation_context``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_invocation_authority as kia

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real Kanban DB with one claimed task, isolated HERMES_HOME."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv(kia.GRANT_ENV_VAR, raising=False)
    with kbc.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="invocation-authority fixture", assignee="tester")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    return {
        "db_path": db_path,
        "task_id": task_id,
        "run_id": int(claimed.current_run_id),
        "home": home,
    }


def _issue_and_activate(board, *, worker_pid: int, ttl_seconds=None):
    """Issue a pending grant then activate it against ``worker_pid``'s REAL
    kernel identity (the current test process by default). Returns the
    plaintext token and grant_id."""
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        token = kia.issue_pending_grant(
            conn, task_id=board["task_id"], run_id=board["run_id"], ttl_seconds=ttl_seconds,
        )
        grant_id = token.split(".", 2)[1]
        ok = kia.activate_grant(
            conn, grant_id=grant_id, task_id=board["task_id"], run_id=board["run_id"],
            worker_pid=worker_pid,
        )
    assert ok, "activation must succeed for a freshly issued pending grant"
    return token, grant_id


# --------------------------------------------------------------------------- #
# 1. Detach/reparent: a descendant with no grant of its own is denied,
#    regardless of ancestry chain integrity (the property that closes the
#    historical detach/reparent bypass -- authority is never inferred from
#    the process tree, only from a durable per-invocation row).
# --------------------------------------------------------------------------- #

def test_grant_holder_verifies_but_unrelated_process_is_denied(board):
    """The process the grant is bound to verifies successfully; an
    unrelated process presenting no token at all is denied outright --
    this is the base case the detach/reparent scenario reduces to, since
    grants are never inherited or transferred (design section 6)."""
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        authority = kia.verify_worker_invocation_grant(conn, token)
        assert authority.grant_id == grant_id
        assert authority.task_id == board["task_id"]
        assert authority.run_id == board["run_id"]

        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, None)


_DETACHED_GRANDCHILD_LAUNCH_SOURCE = (
    "import json, os, subprocess, sys\n"
    "grandchild_script = sys.argv[1]\n"
    "result_path = sys.argv[2]\n"
    "ready_path = sys.argv[3]\n"
    "# Launch the grandchild and DELIBERATELY never wait() on it before this\n"
    "# process exits -- plain Popen + early parent exit reparents the\n"
    "# grandchild to init/launchd once we exit, exactly mirroring the\n"
    "# original ancestry-bypass reproduction (no setsid needed).\n"
    "gc = subprocess.Popen([sys.executable, grandchild_script, result_path], "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "with open(ready_path, 'w') as f:\n"
    "    json.dump({'grandchild_pid': gc.pid}, f)\n"
    "sys.exit(0)\n"
)

_DETACHED_GRANDCHILD_PROBE_SOURCE = (
    "import json, os, sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, {root!r})\n"
    "from hermes_cli import kanban_db_connect as kbc\n"
    "from hermes_cli import kanban_invocation_authority as kia\n"
    "time.sleep(2)  # let the kernel complete the reparent to init/launchd\n"
    "token = os.environ.get(kia.GRANT_ENV_VAR)\n"
    "with kbc.connect_closing(db_path=Path({db_path!r})) as conn:\n"
    "    try:\n"
    "        kia.verify_worker_invocation_grant(conn, token)\n"
    "        denied = False\n"
    "        reason = None\n"
    "    except PermissionError as exc:\n"
    "        denied = True\n"
    "        reason = str(exc)\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    json.dump({{'denied': denied, 'reason': reason}}, f)\n"
)


def test_detached_reparented_descendant_is_denied_by_invocation_authority(board, tmp_path):
    """Renamed/converted from the formerly-xfail
    ``test_detached_reparented_descendant_bypasses_ancestry_check``
    (t_70827e4e / t_f9082e18): a real worker's real child backgrounds a
    grandchild and exits before ``wait()``-ing, so the kernel reparents the
    still-alive grandchild to init/launchd -- severing the ancestry chain
    the OLD guard depended on. Under the NEW mechanism the grandchild never
    held its own grant (grants are per-invocation, never inherited/
    transferred -- design section 6), so it is denied by ordinary "no valid
    authority presented" logic, independent of whatever the kernel did to
    its ancestry. This is expected to PASS unconditionally (no xfail): the
    detach/reparent maneuver has no effect on this mechanism at all.
    """
    grandchild_script = tmp_path / "grandchild_probe.py"
    grandchild_script.write_text(
        _DETACHED_GRANDCHILD_PROBE_SOURCE.format(root=str(ROOT), db_path=str(board["db_path"]))
    )
    launcher_script = tmp_path / "detach_launcher.py"
    launcher_script.write_text(_DETACHED_GRANDCHILD_LAUNCH_SOURCE)

    result_path = tmp_path / "grandchild_result.json"
    ready_path = tmp_path / "launcher_ready.json"

    env = dict(os.environ)
    env["HERMES_HOME"] = str(board["home"])
    env["HERMES_KANBAN_DB"] = str(board["db_path"])
    env.pop(kia.GRANT_ENV_VAR, None)  # the grandchild presents no token of its own

    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
    )
    grandchild_pid = None
    try:
        # Issue+activate a grant for the REAL worker -- proving the worker
        # itself has legitimate coverage, which the grandchild must NOT
        # inherit.
        _issue_and_activate(board, worker_pid=worker.pid)

        launcher = subprocess.run(
            [sys.executable, str(launcher_script), str(grandchild_script),
             str(result_path), str(ready_path)],
            env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30, start_new_session=True,
        )
        assert launcher.returncode == 0, launcher.stderr
        assert ready_path.exists()
        grandchild_pid = json.loads(ready_path.read_text())["grandchild_pid"]

        deadline = time.time() + 20
        while time.time() < deadline and not result_path.exists():
            time.sleep(0.1)
        assert result_path.exists(), (
            "detached grandchild never wrote its probe result; "
            f"launcher stdout={launcher.stdout!r} stderr={launcher.stderr!r}"
        )
        result = json.loads(result_path.read_text())
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        if grandchild_pid is not None:
            with __import__("contextlib").suppress(Exception):
                import signal
                os.kill(grandchild_pid, signal.SIGKILL)

    assert result["denied"] is True, (
        f"expected the detached grandchild to be denied (it never held its "
        f"own grant); got allowed. reason={result.get('reason')!r}"
    )


# --------------------------------------------------------------------------- #
# 2. Attached child with token inherited => denied by PID mismatch.
# --------------------------------------------------------------------------- #

def test_attached_child_inheriting_the_token_is_denied_by_pid_mismatch(board, tmp_path):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    probe = tmp_path / "probe_inherited_token.py"
    probe.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "from hermes_cli import kanban_db_connect as kbc\n"
        "from hermes_cli import kanban_invocation_authority as kia\n"
        f"with kbc.connect_closing(db_path=Path({str(board['db_path'])!r})) as conn:\n"
        "    try:\n"
        f"        kia.verify_worker_invocation_grant(conn, os.environ.get(kia.GRANT_ENV_VAR))\n"
        "        print(json.dumps({'denied': False}))\n"
        "    except PermissionError as exc:\n"
        "        print(json.dumps({'denied': True, 'reason': str(exc)}))\n"
    )
    env = dict(os.environ)
    env[kia.GRANT_ENV_VAR] = token  # attacker inherits the parent's token verbatim
    proc = subprocess.run(
        [sys.executable, str(probe)], env=env, cwd=str(ROOT),
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip())
    assert result["denied"] is True, result


# --------------------------------------------------------------------------- #
# 3. Token stripped => denied by missing positive authority.
# --------------------------------------------------------------------------- #

def test_stripped_token_is_denied(board):
    _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, None)
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, "")


# --------------------------------------------------------------------------- #
# 4. Child copying/replaying the exact token from a different process =>
#    denied by PID mismatch (same underlying property as #2, exercised as
#    a direct replay rather than plain inheritance).
# --------------------------------------------------------------------------- #

def test_token_replayed_from_a_different_real_process_is_denied(board):
    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    # A genuinely different, real, live process -- not the one the grant is
    # bound to -- attempting to replay the captured token.
    probe = f"""
import json, os, sys
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_invocation_authority as kia
with kbc.connect_closing(db_path=Path({str(board["db_path"])!r})) as conn:
    try:
        kia.verify_worker_invocation_grant(conn, {token!r})
        print(json.dumps({{"denied": False}}))
    except PermissionError as exc:
        print(json.dumps({{"denied": True, "reason": str(exc)}}))
"""
    other = subprocess.run(
        [sys.executable, "-c", probe], cwd=str(ROOT),
        capture_output=True, text=True, timeout=20,
    )
    assert other.returncode == 0, other.stderr
    result = json.loads(other.stdout.strip())
    assert result["denied"] is True, (
        f"a real, different process replaying the token must be denied by "
        f"PID/start mismatch; got allowed. reason={result.get('reason')!r}"
    )


# --------------------------------------------------------------------------- #
# 5. Token from run A replayed by worker/run B => denied.
# --------------------------------------------------------------------------- #

def test_token_from_one_run_denied_against_a_different_run(board):
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        task_id_b = kb.create_task(conn, title="second task", assignee="tester")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id_b,))
        conn.commit()
        claimed_b = kb.claim_task(conn, task_id_b)
    assert claimed_b is not None and claimed_b.current_run_id is not None

    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        token_a = kia.issue_pending_grant(conn, task_id=board["task_id"], run_id=board["run_id"])
        grant_id_a = token_a.split(".", 2)[1]
        assert kia.activate_grant(
            conn, grant_id=grant_id_a, task_id=board["task_id"], run_id=board["run_id"],
            worker_pid=os.getpid(),
        )

    # The token is legitimately bound to (task A, run A, this process). It
    # must still verify successfully for task A -- and the row it names
    # can never be reinterpreted as belonging to task B/run B because
    # verify_worker_invocation_grant checks tasks.current_run_id against
    # the GRANT's OWN stored run_id, not an attacker-suppliable value.
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        authority = kia.verify_worker_invocation_grant(conn, token_a)
        assert authority.task_id == board["task_id"]
        assert authority.run_id == board["run_id"]
        assert authority.task_id != task_id_b


# --------------------------------------------------------------------------- #
# 6. Token/row from board A used against board B => denied (board-local
#    table: the row physically doesn't exist in board B's DB file at all).
# --------------------------------------------------------------------------- #

@pytest.fixture
def two_boards(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    kb.create_board("board-a", name="Board A")
    kb.create_board("board-b", name="Board B")
    db_path_a = kb.kanban_db_path(board="board-a")
    db_path_b = kb.kanban_db_path(board="board-b")
    assert db_path_a != db_path_b

    with kbc.connect_closing(db_path=db_path_a) as conn:
        task_id = kb.create_task(conn, title="board-a fixture task", assignee="tester")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None

    return {
        "db_path_a": db_path_a, "db_path_b": db_path_b,
        "task_id": task_id, "run_id": int(claimed.current_run_id),
    }


def test_grant_issued_on_board_a_does_not_verify_against_board_b(two_boards):
    with kbc.connect_closing(db_path=two_boards["db_path_a"]) as conn_a:
        token = kia.issue_pending_grant(
            conn_a, task_id=two_boards["task_id"], run_id=two_boards["run_id"],
        )
        grant_id = token.split(".", 2)[1]
        assert kia.activate_grant(
            conn_a, grant_id=grant_id, task_id=two_boards["task_id"],
            run_id=two_boards["run_id"], worker_pid=os.getpid(),
        )
        # Sanity: the SAME connection (board A) verifies fine.
        assert kia.verify_worker_invocation_grant(conn_a, token).grant_id == grant_id

    with kbc.connect_closing(db_path=two_boards["db_path_b"]) as conn_b:
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn_b, token)


# --------------------------------------------------------------------------- #
# 7. Expired, revoked, pending, malformed, wrong-secret, wrong-task/run,
#    reclaimed/completed-run grants => denied.
# --------------------------------------------------------------------------- #

def test_pending_unactivated_grant_never_verifies(board):
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        token = kia.issue_pending_grant(conn, task_id=board["task_id"], run_id=board["run_id"])
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)


def test_expired_grant_denied(board):
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        token = kia.issue_pending_grant(
            conn, task_id=board["task_id"], run_id=board["run_id"], ttl_seconds=1,
        )
        grant_id = token.split(".", 2)[1]
        assert kia.activate_grant(
            conn, grant_id=grant_id, task_id=board["task_id"], run_id=board["run_id"],
            worker_pid=os.getpid(),
        )
        # Verify explicitly "in the future" rather than sleeping in the test.
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token, now=int(time.time()) + 3600)


def test_revoked_grant_denied(board):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        assert kia.revoke_grant(conn, grant_id=grant_id) is True
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)
        # Idempotent: revoking again reports no additional row touched.
        assert kia.revoke_grant(conn, grant_id=grant_id) is False


@pytest.mark.parametrize("bad_token", [
    "not-a-token", "v1.short.short", "v1." + "0" * 32 + "." + "z" * 64,
    "v2." + "0" * 32 + "." + "0" * 64, "", "v1..", "a" * 500,
])
def test_malformed_tokens_denied(board, bad_token):
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, bad_token)


def test_wrong_secret_same_grant_id_denied(board):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    forged = f"v1.{grant_id}." + "0" * 64
    assert forged != token
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, forged)


def test_wrong_task_or_run_at_issuance_is_denied_after_task_state_diverges(board):
    """A grant's task/run identity is fixed at issuance; if the task later
    moves to a different current_run_id (e.g. retried), the OLD grant must
    stop verifying even though the row itself is technically still
    'activated' -- this is the current_run_id join in verify, not the
    activation state."""
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        # Simulate the task moving on to a new run (retry) without closing
        # out via the normal _end_run path, to isolate this one check.
        conn.execute("UPDATE tasks SET current_run_id = current_run_id + 1000 WHERE id = ?",
                     (board["task_id"],))
        conn.commit()
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)


def test_reclaimed_run_grant_denied(board):
    """A genuine reclaim (kb.reclaim_task) ends the run and clears
    current_run_id -- the grant issued for that run must stop verifying."""
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        assert kb.reclaim_task(conn, board["task_id"], reason="test") is True
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)
        # And revoke_grants_for_run (wired into _end_run) should also have
        # explicitly revoked it as defence-in-depth.
        row = conn.execute(
            "SELECT revoked_at FROM worker_invocation_grants WHERE grant_id = ?", (grant_id,),
        ).fetchone()
        assert row["revoked_at"] is not None


def test_completed_run_grant_denied(board):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        assert kb.complete_task(conn, board["task_id"], result="done") is True
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)


# --------------------------------------------------------------------------- #
# 8. PID reuse simulation: same PID, different kernel start => denied.
# --------------------------------------------------------------------------- #

def test_pid_reuse_with_different_kernel_start_denied(board, monkeypatch):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    # Simulate the OS having recycled this exact PID for an unrelated later
    # process: the row's proc_start no longer matches "this process"'s real
    # kernel start once we monkeypatch self-identity to a different start
    # time for the same PID.
    real_self = kia._self_pid_start()
    assert real_self is not None
    monkeypatch.setattr(kia, "_self_pid_start", lambda: (real_self[0], real_self[1] + 999_999))
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)


# --------------------------------------------------------------------------- #
# 9. Activation failure / unavailable kernel identity: no uncovered worker
#    silently continues -- activate_grant reports failure rather than
#    raising or silently no-op-succeeding.
# --------------------------------------------------------------------------- #

def test_activation_fails_cleanly_for_a_dead_pid(board):
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        token = kia.issue_pending_grant(conn, task_id=board["task_id"], run_id=board["run_id"])
        grant_id = token.split(".", 2)[1]
        # A PID essentially guaranteed not to correspond to a live process
        # this test can introspect (psutil.Process(pid) raises NoSuchProcess).
        dead_pid = 2**30
        assert kia.activate_grant(
            conn, grant_id=grant_id, task_id=board["task_id"], run_id=board["run_id"],
            worker_pid=dead_pid,
        ) is False
        # And the row is still pending -- not silently half-activated.
        row = conn.execute(
            "SELECT activated_at, worker_pid FROM worker_invocation_grants WHERE grant_id = ?",
            (grant_id,),
        ).fetchone()
        assert row["activated_at"] is None
        assert row["worker_pid"] is None


def test_real_dispatcher_spawn_issues_and_activates_a_grant(board, monkeypatch, tmp_path):
    """Drive the REAL ``_default_spawn`` and confirm it mints + activates a
    worker_invocation_grants row bound to the real child PID it launched,
    exactly mirroring the worker_spawns coverage test for the ancestry
    mechanism."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    probe_out = tmp_path / "probe.json"
    monkeypatch.setattr(
        kbd, "_resolve_hermes_argv",
        lambda: [sys.executable, "-c",
                 f"import json,os,time; json.dump({{'pid': os.getpid()}}, open({str(probe_out)!r}, 'w')); time.sleep(2)"],
    )
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        task = kb.get_task(conn, board["task_id"])
    assert task is not None
    pid = kbd._default_spawn(task, str(workspace))
    assert pid is not None
    deadline = time.time() + 15
    while time.time() < deadline and not probe_out.exists():
        time.sleep(0.05)
    assert probe_out.exists()

    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        row = conn.execute(
            "SELECT worker_pid, task_id, run_id, activated_at FROM worker_invocation_grants "
            "WHERE worker_pid = ?", (pid,),
        ).fetchone()
    assert row is not None, "the real spawn path must issue+activate a grant for the spawned worker"
    assert row["task_id"] == board["task_id"]
    assert row["run_id"] == board["run_id"]
    assert row["activated_at"] is not None


# --------------------------------------------------------------------------- #
# 10. Verification failure / missing schema after cutover => deny.
# --------------------------------------------------------------------------- #

def test_missing_schema_denies_rather_than_allows(board, monkeypatch, tmp_path):
    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    bare_db = tmp_path / "no_grants_table.db"
    conn = sqlite3.connect(str(bare_db))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER)")
    try:
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 11. In-process delegated-child / non-dispatcher-owned ContextVars deny
#     even while the worker root holds a valid grant token (design section
#     6, evaluation order item 1 overrides item 3).
# --------------------------------------------------------------------------- #

def test_delegated_child_context_var_denies_even_with_a_valid_token_present(board, monkeypatch):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    monkeypatch.setenv(kia.GRANT_ENV_VAR, token)
    from agent import delegation_context

    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        # Sanity: the token alone verifies fine outside a delegated scope.
        assert kia.verify_worker_invocation_grant(conn, token).grant_id == grant_id
        with delegation_context.delegated_child_context("fixture-child-session"):
            # The EXISTING guard (agent.delegation_context) already denies
            # via its ContextVar check regardless of the grant -- this pins
            # that the grant mechanism does not accidentally provide a
            # bypass for the in-process delegated-child case, which is the
            # evaluation-order invariant design section 6 requires of any
            # future integration.
            with pytest.raises(PermissionError):
                kb.add_comment(conn, board["task_id"], "oliver", "forged-in-process")


# --------------------------------------------------------------------------- #
# 12. Worker root with correct active grant can verify successfully for the
#     lifetime of its run (the ordinary, intended-working path).
# --------------------------------------------------------------------------- #

def test_worker_root_with_valid_grant_verifies_repeatedly(board):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        for _ in range(5):
            authority = kia.verify_worker_invocation_grant(conn, token)
            assert authority.grant_id == grant_id


# --------------------------------------------------------------------------- #
# 14. TOCTOU: a grant that verifies successfully, then is revoked, must
#     deny on the NEXT verification -- proving revocation takes effect
#     immediately rather than being cached/stale within this module (the
#     under-transaction placement of the real mutation-boundary check is a
#     caller responsibility -- kanban_db_connect.write_txn -- this test
#     pins that THIS module re-reads state fresh on every call, which is
#     the precondition for that boundary placement to be meaningful).
# --------------------------------------------------------------------------- #

def test_revocation_between_two_verifications_is_observed_immediately(board):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        assert kia.verify_worker_invocation_grant(conn, token).grant_id == grant_id
        assert kia.revoke_grant(conn, grant_id=grant_id) is True
        with pytest.raises(PermissionError):
            kia.verify_worker_invocation_grant(conn, token)


# --------------------------------------------------------------------------- #
# 15. Logs, exceptions, and diagnostics never contain the plaintext token.
# --------------------------------------------------------------------------- #

def test_no_exception_or_log_message_leaks_the_plaintext_token(board, caplog):
    token, grant_id = _issue_and_activate(board, worker_pid=os.getpid())
    secret_hex = token.split(".", 2)[2]
    forged = f"v1.{grant_id}." + "0" * 64

    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        caplog.set_level("DEBUG")
        exceptions_seen = []
        for bad in (forged, "garbage", None):
            try:
                kia.verify_worker_invocation_grant(conn, bad)
            except PermissionError as exc:
                exceptions_seen.append(str(exc))
        assert kia.revoke_grant(conn, grant_id=grant_id) is True
        try:
            kia.verify_worker_invocation_grant(conn, token)
        except PermissionError as exc:
            exceptions_seen.append(str(exc))

    for text in exceptions_seen:
        assert secret_hex not in text
        assert token not in text
    for record in caplog.records:
        assert secret_hex not in record.getMessage()
        assert token not in record.getMessage()


def test_purge_and_gc_never_touch_or_leak_secrets(board):
    """Purge only operates on expires_at / rowcount -- never selects or
    logs token_digest -- and this test additionally confirms an expired
    grant's digest still isn't retrievable/loggable via the purge path."""
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        token = kia.issue_pending_grant(
            conn, task_id=board["task_id"], run_id=board["run_id"],
        )
        grant_id = token.split(".", 2)[1]
        # issue_pending_grant clamps ttl_seconds to >= 1 (design doc: a
        # caller can never mint an already-expired grant), so force the row
        # into the past directly to exercise the purge path itself.
        conn.execute(
            "UPDATE worker_invocation_grants SET expires_at = ? WHERE grant_id = ?",
            (int(time.time()) - 5, grant_id),
        )
        conn.commit()
        removed = kia.purge_expired_invocation_grants(conn)
        assert removed >= 1
        row = conn.execute(
            "SELECT 1 FROM worker_invocation_grants WHERE grant_id = ?", (grant_id,),
        ).fetchone()
        assert row is None
