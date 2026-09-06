from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.runtime_capability_inventory import compare, inventory


def test_candidate_inventory_reports_exact_dispatch_and_caps() -> None:
    repo = Path(__file__).resolve().parents[2]
    result = inventory(repo)
    assert result["capabilities"] == {
        "exact_task_dispatch": True,
        "exact_dispatch_concurrency_caps": True,
    }
    assert result["runtime_commit"]
    assert result["runtime_tree"]
    assert set(result["evidence_files"]) == {
        "hermes_cli/kanban_parser.py",
        "hermes_cli/kanban_ops.py",
        "hermes_cli/kanban_db_dispatch.py",
    }


def test_compare_blocks_loss_and_missing_required() -> None:
    before = {"schema_version": 1, "runtime_commit": "before", "capabilities": {"exact_task_dispatch": True, "exact_dispatch_concurrency_caps": True}}
    after = {"schema_version": 1, "runtime_commit": "after", "capabilities": {"exact_task_dispatch": False, "exact_dispatch_concurrency_caps": True}}
    result = compare(before, after)
    assert result["verdict"] == "BLOCK"
    assert result["lost_capabilities"] == ["exact_task_dispatch"]
    assert result["missing_required_capabilities"] == ["exact_task_dispatch"]


def test_compare_allows_known_absence_to_be_restored() -> None:
    before = {"schema_version": 1, "runtime_commit": "before", "capabilities": {"exact_task_dispatch": False, "exact_dispatch_concurrency_caps": False}}
    after = {"schema_version": 1, "runtime_commit": "after", "capabilities": {"exact_task_dispatch": True, "exact_dispatch_concurrency_caps": True}}
    assert compare(before, after)["verdict"] == "PASS"


def test_cli_snapshot_is_canonical_and_read_only(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    before = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"], text=True)
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts/runtime_capability_inventory.py"), "snapshot", "--repo", str(repo)],
        check=True,
        text=True,
        capture_output=True,
    )
    parsed = json.loads(proc.stdout)
    assert proc.stdout == json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n"
    after = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"], text=True)
    assert after == before
