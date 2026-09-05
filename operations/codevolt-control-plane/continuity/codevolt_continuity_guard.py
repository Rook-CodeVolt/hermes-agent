#!/Users/rook/.hermes/hermes-agent/venv/bin/python
"""Observe CodeVolt continuity without becoming a second manager.

The launchd timer and the four kanban event hooks are two wake paths into one
deterministic, idempotent checker (``check_once``). Neither path triggers a
reasoning governor, a nested manager, or any second scheduler: the hook path
only re-spawns *this* script with ``--watchdog``.

The guard may observe, verify, dispatch exactly one already-ready card, or
alert Rook. It never creates tasks, edits dependencies, reinterprets a
verdict, or writes to any board/claims database.

Evidence sources (all supported, all read-only):

* Boards -- ``hermes_cli.kanban_db`` Python API, ``list_tasks(...,
  include_archived=True)``, so archived rows are never silently dropped and
  ``source_total`` is authoritative and reconciled against the classification.
* Runs -- ``hermes_cli.kanban_db.list_runs`` (worker pid + run heartbeat).
* Links -- ``hermes_cli.kanban_db.parent_ids``/``child_ids`` for literal
  predecessor/successor evidence.
* Standalone execution -- ``~/.hermes/work-claims.db`` opened ``mode=ro`` with
  ``PRAGMA query_only=ON``: ``claims`` joined to ``claim_targets`` and, when
  available, execution leases. A standalone execution needs a live PID plus
  fresh first-seen or advancing lease progress; it occupies collision keys but
  never invents an accountable owner.
* Capability -- profile-local ``~/.hermes/profiles/<name>/config.yaml`` and
  ``~/.hermes/profiles/<name>/skills/**/SKILL.md``.

Everything unknown fails closed: an unreadable board degrades the service, an
unreadable claims DB blocks dispatch, an unrecognised status or block kind is
malformed/protected rather than "probably fine". A ready card is successor
inventory -- it is never counted as a healthy live lane. Standby, stale and
unknown owners are never counted as accountable specialists. Runway cardinality
is unique accountable specialists; ``healthy_worker_count`` separately records
verified worker processes. Every active card emits minimized, actionable
immediate-successor evidence. Only titles carrying the literal
``[critical-path]`` marker require a staged immediate successor, making
critical-path membership explicit.

Dispatch is bound to one exact task through the supported contract
``hermes kanban --board <b> dispatch --task-id <id> --max 1 --json`` and the
returned ``spawned[0]["task_id"]`` must equal the selected task id; anything
else (empty, multiple, or a different -- e.g. higher-priority -- task) is a
fail-closed fault, never a success.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

# ---------------------------------------------------------------------------
# Paths / config. All plain module attributes so tests can monkeypatch them
# (mock.patch.object) without touching live state.
# ---------------------------------------------------------------------------
HOME = Path("/Users/rook")
HERMES = HOME / ".local" / "bin" / "hermes"
PYTHON = HOME / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
SCRIPT = HOME / ".hermes" / "scripts" / "codevolt_continuity_guard.py"

BOARDS: tuple[str, ...] = (
    "platform-command-centre",
    "codevolt-managed-delivery",
    "local-model-security-research",
)
BOARD_TENANTS: dict[str, str] = {board: "codevolt-production" for board in BOARDS}

OWNER = "Tom King"
MANAGER = "Rook"
STATE_DIR = HOME / ".hermes" / "state"
STATE_FILE = STATE_DIR / "codevolt_continuity_guard.json"
LOCK_FILE = STATE_DIR / "codevolt_continuity_guard.lock"
LOG_FILE = HOME / ".hermes" / "logs" / "codevolt-continuity-guard.log"
PROFILES_DIR = HOME / ".hermes" / "profiles"
WORK_CLAIMS_DB = HOME / ".hermes" / "work-claims.db"
SLACK_TARGET = "slack:D0BS6BZGQ3V"
COMMAND_TIMEOUT_SECONDS = 90
MAX_OUTPUT_BYTES = 4 * 1024 * 1024

SCHEMA_VERSION = 5

# The activation gate named by t_3efcd24d's own body: the guard must not begin
# dispatching until CV-A01 is literally accepted.
ACTIVATION_GATE_TASK = "t_13b90c53"

# Real kanban status vocabulary (hermes_cli.kanban_db.VALID_STATUSES). Anything
# outside it is malformed, not silently gated.
KNOWN_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
)
EXECUTING_STATUSES = ("running", "review")
READY_STATUS = "ready"
GATED_STATUSES = ("triage", "todo", "scheduled", "blocked")
HISTORICAL_STATUSES = ("done", "archived")

# A staged successor must still carry an actionable workflow responsibility.
# This explicit allowlist intentionally excludes terminal states and rejects
# missing/unknown status evidence rather than inferring that it is actionable.
ACTIONABLE_SUCCESSOR_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review"}
)

# Real typed block reasons (hermes_cli.kanban_db.VALID_BLOCK_KINDS).
# ``dependency``/``transient`` clear themselves; everything else -- including an
# absent or unrecognised kind -- needs a human, so it is a protected stop.
GATED_BLOCK_KINDS = frozenset({"dependency", "transient"})
PROTECTED_BLOCK_KINDS = frozenset({"needs_input", "capability"})

# Literal verdict tokens. Matched as whole words against the task ``result``
# column and the latest ``task_runs.summary`` -- no fuzzy inference.
VERDICT_BLOCK_TOKENS = ("PIVOT_REQUIRED", "BLOCKED", "BLOCK", "REJECTED", "REJECT")
VERDICT_PASS_TOKEN = "PASS"

BUCKET_EXECUTING = "executing_specialists"
BUCKET_READY = "ready"
BUCKET_GATED = "dependency_gated"
BUCKET_PROTECTED = "protected_stops"
BUCKET_MALFORMED = "malformed"
BUCKET_HISTORICAL = "historical"
BUCKETS = (
    BUCKET_EXECUTING,
    BUCKET_READY,
    BUCKET_GATED,
    BUCKET_PROTECTED,
    BUCKET_MALFORMED,
    BUCKET_HISTORICAL,
)

HEARTBEAT_FRESH_SECONDS = 180
CLAIM_FRESH_SECONDS = 180
CLOCK_SKEW_SECONDS = 5
RUNWAY_MIN_LANES = 2
RUNWAY_MAX_LANES = 3
FAILURE_LIMIT = 3
MAX_PENDING_ALERTS = 16
CRITICAL_PATH_MARKER = "[critical-path]"

# A task that declares a workspace needs a profile toolset that can actually
# reach it. These are real toolset names from profile config.yaml.
WORKSPACE_TOOLSETS = frozenset({"terminal", "code_execution", "file"})

# Decisions the liveness service is allowed to report as success. Anything
# else -- degraded, unknown, faulted -- exits non-zero.
SUCCESS_DECISIONS = frozenset(
    {"dispatched-ready", "runway-at-target", "healthy-active", "holding", "complete", "manager-attention-required"}
)

# Stable contract for the Command Centre consumer of this state file.
CONSUMER_CONTRACT = {
    "schema_version": SCHEMA_VERSION,
    "produced_by": "codevolt_continuity_guard",
    "fields": [
        "board_evidence",
        "reconciled",
        "classification",
        "classification_totals",
        "source_totals",
        "executing_validation",
        "healthy_worker_count",
        "healthy_lane_count",
        "accountable_specialists",
        "ownerless_reviews",
        "standalone_claims",
        "immediate_successors",
        "missing_immediate_successors",
        "ready_preflight",
        "dispatch_gate",
        "dispatch_blockers",
        "last_dispatch",
        "status",
        "checked_at",
    ],
}


# ---------------------------------------------------------------------------
# Process / subprocess plumbing
# ---------------------------------------------------------------------------
def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("HERMES_PROFILE", None)
    env.pop("HERMES_PROFILE_DIR", None)
    env["HOME"] = str(HOME)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/Users/rook/.local/bin"
    return env


def run(argv: list[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=clean_env(),
            cwd=HOME,
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return subprocess.CompletedProcess(argv, 124, output[:MAX_OUTPUT_BYTES], None)
    encoded = result.stdout.encode("utf-8", errors="replace")
    if len(encoded) > MAX_OUTPUT_BYTES:
        result.stdout = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        result.returncode = 125
    return result


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def load_state() -> dict[str, Any]:
    try:
        if STATE_FILE.is_symlink() or not STATE_FILE.is_file() or STATE_FILE.stat().st_size > 1048576:
            return {}
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def append_log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message[:4000]}\n")


def parse_json_result(result: subprocess.CompletedProcess[str], label: str) -> Any:
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed rc={result.returncode}: {result.stdout[:1000]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def as_int(value: Any, default: int = 0) -> int:
    return value if is_int(value) else default


def nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ---------------------------------------------------------------------------
# Literal verdict / predecessor semantics
# ---------------------------------------------------------------------------
def verdict_tokens(*texts: Any) -> list[str]:
    """Return the literal blocking verdict tokens present in ``texts``.

    Whole-word matching only, so ``UNBLOCKED`` is not a ``BLOCK`` verdict and
    ``PASSPORT`` is not a ``PASS``.
    """
    found: list[str] = []
    blob = "\n".join(text for text in texts if isinstance(text, str))
    if not blob:
        return found
    for token in VERDICT_BLOCK_TOKENS:
        if re.search(rf"(?<![A-Z0-9_]){token}(?![A-Z0-9_])", blob):
            found.append(token)
    return found


def has_pass_verdict(*texts: Any) -> bool:
    blob = "\n".join(text for text in texts if isinstance(text, str))
    if not blob:
        return False
    return bool(re.search(rf"(?<![A-Z0-9_]){VERDICT_PASS_TOKEN}(?![A-Z0-9_])", blob))


def is_protected_block(task: dict[str, Any]) -> bool:
    """A blocked card needs a human unless its kind literally self-clears."""
    if task.get("status") != "blocked":
        return False
    kind = task.get("block_kind")
    self_clearing = isinstance(kind, str) and kind in GATED_BLOCK_KINDS
    # needs_input / capability / NULL / anything unrecognised -> fail closed.
    return not self_clearing


# ---------------------------------------------------------------------------
# Evidence gathering: supported read-only kanban_db API.
# ---------------------------------------------------------------------------
def _kanban_modules():
    from hermes_cli import kanban_db  # imported lazily so tests need no hermes
    from hermes_cli import kanban_db_connect

    return kanban_db, kanban_db_connect


TASK_FIELDS = (
    "id",
    "title",
    "status",
    "assignee",
    "priority",
    "tenant",
    "workspace_kind",
    "workspace_path",
    "branch_name",
    "project_id",
    "result",
    "skills",
    "block_kind",
    "block_recurrences",
    "consecutive_failures",
    "claim_lock",
    "claim_expires",
    "worker_pid",
    "last_heartbeat_at",
    "current_run_id",
    "session_id",
    "created_at",
    "started_at",
    "completed_at",
)


def task_to_record(task: Any) -> dict[str, Any]:
    """Serialize a kanban_db.Task using its real attribute names only."""
    record: dict[str, Any] = {}
    for field in TASK_FIELDS:
        value = getattr(task, field, None)
        record[field] = list(value) if isinstance(value, list) else value
    return record


def fetch_board_snapshot(board: str) -> dict[str, Any]:
    """Authoritative whole-board read, archived rows included.

    ``source_total`` comes from the same query that produced ``tasks`` so the
    classifier can reconcile against it rather than against its own output.
    """
    kb, kbc = _kanban_modules()
    tenant = BOARD_TENANTS[board]
    db_path = str(Path(kb.kanban_db_path(board=board)).resolve())
    with kbc.connect_closing(board=board) as conn:
        tasks = kb.list_tasks(conn, include_archived=True)
    records = [task_to_record(task) for task in tasks]
    scoped = [r for r in records if r.get("tenant") == tenant]
    return {
        "board": board,
        "db_path": db_path,
        "tenant": tenant,
        "tasks": scoped,
        "source_total": len(scoped),
        "archived_count": sum(1 for r in scoped if r.get("status") == "archived"),
        "out_of_tenant_count": len(records) - len(scoped),
    }


RUN_FIELDS = (
    "id",
    "task_id",
    "profile",
    "status",
    "worker_pid",
    "last_heartbeat_at",
    "started_at",
    "ended_at",
    "outcome",
    "summary",
)


def fetch_task_runs(board: str, task_id: str) -> list[dict[str, Any]]:
    kb, kbc = _kanban_modules()
    with kbc.connect_closing(board=board) as conn:
        runs = kb.list_runs(conn, task_id)
    return [{field: getattr(r, field, None) for field in RUN_FIELDS} for r in runs]


def fetch_task_links(board: str, task_id: str) -> dict[str, list[dict[str, Any]]]:
    """Literal predecessor/successor evidence from the task_links table."""
    kb, kbc = _kanban_modules()
    with kbc.connect_closing(board=board) as conn:
        parents = kb.parent_ids(conn, task_id)
        children = kb.child_ids(conn, task_id)

        def described(ids: Iterable[str]) -> list[dict[str, Any]]:
            out = []
            for other in ids:
                row = kb.get_task(conn, other)
                out.append({"id": other, "status": getattr(row, "status", None) if row else None})
            return out

        return {"parents": described(parents), "children": described(children)}


def actionable_successors(children: Any) -> list[dict[str, str]]:
    """Return only minimized child rows with an explicitly actionable status."""
    if not isinstance(children, list):
        return []
    staged: list[dict[str, str]] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        task_id = child.get("id")
        status = child.get("status")
        if not isinstance(task_id, str) or not task_id.startswith("t_"):
            continue
        if not isinstance(status, str) or status not in ACTIONABLE_SUCCESSOR_STATUSES:
            continue
        staged.append({"id": task_id, "status": status})
    return staged


def latest_run(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Most recent run by (started_at, id); missing keys sort last, not first."""
    usable = [r for r in runs if isinstance(r, dict)]
    if not usable:
        return {}
    return max(usable, key=lambda r: (as_int(r.get("started_at"), -1), as_int(r.get("id"), -1)))


