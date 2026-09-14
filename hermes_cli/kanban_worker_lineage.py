"""Kernel-verified process lineage for the Kanban delegated-child mutation guard.

CV-A01 (t_70827e4e / t_11e8c077): the cross-process half of the delegated-child
guard (``agent.delegation_context.is_delegated_child_process_context``) used to
rely solely on an inherited environment variable
(``HERMES_DELEGATED_CHILD_CONTEXT``). That variable is plain process
environment: a subprocess that inherits it can simply unset it before
re-exec'ing ``hermes`` (``env -u HERMES_DELEGATED_CHILD_CONTEXT hermes kanban
comment ...``), which defeats the CLI mutation gate and every DB/tool-layer
guard built on top of it, because they all read the very same environment.

This module replaces that half of the signal with a fact the calling process
cannot edit about itself: whether one of its REAL kernel ancestors is a
process the dispatcher actually ``Popen``'d as a Kanban worker.

IMPORTANT SCOPE NOTE -- this is a PARTIAL / INTERIM hardening measure, not a
complete fix for t_70827e4e:

* It closes exactly the REPORTED bypass: a subprocess unsetting
  ``HERMES_DELEGATED_CHILD_CONTEXT`` before re-exec'ing ``hermes`` while it is
  still a live, still-attached descendant of the process the dispatcher
  ``Popen``'d. It does NOT close "the gap" against every conceivable process
  ancestry manipulation.
* KNOWN RESIDUAL RISK (HIGH, tracked t_70827e4e / t_f9082e18): ordinary
  process detachment/reparenting defeats this check by construction, with no
  special privilege required. If a worker's descendant backgrounds a further
  child and its own immediate parent exits before ``wait()`` (plain ``cmd &``
  shell backgrounding, or ``subprocess.Popen`` without ever ``wait()``-ing —
  e.g. the ``terminal(background=true)`` tool primitive, no ``setsid()``
  needed), that grandchild is reparented by the kernel to PID 1/launchd once
  its parent exits. Its REAL kernel ancestry chain up to the recorded worker
  PID is then severed, so ``is_descendant_of_dispatcher_worker`` walks off
  the end of a truncated chain, finds no matching ``worker_spawns`` row, and
  returns ``False`` ("not a descendant") -- silently falling back to
  ALLOWED for what is, in every way that matters, still delegated-child
  work. This is reachable with completely ordinary agent primitives and is
  MORE trivially reachable than the original env-strip bypass this module
  closes. See ``tests/hermes_cli/test_kanban_worker_lineage.py::
  test_detached_reparented_descendant_bypasses_ancestry_check`` for a
  reproduction (xfail, not silently skipped).
* Fixing the residual detach/reparent gap requires a different signal
  entirely (e.g. a durable, unforgeable per-invocation credential minted by
  the dispatcher and threaded through env/argv rather than inferred from
  live process ancestry) and is out of scope for this change.

Design:

* ``record_worker_spawn`` is called exactly once, by the dispatcher's own
  spawn path (``kanban_db_dispatch._default_spawn``), immediately after
  ``Popen`` returns. It reads the child's kernel process-start time from the
  OS itself (``psutil`` -- the same primitive already used for PID-recycle
  safety elsewhere in this codebase, e.g. ``tools/process_registry.py``,
  ``gateway/status.py``) and stores ``(task_id, run_id, worker_pid,
  proc_start)`` in the authoritative Kanban DB. Nothing about this row is
  supplied by, or readable from, the spawned child's own environment.
* ``is_descendant_of_dispatcher_worker`` walks the CALLING process's real
  kernel ancestry (``psutil.Process().parents()``) and asks, for each
  ancestor, "does the kernel-verified (pid, start-time) pair for this
  ancestor match a row this dispatcher itself wrote?". A subprocess cannot
  fake its own ancestry (``getppid()`` and each ancestor's kernel start time
  come from the OS, not from anything the process can set), so this check is
  unforgeable by ordinary process-level manipulation -- unlike the env var it
  replaces.
* The walk deliberately EXCLUDES the calling process itself. The worker
  process the dispatcher spawned legitimately mutates Kanban state through
  the in-process tool surface (``tools/kanban_tools.py``), which is already
  gated correctly by the in-process ``ContextVar`` half of
  ``is_delegated_child_process_context`` (unforgeable for the same reason:
  a ContextVar cannot be read or written from outside the process). Treating
  the worker's own PID as tainted here would incorrectly deny that legitimate
  in-process path. Only its DESCENDANTS -- delegate_task children, cron
  subprocesses, and anything a worker shells out to (e.g. the ``terminal``
  tool) -- are subprocesses of that PID, and it is exactly those descendants
  invoking ``hermes kanban ...`` as a fresh process that this closes.

Threat model note (matches the existing ``_assert_not_delegated_child_mutation``
docstring): this is cooperative-process-boundary scoping, not confinement
against an attacker who already has direct write access to the Kanban
SQLite file. Someone who can already write arbitrary rows into ``tasks``
could also insert a fake ``worker_spawns`` row -- but at that point they no
longer need this guard's cooperation to forge anything.

INTERIM MEASURE, NOT A COMPLETE FIX: this module closes the specific
reported env-strip bypass (t_70827e4e / t_11e8c077) for a still-attached
descendant process. It does NOT close the detach/reparent bypass described
above -- a KNOWN, HIGH-severity residual risk, more trivially reachable
than the bug this closes, tracked separately (t_70827e4e / t_f9082e18) and
left open pending a fuller fix based on a durable per-invocation credential
rather than live process ancestry.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Iterator, Optional

from hermes_cli.kanban_db_connect import write_txn

_log = logging.getLogger(__name__)

# Bounds the ancestry walk so a pathological /proc or a process-tree cycle
# (should never happen on a sane kernel, but ancestry code must not assume
# one) cannot spin forever. Comfortably deeper than any real Hermes process
# tree (dispatcher -> worker -> delegate_task child -> tool subprocess).
_MAX_ANCESTOR_WALK = 64

# How long a recorded worker-spawn row remains authoritative. Generous: this
# only needs to outlive the worker process itself (which the row's own
# kernel proc_start match already scopes to one exact PID incarnation), but
# a stuck/never-cleaned row must not accumulate forever.
DEFAULT_WORKER_SPAWN_TTL_SECONDS = 7 * 24 * 3600


def _proc_start_micros(create_time: float) -> int:
    """Kernel start time as an integer microsecond count.

    Stored (not compared as float) so two independent reads of the same
    kernel value -- one at spawn time, one during an ancestry walk seconds
    or days later -- compare exactly equal.
    """
    return int(round(create_time * 1_000_000))


def kernel_pid_start_micros(pid: int) -> Optional[int]:
    """Kernel-owned process-start identity for ``pid``, in microseconds, or
    ``None`` when the PID is gone or introspection is unavailable.

    Sourced from ``psutil.Process.create_time()`` -- the OS, not the
    process's own claim -- exactly like the existing PID-recycle guards in
    ``tools/process_registry.py`` and ``gateway/status.py``.
    """
    try:
        import psutil

        return _proc_start_micros(psutil.Process(pid).create_time())
    except Exception:
        return None


def record_worker_spawn(
    conn: sqlite3.Connection, *, task_id: str, run_id: Optional[int], worker_pid: int,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Record that the dispatcher itself just ``Popen``'d ``worker_pid`` for
    ``task_id``/``run_id``. Call this ONLY immediately after ``Popen``
    returns (``kanban_db_dispatch._default_spawn``) so the kernel start time
    read here is unambiguously the fresh child's, never a stale/reused PID's.

    Silently no-ops if the kernel start time cannot be read (dead PID,
    unsupported platform) -- a missing row just means this worker gets no
    unforgeable-ancestry protection, which is a strict improvement over
    raising and failing the whole dispatch.
    """
    proc_start = kernel_pid_start_micros(worker_pid)
    if proc_start is None:
        _log.warning(
            "kanban: could not read kernel process-start identity for spawned "
            "worker pid %s (task %s); delegated-child ancestry guard will not "
            "cover this worker.", worker_pid, task_id,
        )
        return
    now = int(time.time())
    ttl = DEFAULT_WORKER_SPAWN_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    with write_txn(conn, allow_nested=True):
        conn.execute(
            "INSERT INTO worker_spawns (task_id, run_id, worker_pid, proc_start, "
            "issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, int(run_id) if run_id is not None else None, int(worker_pid),
             proc_start, now, now + ttl),
        )


