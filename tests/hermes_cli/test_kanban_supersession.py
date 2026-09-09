from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


ROOT = Path(__file__).parents[2]


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _snapshot(conn, task_ids: tuple[str, ...]) -> dict:
    marks = ",".join("?" for _ in task_ids)
    return {
        "tasks": [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({marks}) ORDER BY id", task_ids
            )
        ],
        "links": [
            tuple(row)
            for row in conn.execute(
                "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
            )
        ],
        "events": [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM task_events WHERE task_id IN ({marks}) ORDER BY id", task_ids
            )
        ],
        "comments": [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM task_comments WHERE task_id IN ({marks}) ORDER BY id", task_ids
            )
        ],
        "runs": [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM task_runs WHERE task_id IN ({marks}) ORDER BY id", task_ids
            )
        ],
        "attachments": [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM task_attachments WHERE task_id IN ({marks}) ORDER BY id", task_ids
            )
        ],
    }


def _graph(conn) -> list[tuple[str, str]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        )
    ]


def test_supersede_preserves_evidence_and_unrelated_security_parents(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        old = kb.create_task(conn, title="old", assignee="worker", initial_status="blocked")
        replacement = kb.create_task(conn, title="replacement", assignee="worker", initial_status="blocked")
        security = kb.create_task(conn, title="security gate", assignee="maya", initial_status="blocked")
        child_a = kb.create_task(conn, title="child a", assignee="worker", parents=(old, security))
        child_b = kb.create_task(conn, title="child b", assignee="worker", parents=(old, replacement))
        kb.add_comment(conn, old, "rook", "preserved context")
        kb.add_attachment(
            conn,
            old,
            filename="evidence.txt",
            stored_path=str(kanban_home / "evidence.txt"),
            size=8,
            uploaded_by="rook",
        )
        before_old = _snapshot(conn, (old,))["tasks"][0]
        before_comments = _snapshot(conn, (old,))["comments"]
        before_attachments = _snapshot(conn, (old,))["attachments"]

        receipt = kb.supersede_task(conn, old, replacement, actor="rook")

        assert receipt == {
            "old_task_id": old,
            "replacement_task_id": replacement,
            "children": sorted((child_a, child_b)),
            "actor": "rook",
        }
        assert kb.child_ids(conn, old) == []
        assert kb.parent_ids(conn, child_a) == sorted((replacement, security))
        assert kb.parent_ids(conn, child_b) == [replacement]
        assert _snapshot(conn, (old,))["tasks"][0] == before_old
        assert _snapshot(conn, (old,))["comments"] == before_comments
        assert _snapshot(conn, (old,))["attachments"] == before_attachments
        assert [event.kind for event in kb.list_events(conn, old)][-1] == "superseded"
        assert kb.get_task(conn, child_a).status == "todo"
        assert kb.get_task(conn, child_b).status == "todo"


@pytest.mark.parametrize("replacement_status", ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"])
def test_replacement_lifecycle_is_respected(
    kanban_home: Path, replacement_status: str
) -> None:
    with kbc.connect() as conn:
        old = kb.create_task(conn, title="old", initial_status="blocked")
        replacement = kb.create_task(conn, title="replacement", initial_status="blocked")
        child = kb.create_task(conn, title="child", assignee="worker", parents=(old,))
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (replacement_status, replacement))
        conn.commit()

        kb.supersede_task(conn, old, replacement, actor="rook")

        expected = "ready" if replacement_status == "done" else "todo"
        assert kb.get_task(conn, child).status == expected
        assert kb.parent_ids(conn, child) == [replacement]


@pytest.mark.parametrize(
    ("child_status", "expected"),
    [
        ("triage", "triage"),
        ("todo", "todo"),
        ("scheduled", "scheduled"),
        ("ready", "todo"),
        ("review", "todo"),
        ("blocked", "blocked"),
        ("done", "done"),
        ("archived", "archived"),
    ],
)
def test_child_lifecycle_is_reconciled_without_overwriting_terminal_or_sticky_state(
    kanban_home: Path, child_status: str, expected: str
) -> None:
    with kbc.connect() as conn:
        old = kb.create_task(conn, title="old", initial_status="blocked")
        replacement = kb.create_task(conn, title="replacement", initial_status="blocked")
        child = kb.create_task(conn, title="child", assignee="worker", parents=(old,))
        if child_status == "blocked":
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
            conn.commit()
            assert kb.block_task(conn, child, reason="security gate")
        else:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (child_status, child))
            conn.commit()

        kb.supersede_task(conn, old, replacement, actor="rook")

        assert kb.get_task(conn, child).status == expected
        assert kb.parent_ids(conn, child) == [replacement]


