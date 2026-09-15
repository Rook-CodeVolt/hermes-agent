"""Durable per-invocation Kanban mutation authority (t_714420e1, design t_a1260456).

Replaces kernel-ancestry authorization (``kanban_worker_lineage``) as the
FINAL authority for the delegated-child Kanban mutation guard. Ancestry is a
live process-tree fact: it is erased the instant a descendant detaches and
is reparented to init/launchd (see
``tests/hermes_cli/test_kanban_worker_lineage.py::
test_detached_reparented_descendant_bypasses_ancestry_check``, formerly
xfail). This module is deliberately NOT ancestry-based: authorization here
depends only on a durable DB row plus the CALLING process's own CURRENT
kernel identity (self PID + self kernel start time), never on whether the
process tree between the dispatcher's worker and this process is still
intact.

POSITIVE ALLOWLIST, NOT A BLOCKLIST (design doc section 1): absence,
malformed input, lookup failure, stale/revoked state, wrong board/task/run,
or wrong PID/start all deny. There is no "assume ordinary caller, allow"
fallback anywhere in this module -- every exit that isn't an explicit,
verified authority raises ``PermissionError``.

Lifecycle (design doc sections 3-4):

1. ``issue_pending_grant`` -- called by the dispatcher's own spawn path
   (``kanban_db_dispatch._default_spawn``), in the target board's own
   connection, BEFORE ``Popen``. Creates a ``pending`` row (no PID/start
   yet) and returns the plaintext token. If this fails, the caller must not
   spawn.
2. The plaintext token is placed ONLY in the new worker's environment
   (``HERMES_KANBAN_INVOCATION_GRANT``); this module never logs, returns, or
   persists the plaintext anywhere.
3. ``Popen`` runs.
4. ``activate_grant`` -- called immediately after ``Popen`` returns, reading
   the child's OWN kernel process-start time and atomically binding the
   pending row to ``(pid, proc_start)``. A pending row is NEVER itself
   sufficient authority -- a child that reaches a mutation before activation
   is denied, never fail-open (design doc section 4.6).
5. If activation fails, the caller must terminate the spawned process and
   revoke/delete the pending grant -- this module provides
   ``revoke_grant``/``delete_pending_grant`` for that; it does not itself
   manage the subprocess.

Verification (design doc section 5): ``verify_worker_invocation_grant``
returns a typed ``GrantAuthority`` or raises ``PermissionError``. It:

* uses ONLY the supplied mutation connection (never re-resolves a board from
  ambient environment);
* reads this process's OWN pid/kernel-start from the OS (never trusts a
  caller-supplied value for identity, only for the token itself);
* checks token digest, activation, expiry, revocation, PID/start match,
  task/run identity, ``tasks.current_run_id``, ``tasks.status='running'``,
  and ``task_runs.status='running'`` in one query;
* fails closed (denies) on missing schema, malformed/missing token,
  unavailable process introspection, DB errors, or board mismatch.

Token format: ``v1.<grant_id>.<secret>`` in
``HERMES_KANBAN_INVOCATION_GRANT``. ``grant_id`` and the 256-bit secret are
generated independently with ``secrets`` -- never derived from task/run/PID
data. Only ``sha256(secret)`` is ever stored; digest comparison uses
``hmac.compare_digest``. The plaintext token must never appear in logs,
task events, exceptions, session context, or task metadata -- callers that
handle the token string directly (dispatcher spawn, CLI/tool env plumbing)
carry that obligation too; this module's own error paths never include it.

Reusability: the grant authorizes ONE WORKER INVOCATION (every mutation
that process makes for the lifetime of its run), not one mutation --
one-time-per-mutation tokens create concurrency/retry ambiguity without
adding protection here (design doc section 3). TTL is bounded by
``max_ttl_seconds`` (a hard ceiling regardless of caller-supplied
``ttl_seconds``, so a misconfigured caller cannot mint a near-permanent
grant).
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)

GRANT_ENV_VAR = "HERMES_KANBAN_INVOCATION_GRANT"

# Phase B enforcement toggle (design t_a1260456 section 8, Phase A/B cutover
# gate on t_c67e90a0). Default OFF: Phase A telemetry runs unconditionally
# (see ``record_authority_decision_telemetry`` / write_txn wiring), but this
# module never DENIES a real mutation until an operator explicitly sets this
# env var, which happens only after (a) Maya has code-reviewed this exact
# diff and (b) Tom has signed off on the C1 interactive-CLI compatibility
# break (see the Phase A/B cutover report on t_c67e90a0) -- this is a
# deliberate one-line flip point, not a config default anyone should set
# themselves ahead of that sign-off.
ENFORCEMENT_ENV_VAR = "HERMES_KANBAN_INVOCATION_AUTHORITY_ENFORCE"


def enforcement_enabled() -> bool:
    """Whether Phase B default-deny enforcement is live. Default False (Phase
    A telemetry-only). Truthy env values: ``1``/``true``/``yes`` (case
    insensitive); anything else, including unset, is False -- never a
    silent typo-activated cutover."""
    return os.environ.get(ENFORCEMENT_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}

# Bounds a caller-supplied ttl_seconds; see module docstring. Generous enough
# to cover the longest ordinary max_runtime_seconds task plus shutdown grace,
# but never "effectively unbounded" -- an expired grant must eventually stop
# authorizing even a worker that never reports completion (crash, kill -9).
DEFAULT_GRANT_TTL_SECONDS = 6 * 3600
MAX_GRANT_TTL_SECONDS = 24 * 3600
# Small grace after a worker's own max_runtime_seconds so an in-flight
# mutation racing shutdown doesn't get spuriously denied by expiry alone
# (revocation on terminal lifecycle transition is the real fast-path deny).
SHUTDOWN_GRACE_SECONDS = 5 * 60

_TOKEN_RE = re.compile(r"^v1\.([0-9a-f]{32})\.([0-9a-f]{64})$")
_GRANT_ID_BYTES = 16   # 128 bits, hex-encoded = 32 chars
_SECRET_BYTES = 32     # 256 bits, hex-encoded = 64 chars


@dataclass(frozen=True)
class GrantAuthority:
    """Typed, immutable proof that the calling process holds a currently
    valid worker-invocation grant. Never constructed except by
    :func:`verify_worker_invocation_grant` on a genuine DB-verified match --
    callers must not hand-build one to short-circuit verification."""

    grant_id: str
    task_id: str
    run_id: int
    board_scope: Optional[str] = None


class GrantDenied(PermissionError):
    """``PermissionError`` subclass carrying a non-secret classification
    code for Phase A telemetry (design section 8 Phase A telemetry decision:
    "denial reason code (missing, malformed, wrong-process, stale-run,
    expired, revoked, context-override, unadmitted-path, missing-schema)").
    Callers that only ``except PermissionError`` (every existing caller)
    are unaffected -- this is a strict narrowing, never a behavior change."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _token_digest(secret_hex: str) -> bytes:
    import hashlib

    return hashlib.sha256(bytes.fromhex(secret_hex)).digest()


