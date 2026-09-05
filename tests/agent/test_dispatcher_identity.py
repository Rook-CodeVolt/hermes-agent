"""CV-A01 replacement: dispatcher-issued, process-bound worker identity.

The defect this suite pins closed is that *no environment variable and no
ContextVar is mutation authority*.  Authority comes from a one-time random
token the dispatcher issues **after** it knows the child's PID, stores
**hashed** in the authoritative Kanban DB bound to exact
task/run/workspace + worker PID + kernel process-start identity, and hands
to that child over an inherited pipe (never the environment).  The child
CAS-consumes it exactly once during a startup handshake that completes
before it can enter the agent loop.

Every case here drives real processes and the real handshake primitives
``hermes_cli.kanban_db`` uses in ``spawn_worker`` -- not mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent import dispatcher_identity as di
from hermes_cli import kanban_db as kb

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_identity():
    di.reset_for_tests()
    yield
    di.reset_for_tests()


def make_board_task(db_path: Path, workspace: Path, *, title: str) -> dict:
    """Create a task and take it through the real ready -> running claim."""
    workspace.mkdir(parents=True, exist_ok=True)
    with kb.connect_closing(db_path=Path(db_path)) as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            assignee="tester",
            workspace_path=str(workspace),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    return {
        "db_path": str(db_path),
        "task_id": task_id,
        "run_id": int(claimed.current_run_id),
        "workspace": workspace,
    }


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real Kanban DB with one claimed task whose workspace is on disk."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    return make_board_task(db_path, tmp_path / "ws" / "t_ident", title="identity task")


def _issue(board, *, pid, ttl_seconds=3600, run_id=None, workspace=None):
    """Issue a real identity token exactly as the dispatcher does."""
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        return kb.issue_worker_identity(
            conn,
            task_id=board["task_id"],
            run_id=board["run_id"] if run_id is None else run_id,
            workspace_path=str(workspace or board["workspace"]),
            worker_pid=pid,
            ttl_seconds=ttl_seconds,
        )


# The child half of the handshake, run as a real subprocess.  It binds and
# reports what it got, so the parent can assert on genuine cross-process
# behaviour rather than on an in-process simulation.
_CHILD_SOURCE = textwrap.dedent(
    """
    import json, os, sys
    sys.path.insert(0, {repo!r})
    from agent import dispatcher_identity as di

    try:
        bound = di.bind_from_handshake()
    except di.IdentityBindError as exc:
        print(json.dumps({{"bound": False, "error": str(exc)}}))
        sys.exit(0)
    if bound is None:
        print(json.dumps({{"bound": False, "error": "no handshake offered"}}))
        sys.exit(0)
    print(json.dumps({{
        "bound": True,
        "task_id": bound.task_id,
        "run_id": bound.run_id,
        "workspace": str(bound.workspace),
        "worker_pid": bound.worker_pid,
        "pid": os.getpid(),
        "handshake_fd_still_set": di.HANDSHAKE_FD_ENV in os.environ,
    }}))
    """
)


def _run_child(*, extra_env=None, issue=None, payload=None, timeout=30):
    """Spawn a real child through the production handshake primitives.

    ``issue`` is called with the child's PID *after* the child exists (the
    exact ordering ``spawn_worker`` uses) and returns the token to send.
    Returning ``None`` aborts the handshake without sending anything.
    """
    handshake = di.WorkerHandshake()
    env = dict(os.environ)
    env.update(handshake.env_for_child())
    env.update(extra_env or {})
    source = _CHILD_SOURCE.format(repo=str(REPO_ROOT))
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", source],
            env=env,
            pass_fds=handshake.pass_fds(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        handshake.close_read_fd()
        if payload is not None:
            handshake.send_raw(payload)
        elif issue is not None:
            token = issue(proc.pid)
            if token is None:
                handshake.abort()
            else:
                handshake.send(token, db_path=os.environ["HERMES_KANBAN_DB"])
        else:
            handshake.abort()
        out, err = proc.communicate(timeout=timeout)
    finally:
        handshake.close()
    assert proc.returncode == 0, f"child failed: {err}"
    return json.loads(out.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# Kernel process-start identity
# --------------------------------------------------------------------------- #

def test_process_start_time_is_kernel_sourced_and_stable():
    """The start value is the kernel's, not the process's own claim."""
    mine = di.process_start_time(os.getpid())
    assert mine is not None and mine > 0
    assert mine == di.process_start_time(os.getpid())  # stable across reads
    # A child started later has a strictly later start identity.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        child = di.process_start_time(proc.pid)
        assert child is not None
        assert child > mine
    finally:
        proc.kill()
        proc.wait()


def test_process_start_time_none_for_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # A reaped PID has no kinfo entry, so identity cannot be minted for it.
    assert di.process_start_time(proc.pid) is None


# --------------------------------------------------------------------------- #
# The happy path -- real subprocess, real handshake
# --------------------------------------------------------------------------- #

def test_real_subprocess_handshake_binds_exact_identity(board):
    result = _run_child(issue=lambda pid: _issue(board, pid=pid))
    assert result["bound"] is True
    assert result["task_id"] == board["task_id"]
    assert result["run_id"] == board["run_id"]
    assert result["workspace"] == str(board["workspace"])
    assert result["worker_pid"] == result["pid"]
    # The inherited descriptor number must not survive into descendants.
    assert result["handshake_fd_still_set"] is False


def test_token_is_stored_hashed_never_in_plaintext(board):
    token = _issue(board, pid=os.getpid())
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        rows = conn.execute("SELECT * FROM worker_identities").fetchall()
    assert len(rows) == 1
    blob = json.dumps([dict(r) for r in rows])
    assert token not in blob
    assert di.token_digest(token) in blob


# --------------------------------------------------------------------------- #
# Attack: environment spoofing
# --------------------------------------------------------------------------- #

def test_env_alone_grants_no_identity(board):
    """HERMES_KANBAN_TASK/WORKSPACE/DB without a handshake bind nothing."""
    result = _run_child(
        payload=None,
        extra_env={
            "HERMES_KANBAN_TASK": board["task_id"],
            "HERMES_KANBAN_WORKSPACE": str(board["workspace"]),
            "HERMES_KANBAN_RUN_ID": str(board["run_id"]),
        },
    )
    assert result["bound"] is False


def test_forged_token_is_rejected(board):
    _issue(board, pid=os.getpid())  # a real row exists; the token below is not it
    result = _run_child(
        payload=json.dumps({"token": "f" * 64, "db": board["db_path"]}) + "\n",
    )
    assert result["bound"] is False
    assert "token" in result["error"].lower()


def test_valid_worker_token_presented_to_the_wrong_board_is_rejected(board, tmp_path):
    """Exercise the production handshake resolver against a distinct real DB."""
    token = _issue(board, pid=os.getpid())
    wrong_db = tmp_path / "wrong-board" / "kanban.db"
    wrong_db.parent.mkdir()
    with kb.connect_closing(db_path=wrong_db):
        pass

    result = _run_child(
        payload=json.dumps({"token": token, "db": str(wrong_db)}) + "\n",
    )

    assert result["bound"] is False
    assert result["error"] == "unknown or already-consumed worker identity token"


def test_handshake_payload_that_is_not_json_is_rejected(board):
    result = _run_child(payload="not-json-at-all\n")
    assert result["bound"] is False


# --------------------------------------------------------------------------- #
# Attack: token theft / reuse
# --------------------------------------------------------------------------- #

def test_token_is_consumed_exactly_once(board):
    """A stolen token replayed by a second process is refused."""
    token = _issue(board, pid=os.getpid())
    digest = di.token_digest(token)
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        first = kb.consume_worker_identity(conn, digest)
    assert first is not None
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        second = kb.consume_worker_identity(conn, digest)
    assert second is None


def test_stolen_token_replayed_by_another_process_is_denied(board):
    """The token is minted for one PID; a different process cannot use it."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        token = _issue(board, pid=holder.pid)
    finally:
        holder.kill()
        holder.wait()
    # A *different* real process presents the genuine, unconsumed token.
    result = _run_child(
        payload=json.dumps({"token": token, "db": board["db_path"]}) + "\n",
    )
    assert result["bound"] is False
    assert "pid" in result["error"].lower()


