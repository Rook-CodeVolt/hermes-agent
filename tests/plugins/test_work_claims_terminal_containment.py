"""CV-A01 Stage 2A: terminal containment for a bound dispatcher worker.

A pre-tool path check can only describe `workdir`.  It says nothing about
what the *command* does once it is running: an absolute path, a `../`
traversal, a shell redirect, `tee`, `cp`, or any child process it spawns
can all write wherever the worker's uid can write.

So the gate does not merely inspect the terminal call -- it rewrites it to
run through the OS sandbox (`/usr/bin/sandbox-exec`) under a profile
generated for that worker's exact workspace: reads are permitted, writes
are permitted **only** inside the workspace subtree.  Every escape below is
executed as a real subprocess and asserted against the real filesystem;
nothing here is mocked.

The gate fails closed: no sandbox binary, or a workspace whose path cannot
be expressed safely in a profile, denies the call outright rather than
running it unconfined.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from agent import command_containment as cc
from agent import delegation_context
from agent import dispatcher_identity as di
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_identity as kbi
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import plugins as pmod
from plugins.work_claims import core

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="sandbox-exec containment is a Darwin primitive"
)


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
    return mgr


@pytest.fixture
def worker(tmp_path):
    """A genuinely bound dispatcher worker with a real on-disk workspace.

    ``tmp_path`` is already a canonical path on macOS (``/private/var/...``),
    which matters twice over: the identity's ``canonical_walk`` rejects any
    symlinked component, and the sandbox profile matches on real paths.
    """
    db_path = tmp_path / "kanban.db"
    workspace = tmp_path / "worker-ws"
    workspace.mkdir(parents=True, exist_ok=True)
    with kbc.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(
            conn, title="terminal worker", assignee="tester",
            workspace_path=str(workspace),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        token = kbi.issue_worker_identity(
            conn,
            task_id=task_id,
            run_id=int(claimed.current_run_id),
            workspace_path=str(workspace),
            worker_pid=os.getpid(),
            ttl_seconds=3600,
        )
    di.bind_token(token, db_path=str(db_path))
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    return {"workspace": workspace, "outside": outside, "task_id": task_id}


def _hook(manager, args, session_id="s-dispatcher"):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name="terminal", args=args, session_id=session_id
    )
    return results[0] if results else None


def contained_command(manager, worker, command: str) -> str:
    """Run the command through the gate and return what the host would execute."""
    args = {"command": command, "workdir": str(worker["workspace"])}
    result = _hook(manager, args)
    assert result is not None, f"gate returned no directive for {command!r}"
    assert result.get("action") == "modify", f"expected a contained rewrite, got: {result}"
    rewritten = result["args"]["command"]
    assert rewritten != command, "the command was passed through unchanged"
    assert rewritten.startswith(cc.SANDBOX_EXEC), rewritten
    return rewritten


def run_contained(manager, worker, command: str) -> subprocess.CompletedProcess:
    """Execute the *rewritten* command exactly as the terminal backend would."""
    return subprocess.run(
        ["/bin/sh", "-c", contained_command(manager, worker, command)],
        cwd=str(worker["workspace"]),
        capture_output=True, text=True, timeout=60,
    )


def _blocked(manager, args, session_id="s-dispatcher") -> str:
    result = _hook(manager, args, session_id=session_id)
    assert result is not None and result.get("action") == "block", f"expected block, got: {result}"
    return result["message"]


# --------------------------------------------------------------------------- #
# Real escapes, executed and asserted against the filesystem.
# --------------------------------------------------------------------------- #

def test_legitimate_in_workspace_write_still_works(manager, worker):
    proc = run_contained(manager, worker, "echo hello > inside.txt")
    target = worker["workspace"] / "inside.txt"
    assert proc.returncode == 0, proc.stderr
    assert target.read_text().strip() == "hello"


def test_reads_outside_the_workspace_are_still_permitted(manager, worker):
    """Containment is a *write* boundary; a worker that cannot read its
    toolchain is useless.  Proven positively so the deny cases below are
    not passing for the trivial reason that nothing runs at all."""
    proc = run_contained(manager, worker, "cat /etc/hosts > read-back.txt && echo READ_OK")
    assert proc.returncode == 0, proc.stderr
    assert "READ_OK" in proc.stdout
    assert (worker["workspace"] / "read-back.txt").read_text()


def test_absolute_path_write_outside_workspace_is_denied(manager, worker):
    escape = worker["outside"] / "absolute.txt"
    proc = run_contained(manager, worker, f"echo pwned > {escape}")
    assert not escape.exists(), "an absolute path escaped the sandbox"
    assert proc.returncode != 0


def test_dotdot_traversal_write_is_denied(manager, worker):
    (worker["workspace"] / "sub").mkdir()
    escape = worker["outside"] / "dotdot.txt"
    relative = "sub/../../outside/dotdot.txt"
    proc = run_contained(manager, worker, f"echo pwned > {relative}")
    assert not escape.exists(), "a ../ traversal escaped the sandbox"
    assert proc.returncode != 0


def test_shell_append_redirect_outside_is_denied(manager, worker):
    escape = worker["outside"] / "redirect.txt"
    run_contained(manager, worker, f"echo pwned >> {escape}")
    assert not escape.exists()


def test_tee_outside_is_denied(manager, worker):
    escape = worker["outside"] / "tee.txt"
    run_contained(manager, worker, f"echo pwned | tee {escape}")
    assert not escape.exists()


def test_cp_outside_is_denied(manager, worker):
    source = worker["workspace"] / "src.txt"
    source.write_text("payload")
    escape = worker["outside"] / "copied.txt"
    run_contained(manager, worker, f"cp {source} {escape}")
    assert not escape.exists()


def test_spawned_child_process_cannot_escape(manager, worker):
    """The sandbox is inherited: a grandchild spawned by the command is
    confined too, so `python -c "subprocess.run(...)"` buys nothing."""
    escape = worker["outside"] / "grandchild.txt"
    payload = (
        "import subprocess; subprocess.run(['/bin/sh','-c',"
        f"'echo pwned > {escape}'])"
    )
    run_contained(manager, worker, f'{sys.executable} -c "{payload}"')
    assert not escape.exists()


def test_hermes_config_style_write_to_hermes_home_is_denied(manager, worker, tmp_path):
    """`hermes config`/`hermes profile` mutations are only reachable through
    the terminal, and land outside the workspace -- so the sandbox denies
    them at the OS layer rather than by parsing a command line."""
    target = tmp_path / ".hermes" / "config.yaml"
    before = target.read_text()
    run_contained(manager, worker, f"echo 'approvals: off' > {target}")
    assert target.read_text() == before


def test_symlink_planted_in_the_workspace_cannot_redirect_a_write(manager, worker):
    """A symlink *inside* the workspace pointing out of it is still an
    outside write when it is followed -- the kernel checks the real path."""
    escape = worker["outside"] / "via-symlink.txt"
    link = worker["workspace"] / "link.txt"
    link.symlink_to(escape)
    run_contained(manager, worker, "echo pwned > link.txt")
    assert not escape.exists()


# --------------------------------------------------------------------------- #
# The gate's own preconditions.
# --------------------------------------------------------------------------- #

def test_background_execution_is_denied(manager, worker):
    message = _blocked(manager, {
        "command": "echo hi", "workdir": str(worker["workspace"]), "background": True,
    })
    assert "background" in message.lower()


def test_missing_workdir_is_denied(manager, worker):
    message = _blocked(manager, {"command": "echo hi"})
    assert "workdir" in message.lower()


def test_workdir_outside_the_workspace_is_denied(manager, worker):
    message = _blocked(manager, {"command": "echo hi", "workdir": str(worker["outside"])})
    assert "confined dispatcher workspace" in message


def test_nonexistent_workdir_is_denied(manager, worker):
    message = _blocked(manager, {
        "command": "echo hi", "workdir": str(worker["workspace"] / "not-created-yet"),
    })
    assert "workdir" in message.lower() or "not a directory" in message.lower()


def test_workdir_that_is_a_file_is_denied(manager, worker):
    file_path = worker["workspace"] / "a-file.txt"
    file_path.write_text("x")
    _blocked(manager, {"command": "echo hi", "workdir": str(file_path)})


def test_the_workspace_root_itself_is_a_valid_workdir(manager, worker):
    result = _hook(manager, {"command": "echo hi", "workdir": str(worker["workspace"])})
    assert result is not None and result.get("action") == "modify"


def test_read_only_allowlisted_commands_are_contained_too(manager, worker):
    """The read-only shortcut is not an escape hatch in dispatcher scope:
    `git status` is still rewritten to run inside the sandbox."""
    contained_command(manager, worker, "git status --short")


def test_a_command_that_pre_wraps_itself_is_still_wrapped(manager, worker):
    """A model that emits its own `sandbox-exec -f <permissive>` prefix must
    not be taken at its word -- the gate always adds its own layer, and
    macOS sandboxes only ever intersect."""
    escape = worker["outside"] / "self-wrapped.txt"
    permissive = worker["workspace"] / "permissive.sb"
    permissive.write_text("(version 1)\n(allow default)\n")
    rewritten = contained_command(
        manager, worker,
        f"{cc.SANDBOX_EXEC} -f {permissive} /bin/sh -c 'echo pwned > {escape}'",
    )
    assert rewritten.count(cc.SANDBOX_EXEC) == 2
    subprocess.run(["/bin/sh", "-c", rewritten], cwd=str(worker["workspace"]),
                   capture_output=True, text=True, timeout=60)
    assert not escape.exists()


# --------------------------------------------------------------------------- #
# The host's own dispatch point, not just the plugin's return value.
# --------------------------------------------------------------------------- #

def test_the_host_dispatch_point_carries_the_containment_rewrite(manager, worker, monkeypatch):
    """The rewrite is only enforcement if the host actually applies it.

    Driven through ``hermes_cli.plugins._dispatch_pre_tool_call_hooks`` --
    the single point ``model_tools`` calls before executing any tool, and
    the only place a ``modify`` directive is merged into the args the tool
    finally receives.
    """
    monkeypatch.setattr(pmod, "get_plugin_manager", lambda *a, **k: manager)
    original = {"command": "echo hi", "workdir": str(worker["workspace"])}

    block_message, modified = pmod._dispatch_pre_tool_call_hooks(
        "terminal", dict(original), session_id="s-dispatcher"
    )

    assert block_message is None
    assert modified is not None, "the host received no rewrite to apply"
    assert modified["command"].startswith(cc.SANDBOX_EXEC)
    # The rest of the call survives the merge untouched.
    assert modified["workdir"] == original["workdir"]


def test_the_host_dispatch_point_blocks_an_unconfinable_call(manager, worker, monkeypatch):
    monkeypatch.setattr(pmod, "get_plugin_manager", lambda *a, **k: manager)

    block_message, modified = pmod._dispatch_pre_tool_call_hooks(
        "terminal",
        {"command": "echo hi", "workdir": str(worker["outside"])},
        session_id="s-dispatcher",
    )

    assert modified is None
    assert block_message is not None and "confined dispatcher workspace" in block_message


# --------------------------------------------------------------------------- #
# Fail closed.
# --------------------------------------------------------------------------- #

def test_missing_sandbox_binary_denies_the_call(manager, worker, monkeypatch):
    monkeypatch.setattr(cc, "SANDBOX_EXEC", "/usr/bin/sandbox-exec-DOES-NOT-EXIST")
    message = _blocked(manager, {
        "command": "echo hi", "workdir": str(worker["workspace"]),
    })
    assert "sandbox" in message.lower()


def test_profile_generation_failure_denies_the_call(manager, worker, monkeypatch):
    def _explode(_workspace):
        raise cc.ProfileGenerationError("profile could not be generated")

    monkeypatch.setattr(cc, "generate_profile", _explode)
    message = _blocked(manager, {
        "command": "echo hi", "workdir": str(worker["workspace"]),
    })
    assert "sandbox" in message.lower() or "profile" in message.lower()


def test_a_workspace_path_that_cannot_be_expressed_in_a_profile_is_refused(tmp_path):
    """Profile generation is string-built, so a path containing a quote or a
    backslash could otherwise terminate the s-expression early.  Refuse."""
    for hostile in ('/tmp/ws"escape', "/tmp/ws\\escape", "/tmp/ws\nescape"):
        with pytest.raises(cc.ProfileGenerationError):
            cc.generate_profile(Path(hostile))


def test_a_symlinked_workspace_is_refused(tmp_path):
    """The profile matches on real paths, so a workspace whose own path is
    not canonical would confine the wrong subtree."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(cc.ProfileGenerationError):
        cc.generate_profile(link)