def _parse_token(token: Optional[str]) -> Optional[tuple[str, str]]:
    """``(grant_id, secret_hex)`` from a well-formed token, else ``None``.

    Strict length/charset/version bounds BEFORE any DB lookup -- a malformed
    token must never reach SQL, and must never raise (a raise from parsing
    would need its own careful non-leaking error path; simpler to make
    "unparseable" just another denial reason the caller reports generically).
    """
    if not token or not isinstance(token, str):
        return None
    if len(token) > 128:  # generous ceiling; real tokens are ~103 chars
        return None
    m = _TOKEN_RE.match(token)
    if not m:
        return None
    return m.group(1), m.group(2)


def _self_pid_start() -> Optional[tuple[int, int]]:
    """This process's OWN kernel (pid, start-micros), or ``None`` if kernel
    introspection is unavailable. Reused from the same OS primitive
    ``kanban_worker_lineage`` already uses (``psutil``), but evaluated
    against THIS process only -- never an ancestor walk. That is the whole
    point: a detached grandchild has a perfectly good self-identity even
    though its ancestry chain back to the recorded worker is severed.
    """
    try:
        import os

        import psutil

        from hermes_cli.kanban_worker_lineage import _proc_start_micros

        p = psutil.Process(os.getpid())
        return os.getpid(), _proc_start_micros(p.create_time())
    except Exception:
        return None


