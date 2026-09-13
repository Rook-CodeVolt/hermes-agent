"""Regression tests for delegate_task isolation from parent Kanban workers."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

# The subprocess-boundary tests below spawn ``sys.executable -c`` with a tmp
# cwd. Without an explicit PYTHONPATH the child resolves ``hermes_cli`` /
# ``agent`` through whatever install is on sys.path (in a worktree that is the
# MAIN checkout's editable install, which may not contain the code under
# test). Pin the repo root so the child always imports the tree being tested.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _python_with_repo_path(code: str) -> str:
    """Build a shell command running *code* with the repo under test on PYTHONPATH."""
    return (
        f"PYTHONPATH={shlex.quote(str(_REPO_ROOT))} "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    )


def _make_running_kanban_task(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    attachments_root = tmp_path / "attachments"
    workspace = tmp_path / "parent-workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "parent-worker")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(attachments_root))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn,
            title="parent",
            assignee="parent-worker",
            workspace_kind="scratch",
            workspace_path=str(workspace),
        )
        claim = kb.claim_task(conn, tid)
        assert claim is not None
        run_id = claim.current_run_id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return kb, tid, workspace, attachments_root


def test_delegated_child_context_suppresses_env_gated_kanban_tools(monkeypatch, tmp_path):
    """A delegate_task child must not inherit the parent's Kanban tool schema.

    The parent process may be a dispatcher worker with HERMES_KANBAN_TASK set;
    the child is only a subagent, not the run owner.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "123")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # noqa: F401 - ensure registered
    from agent.delegation_context import delegated_child_context
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    with delegated_child_context():
        schema = get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True)

    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "terminal" in names
    assert {n for n in names if n and n.startswith("kanban_")} == set()


def test_build_child_agent_strips_kanban_toolset_even_when_parent_is_worker(monkeypatch):
    """Child construction must fail closed even if the parent exposes kanban."""
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.valid_tool_names = {"terminal"}
            self.session_id = "child-session"

    import run_agent
    from tools import delegate_tool
    import tools.delegate_tool_config as delegate_tool_config

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool_config, "_load_config", lambda: {})

    class Parent:
        enabled_toolsets = ["terminal", "kanban"]
        valid_tool_names = {"terminal", "kanban_complete", "kanban_comment"}
        model = "test-model"
        provider = "test-provider"
        base_url = "http://example.invalid"
        api_mode = "chat_completions"
        platform = "cli"
        session_id = "parent-session"

    child = delegate_tool._build_child_agent(
        task_index=0,
        goal="review only",
        context=None,
        toolsets=None,
        model=None,
        max_iterations=3,
        task_count=1,
        parent_agent=Parent(),
    )

    assert child.valid_tool_names == {"terminal"}
    assert "kanban" not in captured["enabled_toolsets"]
    assert "kanban" in captured["disabled_toolsets"]


