"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel at the fleet root; ``hermes resume`` removes
it. While it exists the cron scheduler, kanban dispatcher and new gateway
turns skip work; in-flight work is never killed. The check is one or two uncached
``os.stat`` calls (process home + fleet root when they differ). The body is optional
JSON ``{"reason", "engaged_at"}``; a corrupt/empty file still counts as engaged
(fail safe, e.g. ``touch ~/.hermes/ESTOP``). Ported from gastownhall/gastown estop.go (MIT).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Same profile-aware / fleet-root resolvers the file-safety guards use (fail-open to ~/.hermes).
from agent.file_safety import _hermes_home_path as _hermes_home, _hermes_root_path as _canonical_root

SENTINEL_NAME = "ESTOP"
AUDIT_NAME = "ESTOP_HISTORY.jsonl"
_AUDIT_MAX_BYTES = 1024 * 1024

# Per-component "logged already for this engagement" flags: log once per engagement, not per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()


def sentinel_path() -> Path:
    """Profile-local ESTOP path used by the low-level component API."""
    return _hermes_home() / SENTINEL_NAME


def fleet_sentinel_path() -> Path:
    """Canonical fleet-wide ESTOP path used by operator-facing pause commands."""
    return _canonical_root() / SENTINEL_NAME


def _candidate_sentinel_paths() -> list:
    """Profile home first, then the fleet root if it is a different directory: a profile
    gateway (HERMES_HOME=~/.hermes/profiles/<n>) must still honor an operator's ~/.hermes/ESTOP."""
    primary = sentinel_path()
    try:
        root = _canonical_root() / SENTINEL_NAME
    except Exception:
        return [primary]
    try:
        distinct = root.resolve() != primary.resolve()
    except Exception:
        # Non-Path test doubles fail .resolve(); plain equality still dedupes.
        distinct = root != primary
    return [primary, root] if distinct else [primary]


def is_engaged() -> bool:
    """True if ANY candidate sentinel exists; fail SAFE (True) on stat errors."""
    saw_stat_error = False
    for path in _candidate_sentinel_paths():
        try:
            if path.exists():
                return True
        except OSError:
            saw_stat_error = True
    return saw_stat_error


def _operator_identity(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("HERMES_PROFILE") or os.environ.get("USER") or "operator").strip()


def _audit(action: str, payload: dict) -> None:
    """Append a bounded durable pause/resume receipt; ESTOP itself is deleted on resume."""
    path = _canonical_root() / AUDIT_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= _AUDIT_MAX_BYTES:
            rotated = path.with_suffix(".jsonl.1")
            with suppress(OSError):
                rotated.unlink()
            path.replace(rotated)
        record = {"action": action, "at": datetime.now(timezone.utc).isoformat(), **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        logging.getLogger(__name__).warning("could not write ESTOP audit receipt", exc_info=True)


def engage(reason: Optional[str] = None, *, engaged_by: Optional[str] = None) -> Path:
    """Create the process-local ESTOP sentinel for component-level callers."""
    return _engage_at(sentinel_path(), reason=reason, engaged_by=engaged_by)


def engage_global(reason: Optional[str] = None, *, engaged_by: Optional[str] = None) -> Path:
    """Create the canonical fleet-root ESTOP regardless of active profile."""
    return _engage_at(fleet_sentinel_path(), reason=reason, engaged_by=engaged_by)


def _engage_at(path: Path, *, reason: Optional[str], engaged_by: Optional[str]) -> Path:
    """Create one ESTOP sentinel. Idempotent; re-engaging updates the file."""
    payload = {
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "engaged_by": _operator_identity(engaged_by),
        "reason": reason or None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        with suppress(OSError):  # Best effort: an empty/partial sentinel still pauses (fail safe).
            path.touch(exist_ok=True)
    _audit("engaged", payload)
    return path


def disengage(*, resumed_by: Optional[str] = None, reason: Optional[str] = None) -> bool:
    """Remove every visible sentinel (process-local and fleet-root)."""
    lifted = False
    for path in _candidate_sentinel_paths():
        try:
            path.unlink()
            lifted = True
        except (OSError, AttributeError):
            continue
    if lifted:
        _audit("resumed", {"resumed_by": _operator_identity(resumed_by), "reason": reason or None})
    return lifted


def get_state() -> Optional[dict]:
    """Return ``{"reason", "engaged_at"}`` or None when not engaged; an unreadable/corrupt
    body still reports engaged with both fields None."""
    if not is_engaged():
        return None
    state = {"reason": None, "engaged_at": None, "engaged_by": None}
    found = False
    for path in _candidate_sentinel_paths():
        try:
            if not path.exists():
                continue
        except OSError:
            return state
        except AttributeError:
            continue
        found = True
        with suppress(OSError, ValueError, AttributeError):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = {
                    "reason": raw.get("reason") or None,
                    "engaged_at": raw.get("engaged_at") or None,
                    "engaged_by": raw.get("engaged_by") or None,
                }
                break
    return state if found else None


def paused_reply() -> Optional[str]:
    """Short user-facing notice for new gateway turns, or None if not paused."""
    state = get_state()
    if state is None:
        return None
    tag = f" ({state['reason']})" if state.get("reason") else ""
    return f"⏸️ Hermes is paused{tag}. New work is on hold; run `hermes resume` to pick things back up."


def check_paused(component: str, logger: logging.Logger) -> bool:
    """Return True when engaged, logging once per engagement per component (re-armed after a resume)."""
    if not is_engaged():
        with _log_lock:
            _logged_components.discard(component)
        return False
    with _log_lock:
        first = component not in _logged_components
        _logged_components.add(component)
    if first:
        reason = (get_state() or {}).get("reason")
        suffix = f" (reason: {reason})" if reason else ""
        logger.info(
            "%s dispatch paused by global emergency stop%s — remove with `hermes resume` (%s)",
            component, suffix, sentinel_path(),
        )
    return True


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
