"""Host-owned execution-turn lease — the unconditional turn boundary.

``AIAgent.run_conversation`` brackets exactly one agent execution turn in a
``try``/``finally`` that every exit path passes through: normal completion,
an empty or tool-only response, a user interrupt, a failed turn, an escaping
exception, and the two early returns taken when the durable session-turn
lease cannot be acquired. That ``finally`` already balances the deferred
review idle queue (``note_turn_started`` / ``note_turn_finished``); this
module hangs a plugin-visible lease off the same boundary.

Why it exists
-------------
A plugin that must know "is an execution turn actually running for this
session right now?" (the work-claims cross-session lock is the motivating
consumer) previously had to infer it from ``pre_llm_call`` / ``post_llm_call``.
That inference is wrong twice over:

* ``pre_llm_call`` and ``post_llm_call`` are in
  ``hermes_cli.plugins._HOOK_TIMEOUT_BOUNDED_HOOKS``, so the callback runs on
  a short-lived ``hermes-hook-*`` daemon worker that dies the moment the
  callback returns. Anything derived from that thread's identity describes
  the hook dispatcher, not the turn.
* ``post_llm_call`` is fired only when ``final_response and not interrupted``
  (see ``agent/turn_finalizer.py``), so interrupted, failed, empty and
  tool-only turns never produce a closing event at all.

The lease here is instead opened once at turn entry, renewed while the turn
runs, and closed in the host's own ``finally`` with the turn's outcome. The
three hooks are registered in ``_HOOK_CALLER_THREAD_HOOKS`` so they run
synchronously on the real execution thread and are never abandoned by the
hook-timeout worker — a lease whose close could be skipped would be no
better than the boundary it replaces.

Consumers must still treat the lease as advisory-with-expiry: a ``SIGKILL``
or a power loss skips the ``finally`` like it skips everything else, so
``expires_at`` (derived from ``renew_interval_seconds``) and the consumer's
own process-identity checks remain the crash backstop.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BEGIN_HOOK = "on_execution_turn_begin"
RENEW_HOOK = "on_execution_turn_renew"
END_HOOK = "on_execution_turn_end"

#: Identifies this host-process incarnation independently of its reusable PID.
PROCESS_BOOT_ID = uuid.uuid4().hex

#: How often a live lease republishes itself. Consumers derive their expiry
#: from this (a few missed renewals, not one), so it doubles as the crash
#: detection granularity.
DEFAULT_RENEW_INTERVAL_SECONDS = 60.0


def _invoke_required(hook_name: str, payload: Dict[str, Any]) -> None:
    """Synchronously persist and acknowledge one required lease transition."""
    from hermes_cli.lifecycle import invoke_required_hook

    invoke_required_hook(hook_name, **payload)


def _observe(hook_name: str, payload: Dict[str, Any]) -> None:
    """Deliver an acknowledged transition to optional first-party observers."""
    try:
        from hermes_cli.observability import observe_lifecycle

        observe_lifecycle(hook_name, **payload)
    except Exception:
        logger.debug("%s observer dispatch failed", hook_name, exc_info=True)


def _invoke(hook_name: str, payload: Dict[str, Any]) -> None:
    """Persist first; observer delivery remains isolated and best-effort."""
    _invoke_required(hook_name, payload)
    _observe(hook_name, payload)


def _consumed() -> bool:
    """Whether any plugin consumes the execution-turn lease hooks.

    Gates the whole mechanism — including its renewal thread — so a host
    with no interested plugin pays nothing per turn.
    """
    try:
        from hermes_cli import plugins

        return plugins.has_hook(BEGIN_HOOK) or plugins.has_hook(END_HOOK)
    except Exception:
        logger.debug("execution-turn hook consumer probe failed", exc_info=True)
        return False


def _admission_required() -> bool:
    """Whether this process must not run a turn without a lease consumer.

    ``_consumed()`` is a cost gate: an ordinary host with no interested plugin
    skips the lease and pays nothing. For a dispatcher-spawned worker that same
    early return is a fail-open. The work-claims plugin *is* the CV-A01
    containment boundary — the claim gate, the workspace file confinement and
    the terminal sandbox rewrite all hang off hooks it registers — so "no
    plugin consumes the hook" describes precisely the process that has no
    containment, and skipping the lease there would let it run the task
    unconfined and silently. (``HERMES_SAFE_MODE`` inherited across the
    dispatcher's spawn boundary was the way that happened in practice; see
    ``hermes_cli.kanban_db._scrub_worker_env``, which severs the inheritance.
    This check is the fail-closed backstop for every other way discovery can
    come up empty — a disabled plugin, a failed scan, a partial install.)

    A bound worker therefore takes the required-hook path unconditionally, and
    an absent consumer aborts the turn through the existing ``RequiredHookError``
    rather than being waved through. ``get_bound()`` is already ``None`` for
    ``delegate_task`` children, in-process cron jobs and explicitly suppressed
    scopes, so they inherit neither the worker's authority nor its admission
    requirement.
    """
    try:
        from agent import dispatcher_identity

        return dispatcher_identity.get_bound() is not None
    except Exception:
        logger.debug("dispatcher identity probe failed", exc_info=True)
        return False


class ExecutionTurnLease:
    """One host execution turn, published to plugins as a renewable lease.

    ``lease_id`` identifies the turn; ``holder_token`` proves ownership so a
    consumer can refuse a renew/end issued by anything other than the opener
    (a successor turn that reused the same session must not be able to close
    its predecessor's lease, and vice versa).
    """

    def __init__(
        self,
        session_id: str,
        turn_id: str,
        *,
        renew_interval_seconds: float = DEFAULT_RENEW_INTERVAL_SECONDS,
    ) -> None:
        self.session_id = str(session_id or "")
        self.turn_id = str(turn_id or "")
        self.lease_id = f"xturn_{uuid.uuid4().hex}"
        self.holder_token = uuid.uuid4().hex
        self.renew_interval_seconds = float(renew_interval_seconds)
        self.pid = os.getpid()
        self.boot_id = PROCESS_BOOT_ID
        self._lock = threading.Lock()
        self._open = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- payload ---------------------------------------------------------

    def payload(self, **extra: Any) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "lease_id": self.lease_id,
            "holder_token": self.holder_token,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "renew_interval_seconds": self.renew_interval_seconds,
        }
        base.update(extra)
        return base

    # -- lifecycle -------------------------------------------------------

    def _start_renewals(self) -> None:
        if self.renew_interval_seconds <= 0:
            return

        def _loop() -> None:
            while not self._stop.wait(self.renew_interval_seconds):
                with self._lock:
                    if not self._open:
                        return
                _invoke(RENEW_HOOK, self.payload())

        self._thread = threading.Thread(
            target=_loop, name="execution-turn-lease-renew", daemon=True
        )
        self._thread.start()

    def open(self) -> "ExecutionTurnLease":
        with self._lock:
            if self._open:
                return self
            self._open = True
        try:
            _invoke(BEGIN_HOOK, self.payload())
        except Exception:
            # A callback can fail after another required consumer has already
            # persisted the begin. Balance that partial admission before the
            # exception aborts the turn. Preserve the begin failure itself.
            try:
                self.close(outcome="begin_failed")
            except Exception:
                logger.warning(
                    "execution-turn begin cleanup was not acknowledged",
                    exc_info=True,
                )
            raise
        self._start_renewals()
        return self

    def rebind(self, session_id: str) -> None:
        """Move the lease onto a session id resolved after turn entry.

        ``run_conversation`` may adopt a successor session (the previous
        holder compressed and rotated the transcript while this process
        waited on the durable turn lease). Close the lease on the id it was
        opened against and open a fresh one on the new id so exactly one
        lease is live and both ends stay symmetric.
        """
        session_id = str(session_id or "")
        with self._lock:
            if not self._open or not session_id or session_id == self.session_id:
                return
        self.close(outcome="rebound")
        with self._lock:
            self.session_id = session_id
            self.lease_id = f"xturn_{uuid.uuid4().hex}"
            self.holder_token = uuid.uuid4().hex
            self._stop = threading.Event()
            self._thread = None
            self._open = False
        self.open()

    def close(self, *, outcome: str = "") -> None:
        """Close the lease exactly once. Safe to call from any exit path."""
        with self._lock:
            if not self._open:
                return
            self._open = False
            self._stop.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        _invoke(END_HOOK, self.payload(outcome=str(outcome or "")))


def begin(
    session_id: str,
    turn_id: str,
    *,
    renew_interval_seconds: float = DEFAULT_RENEW_INTERVAL_SECONDS,
) -> Optional[ExecutionTurnLease]:
    """Open an execution-turn lease, or return ``None`` when unnecessary.

    ``None`` means the caller has nothing to close: either the turn has no
    stable session id to publish, or no loaded plugin consumes the hooks.

    Raises ``RequiredHookError`` when this process is a bound dispatcher
    worker and nothing consumes the begin hook — see :func:`_admission_required`.
    """
    if not session_id or not turn_id:
        return None
    if not _consumed() and not _admission_required():
        return None
    lease = ExecutionTurnLease(
        session_id, turn_id, renew_interval_seconds=renew_interval_seconds
    )
    return lease.open()


def end(lease: Optional[ExecutionTurnLease], *, outcome: str = "") -> None:
    """Close ``lease`` exactly once and require persistence acknowledgement."""
    if lease is None:
        return
    lease.close(outcome=outcome)
