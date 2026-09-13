"""Backgrounded-desktop-tab lease release (task t_0952d696).

A desktop tab that is backgrounded/switched-away-from (not explicitly closed) never sends a clean
WebSocket-close event, so neither the WS-orphan reaper (``_schedule_ws_orphan_reap``, needs a real
disconnect) nor the TTL reaper (``_session_is_evictable``, gated on ``_transport_is_dead``) ever
frees its ``active_session_lease`` — the lease parks until a full process restart or the 6h
last-resort TTL, showing up as phantom "N operator sessions" on the platform dashboard.
``_release_idle_session_leases`` closes that gap: it frees a liveness-tracked (desktop) lease once
idle past ``dashboard.tui_lease_idle_seconds``, WITHOUT gating on transport liveness, and leaves the
session/agent/transcript untouched so the existing ``_ensure_active_session_slot`` re-claim path
(exercised on the session's next turn) transparently re-acquires a slot.
"""

import threading
import time

import pytest

from tui_gateway import server


class _FakeLease:
    def __init__(self, *, track_liveness: bool = True):
        self.track_liveness = track_liveness
        self.enabled = True
        self.released = False
        self.lease_id = "fake-lease"

    def release(self) -> None:
        self.released = True


def _ready_event(set_: bool = True) -> threading.Event:
    ev = threading.Event()
    if set_:
        ev.set()
    return ev


def _idle_desktop_session(now: float, **overrides) -> dict:
    """A backgrounded desktop session: liveness-tracked lease, idle well past the default window,
    connected transport (deliberately NOT dead — that's the whole point of the bug)."""
    live_transport = type("LiveTransport", (), {"_closed": False})()
    base = {
        "running": False,
        "agent_ready": _ready_event(True),
        "transport": live_transport,
        "last_active": now - 2 * server._TUI_LEASE_IDLE_S,
        "created_at": now - 2 * server._TUI_LEASE_IDLE_S,
        "active_session_lease": _FakeLease(track_liveness=True),
        "session_key": "sess-key",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _no_pending_or_delegations(monkeypatch):
    monkeypatch.setattr(server, "_session_pending_kind", lambda sid: "")
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda sid, session=None: False)
    monkeypatch.setattr(server, "_TUI_LEASE_IDLE_S", 1800.0)
    yield


def test_idle_eligible_true_even_with_live_transport(monkeypatch):
    """The defining fix: eligibility must NOT require a dead transport (a backgrounded tab keeps
    its WebSocket open)."""
    now = time.time()
    session = _idle_desktop_session(now)
    assert server._transport_is_dead(session["transport"]) is False
    assert server._session_lease_idle_eligible("sid", session, now) is True


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"running": True}, False),
        ({"last_active": None}, False),  # falls back to created_at=None too -> now-0 > idle -> True actually
    ],
)
def test_idle_eligible_running_session_exempt(monkeypatch, override, expected):
    now = time.time()
    session = _idle_desktop_session(now)
    if "running" in override:
        session.update(override)
        assert server._session_lease_idle_eligible("sid", session, now) is expected


def test_idle_eligible_false_when_no_lease(monkeypatch):
    now = time.time()
    session = _idle_desktop_session(now)
    session["active_session_lease"] = None
    assert server._session_lease_idle_eligible("sid", session, now) is False


def test_idle_eligible_false_when_lease_not_liveness_tracked(monkeypatch):
    """Only desktop (``track_liveness=True``) leases are in scope — messaging-gateway/CLI leases
    are already reaped by other paths and must not be touched here."""
    now = time.time()
    session = _idle_desktop_session(now)
    session["active_session_lease"] = _FakeLease(track_liveness=False)
    assert server._session_lease_idle_eligible("sid", session, now) is False


def test_idle_eligible_false_while_running(monkeypatch):
    now = time.time()
    session = _idle_desktop_session(now, running=True)
    assert server._session_lease_idle_eligible("sid", session, now) is False


