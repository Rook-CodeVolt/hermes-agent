"""Run the candidate guard against synthetic evidence for the launchd canary."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    source = Path(sys.argv[1]).resolve()
    root = Path(sys.argv[2]).resolve()
    spec = importlib.util.spec_from_file_location("canary_guard", source)
    if spec is None or spec.loader is None:
        return 2
    guard: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    guard.STATE_DIR = root
    guard.STATE_FILE = root / "state.json"
    guard.LOCK_FILE = root / "guard.lock"
    guard.LOG_FILE = root / "guard.log"

    def board_snapshot(board: str) -> dict:
        task = {
            "id": f"t_canary_{guard.BOARDS.index(board)}",
            "title": "synthetic completed canary task",
            "status": "done",
            "assignee": None,
            "tenant": guard.BOARD_TENANTS[board],
        }
        return {
            "board": board,
            "db_path": str(root / f"{board}.db"),
            "tenant": guard.BOARD_TENANTS[board],
            "tasks": [task],
            "source_total": 1,
            "archived_count": 0,
            "out_of_tenant_count": 0,
        }

    guard.fetch_board_snapshot = board_snapshot
    guard.fetch_task_runs = lambda board, task_id: []
    guard.fetch_task_links = lambda board, task_id: {"parents": [], "children": []}
    guard.fetch_work_claims = lambda: {
        "available": True,
        "claims": [],
        "has_claim_targets": True,
        "has_execution_leases": True,
    }
    guard.fetch_profiles = dict

    def unexpected_alert(message: str) -> bool:
        raise RuntimeError("synthetic complete state unexpectedly attempted an alert")

    guard.send_alert = unexpected_alert
    return guard.check_once()


if __name__ == "__main__":
    raise SystemExit(main())