# --------------------------------------------------------------------------- #
# No identity, no containment -- and no free pass either.
# --------------------------------------------------------------------------- #

def test_env_spoofed_terminal_call_gets_no_dispatcher_scope(tmp_path, monkeypatch, manager):
    """A process that forges the whole Kanban env but never bound an
    identity is an ordinary unclaimed session: denied, not sandboxed."""
    workspace = tmp_path / "spoof-ws"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "kb_forged")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    message = _blocked(manager, {"command": "rm -rf /", "workdir": str(workspace)})
    assert "No active cross-session work claim" in message


def test_delegated_child_terminal_call_loses_the_bound_containment(manager, worker):
    with delegation_context.delegated_child_context(session_id="s-child"):
        message = _blocked(
            manager,
            {"command": "echo hi", "workdir": str(worker["workspace"])},
            session_id="s-child",
        )
    assert "No active cross-session work claim" in message


_INHERITED_TOKEN_PROBE = textwrap.dedent(
    """
    import json, sys
    from agent import dispatcher_identity as di

    try:
        di.bind_token(sys.argv[1], db_path=sys.argv[2])
        print(json.dumps({"bound": True, "error": ""}))
    except Exception as exc:
        print(json.dumps({"bound": False, "error": str(exc)}))
    """
)


def test_an_inherited_token_cannot_rebind_in_another_process(tmp_path, worker):
    """Even handed the raw token, a second process binds nothing: the row is
    consumed once and pinned to the original PID and kernel start time."""
    db_path = tmp_path / "kanban.db"
    with kbc.connect_closing(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT token_sha256 FROM worker_identities"
        ).fetchone()
    assert row is not None

    # Mint a *fresh* token for this process, then let a child try to use it:
    # the child's PID and start time cannot match the row's.
    with kbc.connect_closing(db_path=db_path) as conn:
        task = kb.get_task(conn, worker["task_id"])
        token = kbi.issue_worker_identity(
            conn,
            task_id=worker["task_id"],
            run_id=int(task.current_run_id),
            workspace_path=str(worker["workspace"]),
            worker_pid=os.getpid(),
            ttl_seconds=3600,
        )

    script = tmp_path / "rebind_probe.py"
    script.write_text(_INHERITED_TOKEN_PROBE)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, str(script), token, str(db_path)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["bound"] is False
    assert "pid mismatch" in payload["error"]


