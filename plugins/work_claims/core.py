from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

BOARD = "work-claims"
DEFAULT_TTL_MINUTES = 240
MAX_TTL_MINUTES = 720
TARGET_RE = re.compile(r"^(repo|project|external|system):(.+)$")
SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Generated once per process incarnation. Part of an execution lease's
#: holder identity (``holder_token`` + ``pid`` + ``boot_id``) so a PID reused
#: by an unrelated later process cannot renew, end, or take over a lease that
#: an earlier incarnation opened. Used only as this module's own fallback
#: when a caller supplies no boot id; the host passes its own
#: ``agent.execution_turn.PROCESS_BOOT_ID``.
_BOOT_ID = uuid.uuid4().hex

#: How many missed renewals a lease tolerates before it is treated as
#: abandoned. Mirrors ``agent/execution_turn.py``'s own "a few missed
#: renewals, not one" expiry philosophy.
_LEASE_EXPIRY_MULTIPLIER = 3
_LEASE_EXPIRY_FLOOR_SECONDS = 30

#: Finalize reasons that are a deliberate, durable conversation boundary --
#: the only ones that authorise releasing a claim. Everything else (an
#: automatic-cleanup stamp such as ``ws_orphan_reap``, an unrecognised
#: reason, or no reason at all) is inconclusive: the absence of a live
#: execution lease is NOT proof that a session finished, so a claim is
#: preserved and left to its own TTL rather than released on a guess. This is
#: the proven premature-finalization incident's root cause.
_DURABLE_TERMINAL_FINALIZE_REASONS = frozenset({
    "shutdown",          # cli.py process exit
    "session_boundary",  # cli.py deliberate session rotation (/new, /clear)
    "new_session",
    "session_reset",
    "session_switch",
    "tui_close",         # a user deliberately closing a gateway session
    "user_close",
})

#: Preserve/release dispositions returned by :func:`finalization_decision`.
PRESERVE = "preserve"
RELEASE = "release"

_DEFAULT_FINALIZE_SUMMARY = "Session finalized; claim released automatically"


class LeaseIdentityConflict(RuntimeError):
    """A turn begin arrived for a live ``lease_id`` under a different holder.

    Execution-lease rows are create-only per identity: an existing row is
    only ever refreshed by the exact holder that opened it. Anything else --
    a replayed lease id from another process, a forged token -- fails closed
    here rather than silently taking ownership.
    """


def is_durable_terminal_reason(reason: Any) -> bool:
    """Whether *reason* is an explicit, durable end-of-conversation signal.

    Fail-closed by construction: only the reasons listed in
    ``_DURABLE_TERMINAL_FINALIZE_REASONS`` qualify, and a reason the host
    classifies as automatic-cleanup (``hermes_state_common``'s own taxonomy,
    when importable) never does even if it were listed.
    """
    if not isinstance(reason, str) or reason not in _DURABLE_TERMINAL_FINALIZE_REASONS:
        return False
    try:
        from hermes_state_common import is_automatic_end_reason
    except ImportError:
        return True
    return not is_automatic_end_reason(reason)


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().resolve()


def shared_root() -> Path:
    """Return the one coordination root shared by every local profile."""
    home = hermes_home()
    if home.parent.name == "profiles":
        return home.parent.parent.resolve()
    return home


def db_path() -> Path:
    return shared_root() / "work-claims.db"


