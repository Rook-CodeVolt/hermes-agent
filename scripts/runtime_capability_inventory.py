#!/usr/bin/env python3
"""Emit and compare a closed inventory of admitted local runtime capabilities.

This is intentionally source-based and side-effect free: it parses committed Python
without importing Hermes, opening a board, recomputing readiness, or spawning work.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REQUIRED_CAPABILITIES = ("exact_task_dispatch", "exact_dispatch_concurrency_caps")


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return next((node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name), None)


def inventory(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    parser_path = repo / "hermes_cli/kanban_parser.py"
    ops_path = repo / "hermes_cli/kanban_ops.py"
    dispatch_path = repo / "hermes_cli/kanban_db_dispatch.py"
    parser_tree, ops_tree, dispatch_tree = map(_tree, (parser_path, ops_path, dispatch_path))
    parser_constants = {node.value for node in ast.walk(parser_tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    ops_symbols = {node.id for node in ast.walk(ops_tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(ops_tree) if isinstance(node, ast.Attribute)
    }
    dispatch_fn = _function(dispatch_tree, "dispatch_exact")
    dispatch_names = {node.id for node in ast.walk(dispatch_fn) if isinstance(node, ast.Name)} if dispatch_fn else set()
    dispatch_keywords = {node.arg for node in ast.walk(dispatch_fn) if isinstance(node, ast.keyword)} if dispatch_fn else set()
    dispatch_args = {arg.arg for arg in dispatch_fn.args.args + dispatch_fn.args.kwonlyargs} if dispatch_fn else set()
    exact = "--task-id" in parser_constants and "dispatch_exact" in ops_symbols and dispatch_fn is not None
    caps = exact and {"max_spawn", "max_in_progress", "max_in_progress_per_profile"}.issubset(dispatch_args) and {
        "_tick_spawn_budget", "_dispatch_lane_task"
    }.issubset(dispatch_names) and "per_profile_cap" in dispatch_keywords
    files = {}
    for path in (parser_path, ops_path, dispatch_path):
        raw = path.read_bytes()
        files[path.relative_to(repo).as_posix()] = {"sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw)}
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime_commit": _git(repo, "rev-parse", "HEAD"),
        "runtime_tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "capabilities": {
            "exact_task_dispatch": bool(exact),
            "exact_dispatch_concurrency_caps": bool(caps),
        },
        "evidence_files": files,
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("schema_version") != SCHEMA_VERSION or after.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported inventory schema")
    before_caps, after_caps = before.get("capabilities"), after.get("capabilities")
    if not isinstance(before_caps, dict) or not isinstance(after_caps, dict):
        raise ValueError("missing capability map")
    lost = sorted(name for name, value in before_caps.items() if value is True and after_caps.get(name) is not True)
    missing_required = sorted(name for name in REQUIRED_CAPABILITIES if after_caps.get(name) is not True)
    verdict = "PASS" if not lost and not missing_required else "BLOCK"
    return {
        "schema_version": SCHEMA_VERSION,
        "before_commit": before.get("runtime_commit"),
        "after_commit": after.get("runtime_commit"),
        "lost_capabilities": lost,
        "missing_required_capabilities": missing_required,
        "verdict": verdict,
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("inventory must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--repo", type=Path, required=True)
    check = sub.add_parser("compare")
    check.add_argument("--before", type=Path, required=True)
    check.add_argument("--after", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = inventory(args.repo) if args.command == "snapshot" else compare(_load(args.before), _load(args.after))
    except (OSError, subprocess.CalledProcessError, SyntaxError, ValueError) as exc:
        print(_canonical({"schema_version": SCHEMA_VERSION, "verdict": "BLOCK", "error": type(exc).__name__}).decode(), end="", file=sys.stderr)
        return 2
    print(_canonical(result).decode(), end="")
    return 0 if result.get("verdict", "PASS") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