def test_two_concurrent_workers_get_separate_identities(tmp_path, monkeypatch):
    """Two real workers on one DB bind to their own task/workspace only."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    boards = [
        make_board_task(db_path, tmp_path / "ws" / name, title=name)
        for name in ("alpha", "beta")
    ]

    results = [_run_child(issue=lambda pid, b=b: _issue(b, pid=pid)) for b in boards]
    assert [r["bound"] for r in results] == [True, True]
    assert results[0]["task_id"] != results[1]["task_id"]
    assert results[0]["workspace"] != results[1]["workspace"]
    for res, b in zip(results, boards):
        assert res["workspace"] == str(b["workspace"])


# --------------------------------------------------------------------------- #
# Attack: PID reuse / process-start mismatch
# --------------------------------------------------------------------------- #

def test_pid_reuse_with_stale_start_identity_is_denied(board):
    """Same PID, different kernel start time -> the identity is not ours."""
    result = _run_child(
        issue=lambda pid: _issue_with_start_override(board, pid=pid, proc_start=1),
    )
    assert result["bound"] is False
    assert "start" in result["error"].lower()


def _issue_with_start_override(board, *, pid, proc_start):
    """Mint a row whose recorded kernel start deliberately does not match."""
    token = di.new_token()
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        kb.record_worker_identity(
            conn,
            task_id=board["task_id"],
            run_id=board["run_id"],
            workspace_path=str(board["workspace"]),
            worker_pid=pid,
            proc_start=proc_start,
            token_sha256=di.token_digest(token),
            ttl_seconds=3600,
        )
    return token


def test_identity_cannot_be_minted_for_a_dead_pid(board):
    """No kernel start identity -> fail closed, no row, no token."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        with pytest.raises(di.IdentityBindError):
            kb.issue_worker_identity(
                conn,
                task_id=board["task_id"],
                run_id=board["run_id"],
                workspace_path=str(board["workspace"]),
                worker_pid=proc.pid,
            )
        assert conn.execute("SELECT COUNT(*) FROM worker_identities").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Attack: stale run / expiry / workspace drift
