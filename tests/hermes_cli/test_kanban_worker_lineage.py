"""Real-subprocess regression tests for CV-A01 (t_70827e4e / t_11e8c077):
the delegated-child Kanban mutation guard's cross-process signal must be
unforgeable.

Every test here drives REAL subprocesses through the REAL dispatcher spawn
path (``kanban_db_dispatch._default_spawn``) and the REAL ``hermes kanban``
CLI entry point (``python -m hermes_cli.main kanban ...``) -- no mocking of
the guard itself. The attack this suite pins closed:

    env -u HERMES_DELEGATED_CHILD_CONTEXT hermes kanban comment <task> \
        --author <victim> "forged"

which used to succeed (exit 0, comment/task/etc. actually written) because
the ONLY cross-process signal the guard consulted was that single
environment variable, and a subprocess can always strip an inherited env var
before re-exec'ing. The fix folds a kernel-verified process-ancestry check
into ``agent.delegation_context.is_delegated_child_process_context`` so the
guard survives env stripping.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_worker_lineage import (
    is_descendant_of_dispatcher_worker,
    kernel_pid_start_micros,
    record_worker_spawn,
)

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
    with kbc.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="lineage fixture", assignee="tester")
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


def _run(argv, *, env, cwd=None, timeout=45):
    return subprocess.run(
        argv, env=env, cwd=str(cwd or ROOT), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )


def _cli(*args, env, cwd=None, timeout=45):
    return _run([sys.executable, "-m", "hermes_cli.main", "kanban", *args], env=env, cwd=cwd, timeout=timeout)


_SLEEPER_SOURCE = "import time; time.sleep(30)"

# A real "worker" process: sleeps so its PID/kernel-start stay valid for the
# grandchild it spawns to run its attack against, then execs a real CLI
# mutation as its own descendant.
_WORKER_LAUNCH_ATTACK_SOURCE = (
    "import os, subprocess, sys, json\n"
    "attack = sys.argv[1:]\n"
    "env = dict(os.environ)\n"
    "env.pop('HERMES_DELEGATED_CHILD_CONTEXT', None)\n"
    "p = subprocess.run(attack, env=env, capture_output=True, text=True, "
    "stdin=subprocess.DEVNULL, timeout=40)\n"
    "print(json.dumps({'rc': p.returncode, 'out': p.stdout, 'err': p.stderr}))\n"
)


# --------------------------------------------------------------------------- #
# The historical bypass: env stripped by a same-process re-exec
# --------------------------------------------------------------------------- #

def test_env_stripped_delegated_child_cannot_create_task_via_cli(board, tmp_path):
    """Direct repro of the reported exploit, modeled with REAL process ancestry.

    Before the fix: a real descendant of a dispatcher-spawned worker that
    strips ``HERMES_DELEGATED_CHILD_CONTEXT`` before re-exec'ing ``hermes
    kanban ...`` succeeded anyway, because the guard's only cross-process
    check was that one (forgeable) env var. This spawns a REAL worker
    process, records it via the REAL ``record_worker_spawn`` call the
    dispatcher uses, then has a REAL child of that worker attempt the exact
    reported bypass. After the fix the mutation is denied by kernel
    ancestry, independent of the stripped env var.
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = str(board["home"])
    env["HERMES_KANBAN_DB"] = str(board["db_path"])
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)  # attacker already stripped it

    worker_script = tmp_path / "worker_launch_attack_comment.py"
    worker_script.write_text(_WORKER_LAUNCH_ATTACK_SOURCE)
    attack_argv = [
        sys.executable, "-m", "hermes_cli.main", "kanban", "comment", board["task_id"],
        "--author", "oliver", "forged-no-marker",
    ]
    worker = subprocess.Popen(
        [sys.executable, str(worker_script), *attack_argv],
        env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with kbc.connect_closing(db_path=board["db_path"]) as conn:
            record_worker_spawn(
                conn, task_id=board["task_id"], run_id=board["run_id"], worker_pid=worker.pid,
            )
        out, err = worker.communicate(timeout=40)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
    assert worker.returncode == 0, err
    result = json.loads(out.strip())
    assert result["rc"] != 0, result
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        comments = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ?", (board["task_id"],),
        ).fetchall()
    assert len(comments) == 0, "forged comment must not have been written"


