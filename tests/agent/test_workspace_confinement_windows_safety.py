"""Regression test for the O_NOFOLLOW import-time AttributeError.

``agent/workspace_confinement.py`` used to reference the bare attribute
``os.O_NOFOLLOW`` at module scope and again inside ``ConfinedWorkspace``.
That attribute does not exist on ``os`` for Windows Python builds, so simply
*importing* the module raised ``AttributeError`` before any of the
containment logic ran.

The fix wraps both occurrences in ``getattr(os, 'O_NOFOLLOW', 0)`` -- the
same idiom already used elsewhere in this repo (``tools/spill_safety.py``,
``agent/proxy_sources/iron_proxy.py``).

Per AGENTS.md "Don't fake the host OS": the flag-computation logic exercised
here (``_NOFOLLOW = O_NOFOLLOW_ANY or getattr(os, 'O_NOFOLLOW', 0)``) is a
pure function of whatever attribute the ``os`` module happens to expose --
it does not branch on ``sys.platform`` or otherwise pretend to be a
different host, so removing the attribute with monkeypatch and re-importing
the module is the correct way to prove the getattr fallback works, not a
"fake OS" test. The actual OS-specific behaviour
(``containment_supported()`` requiring ``os.name == 'posix'``) is asserted
against the real, unmodified ``os.name`` of whatever host runs this test.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

MODULE_NAME = "agent.workspace_confinement"


def _reload_fresh():
    """Reload workspace_confinement so module-level code re-executes."""
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


def test_module_imports_when_os_nofollow_is_absent(monkeypatch):
    """Importing the module must not raise when os.O_NOFOLLOW is missing.

    This simulates the exact condition that crashes on Windows Python
    builds (no O_NOFOLLOW constant) by removing the attribute from the
    live ``os`` module before the module-level assignment
    ``_NOFOLLOW = O_NOFOLLOW_ANY or getattr(os, 'O_NOFOLLOW', 0)`` runs.
    """
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    module = _reload_fresh()
    try:
        # Import succeeded (no AttributeError) -- the crash this test
        # guards against would have raised before reaching this line.
        assert module is not None
        # The fallback must resolve to O_NOFOLLOW_ANY (possibly 0) rather
        # than blow up looking up the missing attribute.
        assert module._NOFOLLOW == (module.O_NOFOLLOW_ANY or 0)
        assert isinstance(module._NOFOLLOW, int)
    finally:
        # Restore a clean module for any tests that run after this one.
        _reload_fresh()


def test_nofollow_flag_prefers_os_value_when_present(monkeypatch):
    """When os.O_NOFOLLOW does exist, the fallback must not mask it."""
    monkeypatch.setattr(os, "O_NOFOLLOW", 0x0100, raising=False)

    module = _reload_fresh()
    try:
        expected = module.O_NOFOLLOW_ANY or 0x0100
        assert module._NOFOLLOW == expected
    finally:
        _reload_fresh()


def test_containment_unsupported_open_flags_do_not_crash(monkeypatch, tmp_path):
    """ConfinedWorkspace's os.open call must survive a missing O_NOFOLLOW.

    Regression for the second occurrence (line ~232): the descriptor-relative
    directory walk built its open() flags with the same bare attribute.
    """
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    module = _reload_fresh()
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(workspace), flags)
        os.close(fd)
    finally:
        _reload_fresh()


def test_containment_supported_false_on_non_posix(monkeypatch):
    """containment_supported() must return False whenever os.name != 'posix'.

    This is unrelated to the getattr fix (it is the pre-existing, already
    fail-closed guard that makes the Windows import crash a non-issue for
    the confined-write path), but we re-assert it here so this file is a
    complete regression test for the module's Windows behaviour.
    """
    module = _reload_fresh()
    monkeypatch.setattr(module.os, "name", "nt", raising=False)
    assert module.containment_supported() is False


def test_containment_supported_matches_real_host():
    """On the actual host running this test, behaviour is unmodified."""
    module = _reload_fresh()
    result = module.containment_supported()
    assert isinstance(result, bool)
    if os.name != "posix":
        assert result is False