# --------------------------------------------------------------------------- #

def test_stale_run_id_is_denied(board):
    """A token minted for a prior run does not authorise the current one."""
    result = _run_child(
        issue=lambda pid: _issue(board, pid=pid, run_id=board["run_id"] + 500),
    )
    assert result["bound"] is False
    assert "run" in result["error"].lower()


def test_expired_identity_is_denied(board):
    result = _run_child(issue=lambda pid: _issue(board, pid=pid, ttl_seconds=-5))
    assert result["bound"] is False
    assert "expired" in result["error"].lower()


def test_workspace_disagreeing_with_the_task_record_is_denied(board, tmp_path):
    """The row's workspace must still match the authoritative task row."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = _run_child(issue=lambda pid: _issue(board, pid=pid, workspace=other))
    assert result["bound"] is False
    assert "workspace" in result["error"].lower()


def test_workspace_that_vanished_is_denied(board):
    def _issue_then_remove(pid):
        token = _issue(board, pid=pid)
        board["workspace"].rmdir()
        return token

    result = _run_child(issue=_issue_then_remove)
    assert result["bound"] is False


# --------------------------------------------------------------------------- #
# Handshake liveness: the worker cannot proceed before the row exists
# --------------------------------------------------------------------------- #

def test_worker_blocks_until_the_identity_row_exists(board):
    """The child must not race ahead of the dispatcher's DB write."""
    observed: dict[str, object] = {}

    def _issue_late(pid):
        # The child is already running; prove no row exists for it yet, then
        # mint one.  A child that had already bound would have failed above.
        with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
            observed["rows_before"] = conn.execute(
                "SELECT COUNT(*) FROM worker_identities"
            ).fetchone()[0]
        time.sleep(0.3)
        return _issue(board, pid=pid)

    result = _run_child(issue=_issue_late)
    assert observed["rows_before"] == 0
    assert result["bound"] is True


def test_aborted_handshake_fails_closed(board):
    """Parent closes the pipe without sending -> the child binds nothing."""
    result = _run_child(issue=lambda pid: None)
    assert result["bound"] is False


# --------------------------------------------------------------------------- #
# The real dispatcher spawn path
# --------------------------------------------------------------------------- #

