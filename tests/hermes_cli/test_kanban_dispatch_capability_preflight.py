"""Dispatcher capability preflight must fail before claim/spawn."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _profile(home: Path, name: str, *, skills: tuple[str, ...] = (), disabled=()) -> None:
    profile = home / "profiles" / name
    profile.mkdir(parents=True)
    disabled_yaml = ", ".join(disabled)
    profile.joinpath("config.yaml").write_text(
        f"agent:\n  disabled_toolsets: [{disabled_yaml}]\n", encoding="utf-8"
    )
    for skill in skills:
        skill_dir = profile / "skills" / "testing" / skill
        skill_dir.mkdir(parents=True)
        skill_dir.joinpath("SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: Test skill.\n---\n\n# Test\n",
            encoding="utf-8",
        )


def _dispatch(home: Path, skills: list[str]):
    spawned = []

    def spawn(task, workspace):
        spawned.append(task.id)
        return 4242

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="capability preflight", assignee="alpha", skills=skills
        )
        result = kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        return result, kb.get_task(conn, task_id), spawned, kb.list_events(conn, task_id)


def test_available_forced_skill_spawns(kanban_home: Path) -> None:
    _profile(kanban_home, "alpha", skills=("security-review",))
    result, task, spawned, _events = _dispatch(kanban_home, ["security-review"])
    assert spawned == [task.id]
    assert task.status == "running"
    assert result.capability_blocked == []


def test_missing_forced_skill_blocks_before_claim(kanban_home: Path) -> None:
    _profile(kanban_home, "alpha")
    result, task, spawned, events = _dispatch(kanban_home, ["missing-skill"])
    assert spawned == []
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert result.capability_blocked == [(task.id, "alpha", ["missing-skill"])]
    reason = json.loads([e for e in events if e.kind == "blocked"][-1].payload["reason"])
    assert reason["code"] == "kanban_forced_skill_unavailable"


def test_skill_matching_disabled_toolset_blocks_before_claim(kanban_home: Path) -> None:
    _profile(
        kanban_home,
        "alpha",
        skills=("computer-use",),
        disabled=("computer_use",),
    )
    result, task, spawned, _events = _dispatch(kanban_home, ["computer-use"])
    assert spawned == []
    assert task.status == "blocked"
    assert task.current_run_id is None
    assert result.capability_blocked == [(task.id, "alpha", ["computer-use"])]