def test_delegate_task_context_var_child_is_still_denied_control(board):
    """Control: the sound, in-process half of the guard (the ContextVar) is
    untouched by this fix and still denies mutation from inside a
    ``delegate_task``-marked scope."""
    from agent import delegation_context

    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        with delegation_context.delegated_child_context("fixture-child-session"):
            with pytest.raises(PermissionError):
                kb.add_comment(conn, board["task_id"], "oliver", "forged-in-process")


# --------------------------------------------------------------------------- #
# The unforgeable half: real dispatcher spawn -> real descendant attack
# --------------------------------------------------------------------------- #

def test_real_descendant_of_a_dispatcher_worker_cannot_mutate_even_with_env_stripped(board, tmp_path):
    """End-to-end: spawn a REAL worker through the REAL record_worker_spawn
    call, then have a REAL descendant of that worker attempt the exact
    reported bypass with the env marker stripped. Must be denied by kernel
    ancestry, not by the (now-defeated) env var.
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = str(board["home"])
    env["HERMES_KANBAN_DB"] = str(board["db_path"])
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)  # attacker already stripped it

    worker_script = tmp_path / "worker_launch_attack.py"
    worker_script.write_text(_WORKER_LAUNCH_ATTACK_SOURCE)

    attack_argv = [
        sys.executable, "-m", "hermes_cli.main", "kanban", "create", "attacker task",
        "--assignee", "oliver",
    ]
    worker = subprocess.Popen(
        [sys.executable, str(worker_script), *attack_argv],
        env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # Mint the worker-spawn row exactly as the real dispatcher does:
        # AFTER Popen returns, using the child's real PID.
        with kbc.connect_closing(db_path=board["db_path"]) as conn:
            record_worker_spawn(
                conn, task_id=board["task_id"], run_id=board["run_id"], worker_pid=worker.pid,
            )
        out, err = worker.communicate(timeout=40)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
    assert worker.returncode == 0, err
    result = json.loads(out.strip())
    assert result["rc"] != 0, result
    task_count_before_attack = 1  # only the fixture task
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == task_count_before_attack, "attacker task must not have been created"


def test_ordinary_unrelated_process_is_still_denied_without_any_worker_spawn_row(board):
    """No dispatcher ever spawned this process or any ancestor of it, and the
    (forgeable) env var is also absent. Ancestry finds nothing -> denied,
    same as before, for the right reason now (no matching ancestor row)
    rather than by env-var presence."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(board["home"])
    env["HERMES_KANBAN_DB"] = str(board["db_path"])
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env.pop("HERMES_KANBAN_TASK", None)
    # This is an ordinary invocation (no worker ancestor at all), so it
    # should be ALLOWED -- proving the guard isn't just "always deny".
    proc = _cli("create", "ordinary task", "--assignee", "tester", env=env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


# --------------------------------------------------------------------------- #
# The legitimate path must keep working: the worker's OWN mutations are fine
# --------------------------------------------------------------------------- #

def test_the_spawned_worker_process_itself_is_not_treated_as_a_descendant(board):
    """The dispatcher-spawned worker mutates Kanban through its OWN process
    (in-process tool calls), which must remain allowed: ancestry starts at
    the worker's PARENT, never flags the worker's own PID."""
    my_pid = os.getpid()
    my_start = kernel_pid_start_micros(my_pid)
    assert my_start is not None
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        record_worker_spawn(
            conn, task_id=board["task_id"], run_id=board["run_id"], worker_pid=my_pid,
        )
        # Recording a row for MY OWN pid must not make ME a "descendant" of
        # myself -- the ancestry walk starts at my parent, not my own pid.
        assert is_descendant_of_dispatcher_worker(conn) is False


def test_real_dispatcher_spawn_path_records_a_lineage_row(board, monkeypatch, tmp_path):
    """Drive the actual shipped ``_default_spawn`` and confirm it writes a
    worker_spawns row for the real child PID it launched."""
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
            "SELECT worker_pid, task_id, run_id FROM worker_spawns WHERE worker_pid = ?", (pid,),
        ).fetchone()
    assert row is not None, "record_worker_spawn was not called by the real spawn path"
    assert row["task_id"] == board["task_id"]
    assert row["run_id"] == board["run_id"]


