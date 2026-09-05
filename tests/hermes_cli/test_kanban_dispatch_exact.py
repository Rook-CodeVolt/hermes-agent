"""Exact named-task kanban dispatch contracts."""

from __future__ import annotations

import argparse
import contextlib
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize(
    ("max_spawn", "expected_spawned"),
    [(1, False), (2, True)],
)
def test_dispatch_task_id_dry_run_honors_live_cap_and_names_target(
    kanban_home, all_assignees_spawnable, max_spawn, expected_spawned,
):
    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="alice", priority=100)
        assert kb.claim_task(conn, first) is not None
        target = kb.create_task(conn, title="target", assignee="bob")

    output = kc.run_slash(
        f"dispatch --task-id {target} --dry-run --max {max_spawn} --json"
    )

    parsed = json.loads(output)
    expected = (
        [{"task_id": target, "assignee": "bob", "workspace": ""}]
        if expected_spawned
        else []
    )
    assert parsed["spawned"] == expected
    with kbc.connect_closing() as conn:
        first_task = kb.get_task(conn, first)
        target_task = kb.get_task(conn, target)
        assert first_task is not None and first_task.status == "running"
        assert target_task is not None and target_task.status == "ready"


def test_dispatch_exact_claims_only_named_task(
    kanban_home, all_assignees_spawnable,
):
    spawned: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 42

    with kbc.connect_closing() as conn:
        other = kb.create_task(conn, title="other", assignee="alice", priority=100)
        target = kb.create_task(conn, title="target", assignee="bob")
        result = kbd.dispatch_exact(conn, target, spawn_fn=fake_spawn)
        claimed = kb.get_task(conn, target)
        untouched = kb.get_task(conn, other)

    assert spawned == [target]
    assert [item[0] for item in result.spawned] == [target]
    assert claimed is not None and claimed.status == "running"
    assert untouched is not None and untouched.status == "ready"


def test_dispatch_exact_cas_allows_one_concurrent_winner(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    with kbc.connect_closing() as conn:
        target = kb.create_task(conn, title="race", assignee="alice")

    barrier = threading.Barrier(2)
    original_claim = kb.claim_task

    @contextlib.contextmanager
    def concurrent_tick_lock(_path):
        yield True

    def racing_claim(conn, task_id, **kwargs):
        barrier.wait(timeout=5)
        return original_claim(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_task", racing_claim)
    monkeypatch.setattr(kbc, "_dispatch_tick_lock", concurrent_tick_lock)
    results = []
    failures: list[BaseException] = []

    def worker():
        try:
            with kbc.connect_closing() as conn:
                results.append(kbd.dispatch_exact(conn, target, spawn_fn=lambda *_args, **_kwargs: 42))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert sum(len(result.spawned) for result in results) == 1
    with kbc.connect_closing() as conn:
        runs = kb.list_runs(conn, target)
    assert len(runs) == 1


def test_dispatch_parser_accepts_task_id():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    args = parser.parse_args(["kanban", "dispatch", "--task-id", "t_exact", "--dry-run"])

    assert args.task_id == "t_exact"