def issue_pending_grant(
    conn: sqlite3.Connection, *, task_id: str, run_id: int,
    ttl_seconds: Optional[int] = None,
) -> str:
    """Mint a PENDING grant row in ``conn`` (the exact target board
    connection) and return the plaintext ``v1.<grant_id>.<secret>`` token.

    MUST be called only from the dispatcher's own trusted spawn path, BEFORE
    ``Popen``. The row has no ``worker_pid``/``proc_start`` yet (the CHECK
    constraint enforces both-or-neither) -- it is not itself sufficient
    authority; :func:`verify_worker_invocation_grant` never authorizes a
    pending row. This function does not gate on an issuer context itself
    (the design doc's ContextVar issuer gate lives in the dispatcher's own
    call site per section 7); it is a private, unexported implementation
    detail of that trusted path, not a public CLI/tool operation.
    """
    import secrets as _secrets

    from hermes_cli.kanban_db_connect import write_txn

    grant_id = _secrets.token_hex(_GRANT_ID_BYTES)
    secret_hex = _secrets.token_hex(_SECRET_BYTES)
    digest = _token_digest(secret_hex)
    now = int(time.time())
    ttl = DEFAULT_GRANT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    ttl = max(1, min(ttl, MAX_GRANT_TTL_SECONDS))
    with write_txn(conn, allow_nested=True):
        conn.execute(
            "INSERT INTO worker_invocation_grants "
            "(grant_id, token_digest, task_id, run_id, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (grant_id, digest, task_id, int(run_id), now, now + ttl),
        )
    return f"v1.{grant_id}.{secret_hex}"


def activate_grant(
    conn: sqlite3.Connection, *, grant_id: str, task_id: str, run_id: int,
    worker_pid: int, extend_ttl_seconds: Optional[int] = None,
) -> bool:
    """Bind a PENDING grant to the freshly spawned child's real kernel
    identity. MUST be called immediately after ``Popen`` returns, against
    ``worker_pid`` (the child's own PID) -- reading kernel start here, not
    later, so it is unambiguously the fresh child's, never a stale/reused
    PID's (mirrors ``kanban_worker_lineage.record_worker_spawn``).

    Returns ``True`` on success. Returns ``False`` (never raises) when the
    row is missing, already activated, already revoked/expired, or belongs
    to a different task/run -- CAS semantics so a caller cannot silently
    activate the wrong row. On ``False`` the caller MUST terminate the
    spawned process and revoke the grant (design doc section 4.5); this
    function does not do that itself, it only reports whether binding
    succeeded.
    """
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_worker_lineage import kernel_pid_start_micros

    proc_start = kernel_pid_start_micros(worker_pid)
    if proc_start is None:
        _log.warning(
            "kanban: could not read kernel process-start identity for spawned "
            "worker pid %s (task %s, grant %s); invocation grant cannot be "
            "activated.", worker_pid, task_id, grant_id,
        )
        return False
    now = int(time.time())
    ttl_extend = None
    if extend_ttl_seconds is not None:
        ttl_extend = max(1, min(int(extend_ttl_seconds), MAX_GRANT_TTL_SECONDS))
    with write_txn(conn, allow_nested=True):
        if ttl_extend is not None:
            cur = conn.execute(
                "UPDATE worker_invocation_grants "
                "SET worker_pid = ?, proc_start = ?, activated_at = ?, expires_at = ? "
                "WHERE grant_id = ? AND task_id = ? AND run_id = ? "
                "AND activated_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
                (worker_pid, proc_start, now, now + ttl_extend, grant_id, task_id,
                 int(run_id), now),
            )
        else:
            cur = conn.execute(
                "UPDATE worker_invocation_grants "
                "SET worker_pid = ?, proc_start = ?, activated_at = ? "
                "WHERE grant_id = ? AND task_id = ? AND run_id = ? "
                "AND activated_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
                (worker_pid, proc_start, now, grant_id, task_id, int(run_id), now),
            )
    return bool(cur.rowcount)


def revoke_grant(conn: sqlite3.Connection, *, grant_id: str) -> bool:
    """Best-effort revoke; returns whether a row was actually revoked now
    (already-revoked or missing both return ``False``, never raise -- a
    caller on a terminal lifecycle transition must not have its own
    completion/block/crash-handling blocked by a revoke failure)."""
    from hermes_cli.kanban_db_connect import write_txn

    now = int(time.time())
    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            "UPDATE worker_invocation_grants SET revoked_at = ? "
            "WHERE grant_id = ? AND revoked_at IS NULL",
            (now, grant_id),
        )
    return bool(cur.rowcount)