def purge_expired_worker_spawns(conn: sqlite3.Connection) -> int:
    """Delete expired worker-spawn rows; returns the number removed."""
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM worker_spawns WHERE expires_at < ?", (int(time.time()),),
        )
    return int(cur.rowcount or 0)


def _ancestor_pid_start_pairs(*, max_depth: int = _MAX_ANCESTOR_WALK) -> Iterator[tuple[int, int]]:
    """Kernel-verified (pid, start) identity for each STRICT ancestor of the
    current process, nearest first. Stops silently at the first unreadable
    or already-visited ancestor (dead process, permission boundary, or a
    same-PID loop) -- exactly the defensive idiom
    ``hermes_cli/gateway.py:_get_ancestor_pids`` already uses for ancestry
    walks elsewhere in this codebase. Never includes the calling process's
    own PID.
    """
    try:
        import psutil

        me = psutil.Process()
    except Exception:
        return
    seen: set[int] = {me.pid}
    depth = 0
    current = me
    while depth < max_depth:
        try:
            parent = current.parent()
        except Exception:
            return
        if parent is None or parent.pid in seen:
            return
        try:
            start = _proc_start_micros(parent.create_time())
        except Exception:
            return
        seen.add(parent.pid)
        yield (parent.pid, start)
        current = parent
        depth += 1


