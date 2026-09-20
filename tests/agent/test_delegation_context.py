"""Regression tests for ``is_delegated_child_process_context``.

A process that once legitimately spawned a ``delegate_task`` subprocess
carries ``HERMES_DELEGATED_CHILD_CONTEXT=1`` in its own ``os.environ``
afterwards (normal subprocess env inheritance via ``env = os.environ.copy()``
call sites such as ``work_claims/core.py::_run()``). That stale marker must
not misidentify unrelated in-process work — most notably a plain cron tick,
which only enters ``_NON_DISPATCHER_OWNED_CONTEXT`` — as a delegated child.
At the same time, both the genuine ContextVar signal and the genuine
fresh-subprocess-boundary env-marker signal must keep working.
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


def test_poisoned_env_overridden_by_non_dispatcher_context(monkeypatch):
    """Case (a): a stale env marker + an active non-dispatcher (cron) context
    must NOT be read as a delegated child -- this is the bug being fixed."""
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, "1")

    token = dc.enter_non_dispatcher_owned_context()
    try:
        assert dc.is_delegated_child_process_context() is False
    finally:
        dc.exit_non_dispatcher_owned_context(token)


def test_genuine_delegated_child_contextvar_wins(monkeypatch):
    """Case (b): the ContextVar always wins, even with a poisoned or absent
    env marker."""
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
    """Case (c): a real fresh-subprocess boundary (env marker present, no
    ContextVars set at all) must still be detected -- do not regress this."""
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, "1")

    assert dc._DELEGATED_CHILD_CONTEXT.get() is False
    assert dc._NON_DISPATCHER_OWNED_CONTEXT.get() is False
    assert dc.is_delegated_child_process_context() is True


def test_no_marker_no_context_is_false(monkeypatch):
    """Sanity: absence of everything means not a delegated child."""
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    assert dc.is_delegated_child_process_context() is False