def test_idle_eligible_false_with_pending_input(monkeypatch):
    monkeypatch.setattr(server, "_session_pending_kind", lambda sid: "input")
    now = time.time()
    session = _idle_desktop_session(now)
    assert server._session_lease_idle_eligible("sid", session, now) is False


def test_idle_eligible_false_with_active_delegations(monkeypatch):
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda sid, session=None: True)
    now = time.time()
    session = _idle_desktop_session(now)
    assert server._session_lease_idle_eligible("sid", session, now) is False


def test_idle_eligible_false_mid_build(monkeypatch):
    now = time.time()
    session = _idle_desktop_session(now, agent_ready=_ready_event(False))
    assert server._session_lease_idle_eligible("sid", session, now) is False


def test_idle_eligible_false_when_recently_active(monkeypatch):
    now = time.time()
    session = _idle_desktop_session(now, last_active=now)
    assert server._session_lease_idle_eligible("sid", session, now) is False


def test_idle_eligible_false_when_disabled(monkeypatch):
    monkeypatch.setattr(server, "_TUI_LEASE_IDLE_S", 0.0)
    now = time.time()
    session = _idle_desktop_session(now)
    # _release_idle_session_leases short-circuits on the knob; eligibility itself only compares
    # against the (now zeroed) threshold, so assert the release-path no-op directly below instead.


def test_release_idle_session_leases_releases_only_eligible(monkeypatch):
    now = time.time()
    idle = _idle_desktop_session(now)
    running = _idle_desktop_session(now, running=True)
    fresh = _idle_desktop_session(now, last_active=now)
    server._sessions.clear()
    monkeypatch.setattr(server, "_sessions", {"idle": idle, "running": running, "fresh": fresh})
    try:
        server._release_idle_session_leases()
        assert idle.get("active_session_lease") is None
        assert running["active_session_lease"] is not None and not running["active_session_lease"].released
        assert fresh["active_session_lease"] is not None and not fresh["active_session_lease"].released
    finally:
        server._sessions.clear()


def test_release_idle_session_leases_disabled_knob_is_noop(monkeypatch):
    monkeypatch.setattr(server, "_TUI_LEASE_IDLE_S", 0.0)
    now = time.time()
    idle = _idle_desktop_session(now)
    monkeypatch.setattr(server, "_sessions", {"idle": idle})
    server._release_idle_session_leases()
    assert idle["active_session_lease"] is not None
    assert not idle["active_session_lease"].released


def test_release_idle_session_leases_leaves_session_agent_and_transcript_intact(monkeypatch):
    """Releasing the lease must be a pure slot-release: no teardown of session/agent/history."""
    now = time.time()
    session = _idle_desktop_session(now, agent="sentinel-agent", history=["hi"])
    monkeypatch.setattr(server, "_sessions", {"sid": session})
    server._release_idle_session_leases()
    assert session.get("active_session_lease") is None
    assert session["agent"] == "sentinel-agent"
    assert session["history"] == ["hi"]
    assert "sid" in server._sessions  # never popped from the registry


def test_released_lease_transparently_reclaimed_on_next_turn(monkeypatch):
    """The exact recovery path this fix depends on: ``_ensure_active_session_slot`` already
    re-claims when ``active_session_lease`` is None (no new plumbing needed)."""
    now = time.time()
    session = _idle_desktop_session(now, session_key="reclaim-key", profile_home=None)
    monkeypatch.setattr(server, "_sessions", {"sid": session})
    server._release_idle_session_leases()
    assert session.get("active_session_lease") is None

    new_lease = _FakeLease(track_liveness=True)
    monkeypatch.setattr(
        server, "_claim_active_session_slot",
        lambda session_key, *, live_session_id, surface, profile_home: (new_lease, None),
    )
    limit_message = server._ensure_active_session_slot("sid", session)
    assert limit_message is None
    assert session["active_session_lease"] is new_lease