# --------------------------------------------------------------------------- #
# Safe mode must not disarm a dispatcher worker (correction to the CV-A01
# containment: HERMES_SAFE_MODE inherited across the spawn boundary).
#
# `hermes_cli.plugins.PluginManager.discover_and_load` returns before it scans
# anything when HERMES_SAFE_MODE is set, and this plugin is what enforces
# dispatcher scope. A worker that inherited safe mode from its dispatcher
# therefore loaded no gate, registered no `pre_tool_call` hook, and ran its
# task with no containment -- the boundary failed open.
#
# The regression below starts from a dispatcher process that really is in safe
# mode, builds the child environment through the real
# `kanban_db._scrub_worker_env`, and runs a genuine second process under it.
# That child discovers plugins for itself, binds its own dispatcher identity,
# opens a real execution-turn lease (proving admission through the required
# hook), and then tries to write outside its workspace through the gate. Every
# step happens in the child; nothing about the outcome is asserted from the
# parent's already-loaded state.
# --------------------------------------------------------------------------- #

_SAFE_MODE_WORKER_PROBE = '''\
"""Child: a dispatcher worker spawned by a dispatcher that was in safe mode."""
import json, os, subprocess, sys
from pathlib import Path

db_path, workspace, outside_file = sys.argv[1:4]
workspace = Path(workspace)
outside_file = Path(outside_file)
report = {"safe_mode_inherited": "HERMES_SAFE_MODE" in os.environ}

from agent import dispatcher_identity as di
from agent import execution_turn
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_identity as kbi
from hermes_cli import plugins as pmod

# 1. Discovery must actually happen in this process.
manager = pmod.PluginManager()
manager.discover_and_load()
loaded = manager._plugins.get("work-claims")
report["plugin_loaded"] = bool(loaded is not None and loaded.enabled)
report["plugin_error"] = "" if loaded is None else str(getattr(loaded, "error", "") or "")
report["begin_hook_registered"] = bool(manager._hooks.get("on_execution_turn_begin"))
pmod._plugin_manager = manager

# 2. Bind this process as a real dispatcher worker (own PID, own start time).
with kbc.connect_closing(db_path=Path(db_path)) as conn:
    task_id = kb.create_task(
        conn, title="safe-mode worker", assignee="tester", workspace_path=str(workspace)
    )
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    conn.commit()
    claimed = kb.claim_task(conn, task_id)
    token = kbi.issue_worker_identity(
        conn,
        task_id=task_id,
        run_id=int(claimed.current_run_id),
        workspace_path=str(workspace),
        worker_pid=os.getpid(),
        ttl_seconds=3600,
    )
di.bind_token(token, db_path=str(db_path))
report["identity_bound"] = di.get_bound() is not None

# 3. Security admission: the required begin hook must accept this turn.
try:
    lease = execution_turn.begin("s-safe-mode", "s-safe-mode:t1:aaaa", renew_interval_seconds=3600.0)
    report["lease_opened"] = lease is not None
    report["admission_error"] = ""
    if lease is not None:
        execution_turn.end(lease, outcome="success")
except Exception as exc:
    report["lease_opened"] = False
    report["admission_error"] = "%s: %s" % (type(exc).__name__, exc)

# 4. An outside-the-workspace terminal write, driven through the real gate.
command = "echo pwned > %s" % outside_file
results = manager.invoke_hook(
    "pre_tool_call",
    tool_name="terminal",
    args={"command": command, "workdir": str(workspace)},
    session_id="s-safe-mode",
)
directive = results[0] if results else None
report["directive"] = directive if isinstance(directive, dict) else None
if isinstance(directive, dict) and directive.get("action") == "modify":
    rewritten = directive["args"]["command"]
    report["rewritten_is_sandboxed"] = rewritten != command
    proc = subprocess.run(
        ["/bin/sh", "-c", rewritten],
        cwd=str(workspace), capture_output=True, text=True, timeout=60,
    )
    report["escape_returncode"] = proc.returncode
else:
    report["rewritten_is_sandboxed"] = False
    report["escape_returncode"] = None

report["outside_file_exists"] = outside_file.exists()
print(json.dumps(report))
'''