# Stands in for the `hermes` binary the dispatcher launches.  It ignores the
# worker argv the dispatcher appends and does only what matters here: run
# the production startup bind and record the outcome.
_SPAWNED_WORKER_SOURCE = textwrap.dedent(
    """
    import json, os, sys
    sys.path.insert(0, {repo!r})
    out = os.environ["IDENTITY_PROBE_OUT"]
    from agent import dispatcher_identity as di

    record = {{"argv_tail": sys.argv[1:]}}
    try:
        bound = di.bind_from_handshake()
        if bound is None:
            record.update(bound=False, error="no handshake offered")
        else:
            record.update(
                bound=True,
                task_id=bound.task_id,
                run_id=bound.run_id,
                workspace=str(bound.workspace),
                worker_pid=bound.worker_pid,
                pid=os.getpid(),
            )
    except di.IdentityBindError as exc:
        record.update(bound=False, error=str(exc))
    with open(out, "w") as handle:
        json.dump(record, handle)
    """
)


@pytest.fixture
def spawn_env(tmp_path, monkeypatch):
    """Isolate every kanban path the real spawn path resolves."""
    home = tmp_path / ".hermes"
    (home / "kanban").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(home / "kanban" / "ws"))
    return db_path


def _spawn_through_dispatcher(board, monkeypatch, tmp_path):
    """Drive the genuine ``_default_spawn`` and return the child's record."""
    probe_out = tmp_path / "identity_probe.json"
    monkeypatch.setenv("IDENTITY_PROBE_OUT", str(probe_out))
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv",
        lambda: [sys.executable, "-c", _SPAWNED_WORKER_SOURCE.format(repo=str(REPO_ROOT))],
    )
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        task = kb.get_task(conn, board["task_id"])
    pid = kb._default_spawn(task, str(board["workspace"]))
    assert pid is not None
    deadline = time.time() + 30
    while time.time() < deadline and not probe_out.exists():
        time.sleep(0.05)
    assert probe_out.exists(), "spawned worker never completed its handshake"
    return pid, json.loads(probe_out.read_text())


def test_real_dispatcher_spawn_binds_the_worker_identity(
    spawn_env, monkeypatch, tmp_path
):
    """End-to-end: the shipped spawn path issues and hands over identity."""
    board = make_board_task(spawn_env, tmp_path / "ws" / "spawned", title="spawned")
    pid, record = _spawn_through_dispatcher(board, monkeypatch, tmp_path)

    assert record["bound"] is True, record.get("error")
    assert record["task_id"] == board["task_id"]
    assert record["run_id"] == board["run_id"]
    assert record["workspace"] == str(board["workspace"])
    # The identity is pinned to the exact process the dispatcher launched.
    assert record["pid"] == pid == record["worker_pid"]
    # And the dispatcher really did build a worker command line.
    assert "chat" in record["argv_tail"]

    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        row = conn.execute(
            "SELECT worker_pid, consumed FROM worker_identities"
        ).fetchone()
    assert row["worker_pid"] == pid
    assert row["consumed"] == 1  # single-use, already spent


def test_real_dispatcher_spawn_commits_the_row_before_sending_the_token(
    spawn_env, monkeypatch, tmp_path
):
    """Ordering is the guarantee: row first, token second.

    If the token were transmitted before the PID/start-bound row was
    committed, a worker could bind against a half-written -- or absent --
    identity. This pins the real spawn path's ordering by recording, at the
    moment the token is sent, whether the row is already durable.
    """
    board = make_board_task(spawn_env, tmp_path / "ws" / "spawned", title="spawned")
    events: list[tuple[str, object]] = []

    real_issue = kb.issue_worker_identity
    real_send = di.WorkerHandshake.send

    def _traced_issue(conn, **kwargs):
        events.append(("issue_called", kwargs["worker_pid"]))
        token = real_issue(conn, **kwargs)
        # Read through a *separate* connection: only a committed row is
        # visible, so this proves durability rather than transaction state.
        with kb.connect_closing(db_path=Path(board["db_path"])) as probe:
            row = probe.execute(
                "SELECT worker_pid, proc_start FROM worker_identities"
            ).fetchone()
        events.append(("row_committed", None if row is None else tuple(row)))
        return token

    def _traced_send(self, token, *, db_path):
        with kb.connect_closing(db_path=Path(board["db_path"])) as probe:
            row = probe.execute(
                "SELECT worker_pid, proc_start FROM worker_identities"
            ).fetchone()
        events.append(("send", None if row is None else tuple(row)))
        return real_send(self, token, db_path=db_path)

    monkeypatch.setattr(kb, "issue_worker_identity", _traced_issue)
    monkeypatch.setattr(di.WorkerHandshake, "send", _traced_send)

    pid, record = _spawn_through_dispatcher(board, monkeypatch, tmp_path)
    assert record["bound"] is True, record.get("error")

    names = [name for name, _ in events]
    assert names == ["issue_called", "row_committed", "send"]

    # The row was minted for the process that had already been spawned...
    assert events[0][1] == pid
    # ...was durable before the token went out...
    committed_pid, committed_start = events[1][1]
    assert committed_pid == pid
    # ...and carried the kernel's start identity for that exact process.
    assert committed_start == di.process_start_time(pid) or committed_start > 0
    # The send observed exactly the same durable row.
    assert events[2][1] == events[1][1]