def test_delegate_child_execute_code_env_bridges_contextvar_and_scrubs_kanban(
    monkeypatch,
    tmp_path,
):
    """The real execute_code child-env builder must bridge ContextVar lineage.

    Regression coverage for the vulnerable path: delegate_task marks child
    execution with a ContextVar, while execute_code used to scrub plain
    ``os.environ`` and therefore never wrote HERMES_DELEGATED_CHILD_CONTEXT into
    the sandbox env.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "123")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "parent-workspace"))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "lock")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    from agent.delegation_context import delegated_child_context
    from tools.code_execution_env import _scrub_child_env

    with delegated_child_context():
        env = _scrub_child_env(
            dict(os.environ),
            is_passthrough=lambda k: k.startswith("HERMES_KANBAN_"),
            is_windows=False,
        )

    assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") is None
    assert env["HERMES_HOME"] == str(home)
    assert env["HERMES_DELEGATED_CHILD_CONTEXT"] == "1"
    assert "HERMES_KANBAN_TASK" not in env
    assert "HERMES_KANBAN_RUN_ID" not in env
    assert "HERMES_KANBAN_DB" not in env
    assert "HERMES_KANBAN_WORKSPACE" not in env
    assert "HERMES_KANBAN_CLAIM_LOCK" not in env




def _kanban_cli(*argv: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    code = (
        "import argparse,sys; from hermes_cli import kanban; "
        "p=argparse.ArgumentParser(); s=p.add_subparsers(dest='cmd'); "
        "kanban.build_parser(s); a=p.parse_args(sys.argv[1:]); "
        "raise SystemExit(kanban.kanban_command(a))"
    )
    return subprocess.run(
        [sys.executable, "-c", code, "kanban", *argv],
        env=env,
        cwd=str(_REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
        check=False,
    )


def test_real_cli_marker_strip_still_cannot_mutate_from_worker_descendant(monkeypatch, tmp_path):
    """A real subprocess cannot turn missing marker text into write authority."""
    kb, tid, _workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
        conn.commit()

    env = os.environ.copy()
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_KANBAN_RUN_ID", None)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = _kanban_cli("comment", tid, "forbidden", env=env)

    assert result.returncode == 1, result.stdout
    assert "delegate_task child contexts cannot mutate Kanban" in result.stdout
    with kbc.connect_closing() as conn:
        assert kb.list_comments(conn, tid) == []


def test_real_cli_rejects_free_form_author_spoof(monkeypatch, tmp_path):
    """Mutating CLI commands no longer accept caller-selected audit identities."""
    kb, tid, _workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    env = os.environ.copy()
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_KANBAN_RUN_ID", None)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env["PYTHONPATH"] = str(_REPO_ROOT)

    result = _kanban_cli("comment", tid, "spoof", "--author", "maya", env=env)

    assert result.returncode == 2, result.stdout
    assert "unrecognized arguments: --author maya" in result.stdout
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.list_comments(conn, tid) == []


def _bind_current_process_as_worker(kb, tid, run_id, workspace, db_path):
    """Mint + consume a real worker identity for THIS test process, exactly as
    a real dispatcher worker would have it bound at startup."""
    from agent import dispatcher_identity as di
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_identity as kbi

    with kbc.connect_closing(db_path=Path(db_path)) as conn:
        token = kbi.issue_worker_identity(
            conn, task_id=tid, run_id=run_id, workspace_path=str(workspace), worker_pid=os.getpid(),
        )
    return di.bind_token(token, db_path=str(db_path))


def test_worker_own_terminal_subprocess_mutation_succeeds_with_no_delegation_markers(
    monkeypatch, tmp_path,
):
    """The primary supported worker-mutation path: a real dispatcher worker's
    own ``terminal`` tool shelling out to ``hermes kanban ...`` must still be
    able to mutate its own task, even though the subprocess inherits no
    ContextVar, no delegation marker and no consumable handshake token (that
    was already single-use-consumed at the worker's own startup).

    This is the missing counterpart to
    ``test_real_cli_marker_strip_still_cannot_mutate_from_worker_descendant``,
    which only proves the (correct) denial half. Regression coverage for the
    fix that closes Maya's changes-requested finding on commit 4be3d28601.
    """
    from agent import dispatcher_identity as di
    from hermes_cli import kanban_db_connect as kbc

    kb, tid, workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    db_path = str(kb.kanban_db_path())

    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
        conn.commit()
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]

    di.reset_for_tests()
    try:
        _bind_current_process_as_worker(kb, tid, int(run_id), workspace, db_path)

        env = os.environ.copy()
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        env.pop("HERMES_KANBAN_TASK", None)
        env.pop("HERMES_KANBAN_RUN_ID", None)
        env.pop(di.HANDSHAKE_FD_ENV, None)
        env["PYTHONPATH"] = str(_REPO_ROOT)

        # The exact production call: LocalEnvironment._finalize_child_env is
        # what every terminal-tool subprocess spawn runs through.
        from tools.environments.local import _finalize_child_env
        env = _finalize_child_env(env)

        assert di.SUBPROCESS_CREDENTIAL_ENV in env, (
            "a worker with a live identity binding must mint a subprocess "
            "credential for its own terminal-tool child"
        )

        result = _kanban_cli("comment", tid, "allowed", env=env)
    finally:
        di.reset_for_tests()

    assert result.returncode == 0, result.stdout
    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
    assert [c.body for c in comments] == ["allowed"]


def test_delegate_task_child_cannot_mint_or_forge_subprocess_credential(monkeypatch, tmp_path):
    """The original abuse case must still fail closed after the fix: a
    delegate_task child inherits no dispatcher identity, so it can neither
    mint a real subprocess credential nor have one injected for it, and a
    hand-forged value in the credential env var must not validate."""
    from agent import dispatcher_identity as di
    from agent.delegation_context import delegated_child_context

    kb, tid, _workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
        conn.commit()

    di.reset_for_tests()
    try:
        with delegated_child_context():
            assert di.mint_subprocess_credential() is None

        env = os.environ.copy()
        env.pop("HERMES_KANBAN_TASK", None)
        env.pop("HERMES_KANBAN_RUN_ID", None)
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        env["PYTHONPATH"] = str(_REPO_ROOT)
        env[di.SUBPROCESS_CREDENTIAL_ENV] = "0" * 64  # forged, never issued by any DB

        result = _kanban_cli("comment", tid, "forbidden", env=env)
    finally:
        di.reset_for_tests()

    assert result.returncode == 1, result.stdout
    assert "delegate_task child contexts cannot mutate Kanban" in result.stdout
    with kbc.connect_closing() as conn:
        assert kb.list_comments(conn, tid) == []


def test_subprocess_credential_cannot_replay_across_tasks_same_board(monkeypatch, tmp_path):
    """A subprocess credential minted for worker-on-task-A's own terminal
    shell-out must not authorise mutating an unrelated task B on the same
    board. Regression for the cross-task bearer-token replay Maya
    independently reproduced against commit b40b5669b0 (BLOCK on
    t_6211a9dd, remediated by t_df2c4900): the credential row now carries
    (and ``find_valid_subprocess_credential``/``_assert_not_delegated_child_mutation``
    now check) the exact ``task_id`` it was minted for.
    """
    from agent import dispatcher_identity as di
    from hermes_cli import kanban_db_connect as kbc

    kb_mod, tid_a, workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    db_path = str(kb_mod.kanban_db_path())

    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid_a))
        conn.commit()
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid_a,)
        ).fetchone()["current_run_id"]
        tid_b = kb_mod.create_task(
            conn, title="victim", assignee="parent-worker",
            workspace_kind="scratch", workspace_path=str(workspace),
        )

    di.reset_for_tests()
    try:
        _bind_current_process_as_worker(kb_mod, tid_a, int(run_id), workspace, db_path)

        env = os.environ.copy()
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        env.pop("HERMES_KANBAN_TASK", None)
        env.pop("HERMES_KANBAN_RUN_ID", None)
        env.pop(di.HANDSHAKE_FD_ENV, None)
        env["PYTHONPATH"] = str(_REPO_ROOT)

        from tools.environments.local import _finalize_child_env
        env = _finalize_child_env(env)
        assert di.SUBPROCESS_CREDENTIAL_ENV in env

        # Sanity: the same credential still authorises its OWN task.
        allowed = _kanban_cli("comment", tid_a, "own task ok", env=env)
        assert allowed.returncode == 0, allowed.stdout

        # The exploit: replay the IDENTICAL credential against a DIFFERENT
        # task on the same board.
        result = _kanban_cli("comment", tid_b, "cross-task replay", env=env)
    finally:
        di.reset_for_tests()

    assert result.returncode == 1, result.stdout
    assert "delegate_task child contexts cannot mutate Kanban" in result.stdout
    with kbc.connect_closing() as conn:
        assert kb_mod.list_comments(conn, tid_b) == []


def test_subprocess_credential_cannot_validate_against_different_board(monkeypatch, tmp_path):
    """A subprocess credential minted while board 'default' (aliased here as
    the effectively-alpha board) is current must not validate when checked
    directly against a connection to a wholly different board 'beta', even
    though both boards live under the same HERMES_HOME. Regression for the
    cross-board validation-fallback exploit Maya independently reproduced
    against commit b40b5669b0 (BLOCK on t_6211a9dd, remediated by
    t_df2c4900): the multi-board probe in
    ``validate_subprocess_credential`` is now used ONLY when the caller
    passes no ``conn`` of its own; a caller that supplies its own board's
    ``conn`` (the normal ``write_txn`` case) never widens past it.
    """
    from agent import dispatcher_identity as di
    from hermes_cli import kanban_db_connect as kbc

    kb_mod, tid, workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    db_path = str(kb_mod.kanban_db_path())

    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
        conn.commit()
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]

    kb_mod.create_board("beta")
    beta_path = kb_mod.kanban_db_path("beta")

    di.reset_for_tests()
    try:
        _bind_current_process_as_worker(kb_mod, tid, int(run_id), workspace, db_path)
        token = di.mint_subprocess_credential()
        assert token

        # Sanity: the credential is genuinely live against its OWN (default) board.
        with kbc.connect_closing() as own_conn:
            assert di.validate_subprocess_credential(token, own_conn) is True

        # The credential row exists ONLY in the default board's DB file.
        with kbc.connect_closing(db_path=beta_path) as beta_conn:
            row = beta_conn.execute(
                "SELECT COUNT(*) AS n FROM worker_subprocess_credentials"
            ).fetchone()
            assert row["n"] == 0

            # The exploit: validate the SAME token directly against beta's
            # own connection -- must fail, not silently widen to a global probe.
            assert di.validate_subprocess_credential(token, beta_conn) is False
    finally:
        di.reset_for_tests()


def test_worker_subprocess_credential_cannot_authorise_admin_action_on_different_board(
    monkeypatch, tmp_path,
):
    """A worker subprocess credential minted while working on board 'default'
    must not authorise a board-ADMIN action (``boards rm``) targeting an
    unrelated board 'victim', via the real ``terminal`` -> ``hermes kanban
    boards rm <slug> --delete`` shell-out path.

    Regression for the cross-board admin-authority bypass found in a fresh
    independent review of commit d977234b54 (BLOCK on t_6211a9dd): the
    retained ``conn is None`` multi-board fallback in
    ``validate_subprocess_credential`` let a credential minted for a worker
    bound to a task on ANY board authorise ``remove_board``/
    ``set_current_board``/``clear_current_board``/``write_board_metadata``
    against ANY other board on the host, because those call sites invoked
    ``_assert_not_delegated_child_mutation()`` with neither ``conn`` nor a
    target board slug even though ``remove_board(slug)`` and friends always
    have one. The fix threads the exact target slug into the guard so the
    same worker-own-subprocess credential remains valid for its OWN board's
    admin actions (sanity-checked below) while failing closed against a
    different board's.
    """
    from agent import dispatcher_identity as di
    from hermes_cli import kanban_db_connect as kbc

    kb, tid, workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    db_path = str(kb.kanban_db_path())
    kb.create_board("victim")
    assert kb.board_exists("victim")

    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), tid))
        conn.commit()
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]

    di.reset_for_tests()
    try:
        _bind_current_process_as_worker(kb, tid, int(run_id), workspace, db_path)

        env = os.environ.copy()
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        env.pop("HERMES_KANBAN_TASK", None)
        env.pop("HERMES_KANBAN_RUN_ID", None)
        env.pop(di.HANDSHAKE_FD_ENV, None)
        env["PYTHONPATH"] = str(_REPO_ROOT)

        # The exact production call: LocalEnvironment._finalize_child_env is
        # what every terminal-tool subprocess spawn runs through -- this
        # worker legitimately gets a real, live subprocess credential.
        from tools.environments.local import _finalize_child_env
        env = _finalize_child_env(env)
        assert di.SUBPROCESS_CREDENTIAL_ENV in env

        # The exploit: the exact repro from the card -- "hermes kanban boards
        # rm <slug> --delete" against an UNRELATED board using this worker's
        # own-task credential.
        result = _kanban_cli("boards", "rm", "victim", "--delete", env=env)
    finally:
        di.reset_for_tests()

    assert result.returncode == 1, result.stdout
    assert "delegate_task child contexts cannot mutate Kanban" in result.stdout
    assert kb.board_exists("victim"), "cross-board admin action must fail closed and leave the board intact"


def test_delegate_child_kanban_cli_cannot_delete_parent_board(
    monkeypatch,
    tmp_path,
):
    kb, _tid, _workspace, _attachments_root = _make_running_kanban_task(
        monkeypatch,
        tmp_path,
    )
    kb.create_board("victim")
    assert kb.board_exists("victim")

    from agent.delegation_context import delegated_child_context
    from tools.environments.local import LocalEnvironment

    code = (
        "from hermes_cli import kanban; "
        "import argparse; "
        "p=argparse.ArgumentParser(); "
        "sub=p.add_subparsers(dest='cmd'); "
        "kanban.build_parser(sub); "
        "args=p.parse_args(['kanban','boards','rm','victim','--delete']); "
        "raise SystemExit(kanban.kanban_command(args))"
    )
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        with delegated_child_context():
            result = env.execute(
                _python_with_repo_path(code),
                timeout=15,
            )
    finally:
        env.cleanup()

    assert result["returncode"] == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks" in result["output"]
    assert kb.board_exists("victim")
    assert kb.board_dir("victim").is_dir()


def test_delegate_child_attach_url_guard_leaves_no_row_or_file(monkeypatch, tmp_path):
    kb, tid, _workspace, attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    from hermes_cli import kanban_db_connect as kbc

    from agent.delegation_context import delegated_child_context
    from tools import kanban_tools

    def forbidden_download(*_args, **_kwargs):
        raise AssertionError("delegated child guard must run before URL download")

    monkeypatch.setattr(kanban_tools, "_download_url_with_cap", forbidden_download)

    with delegated_child_context():
        raw = kanban_tools._handle_attach_url({
            "task_id": tid,
            "url": "https://example.com/leak.txt",
        })

    payload = json.loads(raw)
    assert payload["error"]
    assert "delegate_task child" in payload["error"]

    conn = kbc.connect()
    try:
        assert kb.list_attachments(conn, tid) == []
    finally:
        conn.close()
    task_dir = attachments_root / tid
    assert not task_dir.exists() or list(task_dir.iterdir()) == []


def test_child_attempting_default_complete_does_not_finish_parent_or_delete_workspace(
    monkeypatch,
    tmp_path,
):
    """Deterministic E2E: a delegated child cannot complete its parent task."""
    kb, tid, workspace, _attachments_root = _make_running_kanban_task(monkeypatch, tmp_path)
    from hermes_cli import kanban_db_connect as kbc
    from tools import delegate_tool
    from tools import kanban_tools

    class Parent:
        _current_task_id = tid

        def _touch_activity(self, _desc):
            return None

    class Child:
        tool_progress_callback = None
        _delegate_saved_tool_names = []
        _credential_pool = None
        _subagent_id = "sa-test"
        _delegate_depth = 1
        _parent_subagent_id = None
        model = "test-model"
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_estimated_cost_usd = 0.0
        session_reasoning_tokens = 0

        def get_activity_summary(self):
            return {"api_call_count": 0, "max_iterations": 1, "current_tool": None}

        def run_conversation(self, user_message, task_id, **_kwargs):
            attempted = kanban_tools._handle_complete({"summary": "wrong child completion"})
            return {
                "final_response": attempted,
                "completed": True,
                "api_calls": 0,
                "messages": [],
            }

        def close(self):
            return None

    result = delegate_tool._run_single_child(0, "try to complete parent", Child(), Parent())

    conn = kbc.connect()
    try:
        task = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()

    assert result["status"] == "completed"
    assert "delegate_task child" in result["summary"]
    assert task.status == "running"
    assert run.status == "running"
    assert workspace.is_dir()