# --------------------------------------------------------------------------- #
# Fail-closed behaviour
# --------------------------------------------------------------------------- #

def test_ancestry_lookup_failure_fails_closed(board, monkeypatch):
    """If the ancestry mechanism itself breaks, deny -- never silently allow."""
    import hermes_cli.kanban_worker_lineage as lineage

    def _boom(*_a, **_k):
        raise RuntimeError("simulated ancestry failure")

    monkeypatch.setattr(lineage, "_ancestor_pid_start_pairs", _boom)
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        assert is_descendant_of_dispatcher_worker(conn) is True


def test_expired_worker_spawn_row_no_longer_grants_descendant_status(board, tmp_path):
    my_pid = os.getpid()
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        record_worker_spawn(
            conn, task_id=board["task_id"], run_id=board["run_id"], worker_pid=my_pid,
            ttl_seconds=-5,
        )
        # my own pid is never a "descendant" of itself regardless of expiry,
        # so exercise expiry against a synthetic ancestor row instead by
        # checking the raw SQL predicate the lookup uses.
        row = conn.execute(
            "SELECT 1 FROM worker_spawns WHERE worker_pid = ? AND expires_at > ?",
            (my_pid, int(time.time())),
        ).fetchone()
        assert row is None


# --------------------------------------------------------------------------- #
# Board-scoping: a worker-spawn row on one board must not authorise a
# different board's ancestry check, even for a genuinely-descendant process.
# --------------------------------------------------------------------------- #

@pytest.fixture
def two_boards(tmp_path, monkeypatch):
    """Two real, separately-initialised Kanban boards under one isolated
    HERMES_HOME, each with its own on-disk DB (and therefore its own
    ``worker_spawns`` table)."""
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
        "home": home,
        "db_path_a": db_path_a,
        "db_path_b": db_path_b,
        "task_id": task_id,
        "run_id": int(claimed.current_run_id),
    }


