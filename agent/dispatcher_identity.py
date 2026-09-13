"""Process-bound identity for dispatcher-spawned Kanban workers.

Nothing in the environment and nothing in a ContextVar is mutation
authority.  A worker's authority is a one-time random token that the
dispatcher mints **after** the child process exists, so it can bind the
token to that child's exact kernel identity:

    task id + run id + workspace + worker PID + kernel process-start time

The token is stored **hashed** in the authoritative Kanban database and
handed to the child over an inherited pipe -- never through the
environment, where it would be readable by every sibling process and
inherited by every descendant.  The child CAS-consumes the row exactly
once during a startup handshake that must complete *before* it can enter
the agent loop, and receives an immutable :class:`BoundIdentity`.

Consequences that the work-claims mutation gate depends on:

* ``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_WORKSPACE`` alone bind nothing.
  A process holding only those env vars has no identity and therefore no
  claimless scope -- it falls through to ordinary claim enforcement.
* A forged or replayed token binds nothing: the row is consumed on first
  use and its PID/start pair pins it to one specific process.
* PID reuse cannot resurrect an identity: the kernel's process-start time
  differs even when the number is recycled within the same second
  (microsecond resolution).
* Descendants inherit no authority.  The handshake descriptor is closed
  and unset after binding, the token is already consumed, and in-process
  delegated/cron scopes suppress the binding explicitly.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import logging
import os
import secrets
import select
import struct
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

_log = logging.getLogger(__name__)

# The child reads its identity token from this inherited descriptor.  The
# *number* is not a secret and carries no authority: without the pipe's
# contents it grants nothing.
HANDSHAKE_FD_ENV = "HERMES_WORKER_HANDSHAKE_FD"

# A dispatcher-owned worker's own ``terminal``-tool subprocess (the
# documented "shell out to ``hermes kanban ...`` instead of a tool" fallback)
# carries this env var when the SPAWNING process minted one for it.  Minting
# happens in the parent, gated on ``get_bound() is not None`` at the exact
# moment of spawn -- a decision made from this process's own ContextVar/
# identity state, which a ``delegate_task`` child cannot influence via the
# text of the shell command it asks the ``terminal`` tool to run.  The value
# itself is an opaque token verified server-side (never trusted from env
# alone); see :func:`mint_subprocess_credential` / :func:`kanban_db_identity`.
SUBPROCESS_CREDENTIAL_ENV = "HERMES_WORKER_SUBPROCESS_CREDENTIAL"

# Short: bounds how long a credential minted for one terminal-tool spawn
# remains presentable, limiting the window for the documented residual risk
# (the top-level agent could, in principle, persist the value itself and a
# later delegate_task subprocess replay it within that window).
SUBPROCESS_CREDENTIAL_TTL_SECONDS = 120

# How long the child waits for the dispatcher to finish minting its row.
# The dispatcher writes immediately after Popen returns, so this only ever
# matters when the dispatcher died mid-handshake -- in which case the
# child must fail closed rather than hang forever.
HANDSHAKE_READ_TIMEOUT = 60.0

# A worker identity is single-use and short-lived; the TTL bounds how long
# a leaked-but-unconsumed row could be replayed.
DEFAULT_TTL_SECONDS = 12 * 3600

_MAX_PAYLOAD_BYTES = 8192


class IdentityBindError(RuntimeError):
    """A handshake was offered but could not be bound to this process."""


@dataclass(frozen=True)
class BoundIdentity:
    """An immutable, kernel-bound dispatcher worker identity.

    Frozen on purpose: once bootstrap has bound it, no later code -- tool,
    plugin or prompt-driven -- can widen its own scope by mutating it.
    """

    identity_id: int
    task_id: str
    run_id: int
    workspace: Path
    workspace_key: tuple[int, int]
    worker_pid: int
    proc_start: int
    db_path: str
    expires_at: int


# The single binding for this process, set exactly once by bootstrap.
_BOUND: BoundIdentity | None = None
_BIND_ATTEMPTED = False

# In-process scopes that are NOT the dispatcher-owned worker even though
# they run inside its process (delegate_task children, cron jobs).
_SUPPRESSED: ContextVar[bool] = ContextVar(
    "hermes_dispatcher_identity_suppressed", default=False
)

# Cache of the last-minted subprocess credential for THIS process's binding,
# so a worker issuing many ``terminal`` commands per turn does not re-write
# the Kanban DB on every single one. Keyed by identity_id so a rebind (tests
# only; a real process binds once) cannot reuse a stale token across
# identities. Refreshed once fewer than a third of the TTL remains.
_SUBPROCESS_CREDENTIAL_CACHE: tuple[int, str, int] | None = None


# --------------------------------------------------------------------------- #
# Kernel process-start identity
# --------------------------------------------------------------------------- #

def _macos_process_start(pid: int) -> int | None:
    """Read ``kinfo_proc.kp_proc.p_starttime`` via ``sysctl``.

    ``p_starttime`` is a ``struct timeval`` at offset 0 of ``extern_proc``
    (it shares a union with the unused ``p_forw``/``p_back`` pointers), so
    it is the first 12 meaningful bytes of ``kinfo_proc``.  Returned in
    microseconds: a recycled PID within the same wall-clock second still
    yields a different value.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        CTL_KERN, KERN_PROC, KERN_PROC_PID = 1, 14, 1
        mib = (ctypes.c_int * 4)(CTL_KERN, KERN_PROC, KERN_PROC_PID, pid)
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value < 12:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        # A reaped PID yields a successful call with a zero-length result.
        if size.value == 0:
            return None
        tv_sec, tv_usec = struct.unpack_from("<qi", buf.raw, 0)
        if tv_sec <= 0:
            return None
        return tv_sec * 1_000_000 + max(tv_usec, 0)
    except Exception:
        return None