def _worker_env_from_a_safe_mode_dispatcher() -> dict:
    """The env a safe-mode dispatcher really hands its worker."""
    dispatcher_env = dict(os.environ)
    dispatcher_env["HERMES_SAFE_MODE"] = "1"
    worker_env = dict(dispatcher_env)
    kbd._scrub_worker_env(worker_env)
    worker_env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )
    return worker_env


def test_a_worker_spawned_by_a_safe_mode_dispatcher_is_still_contained(tmp_path):
    """End to end, in a real second process, from HERMES_SAFE_MODE=1.

    One test covering the whole chain, because the chain is the finding: any
    link asserted in isolation passes while the worker is still unconfined.
    """
    db_path = tmp_path / "safe-mode-kanban.db"
    workspace = tmp_path / "safe-mode-ws"
    workspace.mkdir()
    outside = tmp_path / "safe-mode-outside"
    outside.mkdir()
    escape = outside / "pwned.txt"

    script = tmp_path / "safe_mode_worker_probe.py"
    script.write_text(_SAFE_MODE_WORKER_PROBE)

    proc = subprocess.run(
        [sys.executable, str(script), str(db_path), str(workspace), str(escape)],
        env=_worker_env_from_a_safe_mode_dispatcher(),
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    # The scrub reached the child.
    assert report["safe_mode_inherited"] is False
    # Discovery ran, and the gate is loaded.
    assert report["plugin_loaded"] is True, report["plugin_error"]
    assert report["begin_hook_registered"] is True
    # Security admission happened through the required begin hook.
    assert report["identity_bound"] is True
    assert report["lease_opened"] is True, report["admission_error"]
    # And the containment the plugin exists to provide actually denies.
    assert report["rewritten_is_sandboxed"] is True, report["directive"]
    assert report["escape_returncode"] != 0
    assert report["outside_file_exists"] is False
    assert not escape.exists(), "an outside write escaped a safe-mode-spawned worker"


def test_a_worker_that_did_inherit_safe_mode_refuses_to_run(tmp_path):
    """The backstop, proven in the same real second process.

    Undoes the scrub -- the exact pre-correction environment -- and shows the
    child now *fails closed*: discovery is skipped, the gate is absent, and the
    execution-turn lease refuses to admit the turn instead of letting it run
    unconfined. This is what makes the env scrub a repair rather than the only
    line of defence.
    """
    db_path = tmp_path / "unscrubbed-kanban.db"
    workspace = tmp_path / "unscrubbed-ws"
    workspace.mkdir()
    outside = tmp_path / "unscrubbed-outside"
    outside.mkdir()
    escape = outside / "pwned.txt"

    script = tmp_path / "unscrubbed_worker_probe.py"
    script.write_text(_SAFE_MODE_WORKER_PROBE)

    env = _worker_env_from_a_safe_mode_dispatcher()
    env["HERMES_SAFE_MODE"] = "1"  # the inheritance the correction removes

    proc = subprocess.run(
        [sys.executable, str(script), str(db_path), str(workspace), str(escape)],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["safe_mode_inherited"] is True
    assert report["plugin_loaded"] is False
    assert report["begin_hook_registered"] is False
    # Fail closed: the turn is refused, not silently run without a gate.
    assert report["identity_bound"] is True
    assert report["lease_opened"] is False
    assert "RequiredHookError" in report["admission_error"]
    assert "on_execution_turn_begin" in report["admission_error"]
    # No gate is loaded, so nothing rewrites the command -- which is exactly
    # why the turn above was refused before any tool call could be made.
    assert report["directive"] is None