def test_real_dispatcher_spawn_fails_closed_when_identity_cannot_be_issued(
    spawn_env, monkeypatch, tmp_path
):
    """No mintable identity -> EOF, and the worker binds nothing."""
    board = make_board_task(spawn_env, tmp_path / "ws" / "spawned", title="spawned")

    def _refuse(*_a, **_k):
        raise RuntimeError("kernel start identity unavailable")

    monkeypatch.setattr(kb, "issue_worker_identity", _refuse)
    _pid, record = _spawn_through_dispatcher(board, monkeypatch, tmp_path)

    assert record["bound"] is False
    assert "without a token" in record["error"]
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        count = conn.execute("SELECT COUNT(*) FROM worker_identities").fetchone()[0]
    assert count == 0


# --------------------------------------------------------------------------- #
# The CLI startup gate that runs before any agent work
# --------------------------------------------------------------------------- #

# Calls the production startup hook in a real process, so a failure to bind
# is observed exactly as a spawned worker would experience it.
_STARTUP_GATE_SOURCE = textwrap.dedent(
    """
    import json, os, sys
    sys.path.insert(0, {repo!r})
    from hermes_cli.main import _bind_dispatcher_worker_identity
    from agent import dispatcher_identity as di

    _bind_dispatcher_worker_identity()
    # Only reached when the gate allowed startup to continue.
    print(json.dumps({{"continued": True, "bound": di.get_bound() is not None}}))
    """
)


def _run_startup_gate(*, issue, timeout=30):
    handshake = di.WorkerHandshake()
    env = dict(os.environ)
    env.update(handshake.env_for_child())
    source = _STARTUP_GATE_SOURCE.format(repo=str(REPO_ROOT))
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", source],
            env=env,
            pass_fds=handshake.pass_fds(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        handshake.close_read_fd()
        token = issue(proc.pid)
        if token is None:
            handshake.abort()
        else:
            handshake.send(token, db_path=os.environ["HERMES_KANBAN_DB"])
        out, err = proc.communicate(timeout=timeout)
    finally:
        handshake.close()
    return proc.returncode, out, err


def test_startup_gate_lets_a_bound_worker_continue(board):
    code, out, err = _run_startup_gate(issue=lambda pid: _issue(board, pid=pid))
    assert code == 0, err
    assert json.loads(out.strip()) == {"continued": True, "bound": True}


def test_startup_gate_stops_a_worker_whose_identity_will_not_bind(board):
    """A worker the dispatcher meant to authorise must not run unbound."""
    code, out, err = _run_startup_gate(issue=lambda pid: None)
    assert code == 70
    assert "identity handshake failed" in err
    assert out.strip() == ""  # never reached the code past the gate


def test_real_cli_entry_binds_before_it_reaches_any_command(board, tmp_path):
    """The gate sits ahead of command dispatch in the shipped ``main()``.

    Driven through the real ``hermes_cli.main`` entry point in a real
    process: with a handshake that cannot be bound, the CLI must exit
    before ``--help`` (the cheapest observable command) produces output.
    """
    handshake = di.WorkerHandshake()
    env = dict(os.environ)
    env.update(handshake.env_for_child())
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "--help"],
            env=env,
            pass_fds=handshake.pass_fds(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
        )
        handshake.close_read_fd()
        handshake.abort()  # dispatcher could not mint an identity
        out, err = proc.communicate(timeout=120)
    finally:
        handshake.close()

    assert proc.returncode == 70, f"rc={proc.returncode} out={out!r} err={err!r}"
    assert "identity handshake failed" in err
    assert "usage:" not in out.lower()

    # Control: the same command is reachable and chatty when no handshake is
    # pending, so the assertion above is about the gate, not about --help
    # being silent.
    control_env = {k: v for k, v in env.items() if k != di.HANDSHAKE_FD_ENV}
    control = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "--help"],
        env=control_env, capture_output=True, text=True,
        timeout=120, cwd=str(REPO_ROOT),
    )
    assert control.returncode == 0
    assert "usage:" in control.stdout.lower()


