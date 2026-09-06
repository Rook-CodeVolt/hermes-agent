"""Delegated-child lineage must not persist in the shared terminal snapshot."""
from __future__ import annotations

import os
import json
import shlex
import sys
import threading
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _python_command(*args: str) -> str:
    env_prefix = f"PYTHONPATH={shlex.quote(str(_REPO_ROOT))}"
    argv = " ".join(shlex.quote(arg) for arg in (sys.executable, *args))
    return f"{env_prefix} {argv}"


def _identity_command(label: str) -> str:
    return (
        f'printf "{label}=%s|task=%s|run=%s|db=%s|workspace=%s|lock=%s\\n" '
        '"${HERMES_DELEGATED_CHILD_CONTEXT:-clean}" '
        '"${HERMES_KANBAN_TASK:-clean}" "${HERMES_KANBAN_RUN_ID:-clean}" '
        '"${HERMES_KANBAN_DB:-clean}" "${HERMES_KANBAN_WORKSPACE:-clean}" '
        '"${HERMES_KANBAN_CLAIM_LOCK:-clean}"'
    )


def _assert_child_identity(result: dict, label: str) -> None:
    assert result["returncode"] == 0
    assert f"{label}=1|task=clean|run=clean|db=clean|workspace=clean|lock=clean" in result["output"]


@pytest.fixture
def isolated_dispatcher_env(monkeypatch, tmp_path):
    assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") is None
    home = tmp_path / "hermes"
    home.mkdir()
    workspace = tmp_path / "dispatcher-workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "run-parent")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "parent-lock")

    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        yield env, home
    finally:
        env.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_child_restrictions_repeat_and_same_parent_recovers(isolated_dispatcher_env):
    """Clean parent -> real child commands -> same clean parent, against only a temp board."""
    env, home = isolated_dispatcher_env

    before = env.execute(_identity_command("before"))
    assert before["returncode"] == 0
    assert "before=clean|task=t_parent|run=run-parent" in before["output"]

    from agent.delegation_context import delegated_child_context
    from tools import kanban_tools

    with delegated_child_context():
        first_child = env.execute(_identity_command("child-one"))
        second_child = env.execute(_identity_command("child-two"))
        cli_denial = env.execute(
            _python_command("-m", "hermes_cli.main", "kanban", "create", "must not exist", "--json")
        )
        tool_denial = json.loads(kanban_tools._handle_create({"title": "must not exist"}))
        db_denial = env.execute(
            _python_command(
                "-c",
                "from hermes_cli import kanban_db as kb; kb.create_board('must-not-exist')",
            )
        )

    after = env.execute(_identity_command("after"))
    parent_success = env.execute(
        _python_command(
            "-c",
            "from hermes_cli import kanban_db as kb; kb.create_board('parent-authorized')",
        )
    )

    _assert_child_identity(first_child, "child-one")
    _assert_child_identity(second_child, "child-two")
    assert cli_denial["returncode"] == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in cli_denial["output"]
    assert "kanban_create refused: delegate_task child agents are not Kanban run owners" in tool_denial["error"]
    assert db_denial["returncode"] == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks or boards" in db_denial["output"]
    assert after["returncode"] == 0
    assert "after=clean|task=t_parent|run=run-parent" in after["output"]
    assert parent_success["returncode"] == 0, parent_success["output"]

    from hermes_cli import kanban_db as kb

    assert not kb.board_exists("must-not-exist")
    assert kb.board_exists("parent-authorized")
    assert str(home) not in cli_denial["output"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_interleaved_parent_and_child_commands_do_not_transfer_lineage(isolated_dispatcher_env):
    """Concurrent commands sharing one cached LocalEnvironment retain caller identity."""
    env, _home = isolated_dispatcher_env
    barrier = threading.Barrier(3)
    results: dict[str, dict] = {}

    def run_parent() -> None:
        barrier.wait()
        results["parent"] = env.execute(f"sleep 0.1; {_identity_command('parent')}")

    def run_child() -> None:
        from agent.delegation_context import delegated_child_context

        with delegated_child_context():
            barrier.wait()
            results["child"] = env.execute(f"sleep 0.1; {_identity_command('child')}")

    parent = threading.Thread(target=run_parent)
    child = threading.Thread(target=run_child)
    parent.start()
    child.start()
    barrier.wait()
    parent.join(timeout=10)
    child.join(timeout=10)

    assert not parent.is_alive()
    assert not child.is_alive()
    assert results["parent"]["returncode"] == 0
    assert "parent=clean|task=t_parent|run=run-parent" in results["parent"]["output"]
    _assert_child_identity(results["child"], "child")

    restored = env.execute(_identity_command("restored"))
    assert restored["returncode"] == 0
    assert "restored=clean|task=t_parent|run=run-parent" in restored["output"]