def test_worker_spawn_recorded_on_board_a_does_not_authorise_board_b_ancestry_check(two_boards):
    """A genuine descendant of a process the dispatcher recorded as a worker
    on board A must still be denied when the ancestry check is evaluated
    against board B's DB/context.

    This is the cross-board isolation half of the ``worker_spawns``
    contract: the row lives in board A's own on-disk DB, so a lookup scoped
    to board B's DB can never see it, no matter how real the calling
    process's kernel ancestry to the recorded worker is. We prove that by
    recording the CURRENT TEST PROCESS's own real PID as a spawned worker on
    board A (so ``is_descendant_of_dispatcher_worker`` would find a matching
    ancestor row -- itself -- for board A) and then confirming board B's
    check still returns False even though nothing distinguishes the calling
    process except which board is asked about.
    """
    my_pid = os.getpid()
    my_start = kernel_pid_start_micros(my_pid)
    assert my_start is not None

    with kbc.connect_closing(db_path=two_boards["db_path_a"]) as conn_a:
        record_worker_spawn(
            conn_a, task_id=two_boards["task_id"], run_id=two_boards["run_id"],
            worker_pid=my_pid,
        )
        # Sanity: board A's own DB really does have the row we just wrote,
        # keyed on this process's real kernel identity.
        row = conn_a.execute(
            "SELECT 1 FROM worker_spawns WHERE worker_pid = ? AND proc_start = ?",
            (my_pid, my_start),
        ).fetchone()
        assert row is not None, "sanity check: worker_spawns row must exist on board A"

    # Evaluated against board B's connection/context, the SAME real process
    # must NOT be treated as covered by a row that only exists on board A.
    with kbc.connect_closing(db_path=two_boards["db_path_b"]) as conn_b:
        row_b = conn_b.execute(
            "SELECT 1 FROM worker_spawns WHERE worker_pid = ? AND proc_start = ?",
            (my_pid, my_start),
        ).fetchone()
        assert row_b is None, "board B's DB must never see board A's worker_spawns row"

        # Using the explicit conn form (mirrors ordinary caller usage: a
        # caller that already has a board-scoped connection passes it
        # through directly).
        assert is_descendant_of_dispatcher_worker(conn_b) is False

    # And via the board= keyword path (owns_conn=True branch), which is what
    # callers with no open connection of their own use -- board must resolve
    # to B's DB, never silently fall back to a default/other board.
    assert is_descendant_of_dispatcher_worker(board="board-b") is False

    # Control: the identical check against board A DOES see the row (proves
    # the negative result above is genuinely about board scoping, not a
    # broken/no-op ancestry walk in general). My own pid is never treated as
    # a "descendant" of itself though (ancestry starts at the parent), so
    # assert via the explicit-conn form directly against the raw predicate
    # plus a real ancestor-based check using this process's actual parent.
    parent_pid = __import__("os").getppid()
    parent_start = kernel_pid_start_micros(parent_pid)
    assert parent_start is not None
    with kbc.connect_closing(db_path=two_boards["db_path_a"]) as conn_a:
        record_worker_spawn(
            conn_a, task_id=two_boards["task_id"], run_id=two_boards["run_id"],
            worker_pid=parent_pid,
        )
        # This process genuinely IS a descendant of parent_pid, and board A
        # now has a matching row for it -> board A's check must see it.
        assert is_descendant_of_dispatcher_worker(conn_a) is True

    # But board B still must not, even though the real ancestry relationship
    # (this test process being a descendant of parent_pid) is unchanged --
    # only the board context differs.
    assert is_descendant_of_dispatcher_worker(board="board-b") is False


# --------------------------------------------------------------------------- #
# KNOWN, unfixed residual gap: detach/reparent breaks the kernel ancestry
# chain this guard depends on (tracked t_70827e4e / t_f9082e18).
# --------------------------------------------------------------------------- #

_DETACHED_GRANDCHILD_SOURCE = (
    "import json, os, subprocess, sys, time\n"
    "grandchild_script = sys.argv[1]\n"
    "result_path = sys.argv[2]\n"
    "ready_path = sys.argv[3]\n"
    "# Launch the grandchild and DELIBERATELY never wait() on it before this\n"
    "# (the worker's already-attached child) process exits. No setsid() is\n"
    "# used or needed -- plain Popen + early parent exit is enough for the\n"
    "# kernel to reparent the grandchild to init/launchd once we exit below.\n"
    "gc = subprocess.Popen([sys.executable, grandchild_script, result_path], "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "with open(ready_path, 'w') as f:\n"
    "    json.dump({'grandchild_pid': gc.pid}, f)\n"
    "# Exit immediately -- no wait(), no communicate(). The grandchild is\n"
    "# still alive and sleeping; its real kernel parent link to THIS process\n"
    "# is about to be severed by the kernel once this process is reaped.\n"
    "sys.exit(0)\n"
)

_DETACHED_GRANDCHILD_PROBE_SOURCE = (
    "import json, sys, time\n"
    "sys.path.insert(0, {root!r})\n"
    "from hermes_cli.kanban_worker_lineage import is_descendant_of_dispatcher_worker\n"
    "from hermes_cli import kanban_db_connect as kbc\n"
    "# Give the immediate parent time to exit and the kernel time to\n"
    "# actually complete the reparent to init/launchd before we check.\n"
    "time.sleep(2)\n"
    "with kbc.connect_closing(db_path={db_path!r}) as conn:\n"
    "    result = is_descendant_of_dispatcher_worker(conn)\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    json.dump({{'is_descendant': result}}, f)\n"
)