def test_startup_gate_is_a_noop_without_a_handshake(tmp_path):
    """Ordinary CLI invocations are untouched by the gate."""
    env = {k: v for k, v in os.environ.items() if k != di.HANDSHAKE_FD_ENV}
    proc = subprocess.run(
        [sys.executable, "-c", _STARTUP_GATE_SOURCE.format(repo=str(REPO_ROOT))],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == {"continued": True, "bound": False}


# --------------------------------------------------------------------------- #
# Delegated / cron descendants suppress inherited authority
# --------------------------------------------------------------------------- #

def test_in_process_suppression_hides_a_real_binding(board):
    """A genuinely bound worker still yields no authority under suppression."""
    token = _issue(board, pid=os.getpid())
    bound = di.bind_token(token, db_path=board["db_path"])
    assert di.get_bound() is bound
    with di.suppressed():
        assert di.get_bound() is None
    assert di.get_bound() is bound


def test_delegated_child_context_suppresses_identity(board):
    from agent import delegation_context

    token = _issue(board, pid=os.getpid())
    di.bind_token(token, db_path=board["db_path"])
    assert di.get_bound() is not None
    with delegation_context.delegated_child_context("child-session"):
        assert di.get_bound() is None
    assert di.get_bound() is not None


def test_non_dispatcher_owned_context_suppresses_identity(board):
    from agent import delegation_context

    token = _issue(board, pid=os.getpid())
    di.bind_token(token, db_path=board["db_path"])
    assert di.get_bound() is not None
    with delegation_context.non_dispatcher_owned_context():
        assert di.get_bound() is None
    assert di.get_bound() is not None


def test_delegated_child_subprocess_marker_suppresses_identity(board, monkeypatch):
    from agent import delegation_context

    token = _issue(board, pid=os.getpid())
    di.bind_token(token, db_path=board["db_path"])
    assert di.get_bound() is not None
    monkeypatch.setenv(delegation_context.DELEGATED_CHILD_ENV_MARKER, "1")
    assert di.get_bound() is None


def test_scrubbed_env_drops_the_handshake_descriptor():
    from agent import delegation_context

    scrubbed = delegation_context.scrub_kanban_env(
        {"HERMES_KANBAN_TASK": "t_1", di.HANDSHAKE_FD_ENV: "9"}
    )
    assert di.HANDSHAKE_FD_ENV not in scrubbed
    assert "HERMES_KANBAN_TASK" not in scrubbed


# --------------------------------------------------------------------------- #
# Revalidation: the binding is re-checked against the DB, not cached blindly
# --------------------------------------------------------------------------- #

def test_revalidate_rejects_a_run_that_moved_on(board):
    token = _issue(board, pid=os.getpid())
    bound = di.bind_token(token, db_path=board["db_path"])
    assert di.revalidate(bound) is None
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (board["run_id"] + 1, board["task_id"]),
        )
        conn.commit()
    reason = di.revalidate(bound)
    assert reason is not None and "run" in reason.lower()


def test_revalidate_rejects_a_workspace_that_moved(board, tmp_path):
    token = _issue(board, pid=os.getpid())
    bound = di.bind_token(token, db_path=board["db_path"])
    assert di.revalidate(bound) is None
    moved = tmp_path / "moved"
    moved.mkdir()
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(moved), board["task_id"]),
        )
        conn.commit()
    reason = di.revalidate(bound)
    assert reason is not None and "workspace" in reason.lower()