def _migrate(conn: sqlite3.Connection) -> None:
    """Drop superseded/incompatible tables from an older schema version.

    These are ephemeral coordination tables, not durable business data: a
    row lost across an upgrade self-heals from the next begin/renew cycle
    (execution leases) or is backstopped by claim TTL expiry (deferred
    finalizes), so dropping rather than hand-writing an ALTER migration is
    safe and far simpler.
    """
    # Superseded by ``execution_leases`` (callback-thread-owned rows could
    # outlive the callback that created them -- see module docstring).
    conn.execute("DROP TABLE IF EXISTS active_turns")
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(deferred_finalizes)").fetchall()}
    if cols and "claim_id" not in cols:
        # Old schema keyed by session_id alone, which let one session's
        # stale deferred row resolve against a *different*, later claim.
        conn.execute("DROP TABLE deferred_finalizes")
    # finalize_audit is a real audit trail, not ephemeral coordination state:
    # extend it in place rather than dropping recorded decisions.
    audit_cols = {row["name"] for row in conn.execute("PRAGMA table_info(finalize_audit)").fetchall()}
    if audit_cols:
        for column, ddl in (
            ("disposition", "ALTER TABLE finalize_audit ADD COLUMN disposition TEXT"),
            ("durable_terminal", "ALTER TABLE finalize_audit ADD COLUMN durable_terminal INTEGER"),
            ("evidence", "ALTER TABLE finalize_audit ADD COLUMN evidence TEXT"),
        ):
            if column not in audit_cols:
                conn.execute(ddl)


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    _migrate(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS claims (
            claim_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            workspace TEXT,
            source_workspace TEXT,
            kanban_task_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','released','expired')),
            acquired_at INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            released_at INTEGER,
            release_summary TEXT
        );
        CREATE TABLE IF NOT EXISTS claim_targets (
            target TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_claims_session_status
            ON claims(session_id, status);
        CREATE TABLE IF NOT EXISTS claim_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,
            event TEXT NOT NULL,
            occurred_at INTEGER NOT NULL,
            detail TEXT
        );
        -- One row per live host execution-turn lease (agent/execution_turn.py),
        -- consumed via on_execution_turn_begin/renew/end. Liveness is derived
        -- from expires_at (a renewable lease), never from raw PID/thread
        -- inspection -- so it is correct across processes, threads, and PID
        -- reuse without depending on OS-level process identity.
        CREATE TABLE IF NOT EXISTS execution_leases (
            lease_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            holder_token TEXT NOT NULL,
            pid INTEGER,
            boot_id TEXT,
            renew_interval_seconds REAL NOT NULL,
            last_seen_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_execution_leases_session
            ON execution_leases(session_id);
        -- At most one deferred finalize per claim (not per session): a
        -- session that releases/expires one claim and immediately acquires
        -- a new one must never have the old claim's deferred row resolve
        -- against the new claim.
        CREATE TABLE IF NOT EXISTS deferred_finalizes (
            claim_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            requested_at INTEGER NOT NULL,
            summary TEXT NOT NULL,
            reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_deferred_finalizes_session
            ON deferred_finalizes(session_id);
        -- Unconditional structured record of every finalize *decision*,
        -- including "no active claim" -- so an incident is self-documenting
        -- without relying on scattered log correlation or claim_events
        -- (which requires a claim_id to exist at all).
        CREATE TABLE IF NOT EXISTS finalize_audit (
            decision_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            claim_id TEXT,
            reason TEXT,
            outcome TEXT NOT NULL,
            observed_leases TEXT NOT NULL,
            occurred_at INTEGER NOT NULL,
            disposition TEXT,
            durable_terminal INTEGER,
            evidence TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_finalize_audit_session
            ON finalize_audit(session_id);
        """
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def normalize_target(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("each target must be a string")
    value = value.strip()
    match = TARGET_RE.fullmatch(value)
    if not match:
        raise ValueError("targets must start with repo:, project:, external:, or system:")
    kind, raw = match.groups()
    raw = raw.strip()
    if not raw:
        raise ValueError("target value cannot be empty")
    if kind == "repo":
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("repo targets must use an absolute path")
        raw = str(path.resolve())
    else:
        raw = re.sub(r"\s+", "-", raw.lower())
    return f"{kind}:{raw}"


def normalize_targets(values: Iterable[str]) -> list[str]:
    out = sorted({normalize_target(value) for value in values})
    if not out:
        raise ValueError("at least one target is required")
    if len(out) > 16:
        raise ValueError("a claim may contain at most 16 targets")
    return out


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
    force_shared_home: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = None
    if force_shared_home:
        env = os.environ.copy()
        env["HERMES_HOME"] = str(shared_root())
        env.pop("HERMES_PROFILE", None)
        env.pop("HERMES_PROFILE_NAME", None)
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git_output(workspace: Path, *args: str) -> str:
    result = _run(["git", "-C", str(workspace), *args])
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git workspace inspection failed")
    return result.stdout.strip()


def _is_git_workspace(workspace: Path) -> bool:
    result = _run(["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def _is_secondary_worktree(workspace: Path) -> bool:
    git_dir_raw = _git_output(workspace, "rev-parse", "--git-dir")
    common_raw = _git_output(workspace, "rev-parse", "--git-common-dir")
    git_dir = (workspace / git_dir_raw).resolve() if not Path(git_dir_raw).is_absolute() else Path(git_dir_raw).resolve()
    common = (workspace / common_raw).resolve() if not Path(common_raw).is_absolute() else Path(common_raw).resolve()
    return git_dir != common


def _slug(summary: str) -> str:
    value = SAFE_SLUG_RE.sub("-", summary.lower()).strip("-")[:36]
    return value or "work"


@dataclass(frozen=True)
class WorkspaceResult:
    workspace: str | None
    source_workspace: str | None
    created: bool
    branch: str | None


def prepare_workspace(workspace: str | None, summary: str, claim_id: str, create_worktree: bool) -> WorkspaceResult:
    if not workspace:
        return WorkspaceResult(None, None, False, None)
    source = Path(workspace).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"workspace does not exist or is not a directory: {source}")
    if not _is_git_workspace(source):
        raise ValueError("mutating shared directories must be Git repositories so isolation is enforceable")
    repo_root = Path(_git_output(source, "rev-parse", "--show-toplevel")).resolve()
    if _is_secondary_worktree(repo_root):
        return WorkspaceResult(str(repo_root), str(repo_root), False, _git_output(repo_root, "branch", "--show-current"))
    if not create_worktree:
        raise ValueError("the primary checkout cannot be claimed for mutation; enable create_worktree")
    short = claim_id.split("_")[-1][:8]
    branch = f"claim/{_slug(summary)}-{short}"
    destination = shared_root() / "worktrees" / "claims" / f"{_slug(summary)}-{short}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        ["git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(destination), "HEAD"],
        timeout=90,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "failed to create isolated Git worktree")
    return WorkspaceResult(str(destination.resolve()), str(repo_root), True, branch)


def _cleanup_created_workspace(prepared: WorkspaceResult) -> None:
    if not prepared.created or not prepared.workspace or not prepared.source_workspace:
        return
    _run(["git", "-C", prepared.source_workspace, "worktree", "remove", prepared.workspace], timeout=60)
    if prepared.branch:
        _run(["git", "-C", prepared.source_workspace, "branch", "-D", prepared.branch], timeout=30)


def _kanban_create(claim_id: str, session_id: str, summary: str, targets: list[str], prepared: WorkspaceResult, expires_at: int) -> str:
    body = json.dumps(
        {
            "claim_id": claim_id,
            "session_id": session_id,
            "targets": targets,
            "workspace": prepared.workspace,
            "source_workspace": prepared.source_workspace,
            "expires_at_epoch": expires_at,
        },
        sort_keys=True,
    )
    argv = [
        "hermes", "kanban", "--board", BOARD, "create", f"Claim: {summary[:100]}",
        "--body", body,
        "--created-by", "work-claims",
        "--initial-status", "running",
        "--idempotency-key", claim_id,
        "--json",
    ]
    if prepared.workspace:
        argv.extend(["--workspace", f"dir:{prepared.workspace}"])
    result = _run(argv, timeout=45, force_shared_home=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Kanban claim creation failed")
    payload = json.loads(result.stdout)
    task_id = payload.get("id") or payload.get("task_id")
    if not task_id:
        raise RuntimeError("Kanban claim creation returned no task id")
    return str(task_id)


def _kanban_complete(task_id: str, summary: str) -> str | None:
    result = _run([
        "hermes", "kanban", "--board", BOARD, "complete", task_id,
        "--summary", summary[:500],
    ], timeout=45, force_shared_home=True)
    if result.returncode:
        return result.stderr.strip() or result.stdout.strip() or "Kanban completion failed"
    return None


def _expire_stale(conn: sqlite3.Connection, now: int) -> None:
    stale = conn.execute(
        "SELECT claim_id FROM claims WHERE status='active' AND expires_at <= ?", (now,)
    ).fetchall()
    for row in stale:
        claim_id = row["claim_id"]
        conn.execute("DELETE FROM claim_targets WHERE claim_id=?", (claim_id,))
        conn.execute(
            "UPDATE claims SET status='expired', released_at=?, release_summary='TTL expired' WHERE claim_id=?",
            (now, claim_id),
        )
        conn.execute(
            "INSERT INTO claim_events(claim_id,event,occurred_at,detail) VALUES(?,?,?,?)",
            (claim_id, "expired", now, "TTL expired"),
        )
        # Retire any deferred finalize for a claim that just expired: nothing
        # must resolve against it later.
        conn.execute("DELETE FROM deferred_finalizes WHERE claim_id=?", (claim_id,))


def acquire(session_id: str, summary: str, targets: Iterable[str], workspace: str | None = None,
            ttl_minutes: int = DEFAULT_TTL_MINUTES, create_worktree: bool = True) -> dict[str, Any]:
    if not session_id:
        return {"success": False, "error": "No stable Hermes session id was available; claim refused."}
    summary = (summary or "").strip()
    if not summary:
        return {"success": False, "error": "summary is required"}
    ttl_minutes = int(ttl_minutes)
    if not 15 <= ttl_minutes <= MAX_TTL_MINUTES:
        return {"success": False, "error": f"ttl_minutes must be between 15 and {MAX_TTL_MINUTES}"}
    try:
        normalized = normalize_targets(targets)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    claim_id = f"claim_{uuid.uuid4().hex}"
    now = int(time.time())
    expires_at = now + ttl_minutes * 60
    conn = _connect()
    prepared = WorkspaceResult(None, None, False, None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        existing_session = conn.execute(
            "SELECT claim_id FROM claims WHERE session_id=? AND status='active'", (session_id,)
        ).fetchone()
        if existing_session:
            conn.execute("ROLLBACK")
            return {"success": False, "error": "This session already has an active claim; release it before acquiring another.", "claim_id": existing_session["claim_id"]}
        # No active claim for this session (just proven above): any deferred
        # row left over belongs to a claim this session already replaced.
        # Retire it now so it can never resolve against the claim below.
        conn.execute("DELETE FROM deferred_finalizes WHERE session_id=?", (session_id,))
        placeholders = ",".join("?" for _ in normalized)
        conflicts = conn.execute(
            f"SELECT ct.target,c.claim_id,c.session_id,c.summary,c.expires_at FROM claim_targets ct JOIN claims c ON c.claim_id=ct.claim_id WHERE ct.target IN ({placeholders}) AND c.status='active'",
            normalized,
        ).fetchall()
        if conflicts:
            conn.execute("ROLLBACK")
            return {"success": False, "error": "One or more targets are already claimed.", "conflicts": [dict(row) for row in conflicts]}
        prepared = prepare_workspace(workspace, summary, claim_id, bool(create_worktree))
        task_id = _kanban_create(claim_id, session_id, summary, normalized, prepared, expires_at)
        conn.execute(
            "INSERT INTO claims(claim_id,session_id,summary,workspace,source_workspace,kanban_task_id,status,acquired_at,heartbeat_at,expires_at) VALUES(?,?,?,?,?,?,'active',?,?,?)",
            (claim_id, session_id, summary, prepared.workspace, prepared.source_workspace, task_id, now, now, expires_at),
        )
        conn.executemany(
            "INSERT INTO claim_targets(target,claim_id) VALUES(?,?)",
            [(target, claim_id) for target in normalized],
        )
        conn.execute(
            "INSERT INTO claim_events(claim_id,event,occurred_at,detail) VALUES(?,?,?,?)",
            (claim_id, "acquired", now, json.dumps({"targets": normalized}, sort_keys=True)),
        )
        conn.execute("COMMIT")
        return {
            "success": True,
            "claim_id": claim_id,
            "kanban_board": BOARD,
            "kanban_task_id": task_id,
            "targets": normalized,
            "workspace": prepared.workspace,
            "source_workspace": prepared.source_workspace,
            "worktree_created": prepared.created,
            "branch": prepared.branch,
            "expires_at_epoch": expires_at,
            "instruction": "Use the returned workspace as the absolute path/workdir for every mutation. Release the claim when verified work is complete.",
        }
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _cleanup_created_workspace(prepared)
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()


def active_claim(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    now = int(time.time())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        row = conn.execute(
            "SELECT * FROM claims WHERE session_id=? AND status='active'", (session_id,)
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        targets = [r["target"] for r in conn.execute(
            "SELECT target FROM claim_targets WHERE claim_id=? ORDER BY target", (row["claim_id"],)
        ).fetchall()]
        conn.execute("COMMIT")
        result = dict(row)
        result["targets"] = targets
        return result
    finally:
        conn.close()


def renew(session_id: str, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> dict[str, Any]:
    ttl_minutes = int(ttl_minutes)
    if not 15 <= ttl_minutes <= MAX_TTL_MINUTES:
        return {"success": False, "error": f"ttl_minutes must be between 15 and {MAX_TTL_MINUTES}"}
    now = int(time.time())
    expires_at = now + ttl_minutes * 60
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        row = conn.execute("SELECT claim_id FROM claims WHERE session_id=? AND status='active'", (session_id,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return {"success": False, "error": "No active claim for this session."}
        conn.execute("UPDATE claims SET heartbeat_at=?,expires_at=? WHERE claim_id=?", (now, expires_at, row["claim_id"]))
        conn.execute("INSERT INTO claim_events(claim_id,event,occurred_at,detail) VALUES(?,?,?,?)", (row["claim_id"], "renewed", now, str(expires_at)))
        conn.execute("COMMIT")
        return {"success": True, "claim_id": row["claim_id"], "expires_at_epoch": expires_at}
    finally:
        conn.close()


def release(session_id: str, summary: str = "Work verified and claim released") -> dict[str, Any]:
    now = int(time.time())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        row = conn.execute("SELECT claim_id,kanban_task_id FROM claims WHERE session_id=? AND status='active'", (session_id,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return {"success": False, "error": "No active claim for this session."}
        conn.execute("DELETE FROM claim_targets WHERE claim_id=?", (row["claim_id"],))
        conn.execute("UPDATE claims SET status='released',released_at=?,release_summary=? WHERE claim_id=?", (now, summary[:1000], row["claim_id"]))
        conn.execute("INSERT INTO claim_events(claim_id,event,occurred_at,detail) VALUES(?,?,?,?)", (row["claim_id"], "released", now, summary[:1000]))
        # An explicit, deliberate release always retires any deferred intent
        # for the same claim -- there is nothing left for it to resolve.
        conn.execute("DELETE FROM deferred_finalizes WHERE claim_id=?", (row["claim_id"],))
        conn.execute("COMMIT")
        warning = _kanban_complete(row["kanban_task_id"], summary) if row["kanban_task_id"] else None
        result: dict[str, Any] = {"success": True, "claim_id": row["claim_id"], "kanban_task_id": row["kanban_task_id"]}
        if warning:
            result["warning"] = f"The technical lock was released, but the Kanban mirror could not be completed: {warning}"
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Execution-turn lease consumption (agent/execution_turn.py)
#
# Liveness for "is a turn actually running for this session" is bound
# entirely to the host's own renewable, holder-token-owned execution-turn
# lease -- never to the identity of whichever thread happened to run a hook
# callback. The three hooks below (on_execution_turn_begin/renew/end) are
# registered in hermes_cli.plugins._HOOK_CALLER_THREAD_HOOKS, so they always
# run synchronously on the real execution thread and are never abandoned by
# the hook-timeout worker; on_execution_turn_end fires from the host's own
# unconditional turn ``finally`` for every outcome -- success, empty,
# tool-only, interrupted, failed, an escaping exception, and both
# durable-lease early returns -- unlike the old post_llm_call, which only
# fired when ``final_response and not interrupted``.
# --------------------------------------------------------------------------


def _new_decision_id() -> str:
    return f"fdec_{uuid.uuid4().hex}"


def _lease_expiry(now: int, renew_interval_seconds: float) -> int:
    interval = max(float(renew_interval_seconds or 0.0), 0.0)
    window = max(interval * _LEASE_EXPIRY_MULTIPLIER, _LEASE_EXPIRY_FLOOR_SECONDS)
    return now + int(window)


def _prune_expired_leases(conn: sqlite3.Connection, now: int) -> list[str]:
    """Self-healing crash backstop: a lease whose owner never called end()
    (SIGKILL, power loss -- the same cases ``execution_turn.py`` documents
    as skipping its own ``finally``) is dropped once its renewable expiry
    has passed, so it cannot block a finalize decision forever.

    Returns the pruned lease ids so a finalize decision can record exactly
    what it removed, rather than losing that state silently.
    """
    pruned = [
        row["lease_id"]
        for row in conn.execute(
            "SELECT lease_id FROM execution_leases WHERE expires_at<=? ORDER BY lease_id", (now,)
        ).fetchall()
    ]
    conn.execute("DELETE FROM execution_leases WHERE expires_at<=?", (now,))
    return pruned


def _holder_identity(pid: Any, boot_id: Any) -> tuple[int, str]:
    """Normalize the process half of a holder identity.

    A caller that supplies neither (a direct in-process ``core`` call) is
    pinned to *this* process incarnation, so it stays self-consistent across
    begin/renew/end while remaining distinguishable from any other process.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        resolved_pid = os.getpid()
    else:
        resolved_pid = pid
    resolved_boot = str(boot_id or "") or _BOOT_ID
    return resolved_pid, resolved_boot


def _holder_fingerprint(holder_token: Any) -> str:
    """A stable, non-reversible label for a holder token.

    Ownership secrets must never reach the audit trail or an error message;
    a fingerprint still lets two records be compared for "same holder?".
    """
    token = str(holder_token or "")
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _session_has_live_lease(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM execution_leases WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
    return row is not None


def session_has_live_execution_lease(session_id: str) -> bool:
    """Public, self-contained liveness read -- for diagnostics/tests."""
    if not session_id:
        return False
    now = int(time.time())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _prune_expired_leases(conn, now)
        live = _session_has_live_lease(conn, session_id)
        conn.execute("COMMIT")
        return live
    finally:
        conn.close()


def on_execution_turn_begin(
    *,
    session_id: str = "",
    turn_id: str = "",
    lease_id: str = "",
    holder_token: str = "",
    pid: int | None = None,
    boot_id: str = "",
    renew_interval_seconds: float = 0.0,
    **_: Any,
) -> None:
    """Admit one execution-turn lease, create-only per holder identity.

    A single ``BEGIN IMMEDIATE`` transaction compares the stored row against
    the full incoming identity and then either inserts (no row), refreshes
    (the exact same holder re-announcing itself -- a retried admission is
    idempotent), or fails closed with :class:`LeaseIdentityConflict`. The
    previous ``INSERT OR REPLACE`` keyed on ``lease_id`` alone let anything
    replaying a live lease id overwrite the holder identity and leave the
    real owner unable to renew or end its own turn.
    """
    if not session_id or not turn_id or not lease_id or not holder_token:
        return
    resolved_pid, resolved_boot = _holder_identity(pid, boot_id)
    now = int(time.time())
    conflict: str | None = None
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _prune_expired_leases(conn, now)
        existing = conn.execute(
            "SELECT session_id,turn_id,holder_token,pid,boot_id FROM execution_leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO execution_leases"
                "(lease_id,session_id,turn_id,holder_token,pid,boot_id,renew_interval_seconds,last_seen_at,expires_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    lease_id, session_id, turn_id, holder_token, resolved_pid, resolved_boot,
                    float(renew_interval_seconds or 0.0), now, _lease_expiry(now, renew_interval_seconds),
                ),
            )
        elif (
            existing["session_id"] == session_id
            and existing["turn_id"] == turn_id
            and existing["holder_token"] == holder_token
            and existing["pid"] == resolved_pid
            and existing["boot_id"] == resolved_boot
        ):
            conn.execute(
                "UPDATE execution_leases SET renew_interval_seconds=?, last_seen_at=?, expires_at=? WHERE lease_id=?",
                (float(renew_interval_seconds or 0.0), now, _lease_expiry(now, renew_interval_seconds), lease_id),
            )
        else:
            conflict = (
                f"execution lease {lease_id!r} is already held by "
                f"session={existing['session_id']!r} turn={existing['turn_id']!r} "
                f"holder={_holder_fingerprint(existing['holder_token'])} "
                f"pid={existing['pid']} boot={existing['boot_id']}; refusing a begin from "
                f"session={session_id!r} turn={turn_id!r} "
                f"holder={_holder_fingerprint(holder_token)} pid={resolved_pid} boot={resolved_boot}"
            )
        conn.execute("ROLLBACK" if conflict else "COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    if conflict:
        raise LeaseIdentityConflict(conflict)


def on_execution_turn_renew(
    *,
    session_id: str = "",
    turn_id: str = "",
    lease_id: str = "",
    holder_token: str = "",
    pid: int | None = None,
    boot_id: str = "",
    renew_interval_seconds: float = 0.0,
    **_: Any,
) -> None:
    """Refresh a lease, only for the exact identity that opened it.

    Identity is the whole tuple (lease, session, turn, holder token, pid,
    boot id): a renew that differs in any component -- a replayed lease id
    from another process, a PID reused after a restart, a forged token --
    matches no row and therefore changes nothing.
    """
    if not lease_id or not holder_token:
        return
    resolved_pid, resolved_boot = _holder_identity(pid, boot_id)
    now = int(time.time())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE execution_leases SET last_seen_at=?, expires_at=? WHERE lease_id=?"
            " AND session_id=? AND turn_id=? AND holder_token=? AND pid IS ? AND boot_id IS ?",
            (
                now, _lease_expiry(now, renew_interval_seconds), lease_id,
                session_id, turn_id, holder_token, resolved_pid, resolved_boot,
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _resolve_deferred_for_idle_session(conn: sqlite3.Connection, session_id: str, now: int) -> dict[str, Any] | None:
    """Exactly-once resolution of a deferred finalize once its session has
    gone genuinely idle (checked and cleared inside the caller's own
    transaction, so two concurrent resolvers can never both act on it).
    """
    if _session_has_live_lease(conn, session_id):
        return None
    row = conn.execute(
        "SELECT claim_id, decision_id, summary, reason FROM deferred_finalizes WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    claim_id = row["claim_id"]
    # Delete first: whichever concurrent resolver observes this row wins
    # exactly once, regardless of what it finds next.
    conn.execute("DELETE FROM deferred_finalizes WHERE claim_id=?", (claim_id,))
    claim = conn.execute(
        "SELECT kanban_task_id FROM claims WHERE claim_id=? AND status='active'", (claim_id,)
    ).fetchone()
    if claim is None:
        _write_finalize_audit(
            conn,
            decision_id=_new_decision_id(),
            session_id=session_id,
            claim_id=claim_id,
            reason=row["reason"],
            outcome="deferred_resolution_claim_gone",
            disposition=PRESERVE,
            durable_terminal=True,
            observed_leases=[],
            evidence={"resolved_decision_id": row["decision_id"], "observed_at": now},
            now=now,
        )
        return None
    summary = row["summary"]
    conn.execute("DELETE FROM claim_targets WHERE claim_id=?", (claim_id,))
    conn.execute(
        "UPDATE claims SET status='released', released_at=?, release_summary=? WHERE claim_id=? AND status='active'",
        (now, summary[:1000], claim_id),
    )
    conn.execute(
        "INSERT INTO claim_events(claim_id,event,occurred_at,detail) VALUES(?,?,?,?)",
        (claim_id, "released", now, json.dumps({"summary": summary, "resolved_decision_id": row["decision_id"]}, sort_keys=True)),
    )
    _write_finalize_audit(
        conn,
        decision_id=_new_decision_id(),
        session_id=session_id,
        claim_id=claim_id,
        reason=row["reason"],
        outcome="deferred_resolved_released",
        disposition=RELEASE,
        durable_terminal=True,
        observed_leases=[],
        evidence={"resolved_decision_id": row["decision_id"], "observed_at": now},
        now=now,
    )
    return {"claim_id": claim_id, "kanban_task_id": claim["kanban_task_id"], "summary": summary}


def on_execution_turn_end(
    *,
    session_id: str = "",
    turn_id: str = "",
    lease_id: str = "",
    holder_token: str = "",
    pid: int | None = None,
    boot_id: str = "",
    outcome: str = "",
    **_: Any,
) -> list[dict[str, Any]]:
    """Clear one turn's execution lease and, if the session has gone truly
    idle, resolve any finalize that was deferred while it ran -- exactly
    once. Fires for every turn outcome (including ``rebound``, the mid-turn
    lease handoff when a session id rotates: the old session id genuinely
    has no more live turns once that happens, so resolving against it is
    correct, not a special case).
    """
    del outcome
    if not session_id or not lease_id or not holder_token:
        return []
    resolved_pid, resolved_boot = _holder_identity(pid, boot_id)
    now = int(time.time())
    conn = _connect()
    resolved: dict[str, Any] | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Identity-checked delete: only the exact holder that opened this
        # lease (same session, turn, token, pid and boot id) can close it, so
        # neither a foreign/stale token nor a PID reused by a later process
        # can delete another turn's row out from under it.
        conn.execute(
            "DELETE FROM execution_leases WHERE lease_id=? AND session_id=? AND turn_id=?"
            " AND holder_token=? AND pid IS ? AND boot_id IS ?",
            (lease_id, session_id, turn_id, holder_token, resolved_pid, resolved_boot),
        )
        _prune_expired_leases(conn, now)
        resolved = _resolve_deferred_for_idle_session(conn, session_id, now)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    if resolved is None:
        return []
    warning = _kanban_complete(resolved["kanban_task_id"], resolved["summary"]) if resolved.get("kanban_task_id") else None
    result: dict[str, Any] = {"success": True, "claim_id": resolved["claim_id"], "kanban_task_id": resolved.get("kanban_task_id")}
    if warning:
        result["warning"] = f"The technical lock was released, but the Kanban mirror could not be completed: {warning}"
    return [result]


def _lease_snapshot(conn: sqlite3.Connection, session_id: str, now: int) -> list[dict[str, Any]]:
    """Every same-session lease row exactly as it stands *before* any prune.

    Freshness is derived here (age since the last renewal, remaining TTL,
    live/stale, and whether this process opened the row at all) so a finalize
    decision can be audited from its own record without re-reading a table
    the same transaction is about to modify.
    """
    snapshot: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM execution_leases WHERE session_id=? ORDER BY lease_id", (session_id,)
    ).fetchall():
        last_seen_at = int(row["last_seen_at"])
        expires_at = int(row["expires_at"])
        snapshot.append({
            "lease_id": row["lease_id"],
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "holder_fingerprint": _holder_fingerprint(row["holder_token"]),
            "pid": row["pid"],
            "boot_id": row["boot_id"],
            "renew_interval_seconds": row["renew_interval_seconds"],
            "last_seen_at": last_seen_at,
            "expires_at": expires_at,
            "age_seconds": now - last_seen_at,
            "expires_in_seconds": expires_at - now,
            "live": expires_at > now,
            # Evidence only -- a foreign row is never treated as weaker or
            # stronger proof than one this process opened.
            "foreign": (row["pid"], row["boot_id"]) != (os.getpid(), _BOOT_ID),
        })
    return snapshot


def _claim_snapshot(conn: sqlite3.Connection, session_id: str, now: int) -> dict[str, Any] | None:
    """This session's active claim with its heartbeat/expiry freshness, read
    before the TTL sweep so an expired-but-not-yet-swept claim is recorded as
    the evidence it is rather than vanishing from the record."""
    row = conn.execute(
        "SELECT claim_id,status,acquired_at,heartbeat_at,expires_at,kanban_task_id "
        "FROM claims WHERE session_id=? AND status='active'", (session_id,)
    ).fetchone()
    if row is None:
        return None
    heartbeat_at = int(row["heartbeat_at"])
    expires_at = int(row["expires_at"])
    return {
        "claim_id": row["claim_id"],
        "status": row["status"],
        "acquired_at": int(row["acquired_at"]),
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": now - heartbeat_at,
        "expires_at": expires_at,
        "expires_in_seconds": expires_at - now,
        "expired": expires_at <= now,
    }


def _write_finalize_audit(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    session_id: str,
    claim_id: str | None,
    reason: str,
    outcome: str,
    disposition: str,
    durable_terminal: bool,
    observed_leases: list[str],
    evidence: dict[str, Any] | None,
    now: int,
) -> None:
    conn.execute(
        "INSERT INTO finalize_audit"
        "(decision_id,session_id,claim_id,reason,outcome,observed_leases,occurred_at,disposition,durable_terminal,evidence)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            decision_id, session_id, claim_id, reason, outcome, json.dumps(observed_leases), now,
            disposition, int(bool(durable_terminal)),
            json.dumps(evidence, sort_keys=True) if evidence is not None else None,
        ),
    )


def finalization_decision(
    session_id: str,
    reason: str,
    durable_terminal: bool,
    *,
    summary: str = _DEFAULT_FINALIZE_SUMMARY,
) -> dict[str, Any]:
    """Decide, in one transaction, whether to preserve or release a claim.

    The whole decision -- snapshotting every same-session lease *before* any
    prune, snapshotting the claim's own heartbeat/expiry, pruning expired
    leases, sweeping expired claims, recording the structured evidence, and
    the resulting defer/release mutation with its claim CAS
    (``status='active'`` in the UPDATE) -- runs inside a single
    ``BEGIN IMMEDIATE``, so no other writer can interleave a
    begin/renew/end/release/expire part-way through and no partial state can
    survive a failure.

    Two rules decide it:

    * A live same-session execution lease always preserves the claim. A turn
      is running; finalizing now is the proven premature-finalization bug.
    * Nothing else is proof that the session is over. A stale lease, another
      process's lease, and no lease row at all are all merely inconclusive,
      so a claim is only ever released when the caller passes an explicit
      ``durable_terminal`` signal (see :func:`is_durable_terminal_reason`).
      Otherwise it is preserved and left to its own TTL.

    Returns the decision: ``disposition`` is :data:`PRESERVE` or
    :data:`RELEASE`, and ``evidence`` is the same structured record written
    to ``finalize_audit``.
    """
    now = int(time.time())
    decision_id = _new_decision_id()
    durable_terminal = bool(durable_terminal)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        leases = _lease_snapshot(conn, session_id, now)
        claim_before = _claim_snapshot(conn, session_id, now)
        pruned = _prune_expired_leases(conn, now)
        _expire_stale(conn, now)
        live_leases = [lease for lease in leases if lease["live"]]
        evidence: dict[str, Any] = {
            "reason": reason,
            "durable_terminal": durable_terminal,
            "observed_at": now,
            "leases": leases,
            "lease_counts": {
                "total": len(leases),
                "live": len(live_leases),
                "stale": len(leases) - len(live_leases),
                "foreign": sum(1 for lease in leases if lease["foreign"]),
            },
            "pruned_lease_ids": pruned,
            "claim": claim_before,
        }
        observed = [lease["lease_id"] for lease in leases]
        claim_row = conn.execute(
            "SELECT claim_id, kanban_task_id FROM claims WHERE session_id=? AND status='active'",
            (session_id,),
        ).fetchone()
        decision: dict[str, Any] = {
            "decision_id": decision_id,
            "session_id": session_id,
            "reason": reason,
            "durable_terminal": durable_terminal,
            "summary": summary,
            "claim_id": claim_row["claim_id"] if claim_row else None,
            "kanban_task_id": claim_row["kanban_task_id"] if claim_row else None,
            "disposition": PRESERVE,
            "outcome": "no_claim",
            "evidence": evidence,
        }
        if claim_row is not None:
            claim_id = claim_row["claim_id"]
            if live_leases and durable_terminal:
                # Authorised, but a turn is still running: record the intent
                # so the turn's own end resolves it exactly once.
                decision["outcome"] = "deferred"
                conn.execute(
                    "INSERT INTO deferred_finalizes(claim_id,session_id,decision_id,requested_at,summary,reason) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(claim_id) DO UPDATE SET "
                    "decision_id=excluded.decision_id, requested_at=excluded.requested_at, summary=excluded.summary, reason=excluded.reason",
                    (claim_id, session_id, decision_id, now, summary, reason),
                )
                conn.execute(
                    "INSERT INTO claim_events(claim_id,event,occurred_at,detail) VALUES(?,?,?,?)",
                    (claim_id, "finalize_deferred", now, json.dumps({"reason": reason, "summary": summary, "decision_id": decision_id}, sort_keys=True)),
                )
            elif live_leases:
                decision["outcome"] = "preserved_live_lease"
            elif not durable_terminal:
                decision["outcome"] = "preserved_no_durable_terminal"
            else:
                decision["outcome"] = "released"
                decision["disposition"] = RELEASE
                conn.execute("DELETE FROM claim_targets WHERE claim_id=?", (claim_id,))
                conn.execute(
                    "UPDATE claims SET status='released', released_at=?, release_summary=? WHERE claim_id=? AND status='active'",
                    (now, summary[:1000], claim_id),
                )
                conn.execute(
                    "INSERT INTO claim_events(claim_id,event,occurred_at,detail) VALUES(?,?,?,?)",
                    (claim_id, "released", now, json.dumps({"summary": summary, "reason": reason, "decision_id": decision_id}, sort_keys=True)),
                )
                conn.execute("DELETE FROM deferred_finalizes WHERE claim_id=?", (claim_id,))
        _write_finalize_audit(
            conn,
            decision_id=decision_id,
            session_id=session_id,
            claim_id=decision["claim_id"],
            reason=reason,
            outcome=decision["outcome"],
            disposition=decision["disposition"],
            durable_terminal=durable_terminal,
            observed_leases=observed,
            evidence=evidence,
            now=now,
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return decision


def mirror_released_claim(decision: dict[str, Any]) -> str | None:
    """Complete the Kanban mirror for an explicitly released claim.

    Deliberately outside :func:`finalization_decision`'s transaction: the
    mirror is a best-effort external side effect and must never hold the
    coordination database open across a subprocess call.
    """
    if decision.get("disposition") != RELEASE or not decision.get("kanban_task_id"):
        return None
    return _kanban_complete(decision["kanban_task_id"], decision.get("summary") or _DEFAULT_FINALIZE_SUMMARY)


def release_all_for_session(
    session_id: str,
    summary: str,
    *,
    reason: str = "",
    durable_terminal: bool = False,
) -> list[dict[str, Any]]:
    """Run one :func:`finalization_decision` and act only on an explicit
    release. A preserved claim (live turn, or no durable terminal signal)
    returns no results and leaves the claim exactly as it was; every attempt
    is recorded in ``finalize_audit`` either way.
    """
    decision = finalization_decision(session_id, reason, durable_terminal, summary=summary)
    if decision["disposition"] != RELEASE:
        return []
    warning = mirror_released_claim(decision)
    result: dict[str, Any] = {
        "success": True,
        "claim_id": decision["claim_id"],
        "kanban_task_id": decision["kanban_task_id"],
        "decision_id": decision["decision_id"],
    }
    if warning:
        result["warning"] = f"The technical lock was released, but the Kanban mirror could not be completed: {warning}"
    return [result]


def path_within(path: str, root: str) -> bool:
    try:
        candidate = Path(path).expanduser().resolve()
        base = Path(root).expanduser().resolve()
        candidate.relative_to(base)
        return True
    except (ValueError, OSError):
        return False


def _mutation_targets(tool_name: str, args: dict[str, Any]) -> list[str] | None:
    """Return the paths a file-mutating tool call would touch, or None if
    ``tool_name`` carries no path-scoped targets at all.

    ``terminal`` is deliberately absent: its ``workdir`` describes only
    where the command starts, never where it writes, so it is confined by
    :func:`_contained_terminal_decision` in the kernel instead.
    """
    if tool_name in {"write_file", "patch"}:
        paths: list[str] = []
        if isinstance(args.get("path"), str):
            paths.append(args["path"])
        patch_text = args.get("patch")
        if isinstance(patch_text, str):
            paths.extend(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch_text, flags=re.MULTILINE))
        return paths
    return None


def _exact_case_entry(parent: Path, name: str) -> str | None:
    """Return ``name`` if a directory entry with that exact byte-for-byte
    name exists in ``parent``, else None. Never trusts a case-insensitive
    match -- APFS is case-insensitive-but-preserving by default, so a
    lexically different name can resolve to the same inode."""
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name == name:
                    return entry.name
    except OSError:
        return None
    return None


def _case_insensitive_alias(parent: Path, name: str) -> bool:
    try:
        with os.scandir(parent) as entries:
            return any(entry.name != name and entry.name.lower() == name.lower() for entry in entries)
    except OSError:
        return False


def _canonical_walk(path: Path, *, require_dir: bool) -> tuple[bool, tuple[int, int] | None, str | None]:
    """Walk ``path`` component-by-component from the filesystem root,
    verifying every ancestor is an exact case match for its on-disk
    directory entry and is not a symlink. Re-derived fresh on every call
    (never cached) so a swap between two calls (TOCTOU) is caught by the
    next verification rather than trusted from a stale prior result.

    Returns ``(ok, (st_dev, st_ino), reason)``.
    """
    if not path.is_absolute():
        return False, None, f"path is not absolute: {path}"
    current = Path(path.anchor)
    for part in path.relative_to(path.anchor).parts:
        real_name = _exact_case_entry(current, part)
        if real_name is None:
            if _case_insensitive_alias(current, part):
                return False, None, f"case-variant path alias rejected: {current / part}"
            return False, None, f"ancestor does not exist: {current / part}"
        current = current / part
        try:
            st = current.lstat()
        except OSError:
            return False, None, f"path vanished during verification: {current}"
        if stat.S_ISLNK(st.st_mode):
            return False, None, f"symlink rejected in path: {current}"
    try:
        st = current.lstat()
    except OSError:
        return False, None, f"path vanished during verification: {current}"
    if require_dir and not stat.S_ISDIR(st.st_mode):
        return False, None, f"not a directory: {current}"
    return True, (st.st_dev, st.st_ino), None


def _confined_by_ancestor_walk(candidate: str, workspace: Path, workspace_key: tuple[int, int]) -> tuple[bool, str | None]:
    """Confine ``candidate`` to ``workspace`` via a fresh, symlink- and
    case-alias-aware ancestor walk (not ``Path.resolve()``, which silently
    normalizes both). ``workspace`` itself must already have passed
    :func:`_canonical_walk`; ``workspace_key`` is its verified (st_dev,
    st_ino), re-checked at every descended component so a bind-mount
    aliasing a subdirectory onto a different device is caught."""
    if not isinstance(candidate, str) or not candidate.strip():
        return False, "no path was given"
    target = Path(candidate).expanduser()
    if not target.is_absolute():
        target = workspace / target
    try:
        rel_parts = target.relative_to(workspace).parts
    except ValueError:
        return False, f"path escapes the confined workspace: {candidate}"
    current = workspace
    for index, part in enumerate(rel_parts):
        is_last = index == len(rel_parts) - 1
        real_name = _exact_case_entry(current, part)
        if real_name is None:
            if _case_insensitive_alias(current, part):
                return False, f"case-variant path alias rejected: {current / part}"
            if is_last:
                # A not-yet-existing final component (new file/dir) is fine
                # as long as every ancestor above it was verified.
                return True, None
            return False, f"ancestor does not exist: {current / part}"
        current = current / part
        try:
            st = current.lstat()
        except OSError:
            return False, f"path vanished during verification: {current}"
        if stat.S_ISLNK(st.st_mode):
            return False, f"symlink rejected in path: {current}"
        if st.st_dev != workspace_key[0]:
            return False, f"mount/bind boundary crossed at: {current}"
        if is_last and stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            return False, f"hard-linked regular file rejected: {current}"
    return True, None


#: The file-mutating tools a dispatcher-scoped worker may use, confined to
#: its workspace by an ancestor walk.  ``terminal`` is handled separately:
#: its ``workdir`` is not a description of where it writes, so it needs the
#: OS sandbox rather than a path check.
_DISPATCHER_FILE_TOOLS = frozenset({"write_file", "patch"})

#: Tool families denied outright in dispatcher scope, whatever action they
#: are asked for.  These reach outside the workspace by construction: they
#: run code, rewrite the profile's own configuration, drive a browser or
#: desktop, edit long-lived memory/skills/cron, register MCP servers, or
#: start and steer other processes.  A worker's mandate is its own
#: workspace; none of this is inside it.
#:
#: Redundant with the default-deny fallthrough below, and kept anyway: it
#: names the families deliberately excluded rather than merely unlisted, so
#: removing a name is a visible decision, and it carries a message that
#: says *why* rather than "unrecognised".  Both the currently registered
#: names and the older aliases the plugin's own ``_is_mutating`` still
#: recognises appear, so a rename cannot quietly change a denial into an
#: "unrecognised".
_DISPATCHER_DENIED_TOOLS = frozenset({
    "execute_code",
    "skill_manage",
    "memory",
    "setup_mcp",
    "delegate_task",
    "process", "process_manage",
    "cronjob", "cronjob_manage",
    "project", "project_create", "desktop_project",
    "drive_preview",
    "computer_use",
})

#: Denied family prefixes, for tool sets whose members are enumerated by the
#: provider rather than fixed (``browser_click``, ``browser_navigate``, ...).
_DISPATCHER_DENIED_PREFIXES = ("browser_", "computer_", "desktop_", "mcp_")

#: The tools a dispatcher-scoped worker may use that change nothing outside
#: its own task: reading and searching, reporting progress on its Kanban
#: card, and looking things up.  This list is the *whole* of what dispatcher
#: scope permits besides the three confinable mutators above -- an
#: unrecognised tool is denied, because a gate that cannot name a tool
#: cannot know it is safe.  Erring here costs a worker one tool and a clear
#: message naming it; erring the other way costs the containment boundary.
_DISPATCHER_READ_ONLY_TOOLS = frozenset({
    "read_file", "search_files", "read_terminal", "close_terminal",
    "read_preview", "open_preview", "close_preview", "read_window_below",
    "session_search", "todo_list", "clarify", "show_tip", "focus_pane",
    "web_search", "web_extract", "x_search",
    "skill_view", "skills_list",
    "send_message", "react_to_message",
    "vision_analyze", "video_analyze",
    # Progressive-disclosure catalog reads describe the already-admitted
    # surface. The generic execution bridge (tool_call) remains default-deny.
    "tool_search", "tool_describe",
})

#: Kanban tools are the worker's own reporting channel -- completing,
#: commenting on and attaching to the very task it was spawned for is its
#: mandate, not an escape from it.
_DISPATCHER_ALLOWED_PREFIXES = ("kanban_",)


@dataclass(frozen=True)
class GateDecision:
    """One pre-tool decision.

    ``modified_args`` carries a *required* rewrite -- today, the terminal
    command re-written to run under OS containment.  A caller that cannot
    apply it must treat the call as blocked, never as allowed: the rewrite
    is the enforcement, not a hint.
    """

    allowed: bool
    reason: str | None = None
    modified_args: dict[str, Any] | None = None


def _resolve_dispatcher_scope() -> tuple[Path, tuple[int, int]] | str | None:
    """Resolve this process's verified dispatcher workspace, or say why not.

    Authority comes from exactly one place: the :class:`BoundIdentity` that
    ``agent.dispatcher_identity`` installed during the startup handshake.
    No environment variable and no ContextVar is consulted as authority --
    neither can carry it across a process boundary, which is the whole
    CV-A01 finding.  ``HERMES_KANBAN_TASK`` and ``HERMES_KANBAN_WORKSPACE``
    are not read here at all.

    Returns:
      * ``None`` -- no identity is in force for this scope: an ordinary
        session, a delegated child, an in-process cron job, a spawned
        subprocess, or any process merely carrying inherited Kanban env.
        The caller falls through to ordinary claim enforcement.
      * ``str`` -- an identity is bound but no longer valid (its run
        advanced, its workspace moved or vanished, its token expired, or it
        does not belong to this process). The string is the reason the
        caller must reject the mutation; it must NOT fall through, because
        a worker whose authority has been revoked is not an ordinary
        session that might hold a claim.
      * ``(workspace, workspace_key)`` -- a verified, canonical, existing
        workspace directory to confine mutations to.
    """
    try:
        from agent import dispatcher_identity
    except ImportError:
        return None

    identity = dispatcher_identity.get_bound()
    if identity is None:
        return None

    # Re-checked on every decision, never trusted from bind time: a task
    # whose run advanced, whose workspace moved, or whose token expired
    # must stop authorising writes at the very next tool call.
    reason = dispatcher_identity.revalidate(identity)
    if reason is not None:
        return f"dispatcher worker identity is no longer valid: {reason}"

    workspace = identity.workspace
    ok, key, reason = _canonical_walk(workspace, require_dir=True)
    if not ok or key is None:
        return f"dispatcher worker workspace failed verification: {reason}"
    if key != identity.workspace_key:
        return (
            "dispatcher worker workspace no longer matches the directory the "
            f"identity was bound to: {workspace}"
        )
    return workspace, key


def _confined_path_decision(
    tool_name: str, args: dict[str, Any], workspace: Path, workspace_key: tuple[int, int]
) -> GateDecision:
    """Confine ``write_file``/``patch`` targets to the worker's workspace."""
    targets = _mutation_targets(tool_name, args)
    kind = "File mutation"
    if not targets:
        return GateDecision(
            False,
            f"{kind} must stay inside the confined dispatcher workspace "
            f"{workspace}: no target path given",
        )
    for candidate in targets:
        ok, reason = _confined_by_ancestor_walk(candidate, workspace, workspace_key)
        if not ok:
            return GateDecision(
                False,
                f"{kind} must stay inside the confined dispatcher workspace "
                f"{workspace}: {reason}",
            )
    return GateDecision(True)


def _contained_terminal_decision(
    args: dict[str, Any], workspace: Path, workspace_key: tuple[int, int]
) -> GateDecision:
    """Confine a terminal call to the workspace, in the kernel.

    Three separate requirements, all fail-closed:

    1. an exact, existing, in-workspace ``workdir`` -- so the command starts
       inside the boundary rather than somewhere it can be relative to;
    2. no ``background`` -- a detached process outlives the decision that
       authorised it and cannot be re-checked when the identity is revoked;
    3. the command re-written to run under ``sandbox-exec`` with a profile
       generated for this exact workspace.  Requirement 3 is the one that
       actually holds: 1 and 2 only shape *what* is contained.
    """
    if args.get("background"):
        return GateDecision(
            False,
            "Terminal mutations in dispatcher scope must run in the foreground: "
            "a background process outlives the identity check that authorised it",
        )

    workdir = args.get("workdir")
    if not isinstance(workdir, str) or not workdir.strip():
        return GateDecision(
            False,
            "Terminal mutations must stay inside the confined dispatcher workspace "
            f"{workspace}: an explicit in-workspace workdir is required",
        )
    ok, reason = _confined_by_ancestor_walk(workdir, workspace, workspace_key)
    if not ok:
        return GateDecision(
            False,
            "Terminal mutations must stay inside the confined dispatcher workspace "
            f"{workspace}: {reason}",
        )
    ok, _, reason = _canonical_walk(Path(workdir), require_dir=True)
    if not ok:
        return GateDecision(
            False,
            "Terminal mutations require an existing in-workspace workdir "
            f"directory: {reason}",
        )

    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return GateDecision(False, "Terminal calls require a command to contain")

    try:
        from agent import command_containment
    except ImportError as exc:  # pragma: no cover - agent package is always present
        return GateDecision(
            False, f"OS command containment is unavailable, refusing to run: {exc}"
        )
    try:
        contained = command_containment.contain_command(command, workspace)
    except (
        command_containment.ContainmentUnavailable,
        command_containment.ProfileGenerationError,
        OSError,
    ) as exc:
        return GateDecision(
            False,
            "Terminal commands in dispatcher scope must run inside an OS sandbox "
            f"confined to {workspace}, and one could not be established: {exc}",
        )
    return GateDecision(True, modified_args={"command": contained})


def _leaves_nothing_outside_the_task(tool_name: str) -> bool:
    """True for the tools a worker may use that touch nothing it must not."""
    return (
        tool_name in _DISPATCHER_READ_ONLY_TOOLS
        or tool_name.startswith(_DISPATCHER_ALLOWED_PREFIXES)
    )


def _dispatcher_leaf_delegation_decision(args: dict[str, Any]) -> GateDecision:
    """Admit only the bounded, synchronous, no-tool dispatcher pilot."""
    action = str(args.get("action") or "spawn").strip().lower()
    if action != "spawn":
        return GateDecision(
            False,
            "delegate_task control actions are not available in dispatcher scope",
        )
    unexpected = set(args) - {"tasks", "action"}
    if unexpected:
        return GateDecision(
            False,
            "dispatcher delegation accepts only a tasks batch and optional action='spawn'",
        )
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return GateDecision(
            False,
            "dispatcher delegation requires a non-empty tasks batch; the legacy single-goal form is denied",
        )
    if len(tasks) > 2:
        return GateDecision(
            False,
            f"dispatcher delegation admits at most 2 leaf tasks; {len(tasks)} were provided and no child was spawned",
        )
    # Structured-output retry performs an additional provider turn outside the
    # ordinary child timeout wrapper, so it is not admitted in this pilot.
    allowed_task_keys = {"goal", "context"}
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            return GateDecision(False, f"dispatcher delegation task {index} must be an object")
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return GateDecision(False, f"dispatcher delegation task {index} requires a non-empty goal")
        if set(task) - allowed_task_keys:
            return GateDecision(
                False,
                f"dispatcher delegation task {index} contains unsupported fields; only goal and context are admitted",
            )
    # The host runtime consumes this private marker to force synchronous
    # execution, a tiny iteration budget, and an empty child tool surface.
    modified_args = dict(args)
    modified_args["_dispatcher_leaf_no_tools"] = True
    return GateDecision(True, modified_args=modified_args)


def finalize_dispatcher_delegation_args(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Re-authorize dispatcher delegation at the final host dispatch choke point.

    The ordinary pre-tool hook runs before tool-execution middleware.  A trusted
    middleware may replace the payload, so its earlier marker is not authority:
    when a dispatcher identity is bound we require the marker *and* repeat the
    complete shape and identity checks immediately before child construction.
    Dropped markers and rewritten payloads therefore fail closed.

    Returns ``(sanitized_args, restricted)``.  Ordinary non-dispatcher sessions
    are returned unchanged with ``restricted=False``.
    """
    scope = _resolve_dispatcher_scope()
    if scope is None:
        return dict(args), False
    if isinstance(scope, str):
        raise PermissionError(scope)
    if args.get("_dispatcher_leaf_no_tools") is not True:
        raise PermissionError(
            "dispatcher delegation lost its restricted-mode authorization before execution"
        )

    public_args = {key: value for key, value in args.items() if key != "_dispatcher_leaf_no_tools"}
    decision = _dispatcher_leaf_delegation_decision(public_args)
    if not decision.allowed or decision.modified_args is None:
        raise PermissionError(decision.reason or "dispatcher delegation was denied")
    # Return only the fields the policy reconstructed.  Never carry arbitrary
    # middleware additions through this authority boundary.
    return dict(decision.modified_args), True


def dispatcher_delegation_authority_reason() -> str | None:
    """Return a revocation reason while a restricted child is in flight."""
    scope = _resolve_dispatcher_scope()
    if isinstance(scope, tuple):
        return None
    if isinstance(scope, str):
        return scope
    return "dispatcher worker identity is no longer bound"


def _dispatcher_scope_decision(
    tool_name: str, args: dict[str, Any],
    workspace: Path, workspace_key: tuple[int, int],
) -> GateDecision:
    """Default-deny policy for a verified dispatcher worker.

    The worker's mandate is its own workspace, so a tool proceeds only if it
    is one this gate can confine to that workspace, or one it can name as
    leaving nothing outside the task behind.  Everything else -- including a
    tool nobody has classified yet -- is denied, which is what makes this
    default-deny rather than a blocklist with gaps in it.  The host's own
    mutating/non-mutating classification is deliberately not consulted: a
    gate that trusts it inherits its blind spots.
    """
    if tool_name == "terminal":
        return _contained_terminal_decision(args, workspace, workspace_key)
    if tool_name in _DISPATCHER_FILE_TOOLS:
        return _confined_path_decision(tool_name, args, workspace, workspace_key)
    if tool_name == "delegate_task":
        return _dispatcher_leaf_delegation_decision(args)
    if tool_name in _DISPATCHER_DENIED_TOOLS or tool_name.startswith(_DISPATCHER_DENIED_PREFIXES):
        return GateDecision(
            False,
            f"{tool_name} is not available in the confined dispatcher workspace "
            f"{workspace}: it cannot be confined to the assigned workspace",
        )
    if _leaves_nothing_outside_the_task(tool_name):
        return GateDecision(True)
    return GateDecision(
        False,
        f"{tool_name} is not available in the confined dispatcher workspace "
        f"{workspace}: dispatcher scope permits only tools it can confine to "
        "that workspace",
    )


def pre_tool_decision(
    session_id: str, tool_name: str, args: dict[str, Any], *, mutating: bool
) -> GateDecision:
    """The single authorization decision for one tool call.

    ``mutating`` is the host plugin's own classification.  It selects the
    ordinary claim-enforcement regime and is not consulted inside dispatcher
    scope, which is default-deny for every tool rather than only for the
    ones already known to mutate.
    """
    scope = _resolve_dispatcher_scope()
    if isinstance(scope, tuple):
        return _dispatcher_scope_decision(tool_name, args, *scope)
    if isinstance(scope, str):
        # A bound identity whose authority has been revoked. Deny on the same
        # default-deny terms as a valid one -- falling back to the host's
        # looser classification here would make revocation *widen* scope --
        # but leave reads and Kanban reporting so the worker can still say
        # what happened.
        if _leaves_nothing_outside_the_task(tool_name):
            return GateDecision(True)
        return GateDecision(False, scope)
    if not mutating:
        return GateDecision(True)
    allowed, reason = _claimed_session_mutation_allowed(session_id, tool_name, args)
    return GateDecision(allowed, reason)


def mutation_allowed(session_id: str, tool_name: str, args: dict[str, Any]) -> tuple[bool, str | None]:
    """Back-compatible tuple form of :func:`pre_tool_decision`.

    Callers using this form cannot apply a required rewrite, so a decision
    that carries one is reported as denied rather than silently allowed
    without its containment.
    """
    decision = pre_tool_decision(session_id, tool_name, args, mutating=True)
    if decision.allowed and decision.modified_args:
        return False, (
            f"{tool_name} requires a contained rewrite that this caller cannot apply"
        )
    return decision.allowed, decision.reason


def _claimed_session_mutation_allowed(
    session_id: str, tool_name: str, args: dict[str, Any]
) -> tuple[bool, str | None]:
    """Ordinary claim enforcement, unchanged since before CV-A01."""
    claim = active_claim(session_id)
    if not claim:
        return False, "No active cross-session work claim. Acquire one with work_claim_acquire before using mutating tools."
    workspace = claim.get("workspace")
    if tool_name in {"write_file", "patch"} and workspace:
        paths: list[str] = []
        if isinstance(args.get("path"), str):
            paths.append(args["path"])
        patch_text = args.get("patch")
        if isinstance(patch_text, str):
            paths.extend(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch_text, flags=re.MULTILINE))
        if not paths or any(not path_within(path, workspace) for path in paths):
            return False, f"File mutation is outside the claimed isolated workspace: {workspace}"
    if tool_name == "terminal" and workspace:
        workdir = args.get("workdir")
        if not isinstance(workdir, str) or not path_within(workdir, workspace):
            return False, f"Terminal mutations require workdir inside the claimed isolated workspace: {workspace}"
    return True, None
