"""CV-A01 Stage 2A: dispatcher scope is authorized by a bound identity only.

Stage 1 (``agent/dispatcher_identity.py``) established a process-bound
worker identity: a one-time token the dispatcher mints *after* the child
exists, stored hashed in the authoritative Kanban DB pinned to exact
task/run/workspace + PID + kernel process-start time, handed over an
inherited pipe and CAS-consumed once at startup.

This stage makes that identity the *only* source of dispatcher authority
in ``plugins/work_claims``.  What this suite pins closed:

* ``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_WORKSPACE`` / the
  ``delegation_context`` ContextVars are no longer authority.  A process
  carrying a perfect set of forged Kanban env vars gets nothing.
* The binding is re-verified against the database, the task's current run,
  the recorded workspace, this process's PID, its kernel start time and
  the token's expiry at **every** pre-tool decision -- never trusted from
  bind time.
* Delegated children, in-process cron, explicitly suppressed scopes and
  any spawned subprocess carry no authority.
* Dispatcher scope is default-deny: only the path-scoped mutators
  (``write_file``/``patch``, confined) and ``terminal`` (confined and
  OS-sandboxed) may proceed.  Every other mutator -- and every unknown
  mutating tool -- is denied.
* Ordinary claim-holding sessions are untouched.

Every case is driven through a real, discovery-loaded
``hermes_cli.plugins.PluginManager``'s ``pre_tool_call`` hook -- the exact
path the host calls -- not direct ``core.mutation_allowed()`` calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from agent import delegation_context
from agent import dispatcher_identity as di
from hermes_cli import kanban_db as kb
from hermes_cli import plugins as pmod
from plugins.work_claims import core

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["work-claims"]}})
    )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(core, "_kanban_create", lambda *a, **k: "t_1")
    monkeypatch.setattr(core, "_kanban_complete", lambda *a, **k: None)
    for key in delegation_context.KANBAN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    di.reset_for_tests()
    yield
    di.reset_for_tests()


@pytest.fixture
def manager():
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins.get("work-claims")
    assert loaded is not None and loaded.enabled, getattr(loaded, "error", "not discovered")
    assert mgr.has_hook("pre_tool_call")
    return mgr


def _claimed_task(db_path: Path, workspace: Path, *, title: str = "dispatcher worker task") -> dict:
    """Create a task and take it through the real ready -> running claim."""
    workspace.mkdir(parents=True, exist_ok=True)
    with kb.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(
            conn, title=title, assignee="tester", workspace_path=str(workspace)
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


def bind_worker(tmp_path, *, workspace_name: str = "worker-ws", ttl_seconds: int = 3600) -> dict:
    """Mint and bind a genuine identity for *this* process, as the dispatcher does."""
    db_path = tmp_path / "kanban.db"
    workspace = tmp_path / workspace_name
    board = _claimed_task(db_path, workspace)
    with kb.connect_closing(db_path=db_path) as conn:
        token = kb.issue_worker_identity(
            conn,
            task_id=board["task_id"],
            run_id=board["run_id"],
            workspace_path=str(workspace),
            worker_pid=os.getpid(),
            ttl_seconds=ttl_seconds,
        )
    board["identity"] = di.bind_token(token, db_path=str(db_path))
    return board


def _spoof_env(monkeypatch, board) -> None:
    """Set the full, *correct* Kanban env a worker would carry.  After
    Stage 2A this is decoration: it must confer no authority at all."""
    monkeypatch.setenv("HERMES_KANBAN_DB", board["db_path"])
    monkeypatch.setenv("HERMES_KANBAN_TASK", board["task_id"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(board["run_id"]))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(board["workspace"]))


def _pre_tool_call(manager, tool_name: str, args: dict, session_id: str = "s-dispatcher"):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id=session_id
    )
    return results[0] if results else None


def _assert_allowed(manager, tool_name, args, **kw):
    result = _pre_tool_call(manager, tool_name, args, **kw)
    assert result is None or result.get("action") == "modify", f"expected allow, got: {result}"
    return result


def _assert_blocked(manager, tool_name, args, **kw):
    result = _pre_tool_call(manager, tool_name, args, **kw)
    assert result is not None and result.get("action") == "block", f"expected block, got: {result}"
    return result["message"]


# --------------------------------------------------------------------------- #
# The env is not authority any more.
# --------------------------------------------------------------------------- #

def test_perfect_kanban_env_without_a_bound_identity_confers_nothing(tmp_path, monkeypatch, manager):
    """The Stage 1 premise, enforced at the gate: a process holding a
    complete and *truthful* HERMES_KANBAN_* env -- but no identity bound
    through the dispatcher handshake -- has no claimless scope at all."""
    db_path = tmp_path / "kanban.db"
    workspace = tmp_path / "worker-ws"
    board = _claimed_task(db_path, workspace)
    _spoof_env(monkeypatch, board)

    message = _assert_blocked(
        manager, "write_file", {"path": str(workspace / "notes.md"), "content": "x"}
    )
    assert "No active cross-session work claim" in message


def test_forged_env_pointing_at_someone_elses_task_confers_nothing(tmp_path, monkeypatch, manager):
    """An attacker who reads another worker's env and replays it verbatim
    still binds nothing: there is no row consumed for this process."""
    db_path = tmp_path / "kanban.db"
    victim = _claimed_task(db_path, tmp_path / "victim-ws", title="victim task")
    _spoof_env(monkeypatch, victim)

    message = _assert_blocked(
        manager, "write_file", {"path": str(victim["workspace"] / "pwn.txt"), "content": "x"}
    )
    assert "No active cross-session work claim" in message


def test_contextvar_alone_confers_nothing(tmp_path, monkeypatch, manager):
    """``is_dispatcher_owned_worker_context()`` defaults to True in any
    ordinary process.  On its own that must not authorise anything."""
    workspace = tmp_path / "unclaimed-ws"
    workspace.mkdir()
    assert delegation_context.is_dispatcher_owned_worker_context()

    message = _assert_blocked(
        manager, "write_file", {"path": str(workspace / "x.txt"), "content": "x"}
    )
    assert "No active cross-session work claim" in message


# --------------------------------------------------------------------------- #
# A bound identity confines rather than bypasses.
# --------------------------------------------------------------------------- #

def test_bound_identity_allows_in_workspace_write(tmp_path, manager):
    board = bind_worker(tmp_path)
    _assert_allowed(
        manager, "write_file", {"path": str(board["workspace"] / "notes.md"), "content": "x"}
    )


def test_bound_identity_denies_write_outside_its_workspace(tmp_path, manager):
    board = bind_worker(tmp_path)
    outside = tmp_path / "somewhere-else"
    outside.mkdir()

    message = _assert_blocked(
        manager, "write_file", {"path": str(outside / "evil.txt"), "content": "x"}
    )
    assert "confined dispatcher workspace" in message


def test_bound_identity_ignores_a_spoofed_workspace_env(tmp_path, monkeypatch, manager):
    """The workspace comes from the bound identity, never from the env: a
    worker that rewrites HERMES_KANBAN_WORKSPACE cannot widen its scope."""
    board = bind_worker(tmp_path)
    spoofed = tmp_path / "spoofed-ws"
    spoofed.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(spoofed))

    _assert_allowed(
        manager, "write_file", {"path": str(board["workspace"] / "ok.txt"), "content": "x"}
    )
    message = _assert_blocked(
        manager, "write_file", {"path": str(spoofed / "pwn.txt"), "content": "x"}
    )
    assert "confined dispatcher workspace" in message


def test_symlinked_target_file_rejected(tmp_path, manager):
    board = bind_worker(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret")
    link = board["workspace"] / "link.txt"
    link.symlink_to(secret)

    message = _assert_blocked(manager, "write_file", {"path": str(link), "content": "x"})
    assert "symlink" in message


def test_symlinked_directory_escape_rejected(tmp_path, manager):
    board = bind_worker(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = board["workspace"] / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    message = _assert_blocked(
        manager, "write_file", {"path": str(escape / "pwn.txt"), "content": "x"}
    )
    assert "symlink" in message
    _assert_blocked(manager, "terminal", {"command": "ls", "workdir": str(escape)})


def test_apfs_case_variant_alias_rejected(tmp_path, manager):
    board = bind_worker(tmp_path)
    (board["workspace"] / "Project").mkdir()
    aliased = board["workspace"] / "project" / "notes.md"

    message = _assert_blocked(manager, "write_file", {"path": str(aliased), "content": "x"})
    if (board["workspace"] / "project").exists():
        assert "case-variant" in message
    else:  # case-sensitive filesystem: degrades to a nonexistent-ancestor reject
        assert "ancestor does not exist" in message


def test_hardlinked_regular_file_rejected(tmp_path, manager):
    board = bind_worker(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_file = outside / "shared.txt"
    real_file.write_text("shared")
    hardlink = board["workspace"] / "shared.txt"
    try:
        os.link(real_file, hardlink)
    except OSError as exc:  # pragma: no cover - only if tmp spans filesystems
        pytest.skip(f"hardlink unsupported across tmp filesystem: {exc}")

    message = _assert_blocked(manager, "write_file", {"path": str(hardlink), "content": "x"})
    assert "hard-linked" in message


def test_toctou_ancestor_swap_between_calls_is_caught(tmp_path, manager):
    board = bind_worker(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target_dir = board["workspace"] / "payload"
    target_dir.mkdir()
    args = {"path": str(target_dir / "x.txt"), "content": "x"}

    _assert_allowed(manager, "write_file", args)

    target_dir.rmdir()
    target_dir.symlink_to(outside, target_is_directory=True)

    message = _assert_blocked(manager, "write_file", args)
    assert "symlink" in message


# --------------------------------------------------------------------------- #
# Revalidation at every decision, not at bind time.
# --------------------------------------------------------------------------- #

def test_expired_identity_stops_authorising(tmp_path, manager):
    """Real elapsed time, not a patched clock: the binding is frozen, so the
    only thing that can retire it is a fresh expiry check at decision time."""
    board = bind_worker(tmp_path, ttl_seconds=1)
    args = {"path": str(board["workspace"] / "ok.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    # Comfortably past the whole-second TTL boundary, so the assertion is
    # about expiry being re-checked rather than about a truncation race.
    time.sleep(2.2)

    message = _assert_blocked(manager, "write_file", args)
    assert "expired" in message


def test_run_advancing_stops_authorising(tmp_path, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "ok.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (board["run_id"] + 99, board["task_id"]),
        )
        conn.commit()

    message = _assert_blocked(manager, "write_file", args)
    assert "current run" in message


def test_workspace_reassignment_stops_authorising(tmp_path, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "ok.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    moved = tmp_path / "moved-ws"
    moved.mkdir()
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(moved), board["task_id"]),
        )
        conn.commit()

    message = _assert_blocked(manager, "write_file", args)
    assert "does not match" in message


def test_deleted_task_stops_authorising(tmp_path, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "ok.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (board["task_id"],))
        conn.commit()

    message = _assert_blocked(manager, "write_file", args)
    assert "does not exist" in message


def test_a_revoked_identity_denies_on_default_deny_terms(tmp_path, manager):
    """Revocation must not *widen* scope.  A worker whose run advanced keeps
    only reads and its own Kanban reporting -- exactly what a valid identity
    permits besides the confinable mutators, never the host's looser
    mutating/non-mutating classification."""
    board = bind_worker(tmp_path)
    with kb.connect_closing(db_path=Path(board["db_path"])) as conn:
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (board["run_id"] + 99, board["task_id"]),
        )
        conn.commit()

    _assert_allowed(manager, "read_file", {"path": str(board["workspace"] / "x.txt")})
    _assert_allowed(manager, "kanban_comment", {"task_id": board["task_id"], "body": "x"})
    # `_is_mutating` calls this one harmless; the gate does not.
    _assert_blocked(manager, "totally_unknown_tool", {"query": "x"})
    _assert_blocked(manager, "execute_code", {"code": "1"})


def test_vanished_workspace_stops_authorising(tmp_path, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "ok.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    for child in board["workspace"].iterdir():
        child.unlink()
    board["workspace"].rmdir()

    _assert_blocked(manager, "write_file", args)


# --------------------------------------------------------------------------- #
# Suppressed scopes: delegated child, in-process cron, explicit suppression.
# --------------------------------------------------------------------------- #

def test_delegated_child_suppresses_the_bound_authority(tmp_path, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "x.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    with delegation_context.delegated_child_context(session_id="s-child"):
        message = _assert_blocked(manager, "write_file", args, session_id="s-child")
    assert "No active cross-session work claim" in message


def test_in_process_cron_suppresses_the_bound_authority(tmp_path, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "x.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    with delegation_context.non_dispatcher_owned_context():
        message = _assert_blocked(manager, "write_file", args, session_id="s-cron")
    assert "No active cross-session work claim" in message


def test_explicit_suppression_suppresses_the_bound_authority(tmp_path, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "x.txt"), "content": "x"}
    _assert_allowed(manager, "write_file", args)

    with di.suppressed():
        message = _assert_blocked(manager, "write_file", args)
    assert "No active cross-session work claim" in message

    _assert_allowed(manager, "write_file", args)


def test_delegated_child_process_marker_suppresses_across_a_fork(tmp_path, monkeypatch, manager):
    board = bind_worker(tmp_path)
    args = {"path": str(board["workspace"] / "x.txt"), "content": "x"}
    monkeypatch.setenv(delegation_context.DELEGATED_CHILD_ENV_MARKER, "1")

    message = _assert_blocked(manager, "write_file", args)
    assert "No active cross-session work claim" in message


# --------------------------------------------------------------------------- #
# Dispatcher scope is default-deny for every non-path mutator.
# --------------------------------------------------------------------------- #

_DENIED_CALLS = [
    ("execute_code", {"code": "open('/etc/hosts','w')"}),
    ("skill_manage", {"action": "create", "name": "x"}),
    ("memory", {"action": "write", "content": "x"}),
    ("browser_exec", {"script": "x"}),
    ("browser_click", {"selector": "x"}),
    ("computer_use", {"action": "click", "x": 1, "y": 1}),
    ("drive_preview", {"action": "click"}),
    ("setup_mcp", {"name": "x"}),
    ("project_create", {"name": "x"}),
    ("desktop_project", {"action": "create"}),
    ("delegate_task", {"prompt": "x"}),
    ("process_manage", {"action": "kill", "pid": 1}),
    ("process", {"action": "kill", "pid": 1}),
    ("cronjob_manage", {"action": "create"}),
    ("cronjob", {"action": "create"}),
    ("some_future_mutating_tool", {"action": "write"}),
]


@pytest.mark.parametrize("tool_name,args", _DENIED_CALLS, ids=[c[0] for c in _DENIED_CALLS])
def test_non_path_mutators_are_denied_in_dispatcher_scope(tmp_path, manager, tool_name, args):
    bind_worker(tmp_path)
    message = _assert_blocked(manager, tool_name, args)
    assert "dispatcher" in message.lower()


def test_read_only_tools_still_work_for_a_bound_worker(tmp_path, manager):
    """Default-deny must not break the worker's ability to do its job."""
    board = bind_worker(tmp_path)
    _assert_allowed(manager, "read_file", {"path": str(board["workspace"] / "x.txt")})
    _assert_allowed(manager, "search_files", {"query": "x"})
    _assert_allowed(manager, "kanban_complete", {"task_id": board["task_id"]})
    _assert_allowed(manager, "web_search", {"query": "x"})


