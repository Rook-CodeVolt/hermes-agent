"""CV-A01 Stage 2B: write_file/patch are confined in the *operation*.

Stage 2A confined the file mutators with a pre-tool ancestor walk.  A
pre-tool check can only ever describe the filesystem as it was at check
time: between the check and the write, anything with a foothold in the
workspace can swap a component for a symlink and redirect the write.  The
shipped ``_atomic_write`` made that trivially exploitable -- it explicitly
``readlink -f``s a symlinked target and writes to whatever it resolves to.

So this stage moves the boundary into the write itself.  Under a bound
dispatcher identity the real ``write_file``/``patch`` implementations in
``tools/file_operations.py`` route through the descriptor-relative
primitives in ``agent/workspace_confinement.py``: every component opened
no-follow from a re-verified workspace descriptor, hardlinked targets
refused, temp+fsync+``renameat`` inside the verified parent, and the
identity revalidated at operation time rather than trusted from the hook.

Every case here drives the **real tools** against the **real filesystem**:
``tools.file_tools.write_file_tool`` and ``patch_tool``, the same entry
points the host calls.  Positive controls come first, so no denial below
can pass merely because nothing ran.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from agent import confined_file_ops as cfo
from agent import delegation_context
from agent import dispatcher_identity as di
from agent import workspace_confinement as wc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_identity as kbi
from hermes_cli import plugins as pmod
from plugins.work_claims import core
from tools.file_tools import patch_tool, write_file_tool

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


def _bind_worker(tmp_path, *, workspace: Path | None = None, db_path: Path | None = None) -> dict:
    """Mint and bind a genuine dispatcher identity for *this* process.

    ``tmp_path`` is already canonical on macOS (``/private/var/...``), which
    ``canonical_walk`` requires: it refuses a symlinked component outright.
    """
    db_path = db_path or (tmp_path / "kanban.db")
    workspace = workspace or (tmp_path / "worker-ws")
    workspace.mkdir(parents=True, exist_ok=True)
    with kbc.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(
            conn, title="stage 2b worker", assignee="tester",
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
    identity = di.bind_token(token, db_path=str(db_path))
    return {
        "db_path": db_path,
        "task_id": task_id,
        "run_id": int(claimed.current_run_id),
        "workspace": workspace,
        "identity": identity,
    }


@pytest.fixture
def worker(tmp_path):
    board = _bind_worker(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    board["outside"] = outside
    return board


@pytest.fixture
def manager():
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins.get("work-claims")
    assert loaded is not None and loaded.enabled, getattr(loaded, "error", "not discovered")
    return mgr


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_TASK = "t-stage2b"


def write(path, content: str, task_id: str = _TASK) -> dict:
    return json.loads(write_file_tool(str(path), content, task_id=task_id))


def patch(path, old: str, new: str, task_id: str = _TASK, **kw) -> dict:
    return json.loads(
        patch_tool(mode="replace", path=str(path), old_string=old,
                   new_string=new, task_id=task_id, **kw)
    )


def refused(result: dict) -> str:
    assert "error" in result, f"expected a refusal, got: {result}"
    return result["error"]


def case_insensitive(root: Path) -> bool:
    probe = root / "HermesCaseProbe"
    probe.write_text("x")
    try:
        return (root / "hermescaseprobe").exists()
    finally:
        probe.unlink()


# --------------------------------------------------------------------------- #
# Positive controls -- confinement must not break the ordinary write
# --------------------------------------------------------------------------- #

def test_confined_worker_creates_a_new_in_workspace_file(worker):
    target = worker["workspace"] / "notes.txt"
    result = write(target, "hello confined world\n")
    assert "error" not in result, result
    assert target.read_text() == "hello confined world\n"
    assert result["bytes_written"] == len("hello confined world\n")
    assert result.get("verified") is True


def test_confined_worker_replaces_an_existing_file_preserving_mode(worker):
    target = worker["workspace"] / "script.sh"
    target.write_text("old\n")
    target.chmod(0o750)
    result = write(target, "new\n")
    assert "error" not in result, result
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


def test_confined_worker_writes_into_an_existing_subdirectory(worker):
    (worker["workspace"] / "a" / "b").mkdir(parents=True)
    target = worker["workspace"] / "a" / "b" / "deep.txt"
    result = write(target, "deep\n")
    assert "error" not in result, result
    assert target.read_text() == "deep\n"


def test_only_the_final_component_of_a_confined_write_may_be_new(worker):
    """The operation is exactly as permissive as the Stage 2A gate.

    That gate allows a not-yet-existing *final* component and nothing above
    it, so the write does too: a writer that created directories the
    verification never walked would be the weaker of the two layers, and
    therefore the real boundary.  Widening both is a separate decision.
    """
    target = worker["workspace"] / "not-yet" / "deep.txt"
    assert "ancestor does not exist" in refused(write(target, "deep\n"))
    assert not target.parent.exists()


def test_confined_worker_patches_an_in_workspace_file(worker):
    target = worker["workspace"] / "mod.py"
    target.write_text("value = 1\nother = 2\n")
    result = patch(target, "value = 1", "value = 42")
    assert "error" not in result, result
    assert result["success"] is True
    assert target.read_text() == "value = 42\nother = 2\n"
    assert "value = 42" in result["diff"]


def test_confined_patch_still_reports_an_already_applied_edit(worker):
    """The preimage semantics the patch tool promises are unchanged."""
    target = worker["workspace"] / "applied.py"
    target.write_text("ALPHA = 1\nBETA = 2\n")
    result = patch(target, "GAMMA = 3", "BETA = 2")
    assert result.get("no_change") is True, result
    assert result["success"] is True
    assert target.read_text() == "ALPHA = 1\nBETA = 2\n"


def test_a_confined_patch_that_matches_nothing_writes_nothing(worker):
    target = worker["workspace"] / "nomatch.py"
    target.write_text("one\ntwo\nthree\n")
    assert "Could not find" in refused(patch(target, "four", "five"))
    assert target.read_text() == "one\ntwo\nthree\n"


def test_confined_patch_preserves_crlf_line_endings(worker):
    target = worker["workspace"] / "win.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")
    result = patch(target, "beta", "gamma")
    assert "error" not in result, result
    assert target.read_bytes() == b"alpha\r\ngamma\r\n"


def test_confined_write_preserves_a_utf8_bom(worker):
    target = worker["workspace"] / "bom.txt"
    target.write_bytes("﻿old\n".encode("utf-8"))
    result = write(target, "new\n")
    assert "error" not in result, result
    assert target.read_bytes() == "﻿new\n".encode("utf-8")


def test_confined_write_keeps_the_failclosed_json_syntax_gate(worker):
    target = worker["workspace"] / "broken.json"
    result = write(target, "{not json")
    assert "syntax validation" in refused(result)
    assert not target.exists()


# --------------------------------------------------------------------------- #
# Escapes: every one is executed and proven not to have written
# --------------------------------------------------------------------------- #

def test_absolute_path_outside_the_workspace_is_refused(worker):
    target = worker["outside"] / "loot.txt"
    assert "escapes the confined workspace" in refused(write(target, "owned\n"))
    assert not target.exists()


def test_parent_traversal_out_of_the_workspace_is_refused(worker):
    target = worker["workspace"] / ".." / "outside" / "loot.txt"
    refused(write(target, "owned\n"))
    assert not (worker["outside"] / "loot.txt").exists()


def test_a_symlinked_final_component_cannot_redirect_the_write(worker):
    """The shipped ``_atomic_write`` deliberately follows a symlinked target.

    Inside the confinement that is exactly the escape: a link planted in the
    workspace pointing at a file outside it.
    """
    victim = worker["outside"] / "victim.txt"
    victim.write_text("original\n")
    link = worker["workspace"] / "innocent.txt"
    link.symlink_to(victim)

    refused(write(link, "owned\n"))
    assert victim.read_text() == "original\n"
    assert link.is_symlink()


def test_a_symlinked_ancestor_cannot_redirect_the_write(worker):
    escape_dir = worker["outside"] / "escape"
    escape_dir.mkdir()
    (worker["workspace"] / "sub").symlink_to(escape_dir)

    target = worker["workspace"] / "sub" / "loot.txt"
    refused(write(target, "owned\n"))
    assert not (escape_dir / "loot.txt").exists()


def test_a_hardlink_alias_to_an_outside_file_is_refused(worker):
    """A hardlink has no path to walk -- only ``st_nlink`` catches it."""
    victim = worker["outside"] / "victim.txt"
    victim.write_text("original\n")
    alias = worker["workspace"] / "alias.txt"
    os.link(victim, alias)

    refused(write(alias, "owned\n"))
    assert victim.read_text() == "original\n"
    assert alias.read_text() == "original\n"


def test_a_hardlinked_patch_target_is_refused(worker):
    victim = worker["outside"] / "config.py"
    victim.write_text("SAFE = True\n")
    alias = worker["workspace"] / "config.py"
    os.link(victim, alias)

    refused(patch(alias, "SAFE = True", "SAFE = False"))
    assert victim.read_text() == "SAFE = True\n"


def test_a_case_variant_of_the_workspace_prefix_cannot_escape(worker):
    """APFS is case-insensitive but case-preserving: ``/WS/x`` and ``/ws/x``
    are the same inode, and only an exact-case walk notices."""
    ws = worker["workspace"]
    variant = ws.parent / ws.name.upper() / "loot.txt"
    refused(write(variant, "owned\n"))
    assert not (ws / "loot.txt").exists()


def test_a_case_variant_of_an_existing_target_is_refused(worker):
    if not case_insensitive(worker["workspace"]):
        pytest.skip("case-sensitive filesystem: no case-variant alias exists")
    target = worker["workspace"] / "notes.txt"
    target.write_text("original\n")
    refused(write(worker["workspace"] / "NOTES.TXT", "owned\n"))
    assert target.read_text() == "original\n"


def test_a_directory_cannot_be_replaced_by_a_confined_write(worker):
    target = worker["workspace"] / "adir"
    target.mkdir()
    refused(write(target, "owned\n"))
    assert target.is_dir()


# --------------------------------------------------------------------------- #
# TOCTOU: the swap lands *after* every precheck, in the real race window
# --------------------------------------------------------------------------- #

def test_a_swap_between_the_precheck_and_the_write_is_caught(worker, monkeypatch):
    """Plant the symlink from inside the write, after all path checks ran.

    ``_snapshot_lsp_baseline`` is the last thing ``write_file`` does before
    it puts bytes on disk, so a swap there is exactly the check/use race a
    pre-tool walk cannot close.
    """
    from tools.file_operations import ShellFileOperations

    victim = worker["outside"] / "victim.txt"
    victim.write_text("original\n")
    target = worker["workspace"] / "innocent.txt"
    target.write_text("innocent\n")

    real_snapshot = ShellFileOperations._snapshot_lsp_baseline

    def swap_then_snapshot(self, path):
        if Path(path) == target and not target.is_symlink():
            target.unlink()
            target.symlink_to(victim)
        return real_snapshot(self, path)

    monkeypatch.setattr(ShellFileOperations, "_snapshot_lsp_baseline", swap_then_snapshot)

    refused(write(target, "owned\n"))
    assert victim.read_text() == "original\n"


def test_a_swap_to_a_hardlink_between_precheck_and_write_is_caught(worker, monkeypatch):
    from tools.file_operations import ShellFileOperations

    victim = worker["outside"] / "victim.txt"
    victim.write_text("original\n")
    target = worker["workspace"] / "innocent.txt"
    target.write_text("innocent\n")

    real_snapshot = ShellFileOperations._snapshot_lsp_baseline

    def swap_then_snapshot(self, path):
        if Path(path) == target and target.stat().st_nlink == 1:
            target.unlink()
            os.link(victim, target)
        return real_snapshot(self, path)

    monkeypatch.setattr(ShellFileOperations, "_snapshot_lsp_baseline", swap_then_snapshot)

    refused(write(target, "owned\n"))
    assert victim.read_text() == "original\n"


def test_a_patch_refuses_to_write_a_preimage_that_no_longer_exists(worker, monkeypatch):
    """The patch must land on the exact file its preimage was read from.

    The swap is planted inside ``fuzzy_find_and_replace`` -- between the
    read that produced the preimage and the write that applies it.
    """
    import tools.fuzzy_match as fuzzy

    target = worker["workspace"] / "mod.py"
    target.write_text("value = 1\n")
    impostor = worker["workspace"] / "impostor.py"
    impostor.write_text("value = 1\n")

    real_replace = fuzzy.fuzzy_find_and_replace
    swapped: list[bool] = []

    def swap_then_replace(*args, **kwargs):
        if not swapped:
            swapped.append(True)
            target.unlink()
            os.rename(impostor, target)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(fuzzy, "fuzzy_find_and_replace", swap_then_replace)

    result = patch(target, "value = 1", "value = 42")
    assert "no longer the file the patch was matched against" in refused(result)
    assert target.read_text() == "value = 1\n"


# --------------------------------------------------------------------------- #
# The control plane may not overwrite itself
# --------------------------------------------------------------------------- #

def test_the_worker_cannot_rewrite_the_gate_it_runs_under(tmp_path, monkeypatch):
    """A worker whose workspace *is* the checkout it runs from must not be
    able to rewrite the module that authorizes it: the next process would
    load the rewritten gate."""
    workspace = tmp_path / "self-ws"
    (workspace / "plugins" / "work_claims").mkdir(parents=True)
    gate = workspace / "plugins" / "work_claims" / "core.py"
    gate.write_text("# the gate\n")
    _bind_worker(tmp_path, workspace=workspace)

    monkeypatch.setattr(core, "__file__", str(gate))
    assert "control-plane" in refused(write(gate, "def mutation_allowed(*a): return True\n"))
    assert gate.read_text() == "# the gate\n"


def test_the_worker_cannot_rewrite_the_hermes_home_control_plane(tmp_path):
    """``HERMES_HOME`` carries the plugin set, the installer and the config
    that decide what runs at all."""
    workspace = tmp_path / "home-ws"
    home = workspace / ".hermes"
    (home / "plugins" / "work-claims").mkdir(parents=True)
    (home / "scripts").mkdir(parents=True)
    os.environ["HERMES_HOME"] = str(home)
    try:
        _bind_worker(tmp_path, workspace=workspace)
        for target in (
            home / "plugins" / "work-claims" / "core.py",
            home / "scripts" / "install_work_claims.py",
        ):
            assert "control-plane" in refused(write(target, "owned\n")), target
            assert not target.exists()
        # ``config.yaml`` decides which plugins load at all.  The confinement
        # guard covers it too, but the pre-existing protected-instruction
        # guard in ``file_tools`` refuses it first, so only the refusal --
        # not which layer produced it -- is asserted here.
        config = home / "config.yaml"
        refused(write(config, "owned\n"))
        assert not config.exists()
        ordinary = workspace / "ordinary.txt"
        assert "error" not in write(ordinary, "fine\n")
    finally:
        os.environ["HERMES_HOME"] = str(tmp_path / ".hermes")


def test_the_worker_cannot_rewrite_the_kanban_database(tmp_path):
    """The database is where identity itself lives."""
    workspace = tmp_path / "db-ws"
    workspace.mkdir()
    db_path = workspace / "kanban.db"
    _bind_worker(tmp_path, workspace=workspace, db_path=db_path)
    before = db_path.read_bytes()
    assert "control-plane" in refused(write(db_path, "owned\n"))
    assert db_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Identity is revalidated at operation time, not trusted from the hook
# --------------------------------------------------------------------------- #

def test_a_revoked_identity_stops_the_write_at_operation_time(worker):
    """Revocation must bite in the write itself: the pre-tool hook may have
    allowed this call before the run advanced."""
    with kbc.connect_closing(db_path=worker["db_path"]) as conn:
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (worker["run_id"] + 99, worker["task_id"]),
        )
        conn.commit()

    target = worker["workspace"] / "after-revocation.txt"
    assert "no longer valid" in refused(write(target, "owned\n"))
    assert not target.exists()


def test_a_workspace_swapped_for_another_directory_stops_the_write(worker):
    """The workspace is re-matched by ``(st_dev, st_ino)`` on every call."""
    ws = worker["workspace"]
    replacement = ws.parent / "replacement"
    replacement.mkdir()
    ws.rmdir()
    replacement.rename(ws)

    target = ws / "loot.txt"
    refused(write(target, "owned\n"))
    assert not target.exists()


def test_confinement_fails_closed_without_platform_primitives(worker, monkeypatch):
    monkeypatch.setattr(wc, "containment_supported", lambda: False)
    target = worker["workspace"] / "unavailable.txt"
    assert "unavailable" in refused(write(target, "owned\n"))
    assert not target.exists()


# --------------------------------------------------------------------------- #
# No descendant inherits the binding -- and none is silently unconfined
# --------------------------------------------------------------------------- #

def test_a_delegated_child_scope_has_no_dispatcher_scope(worker):
    with delegation_context.delegated_child_context("s-child"):
        assert di.get_bound() is None
        assert cfo.active_scope() is None


def test_an_in_process_cron_scope_has_no_dispatcher_scope(worker):
    with delegation_context.non_dispatcher_owned_context():
        assert di.get_bound() is None
        assert cfo.active_scope() is None


def test_a_delegated_child_write_is_still_denied_by_the_gate(worker, manager):
    """No dispatcher scope means no *bypass* either: the child falls through
    to ordinary claim enforcement, which it cannot satisfy."""
    target = worker["outside"] / "child-loot.txt"
    with delegation_context.delegated_child_context("s-child"):
        results = manager.invoke_hook(
            "pre_tool_call", tool_name="write_file",
            args={"path": str(target), "content": "owned\n"},
            session_id="s-child",
        )
    directive = results[0] if results else None
    assert directive is not None and directive.get("action") == "block", directive
    assert not target.exists()


def test_a_spawned_subprocess_inherits_no_confinement_scope(worker, tmp_path):
    """A real process boundary: the child inherits the whole Kanban env and
    the parent's descriptors, and still resolves no scope at all."""
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(
        """
        import json, sys
        from agent import confined_file_ops as cfo
        from agent import dispatcher_identity as di
        print(json.dumps({
            "bound": di.get_bound() is not None,
            "scope": cfo.active_scope() is not None,
        }))
        """
    ))
    env = dict(os.environ)
    env.update({
        "HERMES_KANBAN_DB": str(worker["db_path"]),
        "HERMES_KANBAN_TASK": worker["task_id"],
        "HERMES_KANBAN_RUN_ID": str(worker["run_id"]),
        "HERMES_KANBAN_WORKSPACE": str(worker["workspace"]),
        "PYTHONPATH": str(REPO_ROOT),
    })
    proc = subprocess.run(
        [sys.executable, str(script)], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == {"bound": False, "scope": False}


# --------------------------------------------------------------------------- #
# Ordinary sessions are untouched
# --------------------------------------------------------------------------- #

def test_an_unbound_session_writes_anywhere_it_could_before(tmp_path):
    """No identity, no confinement: Stage 2B must not confine the ordinary
    agent, whose boundary is the claim gate, not a workspace descriptor."""
    assert cfo.active_scope() is None
    target = tmp_path / "anywhere" / "file.txt"
    result = write(target, "ordinary\n", task_id="t-ordinary")
    assert "error" not in result, result
    assert target.read_text() == "ordinary\n"


def test_an_unbound_session_still_follows_a_symlinked_target(tmp_path):
    """The documented pre-existing behaviour: an ordinary write edits the
    file a link points at rather than replacing the link.  Confinement must
    not have changed it for unbound processes."""
    assert cfo.active_scope() is None
    real = tmp_path / "real.txt"
    real.write_text("old\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    result = write(link, "new\n", task_id="t-ordinary")
    assert "error" not in result, result
    assert link.is_symlink()
    assert real.read_text() == "new\n"


def test_an_unbound_session_patches_normally(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("value = 1\n")
    result = patch(target, "value = 1", "value = 2", task_id="t-ordinary")
    assert "error" not in result, result
    assert target.read_text() == "value = 2\n"


def test_a_claimed_session_mutation_still_passes_the_gate(tmp_path, manager, monkeypatch):
    """The ordinary claim regime is unchanged by Stage 2B."""
    workspace = tmp_path / "claimed-ws"
    workspace.mkdir()
    monkeypatch.setattr(
        core, "prepare_workspace",
        lambda *a, **k: core.WorkspaceResult(str(workspace), str(workspace), False, None),
    )
    acquired = core.acquire(
        "s-claimed", "ordinary work", ["repo:" + str(workspace)],
        workspace=str(workspace), create_worktree=False,
    )
    assert acquired["success"], acquired
    try:
        inside = workspace / "file.txt"
        results = manager.invoke_hook(
            "pre_tool_call", tool_name="write_file",
            args={"path": str(inside), "content": "ok\n"},
            session_id="s-claimed",
        )
        assert not [r for r in results if r], f"claimed write was blocked: {results}"
        assert "error" not in write(inside, "ok\n", task_id="t-claimed")
        assert inside.read_text() == "ok\n"

        outside = tmp_path / "elsewhere.txt"
        blocked = manager.invoke_hook(
            "pre_tool_call", tool_name="write_file",
            args={"path": str(outside), "content": "no\n"},
            session_id="s-claimed",
        )
        directive = blocked[0] if blocked else None
        assert directive is not None and directive.get("action") == "block", directive
    finally:
        core.release_all_for_session("s-claimed", "test over", durable_terminal=True)


# --------------------------------------------------------------------------- #
# V4A delete / move are part of patch execution and are confined too
# --------------------------------------------------------------------------- #

def test_v4a_delete_outside_the_workspace_is_refused(worker):
    victim = worker["outside"] / "victim.txt"
    victim.write_text("original\n")
    result = json.loads(patch_tool(
        mode="patch",
        patch=f"*** Begin Patch\n*** Delete File: {victim}\n*** End Patch\n",
        task_id=_TASK,
    ))
    assert "error" in result, result
    assert victim.read_text() == "original\n"


def test_v4a_delete_inside_the_workspace_still_works(worker):
    doomed = worker["workspace"] / "doomed.txt"
    doomed.write_text("bye\n")
    result = json.loads(patch_tool(
        mode="patch",
        patch=f"*** Begin Patch\n*** Delete File: {doomed}\n*** End Patch\n",
        task_id=_TASK,
    ))
    assert "error" not in result, result
    assert not doomed.exists()


def test_a_symlinked_delete_target_cannot_unlink_outside(worker):
    victim = worker["outside"] / "victim.txt"
    victim.write_text("original\n")
    link = worker["workspace"] / "link.txt"
    link.symlink_to(victim)
    result = json.loads(patch_tool(
        mode="patch",
        patch=f"*** Begin Patch\n*** Delete File: {link}\n*** End Patch\n",
        task_id=_TASK,
    ))
    assert "error" in result, result
    assert victim.read_text() == "original\n"


def test_a_move_out_of_the_workspace_is_refused(worker):
    source = worker["workspace"] / "secret.txt"
    source.write_text("secret\n")
    destination = worker["outside"] / "exfil.txt"
    from tools.file_tools import _get_file_ops

    result = _get_file_ops(_TASK).move_file(str(source), str(destination))
    assert result.error, result
    assert not destination.exists()
    assert source.read_text() == "secret\n"


def test_a_move_inside_the_workspace_still_works(worker):
    source = worker["workspace"] / "before.txt"
    source.write_text("moved\n")
    destination = worker["workspace"] / "after.txt"
    from tools.file_tools import _get_file_ops

    result = _get_file_ops(_TASK).move_file(str(source), str(destination))
    assert not result.error, result.error
    assert destination.read_text() == "moved\n"
    assert not source.exists()