def delete_pending_grant(conn: sqlite3.Connection, *, grant_id: str) -> bool:
    """Hard-delete a still-PENDING row (spawn failed before/at activation --
    no worker ever ran with this token, so there is nothing to revoke-audit;
    keeping a dead pending row around only pollutes purge accounting)."""
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            "DELETE FROM worker_invocation_grants "
            "WHERE grant_id = ? AND activated_at IS NULL",
            (grant_id,),
        )
    return bool(cur.rowcount)


def revoke_grants_for_run(conn: sqlite3.Connection, *, task_id: str, run_id: int) -> int:
    """Best-effort revoke every non-revoked grant for one task/run (called on
    terminal lifecycle transitions -- complete/block/crash/reclaim -- so a
    grant cannot outlive the run it authorizes even before TTL expiry or
    purge). Returns rows touched."""
    from hermes_cli.kanban_db_connect import write_txn

    now = int(time.time())
    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            "UPDATE worker_invocation_grants SET revoked_at = ? "
            "WHERE task_id = ? AND run_id = ? AND revoked_at IS NULL",
            (now, task_id, int(run_id)),
        )
    return int(cur.rowcount or 0)


def purge_expired_invocation_grants(conn: sqlite3.Connection) -> int:
    """Delete grants past ``expires_at``; returns rows removed. Revoked rows
    are retained until their natural expiry (non-secret grant_id/task/run/
    pid/timestamps stay available for incident review per design doc
    section 11) -- only the token_digest ever needs protecting, and a
    revoked/expired row can never authorize anything regardless."""
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            "DELETE FROM worker_invocation_grants WHERE expires_at < ?",
            (int(time.time()),),
        )
    return int(cur.rowcount or 0)