def test_capability_catalog_reads_work_but_execution_bridge_stays_denied(tmp_path, manager):
    """A worker may inspect admitted schemas without gaining a generic call bridge."""
    bind_worker(tmp_path)
    _assert_allowed(manager, "tool_search", {"queries": ["inspect capabilities"]})
    _assert_allowed(manager, "tool_describe", {"names": ["work_claim_status"]})
    message = _assert_blocked(
        manager,
        "tool_call",
        {"name": "work_claim_acquire", "arguments": {"targets": ["system:anything"]}},
    )
    assert "tool_call" in message
    assert "dispatcher" in message.lower()


def test_legacy_single_goal_delegate_task_remains_denied_in_dispatcher_scope(tmp_path, manager):
    bind_worker(tmp_path)
    message = _assert_blocked(manager, "delegate_task", {"goal": "do work"})
    assert "delegate_task" in message
    assert "accepts only a tasks batch" in message


def test_an_unrecognised_tool_is_denied_rather_than_assumed_harmless(tmp_path, manager):
    """The gate is default-deny, not a blocklist: a tool it cannot name is a
    tool it cannot know is confinable, whichever way the host happens to
    classify it today."""
    bind_worker(tmp_path)
    message = _assert_blocked(manager, "totally_unknown_tool", {"query": "x"})
    assert "totally_unknown_tool" in message
    _assert_blocked(manager, "some_future_mutating_tool", {"action": "write"})