def is_descendant_of_dispatcher_worker(
    conn: Optional[sqlite3.Connection] = None, *, board: Optional[str] = None,
) -> bool:
    """True when a REAL kernel ancestor of this process is a Kanban worker
    the dispatcher itself spawned (per a durable ``worker_spawns`` row).

    Unlike the environment-variable signal this replaces, nothing here is
    supplied by the calling process: ``getppid()``/parent kernel start times
    come from the OS, and the comparison table was written by the trusted
    dispatcher at spawn time. A subprocess cannot make itself NOT a
    descendant of its real parent by clearing an env var.

    *board* -- when ``conn`` is not already supplied, this MUST be the
    actual board this invocation is targeting (the same resolution its
    caller would otherwise use -- e.g. an explicit ``--board`` flag or the
    active-board context), never a bare re-derived default. Falling back to
    ``kanban_db_path(board=None)`` here would silently probe a DIFFERENT
    board's DB than the one this call is actually about -- the same
    env/board-divergence bug class independently found and fixed twice
    already in this subsystem (Maya BLOCK findings on the subprocess-
    credential guard, t_0977ea27 / t_85586891: a credential or check scoped
    to the wrong board either wrongly denies a legitimate same-board worker
    or wrongly authorises a cross-board mutation, depending on which way the
    divergence points). A caller with no board context of its own should
    pass ``board=None`` deliberately (falls through to the process's
    currently-active board, matching ordinary CLI/tool resolution), not
    silently.

    Fails CLOSED (returns True -- "assume tainted, deny mutation") if the
    ancestry mechanism itself cannot run at all (e.g. ``psutil`` unusable in
    this environment), because that failure mode denies exactly the CLI
    mutation this guard exists to gate, never a read-only or non-Kanban
    operation. Per-ancestor introspection failures during an otherwise
    working walk just stop the walk early (handled inside the generator),
    which is the ordinary "ancestor unreadable" case and not a security
    failure.
    """
    try:
        import psutil  # noqa: F401  (import-probe: absent psutil => fail closed below)
    except Exception:
        _log.warning(
            "kanban: psutil unavailable; cannot verify process ancestry for the "
            "delegated-child mutation guard. Failing closed (treating this "
            "invocation as an untrusted descendant)."
        )
        return True

    owns_conn = conn is None
    if owns_conn:
        # Deliberately NOT hermes_cli.kanban_db_connect.connect()/connect_closing():
        # those themselves call is_delegated_child_process_context() (to decide
        # read-only vs. read-write mode), and this function is one of the two
        # signals that predicate consults. Routing through them here would
        # recurse back into this exact check. A direct, read-only sqlite3
        # connection has no such coupling and needs none of connect()'s
        # schema-init/migration machinery -- this only ever runs a single
        # SELECT against a table the trusted dispatcher already created.
        from hermes_cli.kanban_db import kanban_db_path

        db_path = kanban_db_path(board=board)
        if not db_path.exists():
            # Nothing has ever been written to this board -- in particular no
            # dispatcher has recorded a worker_spawns row -- so no real
            # ancestor can be "the" spawned worker. This is the ordinary
            # first-ever-connect state, not an attacker-controlled one:
            # returning False here lets the very first CLI/DB call against a
            # brand-new board proceed exactly as it always could, instead of
            # permanently wedging fresh boards behind a fail-closed guard.
            return False
        try:
            conn = __import__("sqlite3").connect(
                db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5,
            )
        except Exception:
            _log.warning(
                "kanban: could not open %s read-only for the delegated-child "
                "ancestry check; failing closed.", db_path, exc_info=True,
            )
            return True
    try:
        for pid, start in _ancestor_pid_start_pairs():
            try:
                row = conn.execute(
                    "SELECT 1 FROM worker_spawns WHERE worker_pid = ? AND proc_start = ? "
                    "AND expires_at > ? LIMIT 1",
                    (pid, start, int(time.time())),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    # Schema predates worker_spawns (a board this old has
                    # never had a dispatcher-recorded worker anyway) or the
                    # board is still mid-init. Same reasoning as the
                    # missing-file case above: no legitimate ancestor row
                    # could exist yet, so this is not evidence of tampering.
                    return False
                raise
            if row is not None:
                return True
        return False
    except Exception:
        _log.warning(
            "kanban: worker-spawn ancestry lookup failed; failing closed "
            "(treating this invocation as an untrusted descendant).",
            exc_info=True,
        )
        return True
    finally:
        if owns_conn:
            with __import__("contextlib").suppress(Exception):
                conn.close()
