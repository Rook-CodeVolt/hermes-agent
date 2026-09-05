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