# --------------------------------------------------------------------------- #
# Ordinary claim-holding sessions are unchanged.
# --------------------------------------------------------------------------- #

def test_ordinary_claimed_session_behaviour_is_preserved(tmp_path, manager, monkeypatch):
    workspace = tmp_path / "claimed-ws"
    workspace.mkdir()
    monkeypatch.setattr(
        core, "prepare_workspace",
        lambda *a, **k: core.WorkspaceResult(str(workspace), str(workspace), False, None),
    )
    result = core.acquire(
        "s-ordinary", "ordinary work", ["repo:" + str(workspace)],
        workspace=str(workspace), create_worktree=False,
    )
    assert result["success"], result

    _assert_allowed(
        manager, "write_file", {"path": str(workspace / "in.txt"), "content": "x"},
        session_id="s-ordinary",
    )
    message = _assert_blocked(
        manager, "write_file", {"path": str(tmp_path / "out.txt"), "content": "x"},
        session_id="s-ordinary",
    )
    assert "claimed isolated workspace" in message

    # A claimed session keeps the tools dispatcher scope denies.
    _assert_allowed(manager, "execute_code", {"code": "1"}, session_id="s-ordinary")


# --------------------------------------------------------------------------- #
# A spawned subprocess inherits no authority, even with the full env.
# --------------------------------------------------------------------------- #

