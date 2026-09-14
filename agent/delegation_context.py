"""Context-local state for delegate_task child execution.

A Hermes process may itself be a Kanban dispatcher worker with HERMES_KANBAN_* in
os.environ. In-process delegate_task children and cron jobs fired via
``cronjob(action="run")`` are NOT dispatcher-owned, so identity gates must fail
closed for them without mutating the process-global environment.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping, overload

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar("hermes_delegated_child_context", default=False)
# Any in-process execution that is NOT the dispatcher-owned worker (cron jobs). Kept separate
# so delegate_task-specific behaviour (subprocess env scrubbing, its error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar("hermes_non_dispatcher_owned_context", default=False)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS",
)


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token form of :func:`non_dispatcher_owned_context` for long try/finally scopes."""
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task; without it
    a cron agent run inside a worker is misread as that worker (kanban toolset force-added,
    ``kanban_complete`` defaulting to its task). ContextVar-scoped rather than clearing
    os.environ, which the worker's claim heartbeat and concurrent readers share."""
    token = enter_non_dispatcher_owned_context()
    try:
        yield
    finally:
        exit_non_dispatcher_owned_context(token)


def is_dispatcher_owned_worker_context() -> bool:
    """The single predicate every ``HERMES_KANBAN_*`` identity gate should use."""
    return not (is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get())


def is_delegated_child_process_context(
    conn: "sqlite3.Connection | None" = None, *, board: str | None = None,
) -> bool:
    """Return True in this process or a subprocess spawned by a child.

    *conn*/*board* thread straight through to
    :func:`hermes_cli.kanban_worker_lineage.is_descendant_of_dispatcher_worker`:
    pass ``conn`` when the caller already has a connection bound to the
    specific board/DB file it is mutating (the strongest, most accurate
    signal — no re-derivation at all), else pass ``board`` when the caller
    knows the actual board slug it is operating against (e.g. an explicit
    ``--board``/``args["board"]``/``write_board_metadata(board=...)``
    target). Passing neither falls through to the process's ambient
    current-board resolution, which is only correct when the caller has no
    more specific board context of its own (e.g. ``set_current_board``,
    which mutates the board POINTER itself, not board-scoped data). Getting
    this wrong reopens the same board-divergence bug class already found
    and fixed twice on this subsystem (Maya BLOCK findings, t_0977ea27 /
    t_85586891): a check scoped to the wrong board either wrongly denies a
    legitimate same-board worker or wrongly authorises a cross-board
    mutation.

    Two independent signals, either sufficient:

    * The in-process ContextVar (``_DELEGATED_CHILD_CONTEXT``) — set by
      :func:`delegated_child_context` around an in-process ``delegate_task``
      child's execution. Unforgeable from outside the process: nothing but
      this module's own code can set a ContextVar in this interpreter.
    * Kernel-verified process ancestry (CV-A01 fix, t_70827e4e / t_11e8c077):
      :func:`hermes_cli.kanban_worker_lineage.is_descendant_of_dispatcher_worker`
      walks this process's REAL kernel ancestors and asks whether any of them
      is a Kanban worker the dispatcher itself spawned. A subprocess of that
      worker (a ``terminal`` tool child, a cron job's own subprocess, or a
      re-exec'd ``hermes`` CLI invocation) cannot make itself NOT a
      descendant by clearing an environment variable — ``getppid()`` and each
      ancestor's kernel start time come from the OS, not from anything the
      process can set.

    The previous implementation ALSO consulted the bare environment variable
    ``HERMES_DELEGATED_CHILD_CONTEXT`` here, which a subprocess trivially
    strips before re-exec (``env -u HERMES_DELEGATED_CHILD_CONTEXT hermes
    kanban comment ...``) to impersonate an ordinary, ungated invocation.
    That check is now backstopped by the kernel-ancestry lookup below rather
    than replaced by it: the marker's PRESENCE is still trusted (it is only
    ever set by this module's own ``delegated_child_subprocess_env``/
    ``scrub_kanban_env`` on a subprocess env before exec, so a descendant
    cannot have forged it), but its ABSENCE is no longer sufficient on its
    own -- a descendant that clears it is still caught by the kernel-verified
    ancestry check.
    """
    if bool(_DELEGATED_CHILD_CONTEXT.get()):
        return True
    if bool(os.environ.get(DELEGATED_CHILD_ENV_MARKER)):
        # Trusting the marker's PRESENCE is safe even though its ABSENCE is not:
        # it is only ever set by trusted code (delegated_child_subprocess_env)
        # on a subprocess env before exec, so a descendant seeing it set here
        # cannot have forged it. The kernel-ancestry check below closes the
        # actual vulnerability (a descendant *clearing* the marker to fake
        # non-delegated status); it does not need to also re-litigate the
        # case where the marker is honestly still present.
        return True
    try:
        from hermes_cli.kanban_worker_lineage import is_descendant_of_dispatcher_worker

        return is_descendant_of_dispatcher_worker(conn, board=board)
    except Exception:
        # The ancestry mechanism itself is unavailable (e.g. psutil missing).
        # Fail closed: the env var is no longer trusted as a substitute, so
        # the only remaining honest answer that cannot be gamed by clearing
        # an env var is "assume delegated, deny mutation".
        return True


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Remove worker identity, retaining board/location and an inherited write fence.

    TASK absence alone would promote a descendant to an orchestrator. The marker
    survives later execs, including scripts that remove TASK themselves. This is
    cooperative runtime scoping, not confinement of code with direct SQLite access.
    """
    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    cleaned[DELEGATED_CHILD_ENV_MARKER] = "1"
    return cleaned


@overload
def delegated_child_subprocess_env(env: Mapping[str, str]) -> dict[str, str]: ...


@overload
def delegated_child_subprocess_env(env: None = None) -> dict[str, str] | None: ...


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Carry worker/delegate descendant denial across a real process spawn.

    Location and credentials are untouched; callers retain their existing secret policy.
    Dispatcher workers and supervised tool transports grant their own explicit scope.
    """
    if not (is_delegated_child_process_context() or os.environ.get("HERMES_KANBAN_TASK")
            or (env and (env.get("HERMES_KANBAN_TASK") or env.get(DELEGATED_CHILD_ENV_MARKER)))):
        return None if env is None else dict(env)
    return scrub_kanban_env(os.environ if env is None else env)
