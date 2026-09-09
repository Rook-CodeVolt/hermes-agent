"""Regression tests for dependency-safe completion semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _ready(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))


def test_negative_review_outcome_does_not_release_child(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="review", assignee="maya")
        child = kb.create_task(conn, title="publish", assignee="oliver", parents=(parent,))
        assert kb.complete_task(conn, parent, summary="BLOCK: security defects remain")

        reviewed = kb.get_task(conn, parent)
        assert reviewed.status == "done"
        assert reviewed.completion_outcome == "block"
        assert kb.get_task(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 0


def test_changes_required_prefix_and_explicit_pass_are_typed(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        negative = kb.create_task(conn, title="negative", assignee="maya")
        assert kb.complete_task(conn, negative, summary="CHANGES_REQUIRED: fix race")
        assert kb.get_task(conn, negative).completion_outcome == "changes_required"

        accepted = kb.create_task(conn, title="accepted", assignee="maya")
        assert kb.complete_task(
            conn, accepted, summary="BLOCK was investigated and resolved", completion_outcome="pass"
        )
        assert kb.get_task(conn, accepted).completion_outcome == "pass"


def test_archived_parent_is_not_dependency_acceptance(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="abandoned", initial_status="blocked")
        child = kb.create_task(conn, title="must wait", assignee="worker", parents=(parent,))
        assert kb.archive_task(conn, parent)
        assert kb.get_task(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 0


def test_legacy_null_done_parent_remains_accepted(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="legacy", initial_status="blocked")
        child = kb.create_task(conn, title="child", assignee="worker", parents=(parent,))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completion_outcome=NULL WHERE id=?", (parent,)
            )
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, child).status == "ready"


def test_invalid_dependency_block_is_sticky_and_never_respawns(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="browser impossible", assignee="oliver")
        claimed = kb.claim_task(conn, task_id, claimer="oliver")
        assert claimed is not None
        assert kb.block_task(
            conn, task_id, reason="claimed a missing dependency", kind="dependency"
        )
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind == "dependency"
        assert kb.recompute_ready(conn) == 0
        event = [e for e in kb.list_events(conn, task_id) if e.kind == "invalid_dependency_block"][-1]
        assert event.payload["invalid_dependency_wait"] is True


def test_real_dependency_wait_releases_only_after_accepted_parent(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        _ready(conn, child)
        assert kb.block_task(conn, child, reason="waiting", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        assert kb.complete_task(conn, parent, summary="completed", completion_outcome="pass")
        assert kb.get_task(conn, child).status == "ready"


def test_completed_event_records_typed_outcome(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="review", assignee="maya")
        assert kb.complete_task(conn, task_id, summary="Rejected", completion_outcome="block")
        completed = [e for e in kb.list_events(conn, task_id) if e.kind == "completed"][-1]
        assert completed.payload["completion_outcome"] == "block"


def test_schema_migration_types_legacy_negative_prose_before_dependency_release(
    kanban_home: Path,
) -> None:
    with kbc.connect_closing() as conn:
        rejected = kb.create_task(conn, title="legacy rejected review", assignee="maya")
        accepted = kb.create_task(conn, title="legacy accepted work", assignee="daniel")
        ordinary = kb.create_task(conn, title="not a false positive", assignee="daniel")
        child = kb.create_task(conn, title="must remain gated", parents=(rejected,))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completion_outcome=NULL, result=? WHERE id=?",
                ("BLOCK: privacy defects remain", rejected),
            )
            conn.execute(
                "UPDATE tasks SET status='done', completion_outcome=NULL, result=? WHERE id=?",
                ("Delivered and verified", accepted),
            )
            conn.execute(
                "UPDATE tasks SET status='done', completion_outcome=NULL, result=? WHERE id=?",
                ("Failures were investigated and resolved", ordinary),
            )

    kbc.init_db()

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, rejected).completion_outcome == "block"
        assert kb.get_task(conn, accepted).completion_outcome == "completed"
        assert kb.get_task(conn, ordinary).completion_outcome == "completed"
        assert kb.get_task(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 0