_SUBPROCESS_PROBE = textwrap.dedent(
    """
    import json, sys
    from hermes_cli import plugins as pmod

    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    result = mgr.invoke_hook(
        "pre_tool_call",
        tool_name="write_file",
        args={"path": sys.argv[1] + "/from-subprocess.txt", "content": "x"},
        session_id="subprocess-probe",
    )
    blocked = bool(result and result[0] and result[0].get("action") == "block")
    print(json.dumps({"blocked": blocked, "message": (result[0] or {}).get("message") if result else ""}))
    """
)


def test_spawned_subprocess_inherits_no_dispatcher_authority(tmp_path):
    """The binding lives in one process's memory and its token is already
    consumed.  A child that inherits the whole environment gets nothing."""
    db_path = tmp_path / "kanban.db"
    workspace = tmp_path / "worker-ws"
    board = _claimed_task(db_path, workspace)

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["work-claims"]}})
    )
    script = tmp_path / "probe.py"
    script.write_text(_SUBPROCESS_PROBE)

    env = dict(os.environ)
    env.update({
        "HERMES_HOME": str(hermes_home),
        "HERMES_KANBAN_DB": str(db_path),
        "HERMES_KANBAN_TASK": board["task_id"],
        "HERMES_KANBAN_RUN_ID": str(board["run_id"]),
        "HERMES_KANBAN_WORKSPACE": str(workspace),
        "PYTHONPATH": str(REPO_ROOT) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    })
    proc = subprocess.run(
        [sys.executable, str(script), str(workspace)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    import json as _json

    payload = _json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["blocked"] is True
    assert "No active cross-session work claim" in payload["message"]
