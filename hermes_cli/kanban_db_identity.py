"""Process-bound identity rows for dispatcher-spawned Kanban workers."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

from hermes_cli.kanban_db_connect import write_txn


def record_worker_identity(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    workspace_path: str,
    worker_pid: int,
    proc_start: int,
    token_sha256: str,
    ttl_seconds: Optional[int] = None,
) -> int:
    """Insert one single-use worker identity row and return its row id."""
    from agent.dispatcher_identity import DEFAULT_TTL_SECONDS

    now = int(time.time())
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    with write_txn(conn):
        cur = conn.execute(
            """
            INSERT INTO worker_identities (
                task_id, run_id, workspace_path, worker_pid, proc_start,
                token_sha256, issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                int(run_id),
                str(workspace_path),
                int(worker_pid),
                int(proc_start),
                token_sha256,
                now,
                now + ttl,
            ),
        )
    return int(cur.lastrowid or 0)


def consume_worker_identity(
    conn: sqlite3.Connection, token_sha256: str
) -> Optional[sqlite3.Row]:
    """Atomically claim an unconsumed identity row, or return ``None``."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE worker_identities SET consumed = 1, consumed_at = ? "
            "WHERE token_sha256 = ? AND consumed = 0",
            (int(time.time()), token_sha256),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM worker_identities WHERE token_sha256 = ?",
            (token_sha256,),
        ).fetchone()


def issue_worker_identity(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    workspace_path: str,
    worker_pid: int,
    ttl_seconds: Optional[int] = None,
) -> str:
    """Mint and persist a token bound to an already-running child process."""
    from agent.dispatcher_identity import (
        IdentityBindError,
        new_token,
        process_start_time,
        token_digest,
    )

    proc_start = process_start_time(worker_pid)
    if proc_start is None:
        raise IdentityBindError(
            f"cannot read kernel process-start identity for pid {worker_pid}; "
            "refusing to issue a worker identity"
        )
    token = new_token()
    record_worker_identity(
        conn,
        task_id=task_id,
        run_id=run_id,
        workspace_path=workspace_path,
        worker_pid=worker_pid,
        proc_start=proc_start,
        token_sha256=token_digest(token),
        ttl_seconds=ttl_seconds,
    )
    return token


def purge_expired_worker_identities(conn: sqlite3.Connection) -> int:
    """Delete consumed or expired identity rows."""
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM worker_identities WHERE consumed = 1 OR expires_at < ?",
            (int(time.time()),),
        )
    return int(cur.rowcount or 0)


def issue_subprocess_credential(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    workspace_path: str,
    worker_pid: int,
    proc_start: int,
    token_sha256: str,
    ttl_seconds: int,
) -> int:
    """Insert one worker-subprocess credential row and return its row id.

    Unlike :func:`record_worker_identity`, this row is never CAS-consumed:
    it authorises every subprocess the same worker spawns until it expires.
    """
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            """
            INSERT INTO worker_subprocess_credentials (
                task_id, run_id, workspace_path, worker_pid, proc_start,
                token_sha256, issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                int(run_id),
                str(workspace_path),
                int(worker_pid),
                int(proc_start),
                token_sha256,
                now,
                now + int(ttl_seconds),
            ),
        )
    return int(cur.lastrowid or 0)


def find_valid_subprocess_credential(
    conn: sqlite3.Connection, token_sha256: str, *, task_id: Optional[str] = None
) -> Optional[sqlite3.Row]:
    """Return the matching, unexpired credential row, or ``None``.

    Read-only and repeatable: presenting the same credential from several
    descendants of one compound shell command must succeed every time.

    ``task_id``, when given, scopes the lookup to a credential minted for
    that EXACT task -- closing the cross-task bearer-token replay Maya
    found in commit b40b5669b0 (a credential minted for worker-on-task-A's
    own subprocess could otherwise mutate an unrelated task B on the same
    board). Callers with no single mutation target (task creation, board
    administration) pass ``task_id=None`` and get the pre-existing
    token+expiry check only; :func:`agent.dispatcher_identity.validate_subprocess_credential`
    layers an additional run-liveness check on top of every result this
    returns, targeted or not.
    """
    if task_id is not None:
        return conn.execute(
            "SELECT * FROM worker_subprocess_credentials "
            "WHERE token_sha256 = ? AND expires_at >= ? AND task_id = ?",
            (token_sha256, int(time.time()), str(task_id)),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM worker_subprocess_credentials "
        "WHERE token_sha256 = ? AND expires_at >= ?",
        (token_sha256, int(time.time())),
    ).fetchone()


def purge_expired_subprocess_credentials(conn: sqlite3.Connection) -> int:
    """Delete expired worker-subprocess credential rows."""
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM worker_subprocess_credentials WHERE expires_at < ?",
            (int(time.time()),),
        )
    return int(cur.rowcount or 0)