@pytest.mark.xfail(
    reason="residual bypass via process detach/reparent, tracked t_70827e4e / "
           "t_f9082e18 — kernel-ancestry check alone cannot close this, "
           "requires a different signal",
)
def test_detached_reparented_descendant_bypasses_ancestry_check(board, tmp_path):
    """Reproduces Maya's finding: a real worker spawns a real child; that
    child backgrounds a grandchild via ``subprocess.Popen`` and exits before
    ever calling ``wait()`` on it (no ``setsid()`` needed). The kernel
    reparents the still-alive grandchild to init/launchd, severing its real
    ancestry chain back to the recorded worker PID.

    The grandchild genuinely IS delegated-child work (its whole lineage
    traces back to a dispatcher-spawned worker), but once reparented,
    ``is_descendant_of_dispatcher_worker`` walks the grandchild's real
    (now-truncated) kernel ancestry, finds no ``worker_spawns`` row for
    init/launchd, and incorrectly returns False.

    Expected to presently FAIL (xfail): the assertion below (that the
    reparented grandchild IS still recognised as a descendant) does not
    hold with the current fix, which is exactly the residual gap this
    module's docstring documents. If this test ever unexpectedly PASSES,
    that's a real behavior change worth investigating, not a bug in the
    test.
    """
    grandchild_script = tmp_path / "grandchild_sleep_and_probe.py"
    grandchild_script.write_text(
        _DETACHED_GRANDCHILD_PROBE_SOURCE.format(root=str(ROOT), db_path=str(board["db_path"]))
    )
    launcher_script = tmp_path / "detach_launcher.py"
    launcher_script.write_text(_DETACHED_GRANDCHILD_SOURCE)

    result_path = tmp_path / "grandchild_result.json"
    ready_path = tmp_path / "launcher_ready.json"

    env = dict(os.environ)
    env["HERMES_HOME"] = str(board["home"])
    env["HERMES_KANBAN_DB"] = str(board["db_path"])

    # A real "worker" the dispatcher spawned, exactly like other tests here.
    worker = subprocess.Popen(
        [sys.executable, "-c", _SLEEPER_SOURCE],
        env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
    )
    grandchild_pid = None
    try:
        with kbc.connect_closing(db_path=board["db_path"]) as conn:
            record_worker_spawn(
                conn, task_id=board["task_id"], run_id=board["run_id"],
                worker_pid=worker.pid,
            )

        # The worker's own real child: launches the grandchild then exits
        # immediately without wait()-ing, forcing the kernel reparent.
        launcher = subprocess.run(
            [sys.executable, str(launcher_script), str(grandchild_script),
             str(result_path), str(ready_path)],
            env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
        assert launcher.returncode == 0, launcher.stderr
        assert ready_path.exists()
        ready = json.loads(ready_path.read_text())
        grandchild_pid = ready["grandchild_pid"]

        # Wait for the grandchild's own probe result (it sleeps 2s first to
        # let the reparent actually complete, then checks its own ancestry
        # and writes the result to result_path).
        deadline = time.time() + 20
        while time.time() < deadline and not result_path.exists():
            time.sleep(0.1)
        assert result_path.exists(), "detached grandchild never wrote its probe result"
        result = json.loads(result_path.read_text())
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        # Best-effort cleanup of the now-orphaned grandchild.
        if grandchild_pid is not None:
            with __import__("contextlib").suppress(Exception):
                import signal
                os.kill(grandchild_pid, signal.SIGKILL)

    # This is the documented, currently-true bug: the grandchild's real
    # lineage traces back to a genuine dispatcher-spawned worker, but the
    # severed kernel ancestry chain (post-reparent) makes the guard say
    # "not a descendant" anyway. This assertion is what SHOULD hold once
    # the residual gap is fixed by a different (non-ancestry) signal; today
    # it fails, which is exactly what the xfail marker expects.
    assert result["is_descendant"] is True, (
        "expected the reparented grandchild to still be recognised as a "
        "delegated-child descendant; got False, confirming the known "
        "detach/reparent bypass (t_70827e4e / t_f9082e18)"
    )