# ---------------------------------------------------------------------------
# Evidence gathering: work-claims SQLite, query-only.
# ---------------------------------------------------------------------------
def _open_work_claims() -> sqlite3.Connection:
    if not WORK_CLAIMS_DB.exists():
        raise RuntimeError(f"work-claims db missing at {WORK_CLAIMS_DB}")
    conn = sqlite3.connect(f"file:{WORK_CLAIMS_DB}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def fetch_work_claims() -> dict[str, Any]:
    """Read active work-claims rows and their targets, query-only.

    ``claims`` is the authoritative table on this host. ``execution_leases``
    does not exist here; it is joined only if a future host grows it, and its
    absence is recorded rather than guessed at.
    """
    conn = _open_work_claims()
    try:
        if not _table_exists(conn, "claims"):
            raise RuntimeError("work-claims db has no claims table")
        has_targets = _table_exists(conn, "claim_targets")
        has_leases = _table_exists(conn, "execution_leases")
        rows = conn.execute(
            "SELECT claim_id, session_id, kanban_task_id, status, workspace, "
            "source_workspace, acquired_at, heartbeat_at, expires_at "
            "FROM claims WHERE status = 'active' ORDER BY claim_id"
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            if has_targets:
                targets = conn.execute(
                    "SELECT target FROM claim_targets WHERE claim_id = ? ORDER BY target",
                    (record["claim_id"],),
                ).fetchall()
                record["targets"] = [t["target"] for t in targets]
            else:
                record["targets"] = []
            if has_leases:
                lease = conn.execute(
                    "SELECT * FROM execution_leases WHERE session_id = ? "
                    "ORDER BY expires_at DESC LIMIT 1",
                    (record["session_id"],),
                ).fetchone()
                record["lease"] = dict(lease) if lease else None
            else:
                record["lease"] = None
            records.append(record)
        return {
            "available": True,
            "claims": records,
            "has_claim_targets": has_targets,
            "has_execution_leases": has_leases,
        }
    finally:
        conn.close()


def link_work_claims(
    claims: list[dict[str, Any]],
    known: dict[str, dict[str, Any]],
    executing_ids: set[str],
    now: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind each active claim to an executing board task, or fault it.

    A linked claim is corroborating evidence for a lane that already exists.
    It never invents a lane and never contributes an owner -- standby, stale
    and unknown holders are not accountable specialists.
    """
    linked: list[dict[str, Any]] = []
    faults: list[dict[str, Any]] = []
    for claim in claims:
        task_id = claim.get("kanban_task_id")
        expires_at = claim.get("expires_at")
        heartbeat_at = claim.get("heartbeat_at")
        record = {
            "claim_id": claim.get("claim_id"),
            "session_id": claim.get("session_id"),
            "task_id": task_id,
            "targets": list(claim.get("targets") or []),
            "workspace": claim.get("workspace"),
            "expires_at": expires_at,
            "heartbeat_at": heartbeat_at,
        }
        reasons: list[str] = []
        if not is_int(expires_at):
            reasons.append("missing_expiry")
        elif expires_at <= now:
            reasons.append("lease_expired")
        if not is_int(heartbeat_at):
            reasons.append("missing_heartbeat")
        elif heartbeat_at > now + CLOCK_SKEW_SECONDS:
            reasons.append("future_heartbeat")
        elif now - heartbeat_at > CLAIM_FRESH_SECONDS:
            reasons.append("stale_heartbeat")
        if not nonempty_str(task_id):
            reasons.append("unlinked_to_task")
        elif task_id not in known:
            reasons.append("unknown_task")
        elif task_id not in executing_ids:
            reasons.append(f"task_not_executing:{known[task_id].get('status')}")
        if reasons:
            record["reasons"] = reasons
            faults.append(record)
        else:
            record["board"] = known[task_id].get("_board")
            linked.append(record)
    return linked, faults


def evaluate_work_claims(
    claims: list[dict[str, Any]],
    known: dict[str, dict[str, Any]],
    executing_ids: set[str],
    now: int,
    previous: dict[str, Any] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, int]],
    list[dict[str, Any]],
]:
    """Classify linked claims and independently evidenced standalone work.

    A standalone claim is execution only when its claim heartbeat/expiry and
    execution lease are fresh, its PID exists, and its progress is either a
    fresh first observation or advances beyond the prior checker observation.
    It occupies collision keys but never manufactures an accountable owner.
    """
    linked, linked_faults = link_work_claims(claims, known, executing_ids, now)
    linked_ids = {record.get("claim_id") for record in linked}
    faults = [fault for fault in linked_faults if nonempty_str(fault.get("task_id"))]
    executing: list[dict[str, Any]] = []
    occupied: list[dict[str, Any]] = []
    observations: dict[str, dict[str, int]] = {}
    previous = previous if isinstance(previous, dict) else {}

    for claim in claims:
        claim_id = claim.get("claim_id")
        if claim_id in linked_ids or nonempty_str(claim.get("kanban_task_id")):
            continue
        reasons: list[str] = []
        expires_at: Any = claim.get("expires_at")
        heartbeat_at: Any = claim.get("heartbeat_at")
        if not is_int(expires_at):
            reasons.append("missing_expiry")
        elif expires_at <= now:
            reasons.append("lease_expired")
        if not is_int(heartbeat_at):
            reasons.append("missing_heartbeat")
        elif heartbeat_at > now + CLOCK_SKEW_SECONDS:
            reasons.append("future_heartbeat")
        elif now - heartbeat_at > CLAIM_FRESH_SECONDS:
            reasons.append("stale_heartbeat")

        lease = claim.get("lease")
        if not isinstance(lease, dict):
            reasons.append("missing_execution_lease")
            pid = progress_seq = observed_at = None
        else:
            pid: Any = lease.get("pid")
            progress_seq: Any = lease.get("progress_seq")
            observed_at: Any = lease.get("observed_at")
            if not is_int(pid) or pid <= 0:
                reasons.append("missing_execution_pid")
            elif not process_alive(pid):
                reasons.append("dead_execution_pid")
            if not is_int(progress_seq):
                reasons.append("missing_progress_seq")
            if not is_int(observed_at):
                reasons.append("missing_execution_observed_at")
            elif observed_at > now + CLOCK_SKEW_SECONDS:
                reasons.append("future_execution_observation")
            elif now - observed_at > HEARTBEAT_FRESH_SECONDS:
                reasons.append("stale_execution_observation")

        prior = previous.get(cast(str, claim_id)) if nonempty_str(claim_id) else None
        if isinstance(prior, dict) and is_int(progress_seq) and progress_seq <= as_int(prior.get("progress_seq"), -1):
            reasons.append("execution_not_advancing")

        base = {
            "claim_id": claim_id,
            "task_id": None,
            "workspace": claim.get("workspace"),
            "targets": list(claim.get("targets") or []),
        }
        if nonempty_str(claim_id) and is_int(progress_seq) and is_int(observed_at):
            observations[cast(str, claim_id)] = {
                "progress_seq": cast(int, progress_seq),
                "observed_at": cast(int, observed_at),
            }
        if reasons:
            faults.append({**base, "reasons": reasons})
            continue
        if not nonempty_str(claim_id) or not is_int(pid) or not is_int(progress_seq) or not is_int(observed_at):
            faults.append({**base, "reasons": ["malformed_standalone_execution"]})
            continue
        safe_claim_id = cast(str, claim_id)
        safe_pid = cast(int, pid)
        safe_progress_seq = cast(int, progress_seq)
        safe_observed_at = cast(int, observed_at)
        public = {"claim_id": safe_claim_id, "pid": safe_pid, "progress_seq": safe_progress_seq}
        executing.append(public)
        occupied.append({**base, **public})
        observations[safe_claim_id] = {"progress_seq": safe_progress_seq, "observed_at": safe_observed_at}

    return linked, executing, faults, observations, occupied


def claim_targets_by_task(linked: list[dict[str, Any]]) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for record in linked:
        task_id = record.get("task_id")
        if nonempty_str(task_id):
            mapping.setdefault(task_id, set()).update(
                t for t in record.get("targets") or [] if nonempty_str(t)
            )
    return mapping


# ---------------------------------------------------------------------------
# Evidence gathering: profile-local capability snapshot.
# ---------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # provided by the hermes venv

    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    return value if isinstance(value, dict) else {}


def profile_skill_slugs(profile_dir: Path) -> list[str]:
    """Installed skills = every directory holding a SKILL.md, 1 or 2 deep."""
    skills_dir = profile_dir / "skills"
    slugs: set[str] = set()
    if not skills_dir.is_dir():
        return []
    for pattern in ("*/SKILL.md", "*/*/SKILL.md"):
        for marker in skills_dir.glob(pattern):
            slugs.add(marker.parent.name)
    return sorted(slugs)


def normalize_profile_key(name: str) -> str:
    return name.strip().lower()


def fetch_profiles() -> dict[str, dict[str, Any]]:
    """Scan profile-local config.yaml + skills/ for each profile directory.

    Two profile directories that normalize to the same key are an alias
    collision: both are marked unusable so a preflight against either fails
    closed rather than silently binding to whichever won the dict race.
    """
    profiles: dict[str, dict[str, Any]] = {}
    collisions: dict[str, list[str]] = {}
    if not PROFILES_DIR.is_dir():
        return profiles
    for entry in sorted(PROFILES_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        key = normalize_profile_key(entry.name)
        snapshot: dict[str, Any] = {
            "name": entry.name,
            "key": key,
            "skills": profile_skill_slugs(entry),
            "toolsets": [],
            "model": None,
            "provider": None,
            "config_ok": False,
            "config_error": None,
        }
        config_path = entry / "config.yaml"
        try:
            config = _load_yaml(config_path)
            platform_toolsets = config.get("platform_toolsets")
            available: set[str] = set()
            if isinstance(platform_toolsets, dict):
                for names in platform_toolsets.values():
                    if isinstance(names, list):
                        available.update(str(n) for n in names if n)
            agent_cfg = config.get("agent") if isinstance(config.get("agent"), dict) else {}
            disabled = agent_cfg.get("disabled_toolsets")
            if isinstance(disabled, list):
                available -= {str(n) for n in disabled if n}
            model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
            snapshot["toolsets"] = sorted(available)
            snapshot["model"] = model_cfg.get("default")
            snapshot["provider"] = model_cfg.get("provider")
            snapshot["config_ok"] = True
        except Exception as exc:  # noqa: BLE001 - any config fault -> unusable profile
            snapshot["config_error"] = f"{type(exc).__name__}: {exc}"[:200]
        collisions.setdefault(key, []).append(entry.name)
        profiles[key] = snapshot
    for key, names in collisions.items():
        if len(names) > 1:
            profiles[key]["config_ok"] = False
            profiles[key]["config_error"] = f"alias_collision:{','.join(sorted(names))}"
            profiles[key]["alias_collision"] = sorted(names)
    return profiles


def lookup_profile(profiles: dict[str, dict[str, Any]], assignee: str) -> dict[str, Any] | None:
    return profiles.get(normalize_profile_key(assignee))


# ---------------------------------------------------------------------------
# Classifier: one pass, disjoint buckets, reconciled against source_total.
# ---------------------------------------------------------------------------
def classify_task(task: Any) -> str:
    if not isinstance(task, dict):
        return BUCKET_MALFORMED
    task_id = task.get("id")
    status = task.get("status")
    if not isinstance(task_id, str) or not task_id.startswith("t_"):
        return BUCKET_MALFORMED
    if not isinstance(status, str) or status not in KNOWN_STATUSES:
        return BUCKET_MALFORMED
    if status in HISTORICAL_STATUSES:
        return BUCKET_HISTORICAL
    if is_protected_block(task) or verdict_tokens(task.get("result"), task.get("_latest_summary")):
        return BUCKET_PROTECTED
    if status in EXECUTING_STATUSES:
        return BUCKET_EXECUTING
    if status == READY_STATUS:
        return BUCKET_READY
    return BUCKET_GATED


def classify_all(snapshots: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Classify every record on every board and reconcile to ``source_total``.

    ``source_total`` is taken from the board snapshot -- the authority -- not
    recomputed from the list we happened to iterate.
    """
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in BUCKETS}
    for board, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"{board} snapshot is not an object")
        source_total = snapshot.get("source_total")
        if not is_int(source_total):
            raise RuntimeError(f"{board} snapshot has no authoritative source_total")
        tasks = snapshot.get("tasks")
        if not isinstance(tasks, list):
            raise RuntimeError(f"{board} snapshot has no task list")
        seen: set[str] = set()
        classified = 0
        for index, task in enumerate(tasks):
            classified += 1
            if not isinstance(task, dict):
                buckets[BUCKET_MALFORMED].append(
                    {"id": f"_unparsable_{index}", "status": None, "_board": board, "_raw": repr(task)[:200]}
                )
                continue
            bucket = classify_task(task)
            task_id = task.get("id")
            if isinstance(task_id, str) and task_id in seen:
                # A duplicate id on one board makes every id-keyed conclusion
                # unsound; refuse to classify rather than double-count.
                raise RuntimeError(f"classifier saw duplicate task {board}:{task_id}")
            if isinstance(task_id, str):
                seen.add(task_id)
            buckets[bucket].append({**task, "_board": board})
        if classified != source_total:
            raise RuntimeError(
                f"{board} classification dropped records: source_total={source_total} classified={classified}"
            )
    total_out = sum(len(items) for items in buckets.values())
    total_in = sum(as_int(s.get("source_total")) for s in snapshots.values())
    if total_out != total_in:
        raise RuntimeError(f"classifier totals mismatch: source_total={total_in} classified={total_out}")
    return buckets


def unique_specialists(names: Iterable[Any]) -> list[str]:
    return sorted({name.strip() for name in names if nonempty_str(name)})


# ---------------------------------------------------------------------------
# Liveness validation for executing (running/review) cards.
# ---------------------------------------------------------------------------
def validate_executing_task(task: dict[str, Any], now: int) -> dict[str, Any]:
    """Strict fresh worker/run evidence. Anything missing = unhealthy."""
    board = task["_board"]
    task_id = task["id"]
    reasons: list[str] = []

    assignee = task.get("assignee")
    if not nonempty_str(assignee):
        reasons.append("no_accountable_owner")

    heartbeat = task.get("last_heartbeat_at")
    if not is_int(heartbeat):
        reasons.append("missing_task_heartbeat")
    elif heartbeat > now + CLOCK_SKEW_SECONDS:
        reasons.append("future_task_heartbeat")
    elif now - heartbeat > HEARTBEAT_FRESH_SECONDS:
        reasons.append("stale_task_heartbeat")

    try:
        runs = fetch_task_runs(board, task_id)
    except Exception as exc:  # noqa: BLE001 - unreadable run evidence -> unhealthy, never healthy
        runs = []
        reasons.append(f"run_evidence_unavailable:{type(exc).__name__}")

    latest = latest_run(runs)
    pid: int | None = None
    run_heartbeat = latest.get("last_heartbeat_at")
    if not latest:
        reasons.append("no_run_row")
    else:
        if latest.get("status") != "running" or latest.get("ended_at") is not None:
            reasons.append("no_live_run_row")
        raw_pid = latest.get("worker_pid")
        if not is_int(raw_pid):
            reasons.append("missing_worker_pid")
        elif not process_alive(raw_pid):
            reasons.append("dead_worker_pid")
        else:
            pid = raw_pid
        if not is_int(run_heartbeat):
            reasons.append("missing_run_heartbeat")
        elif run_heartbeat > now + CLOCK_SKEW_SECONDS:
            reasons.append("future_run_heartbeat")
        elif now - run_heartbeat > HEARTBEAT_FRESH_SECONDS:
            reasons.append("stale_run_heartbeat")

    return {
        "board": board,
        "task_id": task_id,
        "status": task.get("status"),
        "assignee": assignee if nonempty_str(assignee) else None,
        "worker_pid": pid,
        "task_heartbeat_at": heartbeat if is_int(heartbeat) else None,
        "run_heartbeat_at": run_heartbeat if is_int(run_heartbeat) else None,
        "run_id": latest.get("id"),
        "healthy": not reasons,
        "reasons": reasons,
    }


def ownerless_review_cards(executing_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"board": task["_board"], "task_id": task["id"]}
        for task in executing_tasks
        if task.get("status") == "review" and not nonempty_str(task.get("assignee"))
    ]