def verify_worker_invocation_grant(
    conn: sqlite3.Connection, token: Optional[str], *, now: Optional[int] = None,
) -> GrantAuthority:
    """Verify ``token`` against ``conn`` (the EXACT mutation connection --
    never independently re-resolved from environment) and return a
    :class:`GrantAuthority`, or raise ``PermissionError``.

    Every exit path other than a fully matched, active, correctly-scoped row
    raises. There is no partial-credit return value: a caller either gets a
    typed authority object or an exception, matching the design doc's
    "never returns ordinary allowed" contract.

    Checked in one query, under the caller's existing transaction (callers
    performing the final authority check must do so inside the same
    ``BEGIN IMMEDIATE``/savepoint as the mutation -- see
    ``kanban_db_connect.write_txn`` -- so a revoke/reclaim racing this check
    cannot slip between verification and write; this function itself does
    not open a transaction, it only reads within whatever the caller
    already holds):

    * token well-formed and digest matches (``hmac.compare_digest``);
    * activated (not pending), not expired, not revoked;
    * the CALLING process's OWN (pid, kernel-start) matches the row's
      bound identity;
    * ``tasks.current_run_id`` = the grant's run_id, ``tasks.status`` =
      'running', and the matching ``task_runs.status`` = 'running' -- so
      reclaim/completion invalidates the grant immediately, before GC.
    """
    parsed = _parse_token(token)
    if parsed is None:
        raise GrantDenied(
            "kanban: no valid worker invocation grant presented",
            reason_code="missing" if not token else "malformed",
        )
    grant_id, secret_hex = parsed
    digest = _token_digest(secret_hex)

    self_identity = _self_pid_start()
    if self_identity is None:
        raise GrantDenied(
            "kanban: cannot verify this process's own kernel identity; "
            "denying mutation (invocation-authority check fails closed)",
            reason_code="unadmitted-path",
        )
    self_pid, self_start = self_identity

    when = int(now) if now is not None else int(time.time())

    try:
        row = conn.execute(
            "SELECT g.token_digest AS token_digest, g.task_id AS task_id, "
            "g.run_id AS run_id, g.worker_pid AS worker_pid, "
            "g.proc_start AS proc_start, g.activated_at AS activated_at, "
            "g.expires_at AS expires_at, g.revoked_at AS revoked_at, "
            "t.current_run_id AS t_current_run_id, t.status AS t_status, "
            "r.status AS r_status "
            "FROM worker_invocation_grants g "
            "LEFT JOIN tasks t ON t.id = g.task_id "
            "LEFT JOIN task_runs r ON r.id = g.run_id "
            "WHERE g.grant_id = ?",
            (grant_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            # Missing schema after enforcement cutover must deny, not
            # silently behave as "no grants exist, allow" -- a board that
            # somehow lacks this table post-cutover is a corrupted/rolled-
            # back deployment, not an ordinary empty-grants state.
            raise GrantDenied(
                "kanban: worker_invocation_grants schema missing; denying mutation",
                reason_code="missing-schema",
            ) from exc
        raise GrantDenied(
            f"kanban: invocation-authority lookup failed: {exc}", reason_code="missing-schema",
        ) from exc
    except sqlite3.Error as exc:
        raise GrantDenied(
            f"kanban: invocation-authority lookup failed: {exc}", reason_code="missing-schema",
        ) from exc

    if row is None:
        raise GrantDenied("kanban: unknown worker invocation grant", reason_code="malformed")
    if not hmac.compare_digest(bytes(row["token_digest"]), digest):
        raise GrantDenied("kanban: worker invocation grant token mismatch", reason_code="malformed")
    if row["activated_at"] is None:
        raise GrantDenied("kanban: worker invocation grant not yet activated", reason_code="missing")
    if row["revoked_at"] is not None:
        raise GrantDenied("kanban: worker invocation grant revoked", reason_code="revoked")
    if row["expires_at"] is None or int(row["expires_at"]) <= when:
        raise GrantDenied("kanban: worker invocation grant expired", reason_code="expired")
    if row["worker_pid"] is None or row["proc_start"] is None:
        raise GrantDenied(
            "kanban: worker invocation grant has no bound identity", reason_code="missing",
        )
    if int(row["worker_pid"]) != self_pid or int(row["proc_start"]) != self_start:
        raise GrantDenied(
            "kanban: worker invocation grant is bound to a different process "
            "(PID/kernel-start mismatch) -- denying mutation",
            reason_code="wrong-process",
        )
    if row["t_current_run_id"] is None or int(row["t_current_run_id"]) != int(row["run_id"]):
        raise GrantDenied(
            "kanban: worker invocation grant's run is no longer the task's "
            "current run -- denying mutation",
            reason_code="stale-run",
        )
    if row["t_status"] != "running":
        raise GrantDenied(
            "kanban: worker invocation grant's task is not in the running "
            "state -- denying mutation",
            reason_code="stale-run",
        )
    if row["r_status"] != "running":
        raise GrantDenied(
            "kanban: worker invocation grant's run is not in the running "
            "state -- denying mutation",
            reason_code="stale-run",
        )

    return GrantAuthority(
        grant_id=grant_id, task_id=str(row["task_id"]), run_id=int(row["run_id"]),
    )


@dataclass(frozen=True)
class AuthorityDecision:
    """Non-secret outcome of :func:`decide_mutation_authority` -- what Phase A
    telemetry records and what Phase B's default-deny cutover enforces.

    ``authority_class`` is one of the design's Phase A telemetry classes:
    ``worker`` / ``dispatcher`` / ``gateway_notifier`` / ``dashboard_request``
    / ``maintenance`` / ``test`` / ``none``. ``grant`` is populated only for
    ``worker``. ``deny_reason`` is populated only when ``allowed`` is False.
    """

    allowed: bool
    authority_class: str
    grant: Optional[GrantAuthority] = None
    deny_reason: Optional[str] = None


def decide_mutation_authority(conn: Optional[sqlite3.Connection]) -> AuthorityDecision:
    """The single evaluation-order predicate for a Kanban mutation (design
    section 6). Never itself mutates anything; callers combine this with
    their own transaction boundary. Order, exactly per the design:

    1. ``_DELEGATED_CHILD_CONTEXT`` / ``_NON_DISPATCHER_OWNED_CONTEXT`` true
       => deny, even over a valid worker token or trusted context (an
       in-process delegate/cron scope must never borrow its parent's
       authority). This mirrors the existing live
       ``_assert_not_delegated_child_mutation`` guard, which callers using
       :func:`hermes_cli.kanban_db_connect.write_txn` already get for free
       as an unconditional (non-Phase-B-gated) deny -- this function's own
       check here exists so Phase A telemetry also sees and classifies that
       denial the same way non-write_txn callers would.
    2. An explicit trusted in-process authority context
       (:mod:`hermes_cli.kanban_authority_context`) => authorize only its
       declared scope (D1/D2/H1/M1/test).
    3. A valid PID-bound worker grant => authorize as ``worker``.
    4. Otherwise => deny.
    """
    from agent.delegation_context import _NON_DISPATCHER_OWNED_CONTEXT, is_delegated_child_process_context
    from hermes_cli import kanban_authority_context as kac

    real_conn = conn if isinstance(conn, sqlite3.Connection) else None

    try:
        # Both signals from design section 6 item 1: the delegate_task
        # child ContextVar/kernel-ancestry check (W3) AND the separate
        # worker-fired-cron ContextVar (W4, ``_NON_DISPATCHER_OWNED_CONTEXT``
        # -- distinct from delegated-child by design so cron-specific
        # subprocess env/error-string behavior stays unchanged, but it is
        # an EQUALLY overriding deny here per the inventory's W4 row: "DENY
        # before all positive grants. The non-dispatcher-owned ContextVar
        # overrides a valid worker grant"). Checking only the delegated-
        # child predicate here would silently let an in-process
        # ``cronjob(action="run")`` mutate Kanban using its parent worker's
        # still-valid grant, since cron never spawns a new kernel process.
        delegated = is_delegated_child_process_context(real_conn) or bool(
            _NON_DISPATCHER_OWNED_CONTEXT.get()
        )
    except Exception:
        delegated = True  # fail closed, mirrors _assert_not_delegated_child_mutation's own except-path
    if delegated:
        return AuthorityDecision(allowed=False, authority_class="none", deny_reason="context-override")

    trusted_class = kac.current_trusted_authority_class()
    if trusted_class is not None:
        return AuthorityDecision(allowed=True, authority_class=trusted_class)

    token = os.environ.get(GRANT_ENV_VAR)
    if real_conn is None:
        # No genuine mutation connection to verify a worker grant against
        # (e.g. board-pointer writers per design section 5, or a caller
        # passing a non-sqlite3 test double) -- not itself sufficient
        # authority regardless of token presence.
        return AuthorityDecision(allowed=False, authority_class="none", deny_reason="unadmitted-path")
    try:
        authority = verify_worker_invocation_grant(real_conn, token)
    except GrantDenied as exc:
        return AuthorityDecision(allowed=False, authority_class="none", deny_reason=exc.reason_code)
    except PermissionError:
        return AuthorityDecision(allowed=False, authority_class="none", deny_reason="malformed")
    return AuthorityDecision(allowed=True, authority_class="worker", grant=authority)


def record_authority_decision_telemetry(
    decision: AuthorityDecision, *, operation: str, board: Optional[str] = None,
) -> None:
    """Phase A non-secret, decision-only telemetry (design section 8 Phase A
    telemetry decision; t_c67e90a0). NEVER logs: token, digest, grant
    plaintext, request auth headers/cookies/tickets, PII, task bodies,
    comments, or attachment content -- only the fields the design's minimum
    set allows (decision, authority class, operation family, board slug,
    non-secret grant id, denial reason code). Telemetry itself never
    authorizes anything and is called unconditionally by
    :func:`hermes_cli.kanban_db_connect.write_txn` regardless of whether
    Phase B enforcement (:func:`enforcement_enabled`) is on.

    Denies log at INFO (an operator watching Phase A needs to see
    would-deny paths without raising the log level); allows log at DEBUG
    (routine, would otherwise flood every ordinary write)."""
    board_label = board or "<active>"
    grant_id = decision.grant.grant_id if decision.grant else None
    if decision.allowed:
        _log.debug(
            "kanban invocation-authority telemetry: decision=would_allow "
            "authority_class=%s operation=%s board=%s grant_id=%s",
            decision.authority_class, operation, board_label, grant_id,
        )
    else:
        _log.info(
            "kanban invocation-authority telemetry: decision=would_deny "
            "authority_class=%s operation=%s board=%s reason=%s",
            decision.authority_class, operation, board_label, decision.deny_reason,
        )
