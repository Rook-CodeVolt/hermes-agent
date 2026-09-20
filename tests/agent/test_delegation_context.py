"""Regression tests for ``is_delegated_child_process_context``.

PR #35 attempted to fix a stale-``delegate_task``-env-marker bug (a process
that once legitimately spawned a ``delegate_task`` subprocess permanently
carries ``HERMES_DELEGATED_CHILD_CONTEXT=1`` in its own ``os.environ``
afterwards via normal subprocess env inheritance) by having
``is_delegated_child_process_context()`` let an active
``_NON_DISPATCHER_OWNED_CONTEXT`` (entered by cron ticks via
``enter_non_dispatcher_owned_context()``) override a stale env marker.

Maya's security review (independent reviewer, CHANGES_REQUESTED) found that
wiring was a confused-deputy privilege escalation: ``enter_non_dispatcher_
owned_context()`` is unauthenticated and module-level, so a genuine
``delegate_task`` child running in a fresh-subprocess boundary (env marker
set, no ContextVar because ContextVars don't cross a fork) could call it
itself — via ``execute_code``/``terminal``, neither of which is in
``DELEGATE_BLOCKED_TOOLS`` — to flip ``is_delegated_child_process_context()``
from True back to False, bypassing the Kanban-mutation trust boundary
(``hermes_cli/kanban_db.py::_assert_not_delegated_child_mutation`` and
``hermes_cli/kanban.py``'s CLI-dispatch guard).

Remediation: ``_NON_DISPATCHER_OWNED_CONTEXT`` is fully decoupled from this
predicate again. ``is_dispatcher_owned_worker_context()`` keeps its own
narrower, unrelated behavior untouched (covered in
``tests/cron/test_cron_kanban_env_isolation.py``). The original stale-marker
bug is fixed at its source instead: ``hermes_cli/kanban_db.py::_default_spawn``
scrubs ``DELEGATED_CHILD_ENV_MARKER`` out of a freshly-dispatched worker's
env, since a dispatcher-spawned Kanban worker is by construction never itself
delegate_task-child lineage (covered in
``tests/hermes_cli/test_kanban_db.py``).
"""

from __future__ import annotations

import pytest

import agent.delegation_context as dc


@pytest.fixture(autouse=True)
def _clean_context_and_env(monkeypatch):
    """Ensure no ContextVar/env state leaks between tests."""
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    dc._DELEGATED_CHILD_CONTEXT.set(False)
    dc._NON_DISPATCHER_OWNED_CONTEXT.set(False)
    yield
    dc._DELEGATED_CHILD_CONTEXT.set(False)
    dc._NON_DISPATCHER_OWNED_CONTEXT.set(False)


def test_genuine_delegated_child_contextvar_wins(monkeypatch):
    """The ContextVar always wins, even with a poisoned or absent env marker."""
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    token = dc._DELEGATED_CHILD_CONTEXT.set(True)
    try:
        assert dc.is_delegated_child_process_context() is True
    finally:
        dc._DELEGATED_CHILD_CONTEXT.reset(token)

    # Also true with a poisoned marker present alongside the ContextVar.
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, "1")
    token = dc._DELEGATED_CHILD_CONTEXT.set(True)
    try:
        assert dc.is_delegated_child_process_context() is True
    finally:
        dc._DELEGATED_CHILD_CONTEXT.reset(token)


def test_env_marker_only_still_detected(monkeypatch):
    """A real fresh-subprocess boundary (env marker present, no ContextVars
    set at all) must still be detected."""
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, "1")

    assert dc._DELEGATED_CHILD_CONTEXT.get() is False
    assert dc._NON_DISPATCHER_OWNED_CONTEXT.get() is False
    assert dc.is_delegated_child_process_context() is True


def test_no_marker_no_context_is_false(monkeypatch):
    """Sanity: absence of everything means not a delegated child."""
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    assert dc.is_delegated_child_process_context() is False


def test_non_dispatcher_owned_context_cannot_override_established_delegated_child_verdict_via_contextvar(
    monkeypatch,
):
    """Confused-deputy regression (PR #35 review): once the ContextVar
    establishes a delegated-child verdict, no subsequent in-process call to
    ``enter_non_dispatcher_owned_context()`` (or the ``with`` form) may flip
    ``is_delegated_child_process_context()`` back to False."""
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    token = dc._DELEGATED_CHILD_CONTEXT.set(True)
    try:
        assert dc.is_delegated_child_process_context() is True

        non_dispatcher_token = dc.enter_non_dispatcher_owned_context()
        try:
            assert dc.is_delegated_child_process_context() is True, (
                "enter_non_dispatcher_owned_context() must not be able to "
                "clear an established delegated-child ContextVar verdict"
            )
        finally:
            dc.exit_non_dispatcher_owned_context(non_dispatcher_token)

        with dc.non_dispatcher_owned_context():
            assert dc.is_delegated_child_process_context() is True
    finally:
        dc._DELEGATED_CHILD_CONTEXT.reset(token)


def test_non_dispatcher_owned_context_cannot_override_established_delegated_child_verdict_via_env_marker(
    monkeypatch,
):
    """Same invariant, but for a genuine fresh-subprocess boundary where only
    the env marker (not the ContextVar) establishes the delegated-child
    verdict — this is the exact repro from Maya's PR #35 review: a real
    ``delegate_task`` child subprocess (HERMES_DELEGATED_CHILD_CONTEXT=1, no
    ContextVar because ContextVars don't cross a fork) calling
    ``enter_non_dispatcher_owned_context()`` on itself must NOT be able to
    flip ``is_delegated_child_process_context()`` back to False."""
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, "1")
    assert dc.is_delegated_child_process_context() is True

    non_dispatcher_token = dc.enter_non_dispatcher_owned_context()
    try:
        assert dc.is_delegated_child_process_context() is True
    finally:
        dc.exit_non_dispatcher_owned_context(non_dispatcher_token)

    with dc.non_dispatcher_owned_context():
        assert dc.is_delegated_child_process_context() is True


def test_non_dispatcher_owned_context_still_free_to_flip_when_no_delegated_verdict(
    monkeypatch,
):
    """Sanity companion: when no delegated-child verdict is established at
    all (no ContextVar, no env marker), entering the non-dispatcher-owned
    context has no bearing on this predicate — it's answering a different
    question (see is_dispatcher_owned_worker_context)."""
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    assert dc.is_delegated_child_process_context() is False

    with dc.non_dispatcher_owned_context():
        assert dc.is_delegated_child_process_context() is False
    assert dc.is_delegated_child_process_context() is False