# ---------------------------------------------------------------------------
# Collision avoidance: one independent key per independently claimable target.
# ---------------------------------------------------------------------------
def collision_keys(task: dict[str, Any], targets: dict[str, set[str]] | None = None) -> set[tuple[str, str]]:
    """Every shared target this card would occupy, serialized independently.

    ``branch_name`` is deliberately NOT part of the workspace key: two cards on
    different branches of the same worktree still share one working tree, so
    they must collide.
    """
    keys: set[tuple[str, str]] = set()
    workspace = task.get("workspace_path")
    if nonempty_str(workspace):
        keys.add(("workspace", workspace.strip()))
    project = task.get("project_id")
    if nonempty_str(project):
        keys.add(("project", project.strip()))
    for target in sorted((targets or {}).get(task.get("id"), set())):
        if nonempty_str(target):
            keys.add(("target", target.strip()))
    return keys


def occupied_collision_keys(
    executing_tasks: list[dict[str, Any]],
    linked_claims: list[dict[str, Any]],
    targets: dict[str, set[str]] | None = None,
    standalone_executing: list[dict[str, Any]] | None = None,
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for task in executing_tasks:
        keys |= collision_keys(task, targets)
    for claim in linked_claims:
        for target in claim.get("targets") or []:
            if nonempty_str(target):
                keys.add(("target", target.strip()))
        workspace = claim.get("workspace")
        if nonempty_str(workspace):
            keys.add(("workspace", workspace.strip()))
    for claim in standalone_executing or []:
        for target in claim.get("targets") or []:
            if nonempty_str(target):
                keys.add(("target", target.strip()))
        workspace = claim.get("workspace")
        if nonempty_str(workspace):
            keys.add(("workspace", cast(str, workspace).strip()))
    return keys


def is_critical_path_task(task: dict[str, Any]) -> bool:
    """Critical-path membership is explicit, never inferred from priority."""
    title = task.get("title")
    return isinstance(title, str) and CRITICAL_PATH_MARKER in title.lower()


def alert_reason_codes(reasons: Iterable[Any]) -> list[str]:
    """Project internal diagnostics onto the external alert reason allowlist."""
    out: set[str] = set()
    for reason in reasons:
        if not isinstance(reason, str):
            continue
        code = reason.split(":", 1)[0]
        if re.fullmatch(r"[A-Za-z0-9_-]+", code):
            out.add(code)
    return sorted(out)


def claim_pseudonym(claim_id: Any) -> str:
    value = claim_id if isinstance(claim_id, str) else "unknown"
    return "claim-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Preflight: capability, verdict, dependency and admission gates for a ready
# card. Every branch fails closed.
# ---------------------------------------------------------------------------
def preflight_ready_task(
    task: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    gates: dict[tuple[str, str], dict[str, Any]] | None = None,
    busy_owners: set[str] | None = None,
    executing_ids: set[tuple[str, str]] | None = None,
) -> list[str]:
    problems: list[str] = []
    gate = (gates or {}).get((task.get("_board"), task.get("id")), {})
    busy_owners = busy_owners or set()
    executing_ids = executing_ids or set()

    if task.get("status") != READY_STATUS:
        problems.append(f"not_ready:{task.get('status')}")

    # -- literal verdict / protected / dependency semantics -------------------
    summary = gate.get("latest_summary")
    tokens = verdict_tokens(task.get("result"), summary)
    for token in tokens:
        problems.append(f"verdict_block:{token}")
    if is_protected_block(task):
        problems.append(f"protected_stop:{task.get('block_kind')}")
    if nonempty_str(task.get("claim_lock")):
        problems.append("already_claimed")
    if as_int(task.get("consecutive_failures")) >= FAILURE_LIMIT:
        problems.append("circuit_breaker_tripped")

    if "parents" in gate:
        for parent in gate.get("parents") or []:
            if parent.get("status") != "done":
                problems.append(f"unsatisfied_dependency:{parent.get('id')}:{parent.get('status')}")
    else:
        problems.append("dependency_evidence_unavailable")

    if "children" in gate:
        for child in gate.get("children") or []:
            if (task.get("_board"), child.get("id")) in executing_ids:
                problems.append(f"successor_executing:{child.get('id')}")
    else:
        problems.append("successor_evidence_unavailable")

    # -- capability ----------------------------------------------------------
    assignee = task.get("assignee")
    if not nonempty_str(assignee):
        problems.append("no_assignee")
        return problems
    if assignee.strip() in busy_owners:
        problems.append(f"owner_already_holds_a_lane:{assignee.strip()}")
    profile = lookup_profile(profiles, assignee)
    if profile is None:
        problems.append(f"profile_missing:{assignee}")
        return problems
    if not profile.get("config_ok"):
        problems.append(f"profile_unusable:{assignee}:{profile.get('config_error')}")
        return problems
    installed = set(profile.get("skills") or [])
    required = task.get("skills")
    if required is not None and not isinstance(required, list):
        problems.append("malformed_skills_field")
    else:
        for skill in required or []:
            if not isinstance(skill, str) or skill not in installed:
                problems.append(f"missing_skill:{skill}")
    toolsets = set(profile.get("toolsets") or [])
    if nonempty_str(task.get("workspace_path")) and not (toolsets & WORKSPACE_TOOLSETS):
        problems.append("missing_workspace_toolset")
    if not nonempty_str(profile.get("model")):
        problems.append("profile_has_no_model")
    return problems


def select_dispatch_candidate(
    ready_tasks: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    occupied_keys: set[tuple[str, str]],
    gates: dict[tuple[str, str], dict[str, Any]] | None = None,
    busy_owners: set[str] | None = None,
    executing_ids: set[tuple[str, str]] | None = None,
    targets: dict[str, set[str]] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Deterministic selection: highest priority, then board, then task id."""
    report: list[dict[str, Any]] = []
    chosen: dict[str, Any] | None = None
    claimed = set(occupied_keys)
    ordered = sorted(ready_tasks, key=lambda t: (-as_int(t.get("priority")), t["_board"], t["id"]))
    for task in ordered:
        problems = preflight_ready_task(task, profiles, gates, busy_owners, executing_ids)
        keys = collision_keys(task, targets)
        conflicts = sorted(f"{kind}:{value}" for kind, value in keys & claimed)
        problems.extend(f"collision:{conflict}" for conflict in conflicts)
        eligible = not problems
        report.append(
            {
                "board": task["_board"],
                "task_id": task["id"],
                "assignee": task.get("assignee"),
                "priority": as_int(task.get("priority")),
                "collision_keys": sorted(f"{kind}:{value}" for kind, value in keys),
                "problems": problems,
                "eligible": eligible,
            }
        )
        if eligible and chosen is None:
            chosen = task
        # Reserve this card's targets so a second ready card in the same pass
        # cannot be judged eligible for a target we just handed out.
        claimed |= keys
    return chosen, report


# ---------------------------------------------------------------------------
# Runway: unique safe owners, deterministic one-lane exception.
# ---------------------------------------------------------------------------
def one_lane_exception(
    candidate: dict[str, Any],
    healthy_lanes: list[dict[str, Any]],
    lane_tasks: dict[tuple[str, str], dict[str, Any]],
    gates: dict[tuple[str, str], dict[str, Any]],
    targets: dict[str, set[str]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Prove it is safe to dispatch while exactly one healthy lane exists.

    Every clause must hold, and the proof is recorded in state so the decision
    is auditable rather than asserted.
    """
    proof: dict[str, Any] = {"clauses": {}}
    if len(healthy_lanes) != 1:
        proof["clauses"]["exactly_one_healthy_lane"] = False
        return False, proof
    proof["clauses"]["exactly_one_healthy_lane"] = True
    lane = healthy_lanes[0]
    lane_key = (lane["board"], lane["task_id"])
    lane_task = lane_tasks.get(lane_key, {})

    candidate_keys = collision_keys(candidate, targets)
    lane_keys = collision_keys(lane_task, targets)
    disjoint = not (candidate_keys & lane_keys)
    proof["clauses"]["targets_disjoint_from_lane"] = disjoint
    proof["candidate_keys"] = sorted(f"{k}:{v}" for k, v in candidate_keys)
    proof["lane_keys"] = sorted(f"{k}:{v}" for k, v in lane_keys)

    distinct_owner = nonempty_str(candidate.get("assignee")) and (
        candidate.get("assignee", "").strip() != (lane.get("assignee") or "").strip()
    )
    proof["clauses"]["distinct_owner"] = distinct_owner

    gate = gates.get((candidate.get("_board"), candidate.get("id")), {})
    parents = gate.get("parents")
    children = gate.get("children")
    parents_done = isinstance(parents, list) and all(p.get("status") == "done" for p in parents)
    proof["clauses"]["all_predecessors_done"] = parents_done
    successor_free = isinstance(children, list) and lane_key[1] not in {c.get("id") for c in children}
    proof["clauses"]["lane_is_not_candidate_successor"] = successor_free
    lane_gate = gates.get(lane_key, {})
    lane_children = lane_gate.get("children")
    not_lane_successor = (
        not isinstance(lane_children, list) or candidate.get("id") not in {c.get("id") for c in lane_children}
    )
    proof["clauses"]["candidate_is_not_lane_successor"] = not_lane_successor

    ok = all(proof["clauses"].values())
    proof["applies"] = ok
    return ok, proof


# ---------------------------------------------------------------------------
# Exact-task dispatch. Bound to one task id; a different (e.g. higher-priority)
# spawned task is a fault, never a success.
# ---------------------------------------------------------------------------
def dispatch_exact_task(board: str, task_id: str) -> subprocess.CompletedProcess[str]:
    return run(
        [str(HERMES), "kanban", "--board", board, "dispatch", "--task-id", task_id, "--max", "1", "--json"]
    )


def assert_exact_spawn(result: subprocess.CompletedProcess[str], task_id: str) -> dict[str, Any]:
    """Verify the dispatcher spawned exactly the task we admitted.

    Returns ``{"spawned": False, "reason": ...}`` when the dispatcher declined
    (a legitimate admission race), and raises when it spawned something other
    than ``task_id`` -- that is a contract violation, not a race.
    """
    payload = parse_json_result(result, "dispatch")
    if not isinstance(payload, dict):
        raise RuntimeError("dispatch response is not an object")
    spawned = payload.get("spawned")
    if not isinstance(spawned, list):
        raise RuntimeError("dispatch response has no spawned array")
    if not spawned:
        skipped = [
            entry
            for key, value in sorted(payload.items())
            if key.startswith("skipped") and isinstance(value, list)
            for entry in value
        ]
        return {"spawned": False, "task_id": None, "reason": f"not_admitted:{json.dumps(skipped, default=str)[:400]}"}
    if len(spawned) != 1:
        raise RuntimeError(f"dispatch spawned {len(spawned)} workers for a --max 1 exact dispatch")
    entry = spawned[0]
    got = entry.get("task_id") if isinstance(entry, dict) else entry
    if got != task_id:
        raise RuntimeError(f"dispatch spawned the wrong task: requested={task_id} spawned={got!r}")
    return {"spawned": True, "task_id": task_id, "assignee": entry.get("assignee") if isinstance(entry, dict) else None}


def reconcile_active_claims(board: str) -> subprocess.CompletedProcess[str]:
    """Reclaim-only dispatcher pass (--max 0 spawns nothing)."""
    return run([str(HERMES), "kanban", "--board", board, "dispatch", "--max", "0", "--json"])


def send_alert(message: str) -> bool:
    result = run([str(HERMES), "send", "--to", SLACK_TARGET, message[:7000], "--json"], timeout=45)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Activation gate: CV-A01 must be literally accepted before any dispatch.
# ---------------------------------------------------------------------------
def activation_gate(buckets: dict[str, list[dict[str, Any]]], task_id: str = ACTIVATION_GATE_TASK) -> dict[str, Any]:
    found = [task for items in buckets.values() for task in items if task.get("id") == task_id]
    if not found:
        return {"ok": False, "task_id": task_id, "reason": "gate_task_not_found"}
    if len(found) > 1:
        return {"ok": False, "task_id": task_id, "reason": "gate_task_ambiguous"}
    task = found[0]
    if task.get("status") != "done":
        return {"ok": False, "task_id": task_id, "reason": f"gate_task_not_done:{task.get('status')}"}
    try:
        summary = latest_run(fetch_task_runs(task["_board"], task_id)).get("summary")
    except Exception as exc:  # noqa: BLE001 - unreadable gate evidence -> gate stays shut
        return {"ok": False, "task_id": task_id, "reason": f"gate_evidence_unavailable:{type(exc).__name__}"}
    blocking = verdict_tokens(task.get("result"), summary)
    if blocking:
        return {"ok": False, "task_id": task_id, "reason": f"gate_task_blocking_verdict:{','.join(blocking)}"}
    if not has_pass_verdict(task.get("result"), summary):
        return {"ok": False, "task_id": task_id, "reason": "gate_task_not_pass"}
    return {"ok": True, "task_id": task_id, "reason": "pass", "board": task["_board"]}


# ---------------------------------------------------------------------------
# Hook wake path. Spawns this same checker -- no governor, no nested manager.
# ---------------------------------------------------------------------------
HOOK_EVENTS = frozenset(
    {
        "kanban_task_completed",
        "kanban_task_blocked",
        "on_kanban_worker_exited",
        "on_kanban_worker_stale_claim",
    }
)


def event_token(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    event = payload.get("hook_event_name")
    extra = payload.get("extra")
    if event not in HOOK_EVENTS or not isinstance(extra, dict) or extra.get("board") not in BOARDS:
        return None
    task_id = extra.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("t_"):
        return None
    return f"{event}:{extra.get('board')}:{task_id}:{extra.get('run_id')}"


def hook_main() -> int:
    try:
        payload = json.load(sys.stdin)
        token = event_token(payload if isinstance(payload, dict) else None)
    except (json.JSONDecodeError, OSError, ValueError):
        token = None
    if token:
        subprocess.Popen(
            [str(PYTHON), str(SCRIPT), "--watchdog", "--event-token", token],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=clean_env(),
            start_new_session=True,
            close_fds=True,
        )
    sys.stdout.write("{}\n")
    return 0


# ---------------------------------------------------------------------------
# Alerting: hashed fault signature, dedupe per category, retry failed sends.
# ---------------------------------------------------------------------------
def fault_signature(category: str, payload: Any) -> str:
    blob = json.dumps({"category": category, "payload": payload}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def alert_if_changed(
    signatures: dict[str, Any],
    pending: dict[str, Any],
    category: str,
    payload: Any,
    message: str,
) -> bool:
    """Send at most one alert per changed fault signature, per category.

    A failed send is never recorded as delivered: the signature is dropped and
    the alert is queued for retry, so a transient Slack failure cannot suppress
    the fault forever.
    """
    signature = fault_signature(category, payload)
    if signatures.get(category) == signature and category not in pending:
        return False
    if send_alert(message):
        signatures[category] = signature
        pending.pop(category, None)
        return True
    signatures.pop(category, None)
    attempts = as_int((pending.get(category) or {}).get("attempts")) + 1
    if len(pending) < MAX_PENDING_ALERTS or category in pending:
        pending[category] = {"signature": signature, "message": message[:7000], "attempts": attempts}
    return False


def retry_pending_alerts(signatures: dict[str, Any], pending: dict[str, Any]) -> list[str]:
    resent: list[str] = []
    for category in sorted(pending):
        item = pending.get(category)
        if not isinstance(item, dict) or not isinstance(item.get("message"), str):
            pending.pop(category, None)
            continue
        if send_alert(item["message"]):
            signatures[category] = item.get("signature")
            resent.append(category)
    for category in resent:
        pending.pop(category, None)
    return resent


LEGACY_STATE_KEYS = (
    "governor_no_progress_exits",
    "governor_runner_pid",
    "governor_runner_started_at",
    "healthy_active_owners",
    "last_governor_output",
    "last_governor_rc",
    "last_governor_trigger_at",
    "last_trigger_token",
    "last_dispatch_at",
    "last_dispatch_output",
    "last_dispatch_rc",
    "standalone_links",
    "counts",
    "last_active_reconciliation",
)


def _status_counts(snapshots: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for board, snapshot in snapshots.items():
        per_board: dict[str, int] = {}
        for task in snapshot.get("tasks") or []:
            status = task.get("status") if isinstance(task, dict) else None
            key = status if isinstance(status, str) else "_unparsable"
            per_board[key] = per_board.get(key, 0) + 1
        counts[board] = per_board
    return counts


def check_once(*, token: str | None = None, now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state = load_state()
        if state.get("schema_version") != SCHEMA_VERSION:
            for stale_key in LEGACY_STATE_KEYS:
                state.pop(stale_key, None)
        state["schema_version"] = SCHEMA_VERSION
        signatures = state.get("alert_signatures")
        if not isinstance(signatures, dict):
            signatures = {}
        pending = state.get("pending_alerts")
        if not isinstance(pending, dict):
            pending = {}
        state.update(
            {
                "checked_at": now,
                "event_token": token,
                "owner": OWNER,
                "manager": MANAGER,
                "boards": list(BOARDS),
                "consumer_contract": CONSUMER_CONTRACT,
            }
        )
        try:
            # Retry anything a previous cycle failed to deliver before doing
            # any new signature comparison.
            state["resent_alerts"] = retry_pending_alerts(signatures, pending)

            # -- board evidence, per board, fail closed -----------------------
            snapshots: dict[str, dict[str, Any]] = {}
            board_evidence: dict[str, dict[str, Any]] = {}
            db_owners: dict[str, str] = {}
            for board in BOARDS:
                try:
                    snapshot = fetch_board_snapshot(board)
                except Exception as exc:  # noqa: BLE001 - any board fault -> degraded, never success
                    board_evidence[board] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
                    continue
                db_path = snapshot.get("db_path")
                if isinstance(db_path, str) and db_path in db_owners:
                    other = db_owners[db_path]
                    collision = f"board_alias_collision:{other}"
                    board_evidence[board] = {"ok": False, "error": collision}
                    board_evidence[other] = {"ok": False, "error": f"board_alias_collision:{board}"}
                    snapshots.pop(other, None)
                    continue
                if isinstance(db_path, str):
                    db_owners[db_path] = board
                snapshots[board] = snapshot
                board_evidence[board] = {
                    "ok": True,
                    "db_path": db_path,
                    "source_total": snapshot.get("source_total"),
                    "archived_count": snapshot.get("archived_count"),
                    "out_of_tenant_count": snapshot.get("out_of_tenant_count"),
                }

            state["board_evidence"] = board_evidence
            state["source_totals"] = {b: e.get("source_total") for b, e in sorted(board_evidence.items())}
            unreconciled = sorted(b for b, e in board_evidence.items() if not e.get("ok"))
            state["reconciled"] = not unreconciled
            if unreconciled:
                # Never report success while any named board is unknown.
                state["degraded_boards"] = unreconciled
                state["status"] = state["last_decision"] = "degraded"
                alert_if_changed(
                    signatures,
                    pending,
                    "board_evidence",
                    {"boards": unreconciled, "errors": {b: board_evidence[b].get("error") for b in unreconciled}},
                    f"CodeVolt liveness guard is DEGRADED: board evidence unavailable for {unreconciled}. "
                    f"Owner: {OWNER}. Manager: {MANAGER}. No classification, dispatch, or repair was attempted.",
                )
                state["alert_signatures"] = signatures
                state["pending_alerts"] = pending
                state["consecutive_failures"] = 0
                state["last_error"] = None
                atomic_json(STATE_FILE, state)
                append_log(f"DEGRADED boards={unreconciled}")
                return 1

            buckets = classify_all(snapshots)
            counts_by_board = _status_counts(snapshots)

            # -- executing liveness, with one reconcile-and-recheck cycle -----
            executing = buckets[BUCKET_EXECUTING]
            validations = [validate_executing_task(task, now) for task in executing]
            unhealthy = [v for v in validations if not v["healthy"]]
            if unhealthy:
                for board in sorted({v["board"] for v in unhealthy}):
                    reconcile_active_claims(board)
                    snapshots[board] = fetch_board_snapshot(board)
                buckets = classify_all(snapshots)
                counts_by_board = _status_counts(snapshots)
                executing = buckets[BUCKET_EXECUTING]
                validations = [validate_executing_task(task, now) for task in executing]
                unhealthy = [v for v in validations if not v["healthy"]]

            healthy_validations = [v for v in validations if v["healthy"]]
            state["executing_validation"] = validations
            # Active cards with no healthy owner anywhere is the worst liveness
            # state there is, but it is still an *observation*: record the whole
            # evidence set and fail closed on dispatch rather than throwing away
            # the diagnosis with an exception.
            no_healthy_owner = bool(executing) and not healthy_validations
            state["no_healthy_owner"] = no_healthy_owner

            ownerless_reviews = ownerless_review_cards(executing)

            # -- standalone execution evidence -------------------------------
            known = {task["id"]: task for items in buckets.values() for task in items if nonempty_str(task.get("id"))}
            executing_task_ids = {task["id"] for task in executing if nonempty_str(task.get("id"))}
            try:
                claims_evidence = fetch_work_claims()
            except Exception as exc:  # noqa: BLE001 - unreadable claims -> unavailable, blocks dispatch
                claims_evidence = {"available": False, "error": f"{type(exc).__name__}: {exc}"[:300], "claims": []}
            claims_available = bool(claims_evidence.get("available"))
            linked, standalone_executing, claim_faults, standalone_observations, standalone_occupied = (
                evaluate_work_claims(
                    list(claims_evidence.get("claims") or []),
                    known,
                    executing_task_ids,
                    now,
                    state.get("standalone_observations"),
                )
            )
            targets = claim_targets_by_task(linked)
            state["standalone_observations"] = standalone_observations
            state["standalone_claims"] = {
                "available": claims_available,
                "error": claims_evidence.get("error"),
                "has_execution_leases": claims_evidence.get("has_execution_leases"),
                "linked": linked,
                "executing": standalone_executing,
                "faults": claim_faults,
            }

            # The runway is unique accountable specialists, not card/process
            # cardinality. Standalone execution contributes a verified worker
            # process but never invents an owner or specialist lane.
            accountable_specialists = unique_specialists(v["assignee"] for v in healthy_validations)
            healthy_lane_count = len(accountable_specialists)
            healthy_worker_count = len(healthy_validations) + len(standalone_executing)
            specialist_lanes: list[dict[str, Any]] = []
            seen_owners: set[str] = set()
            for validation in healthy_validations:
                owner = validation.get("assignee")
                if nonempty_str(owner) and cast(str, owner).strip() not in seen_owners:
                    seen_owners.add(cast(str, owner).strip())
                    specialist_lanes.append(validation)

            # -- ready inventory and preflight -------------------------------
            ready_tasks = buckets[BUCKET_READY]
            executing_ids = {(t["_board"], t["id"]) for t in executing if nonempty_str(t.get("id"))}
            gates: dict[tuple[str, str], dict[str, Any]] = {}
            gate_errors: list[str] = []
            for task in ready_tasks + executing:
                key = (task["_board"], task["id"])
                if key in gates:
                    continue
                entry: dict[str, Any] = {}
                try:
                    entry.update(fetch_task_links(task["_board"], task["id"]))
                except Exception as exc:  # noqa: BLE001 - missing link evidence -> preflight fails closed
                    gate_errors.append(f"{key[0]}:{key[1]}:links:{type(exc).__name__}")
                try:
                    entry["latest_summary"] = latest_run(fetch_task_runs(task["_board"], task["id"])).get("summary")
                except Exception as exc:  # noqa: BLE001 - missing run evidence -> preflight fails closed
                    gate_errors.append(f"{key[0]}:{key[1]}:runs:{type(exc).__name__}")
                gates[key] = entry
            state["gate_evidence_errors"] = gate_errors

            immediate_successors = {
                f"{task['_board']}:{task['id']}": actionable_successors(
                    gates.get((task["_board"], task["id"]), {}).get("children")
                )
                for task in executing
            }
            missing_immediate_successors = sorted(
                f"{task['_board']}:{task['id']}"
                for task in executing
                if is_critical_path_task(task)
                and not immediate_successors.get(f"{task['_board']}:{task['id']}")
            )
            state["immediate_successors"] = immediate_successors
            state["missing_immediate_successors"] = missing_immediate_successors

            busy_owners = {v["assignee"].strip() for v in healthy_validations if nonempty_str(v.get("assignee"))}
            occupied = occupied_collision_keys(executing, linked, targets, standalone_occupied)
            profiles = fetch_profiles()
            candidate, preflight_report = select_dispatch_candidate(
                ready_tasks, profiles, occupied, gates, busy_owners, executing_ids, targets
            )
            state["ready_preflight"] = preflight_report

            gate = activation_gate(buckets)
            state["dispatch_gate"] = gate

            # -- dispatch admission: every blocker fails closed --------------
            blockers: list[str] = []
            if no_healthy_owner:
                blockers.append("no_healthy_worker_owner")
            if unhealthy:
                blockers.append("unhealthy_executing_lane")
            if ownerless_reviews:
                blockers.append("ownerless_review")
            if not claims_available:
                blockers.append("standalone_evidence_unavailable")
            if claim_faults:
                blockers.append("standalone_claim_fault")
            if gate_errors:
                blockers.append("gate_evidence_incomplete")
            if missing_immediate_successors:
                blockers.append("missing_critical_path_successor")
            if not gate["ok"]:
                blockers.append(f"activation_gate:{gate['reason']}")
            if healthy_lane_count >= RUNWAY_MAX_LANES:
                blockers.append("runway_at_ceiling")
            if candidate is None:
                blockers.append("no_eligible_ready_card")

            one_lane_proof: dict[str, Any] | None = None
            if candidate is not None and healthy_lane_count == 1 and RUNWAY_MIN_LANES > 1:
                lane_tasks = {(t["_board"], t["id"]): t for t in executing}
                ok, one_lane_proof = one_lane_exception(candidate, specialist_lanes, lane_tasks, gates, targets)
                if not ok:
                    blockers.append("one_lane_exception_unproven")
            state["one_lane_exception"] = one_lane_proof
            state["dispatch_blockers"] = blockers

            dispatched: dict[str, Any] | None = None
            if candidate is not None and not blockers:
                board = candidate["_board"]
                result = dispatch_exact_task(board, candidate["id"])
                outcome = assert_exact_spawn(result, candidate["id"])
                dispatched = {
                    "board": board,
                    "task_id": candidate["id"],
                    "assignee": candidate.get("assignee"),
                    "at": now,
                    "rc": result.returncode,
                    "spawned": outcome["spawned"],
                    "reason": outcome.get("reason"),
                }
                append_log(
                    f"DISPATCH board={board} task={candidate['id']} spawned={outcome['spawned']} "
                    f"reason={outcome.get('reason')}"
                )
                if not outcome["spawned"]:
                    # A declined admission is a race, not a contract breach:
                    # record it, alert, and try again next cycle.
                    blockers.append("dispatch_not_admitted")
                    alert_if_changed(
                        signatures,
                        pending,
                        "dispatch_not_admitted",
                        {"board": board, "task_id": candidate["id"], "reason": outcome.get("reason")},
                        f"CodeVolt exact dispatch was declined for board={board} task={candidate['id']}. "
                        f"Reason code: not_admitted. No graph mutation was made. Manager: {MANAGER}.",
                    )
            state["last_dispatch"] = dispatched
            state["dispatch_blockers"] = blockers

            # -- roll-up ------------------------------------------------------
            state["classification"] = {
                bucket: sorted(f"{task['_board']}:{task.get('id')}" for task in buckets[bucket]) for bucket in BUCKETS
            }
            state["classification_totals"] = {bucket: len(buckets[bucket]) for bucket in BUCKETS}
            state["status_counts"] = counts_by_board
            state["total_tasks"] = sum(as_int(s.get("source_total")) for s in snapshots.values())
            state["accountable_specialists"] = accountable_specialists
            state["accountable_specialists_count"] = len(accountable_specialists)
            state["healthy_worker_count"] = healthy_worker_count
            state["healthy_lane_count"] = healthy_lane_count
            state["ready_inventory"] = sorted(f"{t['_board']}:{t['id']}" for t in ready_tasks)
            state["runway"] = {
                "min_lanes": RUNWAY_MIN_LANES,
                "max_lanes": RUNWAY_MAX_LANES,
                "current_lanes": healthy_lane_count,
            }
            state["ownerless_reviews"] = ownerless_reviews

            liveness_faults = (
                no_healthy_owner
                or bool(unhealthy)
                or bool(ownerless_reviews)
                or bool(claim_faults)
                or not claims_available
                or bool(missing_immediate_successors)
            )

            if dispatched is not None and dispatched["spawned"]:
                decision = "dispatched-ready"
            elif liveness_faults:
                decision = "liveness-fault"
            elif healthy_lane_count >= RUNWAY_MAX_LANES and ready_tasks:
                decision = "runway-at-target"
            elif healthy_worker_count:
                decision = "healthy-active"
            elif ready_tasks:
                # Ready cards are successor inventory, never a live lane; if we
                # could not admit one, a human decides.
                decision = "manager-attention-required"
            elif buckets[BUCKET_GATED] or buckets[BUCKET_PROTECTED]:
                decision = "manager-attention-required"
            elif buckets[BUCKET_MALFORMED]:
                decision = "liveness-fault"
            else:
                decision = "complete"

            state["status"] = decision
            state["last_decision"] = decision

            # A board needs manager attention when it carries ready/gated/
            # protected backlog with no healthy lane covering it and nothing
            # dispatched into it this cycle.
            backlog = buckets[BUCKET_READY] + buckets[BUCKET_GATED] + buckets[BUCKET_PROTECTED]
            backlog_boards = {task["_board"] for task in backlog}
            healthy_boards = {v["board"] for v in healthy_validations}
            dispatched_board = dispatched["board"] if dispatched and dispatched["spawned"] else None
            attention_boards = sorted(
                board for board in backlog_boards if board not in healthy_boards and board != dispatched_board
            )
            state["attention_boards"] = attention_boards

            if attention_boards:
                alert_if_changed(
                    signatures,
                    pending,
                    "manager_attention",
                    {"boards": attention_boards, "counts": counts_by_board},
                    f"CodeVolt work graph needs manager attention. Owner: {OWNER}. Manager: {MANAGER}. "
                    f"Boards with no healthy lane and no dispatch this cycle: {attention_boards}; "
                    f"counts: {json.dumps(counts_by_board, sort_keys=True)}. "
                    "The continuity guard made no graph mutation.",
                )
            if ownerless_reviews:
                public_ownerless = sorted(f"{r['board']}:{r['task_id']}" for r in ownerless_reviews)
                alert_if_changed(
                    signatures,
                    pending,
                    "ownerless_review",
                    sorted(f"{r['board']}:{r['task_id']}" for r in ownerless_reviews),
                    f"CodeVolt review card(s) have no accountable owner: {public_ownerless}. Manager: {MANAGER}.",
                )
            if claim_faults or not claims_available:
                public_claim_faults = [
                    {
                        "claim": claim_pseudonym(fault.get("claim_id")),
                        "reasons": alert_reason_codes(fault.get("reasons") or []),
                    }
                    for fault in claim_faults
                ]
                alert_if_changed(
                    signatures,
                    pending,
                    "standalone_fault",
                    {
                        "available": claims_available,
                        "faults": sorted(f"{f.get('claim_id')}:{','.join(f.get('reasons') or [])}" for f in claim_faults),
                    },
                    f"CodeVolt standalone work-claims evidence is faulted "
                    f"(available={claims_available}, claims={public_claim_faults}). Manager: {MANAGER}.",
                )
            if no_healthy_owner:
                public_validations = [
                    {
                        "board": item["board"],
                        "task": item["task_id"],
                        "reasons": alert_reason_codes(item.get("reasons") or []),
                    }
                    for item in validations
                ]
                alert_if_changed(
                    signatures,
                    pending,
                    "no_healthy_owner",
                    sorted(f"{v['board']}:{v['task_id']}:{','.join(v['reasons'])}" for v in validations),
                    f"CRITICAL CodeVolt has active cards with no healthy worker owner on any board. "
                    f"Owner: {OWNER}. Manager: {MANAGER}. No automatic graph repair was attempted. "
                    f"Evidence: {public_validations}",
                )
            if unhealthy:
                public_unhealthy = [
                    {
                        "board": item["board"],
                        "task": item["task_id"],
                        "reasons": alert_reason_codes(item.get("reasons") or []),
                    }
                    for item in unhealthy
                ]
                alert_if_changed(
                    signatures,
                    pending,
                    "unhealthy_executing",
                    sorted(f"{v['board']}:{v['task_id']}:{','.join(v['reasons'])}" for v in unhealthy),
                    f"CodeVolt executing card(s) failed liveness validation: {public_unhealthy}. Manager: {MANAGER}.",
                )
            capability_problems = [
                entry
                for entry in preflight_report
                if any(
                    p.startswith(("profile_missing", "profile_unusable", "missing_skill", "missing_workspace_toolset", "profile_has_no_model"))
                    for p in entry["problems"]
                )
            ]
            if capability_problems:
                public_capability = [
                    {
                        "board": entry["board"],
                        "task": entry["task_id"],
                        "reasons": alert_reason_codes(entry.get("problems") or []),
                    }
                    for entry in capability_problems
                ]
                alert_if_changed(
                    signatures,
                    pending,
                    "capability_preflight",
                    sorted(f"{e['board']}:{e['task_id']}:{','.join(e['problems'])}" for e in capability_problems),
                    f"CodeVolt ready card(s) cannot be dispatched -- capability gap: "
                    f"{public_capability}. Manager: {MANAGER}.",
                )
            if missing_immediate_successors:
                alert_if_changed(
                    signatures,
                    pending,
                    "missing_critical_path_successor",
                    missing_immediate_successors,
                    f"CodeVolt critical-path card(s) are missing an immediate successor: "
                    f"{missing_immediate_successors}. Manager: {MANAGER}.",
                )
            if buckets[BUCKET_MALFORMED]:
                alert_if_changed(
                    signatures,
                    pending,
                    "malformed_records",
                    state["classification"][BUCKET_MALFORMED],
                    f"CodeVolt board(s) returned malformed/unexpected task references: "
                    f"{state['classification'][BUCKET_MALFORMED]}. Manager: {MANAGER}.",
                )

            state["alert_signatures"] = signatures
            state["pending_alerts"] = pending
            state["consecutive_failures"] = 0
            state["last_error"] = None
            atomic_json(STATE_FILE, state)
            append_log(
                f"CHECK decision={decision} lanes={healthy_lane_count} "
                f"specialists={accountable_specialists} blockers={blockers}"
            )
            return 0 if decision in SUCCESS_DECISIONS else 1
        except Exception as exc:  # noqa: BLE001 - top-level guard: any fault -> guard-failed, never success
            prior = state.get("consecutive_failures", 0)
            failures = prior + 1 if is_int(prior) else 1
            state.update(
                {
                    "consecutive_failures": failures,
                    "status": "guard-failed",
                    "last_decision": "guard-failed",
                    "reconciled": False,
                    "last_error": str(exc)[:2000],
                }
            )
            if failures == 2 or "spawned the wrong task" in str(exc):
                alert_if_changed(
                    signatures,
                    pending,
                    "guard_failure",
                    {"error": str(exc)[:400], "failures": failures},
                    f"CRITICAL CodeVolt continuity guard failed. Owner: {OWNER}. Manager: {MANAGER}. "
                    f"No automatic graph repair was attempted. Reason code: guard_failure.",
                )
            state["alert_signatures"] = signatures
            state["pending_alerts"] = pending
            atomic_json(STATE_FILE, state)
            append_log(f"ERROR failures={failures} {exc}")
            return 1
    finally:
        os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--event-token")
    args = parser.parse_args()
    if args.hook:
        return hook_main()
    if args.watchdog:
        # The launchd timer and the hook path land here on the same
        # deterministic checker under the same lock.
        return check_once(token=args.event_token)
    parser.error("choose --hook or --watchdog")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