def _linux_process_start(pid: int) -> int | None:
    """Boot-relative start time from ``/proc/<pid>/stat`` field 22.

    Kept in microseconds for parity with the Darwin reader.  The value is
    boot-relative rather than epoch-based, which is strictly stronger for
    this purpose: it cannot drift with wall-clock adjustments.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
        # comm may contain spaces/parens; everything after the final ')'
        # is the fixed-position field list.
        tail = data[data.rindex(b")") + 2:].split()
        starttime_ticks = int(tail[19])
        hz = os.sysconf("SC_CLK_TCK")
        if hz <= 0:
            return None
        return (starttime_ticks * 1_000_000) // hz
    except Exception:
        return None


def process_start_time(pid: int) -> int | None:
    """Kernel-owned process-start identity in microseconds, or None.

    The process cannot set, spoof or inherit this: it is read from the
    kernel by PID.  ``None`` means the PID is gone or the platform cannot
    supply the value -- both of which must fail closed at every call site.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform == "darwin":
        return _macos_process_start(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start(pid)
    return None


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #

def new_token() -> str:
    """A fresh 256-bit identity token."""
    return secrets.token_hex(32)


def token_digest(token: str) -> str:
    """The stored form of a token.  Only the digest ever reaches disk."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Parent side of the handshake
# --------------------------------------------------------------------------- #

class WorkerHandshake:
    """Dispatcher half of the startup handshake.

    Order matters and is the whole point: the pipe is created *before* the
    fork so the child inherits it and blocks; the identity row is minted
    *after* ``Popen`` returns, when the child's PID and kernel start time
    are finally knowable; only then is the token sent.  A child therefore
    cannot reach its agent loop before a row bound to its own kernel
    identity exists.
    """

    def __init__(self) -> None:
        self.read_fd, self._write_fd = os.pipe()
        os.set_inheritable(self.read_fd, True)
        self._read_closed = False
        self._write_closed = False

    def env_for_child(self) -> dict[str, str]:
        return {HANDSHAKE_FD_ENV: str(self.read_fd)}

    def pass_fds(self) -> tuple[int, ...]:
        return (self.read_fd,)

    def close_read_fd(self) -> None:
        """Drop the parent's copy so the child sees EOF if we abort."""
        if not self._read_closed:
            os.close(self.read_fd)
            self._read_closed = True

    def send(self, token: str, *, db_path: str) -> None:
        """Hand the child its token and the authoritative DB to read.

        The database path travels with the token rather than through the
        environment: identity must never be resolvable from env alone.
        """
        payload = json.dumps({"token": token, "db": str(db_path)}) + "\n"
        self.send_raw(payload)

    def send_raw(self, payload: str) -> None:
        if self._write_closed:
            raise RuntimeError("handshake already completed")
        try:
            os.write(self._write_fd, payload.encode("utf-8"))
        finally:
            os.close(self._write_fd)
            self._write_closed = True

    def abort(self) -> None:
        """Fail the handshake closed: EOF, no token, no authority."""
        if not self._write_closed:
            os.close(self._write_fd)
            self._write_closed = True

    def close(self) -> None:
        self.close_read_fd()
        self.abort()


# --------------------------------------------------------------------------- #
# Child side of the handshake
# --------------------------------------------------------------------------- #

def _read_handshake_payload(fd: int, timeout: float) -> str:
    """Read one newline-terminated payload, bounded in size and time."""
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IdentityBindError("worker identity handshake timed out")
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except OSError as exc:
            raise IdentityBindError(f"worker identity handshake failed: {exc}") from exc
        if not ready:
            continue
        try:
            chunk = os.read(fd, 1024)
        except OSError as exc:
            raise IdentityBindError(f"worker identity handshake failed: {exc}") from exc
        if not chunk:
            raise IdentityBindError(
                "worker identity handshake closed without a token"
            )
        chunks.append(chunk)
        joined = b"".join(chunks)
        if b"\n" in joined:
            return joined.split(b"\n", 1)[0].decode("utf-8", "replace")
        if len(joined) > _MAX_PAYLOAD_BYTES:
            raise IdentityBindError("worker identity handshake payload too large")


def bind_from_handshake(*, environ: Mapping[str, str] | None = None) -> BoundIdentity | None:
    """Bind this process's identity, exactly once, at startup.

    Returns ``None`` when no handshake was offered -- an ordinary session,
    which simply has no dispatcher authority.  Raises
    :class:`IdentityBindError` when a handshake *was* offered but cannot be
    bound; the caller must treat that as fatal rather than continuing with
    an unbound worker, because the dispatcher believed it was spawning one.
    """
    global _BIND_ATTEMPTED
    env = os.environ if environ is None else environ
    raw_fd = str(env.get(HANDSHAKE_FD_ENV, "")).strip()
    if not raw_fd:
        return None
    if _BIND_ATTEMPTED:
        raise IdentityBindError("worker identity has already been bound")
    _BIND_ATTEMPTED = True

    try:
        fd = int(raw_fd)
    except ValueError:
        _forget_handshake_env()
        raise IdentityBindError(f"invalid handshake descriptor {raw_fd!r}") from None

    try:
        payload = _read_handshake_payload(fd, HANDSHAKE_READ_TIMEOUT)
    finally:
        # The descriptor and its env pointer must not outlive the
        # handshake: a descendant that inherited either must not be able to
        # replay or extend this binding.
        try:
            os.close(fd)
        except OSError:
            pass
        _forget_handshake_env()

    try:
        parsed = json.loads(payload)
        token = str(parsed["token"])
        db_path = str(parsed["db"])
    except (ValueError, KeyError, TypeError):
        raise IdentityBindError("malformed worker identity handshake payload") from None

    return bind_token(token, db_path=db_path)


def bind_token(token: str, *, db_path: str) -> BoundIdentity:
    """Consume ``token`` and install the resulting immutable identity."""
    global _BOUND
    identity = _resolve_identity(token, db_path)
    _BOUND = identity
    return identity


def _forget_handshake_env() -> None:
    os.environ.pop(HANDSHAKE_FD_ENV, None)


def _resolve_identity(token: str, db_path: str) -> BoundIdentity:
    """CAS-consume the token and validate every bound field.

    Validation is deliberately exhaustive and fail-closed: a mismatch on
    *any* field means this process is not the one the dispatcher minted the
    row for.
    """
    from hermes_cli import kanban_db_connect, kanban_db_identity

    if not isinstance(token, str) or not token.strip():
        raise IdentityBindError("empty worker identity token")

    digest = token_digest(token)
    try:
        with kanban_db_connect.connect_closing(db_path=Path(db_path)) as conn:
            row = kanban_db_identity.consume_worker_identity(conn, digest)
    except IdentityBindError:
        raise
    except Exception as exc:
        raise IdentityBindError(f"worker identity database unreadable: {exc}") from exc

    if row is None:
        raise IdentityBindError("unknown or already-consumed worker identity token")

    my_pid = os.getpid()
    if int(row["worker_pid"]) != my_pid:
        raise IdentityBindError(
            f"worker identity pid mismatch: row={row['worker_pid']} process={my_pid}"
        )
    my_start = process_start_time(my_pid)
    if my_start is None:
        raise IdentityBindError(
            "kernel process-start identity unavailable; refusing to bind"
        )
    if int(row["proc_start"]) != my_start:
        raise IdentityBindError(
            "worker identity process-start mismatch (pid reuse): "
            f"row={row['proc_start']} process={my_start}"
        )
    now = int(time.time())
    if now > int(row["expires_at"]):
        raise IdentityBindError("worker identity token expired")

    task_id = str(row["task_id"])
    run_id = int(row["run_id"])
    workspace_key = _validate_against_task(
        db_path, task_id, run_id, str(row["workspace_path"])
    )
    return BoundIdentity(
        identity_id=int(row["id"]),
        task_id=task_id,
        run_id=run_id,
        workspace=Path(str(row["workspace_path"])),
        workspace_key=workspace_key,
        worker_pid=my_pid,
        proc_start=my_start,
        db_path=str(db_path),
        expires_at=int(row["expires_at"]),
    )


def _validate_against_task(
    db_path: str, task_id: str, run_id: int, workspace_path: str
) -> tuple[int, int]:
    """Check the row still agrees with the authoritative task record."""
    from hermes_cli import kanban_db, kanban_db_connect

    try:
        with kanban_db_connect.connect_closing(db_path=Path(db_path)) as conn:
            task = kanban_db.get_task(conn, task_id)
    except Exception as exc:
        raise IdentityBindError(f"could not read Kanban task {task_id!r}: {exc}") from exc
    if task is None:
        raise IdentityBindError(f"Kanban task {task_id!r} does not exist")
    if task.current_run_id is None or int(task.current_run_id) != run_id:
        raise IdentityBindError(
            f"worker identity run {run_id} is not the current run for task "
            f"{task_id!r} (current={task.current_run_id})"
        )
    recorded = task.workspace_path
    if not isinstance(recorded, str) or not recorded.strip():
        raise IdentityBindError(f"Kanban task {task_id!r} has no recorded workspace")
    if os.path.normpath(recorded) != os.path.normpath(workspace_path):
        raise IdentityBindError(
            f"worker identity workspace does not match task {task_id!r}"
        )

    from agent.workspace_confinement import canonical_walk

    ok, key, reason = canonical_walk(Path(workspace_path), require_dir=True)
    if not ok or key is None:
        raise IdentityBindError(f"worker workspace failed verification: {reason}")
    return key


# --------------------------------------------------------------------------- #
# Reading the binding
# --------------------------------------------------------------------------- #

def get_bound() -> BoundIdentity | None:
    """The identity in force for the *current* execution scope.

    ``None`` for every process that never completed a handshake, and for
    in-process scopes that are not the dispatcher-owned worker even though
    they run inside its process: ``delegate_task`` children and cron jobs.
    Those must never inherit the worker's authority.
    """
    if _BOUND is None:
        return None
    if _SUPPRESSED.get():
        return None
    try:
        from agent import delegation_context
    except ImportError:  # pragma: no cover - agent package is always present
        return _BOUND
    if not delegation_context.is_dispatcher_owned_worker_context():
        return None
    if delegation_context.is_delegated_child_process_context():
        return None
    return _BOUND


def revalidate(identity: BoundIdentity) -> str | None:
    """Re-check a binding against the DB.  Returns a reason, or None.

    Called on every confined mutation rather than trusted from bind time:
    a task whose run advanced, whose workspace moved, or whose identity
    expired must stop authorising writes immediately.
    """
    now = int(time.time())
    if now > identity.expires_at:
        return "worker identity token expired"
    if os.getpid() != identity.worker_pid:
        return "worker identity does not belong to this process"
    if process_start_time(identity.worker_pid) != identity.proc_start:
        return "worker identity process-start mismatch"
    try:
        _validate_against_task(
            identity.db_path,
            identity.task_id,
            identity.run_id,
            str(identity.workspace),
        )
    except IdentityBindError as exc:
        return str(exc)
    return None


@contextmanager
def suppressed() -> Iterator[None]:
    """Run a block with no dispatcher authority, whatever this process is."""
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


def enter_suppressed() -> Token[bool]:
    return _SUPPRESSED.set(True)


def exit_suppressed(token: Token[bool]) -> None:
    _SUPPRESSED.reset(token)


def reset_for_tests() -> None:
    """Clear the process binding.  Test-support only."""
    global _BOUND, _BIND_ATTEMPTED, _SUBPROCESS_CREDENTIAL_CACHE
    _BOUND = None
    _BIND_ATTEMPTED = False
    _SUBPROCESS_CREDENTIAL_CACHE = None


def describe() -> dict[str, Any]:
    """Diagnostic view of the binding (never includes any token)."""
    identity = get_bound()
    if identity is None:
        return {"bound": False}
    return {
        "bound": True,
        "task_id": identity.task_id,
        "run_id": identity.run_id,
        "workspace": str(identity.workspace),
        "worker_pid": identity.worker_pid,
        "expires_at": identity.expires_at,
    }


# --------------------------------------------------------------------------- #
# Subprocess credentials: proving a worker's OWN terminal-tool shell-out
# --------------------------------------------------------------------------- #

def mint_subprocess_credential(
    *, ttl_seconds: int = SUBPROCESS_CREDENTIAL_TTL_SECONDS
) -> str | None:
    """Mint (or reuse a still-fresh cached) short-lived, DB-verified token for
    a subprocess this worker is about to spawn, or ``None`` when this call is
    not entitled to one.

    Callers MUST call this at the moment of spawn (not cache the result
    themselves across turns) and gate injecting it into the child's env on
    getting a non-``None`` return. ``None`` covers every reason a credential
    must not be issued: no live binding at all (ordinary session, cron job,
    or -- critically -- a ``delegate_task`` child, whose whole execution runs
    with :func:`get_bound` suppressed regardless of what shell text it asks
    the ``terminal`` tool to run), or a binding that no longer revalidates
    (stale run, moved workspace, expired token).

    The minted token is unrelated to the one-time handshake token: it is
    reusable (not CAS-consumed) so one worker can authorise many terminal
    calls, and it is bound to worker_pid + kernel process-start rather than
    to any single child PID, so the *worker's own* subsequent descendants
    (including further forks inside one shell command) can all present it.
    """
    global _SUBPROCESS_CREDENTIAL_CACHE

    identity = get_bound()
    if identity is None:
        return None
    if revalidate(identity) is not None:
        return None

    now = int(time.time())
    cached = _SUBPROCESS_CREDENTIAL_CACHE
    if cached is not None:
        cached_identity_id, cached_token, cached_expires_at = cached
        if (
            cached_identity_id == identity.identity_id
            and cached_expires_at - now > ttl_seconds // 3
        ):
            return cached_token

    from hermes_cli import kanban_db_connect, kanban_db_identity

    token = new_token()
    expires_at = now + int(ttl_seconds)
    try:
        with kanban_db_connect.connect_closing(db_path=Path(identity.db_path)) as conn:
            kanban_db_identity.purge_expired_subprocess_credentials(conn)
            kanban_db_identity.issue_subprocess_credential(
                conn,
                task_id=identity.task_id,
                run_id=identity.run_id,
                workspace_path=str(identity.workspace),
                worker_pid=identity.worker_pid,
                proc_start=identity.proc_start,
                token_sha256=token_digest(token),
                ttl_seconds=ttl_seconds,
            )
    except Exception as exc:
        _log.debug("could not mint worker subprocess credential: %s", exc)
        return None
    _SUBPROCESS_CREDENTIAL_CACHE = (identity.identity_id, token, expires_at)
    return token


def validate_subprocess_credential(
    token: str,
    conn: "Any | None" = None,
    *,
    task_id: "str | None" = None,
    board: "str | None" = None,
) -> bool:
    """Whether *token* is a live, DB-recorded worker subprocess credential.

    *conn* -- when the caller already holds a connection to the DB it is
    about to write to (the normal ``write_txn`` case) -- is checked FIRST
    and, when given, EXCLUSIVELY: :func:`hermes_cli.kanban_db_connect.connect_closing`
    re-enters ``_init_if_needed``'s cross-process init lock, which is not
    reentrant across separate file descriptors even within one process, so
    opening a second connection here would risk deadlocking every guarded
    write. More importantly, a caller that passes its own board's ``conn``
    has already told us which board is authoritative for this call; probing
    every OTHER known board's file after a miss there would let a credential
    minted on board A validate against a completely unrelated board B just
    because some third board C's DB file happens to contain a live row for
    the same token digest (fixed regression: Maya's independent review of
    b40b5669b0 reproduced exactly this using two real board files).

    *board* -- for callers with no open ``conn`` of their own (board
    administration: ``set_current_board``, ``clear_current_board``,
    ``write_board_metadata``, ``remove_board``) but which DO have an exact
    target board slug (the board being switched to / archived / deleted /
    whose metadata is being written), this scopes the check to ONLY that
    board's own DB file -- exactly the same "one authoritative board, no
    fallback" behaviour as the ``conn is not None`` branch, just resolved
    from a slug instead of an already-open connection. This closes the
    cross-board admin-authority bypass Maya found in a fresh review of
    d977234b54: a credential minted while working on board A must not
    authorise ``remove_board``/``write_board_metadata``/etc. against an
    unrelated board B just because board B's DB file happens to be probed.
    A missing/unreadable target board file fails closed (no credential can
    validate against a board that isn't there to hold one).

    *conn is None and board is None* -- no caller in this codebase invokes
    the check this way (every board-admin call site now supplies ``board``;
    every task-mutating call site supplies ``conn``). There is no
    remaining legitimate use for a global multi-board probe, so this
    combination now fails closed rather than scanning every known board's
    DB file for a matching token -- that scan was the exact mechanism of
    the cross-board admin-authority bypass this fixes.

    *task_id* -- when the caller can name the single task this call is
    about to mutate, the credential row must have been minted for that
    EXACT task, closing the cross-task replay Maya reproduced (a credential
    minted for worker-on-task-A's own subprocess otherwise authorised
    mutating an unrelated task B on the same board). Callers with no single
    mutation target (task creation, per-board administration, batch
    dispatcher sweeps) pass ``task_id=None`` and get the token+expiry(+board)
    check only -- this is strictly narrower than a plain bearer token, never
    wider than before this fix.
    """
    if not isinstance(token, str) or not token.strip():
        return False

    from hermes_cli import kanban_db, kanban_db_identity

    digest = token_digest(token)

    if conn is not None:
        try:
            row = kanban_db_identity.find_valid_subprocess_credential(conn, digest, task_id=task_id)
        except Exception as exc:
            _log.debug("could not validate worker subprocess credential against the open connection: %s", exc)
            return False
        return row is not None

    if board is not None:
        import sqlite3

        try:
            path = kanban_db.kanban_db_path(board=board)
        except Exception as exc:
            _log.debug("could not resolve target board %r for subprocess credential check: %s", board, exc)
            return False
        if not path.is_file():
            return False
        try:
            probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            probe.row_factory = sqlite3.Row
            try:
                row = kanban_db_identity.find_valid_subprocess_credential(probe, digest, task_id=task_id)
            finally:
                probe.close()
        except Exception as exc:
            _log.debug("could not validate worker subprocess credential against target board %r (%s): %s", board, path, exc)
            return False
        return row is not None

    return False