def test_wrong_identities_cycles_and_active_children_fail_with_full_rollback(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        old = kb.create_task(conn, title="old", initial_status="blocked")
        replacement = kb.create_task(conn, title="replacement", initial_status="blocked")
        child = kb.create_task(conn, title="child", assignee="worker", parents=(old,))
        claim_mirror = kb.create_task(
            conn, title="historical claim", created_by="work-claims", initial_status="blocked"
        )
        kb.link_tasks(conn, child, replacement)
        ids = (old, replacement, child, claim_mirror)

        cases = [
            (ValueError, "itself", lambda: kb.supersede_task(conn, old, old, actor="rook")),
            (ValueError, "actor is required", lambda: kb.supersede_task(conn, old, replacement, actor="  ")),
            (ValueError, "unknown task", lambda: kb.supersede_task(conn, "t_missing", replacement, actor="rook")),
            (ValueError, "claim record", lambda: kb.supersede_task(conn, claim_mirror, replacement, actor="rook")),
            (ValueError, "claim record", lambda: kb.supersede_task(conn, old, claim_mirror, actor="rook")),
            (ValueError, "cycle", lambda: kb.supersede_task(conn, old, replacement, actor="rook")),
        ]
        for error, message, operation in cases:
            before = _snapshot(conn, ids)
            with pytest.raises(error, match=message):
                operation()
            assert _snapshot(conn, ids) == before

        conn.execute("DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (child, replacement))
        conn.execute("UPDATE tasks SET status='running', claim_lock='active', claim_expires=? WHERE id=?", (int(time.time()) + 300, child))
        conn.commit()
        before = _snapshot(conn, ids)
        with pytest.raises(RuntimeError, match="active child"):
            kb.supersede_task(conn, old, replacement, actor="rook")
        assert _snapshot(conn, ids) == before

        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (child,))
        conn.commit()
        before = _snapshot(conn, ids)
        with pytest.raises(RuntimeError, match="active child"):
            kb.supersede_task(conn, old, replacement, actor="rook")
        assert _snapshot(conn, ids) == before


def test_event_and_readiness_failures_roll_back_every_change(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with kbc.connect() as conn:
        old = kb.create_task(conn, title="old", initial_status="blocked")
        replacement = kb.create_task(conn, title="replacement", initial_status="blocked")
        child = kb.create_task(conn, title="child", assignee="worker", parents=(old,))
        ids = (old, replacement, child)
        original_append = kb._append_event

        def fail_second_event(connection, task_id, kind, payload=None, **kwargs):
            if kind == "supersession_applied":
                raise RuntimeError("injected event failure")
            return original_append(connection, task_id, kind, payload, **kwargs)

        before = _snapshot(conn, ids)
        monkeypatch.setattr(kb, "_append_event", fail_second_event)
        with pytest.raises(RuntimeError, match="injected event failure"):
            kb.supersede_task(conn, old, replacement, actor="rook")
        assert _snapshot(conn, ids) == before
        monkeypatch.setattr(kb, "_append_event", original_append)

        before = _snapshot(conn, ids)
        monkeypatch.setattr(
            kb,
            "_reconcile_superseded_children",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected readiness failure")),
        )
        with pytest.raises(RuntimeError, match="injected readiness failure"):
            kb.supersede_task(conn, old, replacement, actor="rook")
        assert _snapshot(conn, ids) == before


def test_concurrent_claim_waits_for_atomic_supersession_and_fails_closed(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with kbc.connect() as setup:
        old = kb.create_task(setup, title="old", initial_status="blocked")
        replacement = kb.create_task(setup, title="replacement", initial_status="blocked")
        child = kb.create_task(setup, title="child", assignee="worker", parents=(old,))
        setup.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        setup.commit()

    entered = threading.Event()
    release = threading.Event()
    original = kb._reconcile_superseded_children

    def paused(connection, child_ids):
        entered.set()
        assert release.wait(5)
        return original(connection, child_ids)

    monkeypatch.setattr(kb, "_reconcile_superseded_children", paused)
    outcomes: dict[str, object] = {}

    def supersede_worker() -> None:
        with kbc.connect() as conn:
            outcomes["supersede"] = kb.supersede_task(conn, old, replacement, actor="rook")

    def claim_worker() -> None:
        with kbc.connect() as conn:
            outcomes["claim"] = kb.claim_task(conn, child, claimer="concurrent")

    supersede_thread = threading.Thread(target=supersede_worker)
    supersede_thread.start()
    assert entered.wait(5)
    claim_thread = threading.Thread(target=claim_worker)
    claim_thread.start()
    time.sleep(0.1)
    assert claim_thread.is_alive(), "claim must not observe or acquire the in-flight intermediate graph"
    release.set()
    supersede_thread.join(5)
    claim_thread.join(5)
    assert not supersede_thread.is_alive() and not claim_thread.is_alive()
    assert outcomes["claim"] is None
    with kbc.connect() as conn:
        assert kb.parent_ids(conn, child) == [replacement]
        assert kb.get_task(conn, child).status == "todo"
        assert kb.get_task(conn, child).claim_lock is None


def _run_cli(home: Path, *args: str, delegated: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    env["HERMES_PROFILE"] = "rook"
    env["PYTHONPATH"] = str(ROOT)
    for key in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT"):
        env.pop(key, None)
    if delegated:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_supported_cli_end_to_end_receipt_and_delegated_denial(tmp_path: Path) -> None:
    home = tmp_path / "cli-home"
    home.mkdir()

    def create(title: str, *extra: str) -> str:
        result = _run_cli(home, "kanban", "create", title, *extra, "--json")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["id"]

    old = create("old", "--initial-status", "blocked")
    replacement = create("replacement", "--initial-status", "blocked")
    child = create("child", "--assignee", "worker", "--parent", old)

    refused = _run_cli(home, "kanban", "supersede", old, replacement, delegated=True)
    assert refused.returncode == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in refused.stderr

    linked = _run_cli(home, "kanban", "link", child, replacement)
    assert linked.returncode == 0, linked.stderr
    cycle = _run_cli(home, "kanban", "supersede", old, replacement, "--json")
    assert cycle.returncode == 1
    assert "would create a cycle" in cycle.stderr
    unchanged = _run_cli(home, "kanban", "show", child, "--json")
    assert unchanged.returncode == 0, unchanged.stderr
    unchanged_payload = json.loads(unchanged.stdout)
    assert unchanged_payload["parents"] == [old]
    unlinked = _run_cli(home, "kanban", "unlink", child, replacement)
    assert unlinked.returncode == 0, unlinked.stderr

    result = _run_cli(home, "kanban", "supersede", old, replacement, "--json")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == {
        "actor": "rook",
        "children": [child],
        "old_task_id": old,
        "replacement_task_id": replacement,
    }

    shown = _run_cli(home, "kanban", "show", child, "--json")
    assert shown.returncode == 0, shown.stderr
    payload = json.loads(shown.stdout)
    assert payload["parents"] == [replacement]
    assert payload["task"]["status"] == "todo"
    assert any(event["kind"] == "dependency_superseded" for event in payload["events"])
