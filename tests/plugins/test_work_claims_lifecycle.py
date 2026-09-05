"""Production-path coverage for the work-claims plugin's finalize/turn-lease
lifecycle: consumption of the host's ``on_execution_turn_begin/renew/end``
hooks (agent/execution_turn.py), claim-bound deferred finalize, atomic
finalize decisions, and owner-checked/renewable execution leases.

Unlike the plugin's own in-tree unit tests (``plugins/work_claims/
test_work_claims.py``), this suite drives the finalize and turn-lease hooks
through a real, discovery-loaded ``hermes_cli.plugins.PluginManager`` --
exactly the path the host actually calls (``hermes_cli.lifecycle.invoke_hook``
-> ``plugins.invoke_hook`` -> ``PluginManager.invoke_hook``) -- so it proves
the callbacks are wired the way the host will really call them, not just
that ``core.py``'s functions behave correctly in isolation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_cli import plugins as pmod
from plugins import work_claims
from plugins.work_claims import core


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_SAFE_MODE", "0")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["work-claims"]}})
    )
    monkeypatch.setattr(core, "_kanban_create", lambda *a, **k: "t_1")
    monkeypatch.setattr(core, "_kanban_complete", lambda *a, **k: None)
    yield hermes_home


@pytest.fixture
def manager(monkeypatch):
    """A real, discovery-loaded PluginManager with work-claims enabled.

    The loader imports the plugin from its own file location, so the module
    the hooks actually run in is a *different* object from the ``plugins.
    work_claims`` this file imports (they share only the coordination
    database). Anything a test asserts about the hook path -- a class
    identity, a patched Kanban mirror -- must therefore come from
    :func:`_plugin_core`, not from the test-side import.
    """
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins.get("work-claims")
    assert loaded is not None and loaded.enabled, getattr(loaded, "error", "not discovered")
    for hook in (
        "on_session_finalize",
        "on_execution_turn_begin",
        "on_execution_turn_renew",
        "on_execution_turn_end",
    ):
        assert mgr.has_hook(hook), f"{hook} not registered by a real load"
    hook_core = loaded.module.core
    monkeypatch.setattr(hook_core, "_kanban_create", lambda *a, **k: "t_1")
    monkeypatch.setattr(hook_core, "_kanban_complete", lambda *a, **k: None)
    return mgr


def _plugin_core(manager) -> Any:
    """The ``core`` module of the plugin instance the hooks really run in."""
    return manager._plugins["work-claims"].module.core


def _acquire(session_id: str, target: str = "system:hermes-profile", summary: str = "test claim") -> str:
    result = core.acquire(session_id, summary, [target])
    assert result["success"], result
    return result["claim_id"]


def _lease(session_id: str, turn_id: str, **overrides) -> dict:
    """One host execution-turn lease payload opened by *this* incarnation.

    ``pid``/``boot_id`` default to the running process's own identity, so a
    test that overrides either is explicitly modelling another process (or
    another incarnation after a PID reuse), which the finalize evidence
    annotates as ``foreign``.
    """
    payload = {
        "session_id": session_id,
        "turn_id": turn_id,
        "lease_id": f"xturn_{uuid.uuid4().hex}",
        "holder_token": uuid.uuid4().hex,
        "pid": os.getpid(),
        "boot_id": core._BOOT_ID,
        "renew_interval_seconds": 60.0,
    }
    payload.update(overrides)
    return payload


def _begin(manager, lease: dict) -> None:
    manager.invoke_required_hook("on_execution_turn_begin", **lease)


def _renew(manager, lease: dict) -> None:
    manager.invoke_required_hook(
        "on_execution_turn_renew",
        session_id=lease["session_id"],
        turn_id=lease["turn_id"],
        lease_id=lease["lease_id"],
        holder_token=lease["holder_token"],
        pid=lease["pid"],
        boot_id=lease["boot_id"],
        renew_interval_seconds=lease["renew_interval_seconds"],
    )


def _end(manager, lease: dict, outcome: str = "success") -> None:
    manager.invoke_required_hook(
        "on_execution_turn_end",
        session_id=lease["session_id"],
        turn_id=lease["turn_id"],
        lease_id=lease["lease_id"],
        holder_token=lease["holder_token"],
        pid=lease["pid"],
        boot_id=lease["boot_id"],
        renew_interval_seconds=lease["renew_interval_seconds"],
        outcome=outcome,
    )


#: A deliberate conversation boundary the host reports on a real session end
#: (cli.py's ``session_boundary``/``shutdown``) -- the only class of reason
#: that authorises releasing a claim. Automatic-cleanup stamps such as
#: ``ws_orphan_reap`` are NOT proof the conversation ended.
DURABLE_TERMINAL_REASON = "shutdown"
NON_TERMINAL_REASON = "ws_orphan_reap"


def _finalize(manager, session_id: str, reason: str = DURABLE_TERMINAL_REASON, summary: str = "auto") -> None:
    manager.invoke_hook("on_session_finalize", session_id=session_id, reason=reason)


@pytest.mark.parametrize(
    ("callback_name", "missing_field"),
    [
        *[
            (callback_name, field)
            for callback_name in (
                "_on_execution_turn_begin",
                "_on_execution_turn_renew",
                "_on_execution_turn_end",
            )
            for field in (
                "session_id",
                "turn_id",
                "lease_id",
                "holder_token",
                "pid",
                "boot_id",
                "renew_interval_seconds",
            )
        ],
        ("_on_execution_turn_end", "outcome"),
    ],
)
def test_required_callbacks_reject_missing_lifecycle_fields(
    callback_name, missing_field
):
    payload = _lease("s-validation", "turn-validation")
    if callback_name == "_on_execution_turn_end":
        payload["outcome"] = "success"
    payload.pop(missing_field)

    with pytest.raises(ValueError, match=missing_field):
        getattr(work_claims, callback_name)(**payload)


def test_finalize_callback_requires_reason():
    with pytest.raises(ValueError, match="reason"):
        work_claims._on_session_finalize(session_id="s-validation", reason="")


def test_callback_failure_propagates_without_acknowledgement(monkeypatch):
    payload = _lease("s-failure", "turn-failure")
    monkeypatch.setattr(
        core,
        "on_execution_turn_begin",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("persistence failed")),
    )

    with pytest.raises(RuntimeError, match="persistence failed"):
        work_claims._on_execution_turn_begin(**payload)


def test_callback_acknowledges_only_after_core_persistence(monkeypatch):
    events = []
    payload = _lease("s-ack-order", "turn-ack-order")
    monkeypatch.setattr(
        core,
        "on_execution_turn_begin",
        lambda **_kwargs: events.append("persisted"),
    )

    result = work_claims._on_execution_turn_begin(**payload)
    events.append("returned")

    assert events == ["persisted", "returned"]
    assert result == {"acknowledged": True, "consumer": "work-claims"}


def _finalize_audit_rows(session_id: str) -> list[dict]:
    conn = core._connect()
    try:
        rows = conn.execute(
            "SELECT decision_id, claim_id, reason, outcome, observed_leases FROM finalize_audit "
            "WHERE session_id=? ORDER BY occurred_at", (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _full_audit_rows(session_id: str) -> list[dict]:
    """Every audit column, including the structured decision evidence."""
    conn = core._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM finalize_audit WHERE session_id=? ORDER BY occurred_at, decision_id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _execution_lease_rows() -> list[dict]:
    conn = core._connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM execution_leases").fetchall()]
    finally:
        conn.close()


def _deferred_rows() -> list[dict]:
    conn = core._connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM deferred_finalizes").fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Real-PluginManager production path / the original incident
# ---------------------------------------------------------------------------


def test_stale_orphan_reap_defers_then_resolves_exactly_once_via_real_plugin_manager(manager):
    """Reproduces the proven premature-finalization incident end-to-end
    through actual hook dispatch, not a direct core.py call."""
    session_id = "20260902_171806_a57d9d"
    _acquire(session_id)
    lease = _lease(session_id, "turn-2")

    _begin(manager, lease)
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id) is not None, (
        "claim was released while a live execution-turn lease was still open"
    )

    _end(manager, lease)
    assert core.active_claim(session_id) is None
    # A second, late end for the same lease must not double-resolve or raise.
    _end(manager, lease)


def test_orphan_reap_alone_never_releases_a_claim(manager):
    """The proven incident's actual trigger: a stale ``ws_orphan_reap`` timer.
    An automatic-cleanup stamp is not proof the conversation ended, so it must
    preserve the claim whether or not a turn happens to be live -- and the
    later turn end must not release it either, because no release was ever
    authorised."""
    session_id = "20260902_171806_a57d9d"
    _acquire(session_id)
    lease = _lease(session_id, "turn-2")
    _begin(manager, lease)

    _finalize(manager, session_id, reason=NON_TERMINAL_REASON)
    assert core.active_claim(session_id) is not None
    assert _deferred_rows() == [], "a non-terminal reap must not schedule a release"

    _end(manager, lease)
    assert core.active_claim(session_id) is not None, (
        "an unauthorised release leaked through the turn-end resolver"
    )


def test_finalize_with_no_live_lease_releases_immediately(manager):
    session_id = "s-idle"
    _acquire(session_id)
    _finalize(manager, session_id)
    assert core.active_claim(session_id) is None


def test_claim_persists_across_sequential_turns(manager):
    session_id = "s-sequential"
    _acquire(session_id)
    for i in range(3):
        lease = _lease(session_id, f"turn-{i}")
        _begin(manager, lease)
        assert core.active_claim(session_id) is not None
        _end(manager, lease)
        assert core.active_claim(session_id) is not None
    core.release(session_id, "done")
    assert core.active_claim(session_id) is None


# ---------------------------------------------------------------------------
# Every turn outcome closes the loop identically (interrupted/failed/empty/
# tool-only/rebound) -- the fix no longer depends on post_llm_call's
# final_response-and-not-interrupted gate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome", ["success", "cancelled", "timed_out", "failed", "rebound", "tool_only"]
)
def test_deferred_finalize_resolves_regardless_of_turn_outcome(manager, outcome):
    session_id = f"s-outcome-{outcome or 'blank'}"
    _acquire(session_id)
    lease = _lease(session_id, "turn-a")
    _begin(manager, lease)
    _finalize(manager, session_id)
    assert core.active_claim(session_id) is not None

    _end(manager, lease, outcome=outcome)
    assert core.active_claim(session_id) is None


# ---------------------------------------------------------------------------
# Finding #2: deferred rows are claim_id-bound, not session_id-bound -- an
# old claim's deferred release must never resolve against a replacement.
# ---------------------------------------------------------------------------


def test_old_deferred_finalize_never_releases_a_replacement_claim(manager):
    session_id = "s-replace"
    claim_1 = _acquire(session_id, target="system:target-a")
    lease = _lease(session_id, "turn-old")
    _begin(manager, lease)
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id)["claim_id"] == claim_1
    assert len(_deferred_rows()) == 1

    # The claim holder explicitly releases (e.g. work_claim_release) before
    # the stale turn's lease ever ends -- this must retire the deferred row.
    assert core.release(session_id, "manually verified and released")["success"]
    assert _deferred_rows() == []

    claim_2 = _acquire(session_id, target="system:target-b")
    assert claim_2 != claim_1
    assert core.active_claim(session_id)["claim_id"] == claim_2

    # The stale turn from claim_1 finally ends. It must not touch claim_2.
    _end(manager, lease)
    active = core.active_claim(session_id)
    assert active is not None and active["claim_id"] == claim_2


def test_acquire_purges_stale_deferred_rows_for_the_session(manager):
    session_id = "s-purge"
    _acquire(session_id, target="system:target-a")
    lease = _lease(session_id, "turn-a")
    _begin(manager, lease)
    _finalize(manager, session_id)
    assert len(_deferred_rows()) == 1

    # TTL expiry (not explicit release) retires the first claim; the second
    # acquire for the same session must not inherit its deferred row.
    conn = core._connect()
    conn.execute("UPDATE claims SET expires_at=? WHERE session_id=?", (int(time.time()) - 1, session_id))
    conn.close()
    _acquire(session_id, target="system:target-b")
    assert _deferred_rows() == []
    _end(manager, lease)  # must be a harmless no-op now
    assert core.active_claim(session_id) is not None


# ---------------------------------------------------------------------------
# Finding #6: TTL expiry is independent of turn liveness, and retires any
# deferred state tied to the expired claim.
# ---------------------------------------------------------------------------


def test_ttl_expiry_ignores_live_lease_and_retires_deferred_state(manager):
    session_id = "s-ttl"
    claim_id = _acquire(session_id)
    lease = _lease(session_id, "turn-a")
    _begin(manager, lease)
    _finalize(manager, session_id)
    assert len(_deferred_rows()) == 1

    conn = core._connect()
    conn.execute("UPDATE claims SET expires_at=? WHERE claim_id=?", (int(time.time()) - 1, claim_id))
    conn.close()

    assert core.active_claim(session_id) is None  # TTL sweep wins regardless of the live lease
    assert _deferred_rows() == []
    _end(manager, lease)  # no claim left to resolve; must not raise


# ---------------------------------------------------------------------------
# Finding #5: liveness is renewable-expiry + owner-token based, not PID
# inspection -- so PID reuse, two backends sharing a session, and a foreign
# holder_token cannot overwrite or delete an execution lease they don't own.
# ---------------------------------------------------------------------------


def test_renew_and_end_are_owner_token_checked(manager):
    session_id = "s-owner"
    lease = _lease(session_id, "turn-a")
    _begin(manager, lease)

    forged = dict(lease, holder_token=uuid.uuid4().hex)
    _end(manager, forged)
    rows = _execution_lease_rows()
    assert len(rows) == 1 and rows[0]["lease_id"] == lease["lease_id"], (
        "a mismatched holder_token deleted a lease it did not own"
    )

    _renew(manager, forged)
    row = _execution_lease_rows()[0]
    assert row["last_seen_at"] < int(time.time()) + 1  # renew from the forged token did nothing harmful
    original_last_seen = row["last_seen_at"]
    time.sleep(1.1)
    _renew(manager, lease)
    refreshed = _execution_lease_rows()[0]
    assert refreshed["last_seen_at"] >= original_last_seen

    _end(manager, lease)
    assert _execution_lease_rows() == []


def test_pid_reuse_across_two_leases_does_not_confuse_ownership(manager):
    """Two logically distinct leases sharing one (reused) pid value must be
    tracked, renewed, and ended independently -- correctness comes from
    holder_token, never from pid."""
    session_id = "s-pid-reuse"
    shared_pid = 424242
    lease_a = _lease(session_id, "turn-a", pid=shared_pid)
    lease_b = _lease(session_id, "turn-b", pid=shared_pid)
    _begin(manager, lease_a)
    _begin(manager, lease_b)
    assert len(_execution_lease_rows()) == 2

    _end(manager, lease_a)
    remaining = _execution_lease_rows()
    assert len(remaining) == 1 and remaining[0]["lease_id"] == lease_b["lease_id"]

    _end(manager, lease_b)
    assert _execution_lease_rows() == []


def test_two_backend_processes_share_one_session_lease_set(manager):
    """Simulates two Hermes backend processes (distinct pid/boot identity,
    same shared work-claims.db) both running a turn for one session."""
    session_id = "s-two-backends"
    _acquire(session_id)
    lease_p1 = _lease(session_id, "turn-p1", pid=11111, boot_id=uuid.uuid4().hex)
    lease_p2 = _lease(session_id, "turn-p2", pid=22222, boot_id=uuid.uuid4().hex)
    _begin(manager, lease_p1)
    _begin(manager, lease_p2)

    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id) is not None

    _end(manager, lease_p1)
    assert core.active_claim(session_id) is not None, "the other backend's lease is still live"

    _end(manager, lease_p2)
    assert core.active_claim(session_id) is None


# ---------------------------------------------------------------------------
# Crash / recovery: a lease whose owner never called end() (SIGKILL, power
# loss) must not permanently block finalize -- it self-heals via renewable
# expiry once the window passes.
# ---------------------------------------------------------------------------


def test_orphaned_lease_self_heals_via_expiry_without_end(manager):
    session_id = "s-crash-orphan"
    _acquire(session_id)
    lease = _lease(session_id, "turn-crash")
    _begin(manager, lease)

    conn = core._connect()
    conn.execute("UPDATE execution_leases SET expires_at=? WHERE lease_id=?", (int(time.time()) - 1, lease["lease_id"]))
    conn.close()

    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id) is None, (
        "a stale, already-expired lease leaked the claim instead of self-healing"
    )


def test_crash_recovery_second_finalize_attempt_resolves_stale_defer(manager):
    """First finalize defers behind a lease that then crashes (no end()
    ever fires); a later recovery pass (a second finalize attempt, e.g. a
    retried reap tick) must resolve it via expiry pruning."""
    session_id = "s-crash-recover"
    _acquire(session_id)
    lease = _lease(session_id, "turn-crash")
    _begin(manager, lease)
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id) is not None
    assert len(_deferred_rows()) == 1

    conn = core._connect()
    conn.execute("UPDATE execution_leases SET expires_at=? WHERE lease_id=?", (int(time.time()) - 1, lease["lease_id"]))
    conn.close()

    _finalize(manager, session_id, reason="session_boundary")
    assert core.active_claim(session_id) is None
    assert _deferred_rows() == []


# ---------------------------------------------------------------------------
# Finding #4 / #1: exactly-once atomic resolution under concurrency.
# ---------------------------------------------------------------------------


def test_concurrent_finalize_attempts_release_exactly_once(manager):
    session_id = "s-concurrent-finalize"
    _acquire(session_id)
    results = []
    barrier = threading.Barrier(8)

    def _attempt():
        barrier.wait(timeout=5)
        core.release_all_for_session(
            session_id, "auto", reason=DURABLE_TERMINAL_REASON, durable_terminal=True
        )
        results.append(core.active_claim(session_id))

    threads = [threading.Thread(target=_attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert core.active_claim(session_id) is None
    # No thread must ever observe a torn/partial state (claim present with
    # no matching row, etc.) -- every observation is a clean None.
    assert all(r is None for r in results)


def test_concurrent_turn_ends_resolve_deferred_release_exactly_once(manager):
    """Two sibling turns for one session both hold the claim's only lease
    set; both ending at once must resolve the deferred release exactly
    once, never twice and never zero times."""
    session_id = "s-concurrent-end"
    _acquire(session_id)
    lease_a = _lease(session_id, "turn-a")
    lease_b = _lease(session_id, "turn-b")
    _begin(manager, lease_a)
    _begin(manager, lease_b)
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id) is not None

    barrier = threading.Barrier(2)

    def _end_one(lease):
        barrier.wait(timeout=5)
        _end(manager, lease)

    t1 = threading.Thread(target=_end_one, args=(lease_a,))
    t2 = threading.Thread(target=_end_one, args=(lease_b,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert core.active_claim(session_id) is None
    assert _deferred_rows() == []
    # Exactly one 'deferred_resolved_released' audit outcome was written.
    resolved = [r for r in _finalize_audit_rows(session_id) if r["outcome"] == "deferred_resolved_released"]
    assert len(resolved) == 1


def test_concurrent_begin_renew_end_stress_leaves_no_leaked_rows(manager):
    session_id = "s-stress"
    errors = []

    def _cycle(i):
        try:
            lease = _lease(session_id, f"turn-{i}", renew_interval_seconds=1.0)
            _begin(manager, lease)
            _renew(manager, lease)
            _end(manager, lease)
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=_cycle, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert [r for r in _execution_lease_rows() if r["session_id"] == session_id] == []


# ---------------------------------------------------------------------------
# Finding #6: unconditional structured audit -- reason, claim identity if
# any, observed lease set, and decision id -- for every finalize attempt.
# ---------------------------------------------------------------------------


def test_finalize_with_no_active_claim_still_writes_structured_audit(manager):
    session_id = "s-no-claim"
    _finalize(manager, session_id, reason="ws_orphan_reap")
    rows = _finalize_audit_rows(session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["claim_id"] is None
    assert row["reason"] == "ws_orphan_reap"
    assert row["outcome"] == "no_claim"
    assert json.loads(row["observed_leases"]) == []
    assert row["decision_id"]


def test_finalize_audit_records_reason_decision_id_and_observed_leases(manager):
    session_id = "s-audit-detail"
    _acquire(session_id)
    lease = _lease(session_id, "turn-a")
    _begin(manager, lease)
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)

    rows = _finalize_audit_rows(session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["claim_id"] is not None
    assert row["reason"] == DURABLE_TERMINAL_REASON
    assert row["outcome"] == "deferred"
    assert json.loads(row["observed_leases"]) == [lease["lease_id"]]

    _end(manager, lease)
    resolved_rows = [r for r in _finalize_audit_rows(session_id) if r["outcome"] == "deferred_resolved_released"]
    assert len(resolved_rows) == 1
    assert resolved_rows[0]["decision_id"] != row["decision_id"]


def test_immediate_release_event_preserves_reason_and_decision_id(manager):
    session_id = "s-release-audit"
    claim_id = _acquire(session_id)
    _finalize(manager, session_id, reason="session_boundary")

    conn = core._connect()
    try:
        events = conn.execute(
            "SELECT event, detail FROM claim_events WHERE claim_id=? ORDER BY event_id", (claim_id,)
        ).fetchall()
    finally:
        conn.close()
    released = [json.loads(e["detail"]) for e in events if e["event"] == "released"]
    assert len(released) == 1
    assert released[0]["reason"] == "session_boundary"
    assert released[0]["decision_id"]


# ---------------------------------------------------------------------------
# Bundled-plugin manifest/version sanity (hooks the manifest advertises must
# match what register() actually wires; obsolete pre_llm_call/post_llm_call
# must be gone).
# ---------------------------------------------------------------------------


def test_manifest_advertises_the_execution_turn_hooks_not_the_old_ones():
    manifest_path = Path(__file__).resolve().parents[2] / "plugins" / "work_claims" / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    hooks = set(manifest["provides_hooks"])
    assert {"on_execution_turn_begin", "on_execution_turn_renew", "on_execution_turn_end"} <= hooks
    assert "pre_llm_call" not in hooks
    assert "post_llm_call" not in hooks


def test_core_no_longer_exposes_the_callback_thread_active_turns_api():
    assert not hasattr(core, "begin_turn")
    assert not hasattr(core, "end_turn")
    assert not hasattr(core, "session_has_live_turn")


# ---------------------------------------------------------------------------
# Slice B2 / review finding 5: an execution lease's holder identity is
# ``(holder_token, pid, boot_id)`` bound to one ``(lease_id, session_id,
# turn_id)``. ``begin`` is an explicit compare-and-insert/update inside a
# single BEGIN IMMEDIATE transaction -- never INSERT OR REPLACE -- so a
# foreign holder cannot take over (and then close) a lease it does not own.
# ---------------------------------------------------------------------------


def _lease_row(lease_id: str) -> dict | None:
    rows = [row for row in _execution_lease_rows() if row["lease_id"] == lease_id]
    return rows[0] if rows else None


def test_foreign_begin_for_an_existing_lease_id_fails_closed(manager):
    """The finding-5 takeover: an attacker replays the owner's lease_id with
    its own holder_token. INSERT OR REPLACE silently handed ownership over and
    left the real owner unable to end its own lease."""
    session_id = "s-foreign-begin"
    owner = _lease(session_id, "turn-owner")
    _begin(manager, owner)
    before = _lease_row(owner["lease_id"])

    forged = dict(owner, holder_token=uuid.uuid4().hex)
    with pytest.raises(pmod.RequiredHookError) as excinfo:
        _begin(manager, forged)
    assert isinstance(excinfo.value.__cause__, _plugin_core(manager).LeaseIdentityConflict)
    # The conflict must not leak either side's ownership secret.
    assert owner["holder_token"] not in str(excinfo.value.__cause__)
    assert forged["holder_token"] not in str(excinfo.value.__cause__)

    assert _lease_row(owner["lease_id"]) == before, "a foreign begin mutated the owner's row"
    # The real owner still owns the lease end-to-end.
    _end(manager, owner)
    assert _lease_row(owner["lease_id"]) is None


@pytest.mark.parametrize("field", ["holder_token", "pid", "boot_id", "session_id", "turn_id"])
def test_begin_collision_on_any_identity_field_fails_closed(manager, field):
    session_id = "s-collision"
    owner = _lease(session_id, "turn-owner")
    _begin(manager, owner)
    before = _lease_row(owner["lease_id"])

    differing = {
        "holder_token": uuid.uuid4().hex,
        "pid": owner["pid"] + 1,
        "boot_id": uuid.uuid4().hex,
        "session_id": f"{session_id}-other",
        "turn_id": "turn-other",
    }[field]
    with pytest.raises(pmod.RequiredHookError):
        _begin(manager, dict(owner, **{field: differing}))
    assert _lease_row(owner["lease_id"]) == before


def test_same_holder_begin_is_an_idempotent_renewal(manager):
    """A duplicate begin from the exact same holder identity (a retried
    delivery of the same admission) must refresh the lease, not raise and not
    fork a second row."""
    session_id = "s-idempotent-begin"
    lease = _lease(session_id, "turn-a", renew_interval_seconds=60.0)
    _begin(manager, lease)
    first = _lease_row(lease["lease_id"])

    time.sleep(1.1)
    _begin(manager, lease)
    rows = [row for row in _execution_lease_rows() if row["session_id"] == session_id]
    assert len(rows) == 1
    again = rows[0]
    assert again["last_seen_at"] > first["last_seen_at"]
    assert again["expires_at"] > first["expires_at"]
    assert again["holder_token"] == lease["holder_token"]

    _end(manager, lease)
    assert _lease_row(lease["lease_id"]) is None


@pytest.mark.parametrize("field", ["holder_token", "pid", "boot_id", "session_id", "turn_id"])
def test_renew_and_end_validate_the_full_holder_identity(manager, field):
    session_id = "s-identity"
    owner = _lease(session_id, "turn-owner")
    _begin(manager, owner)
    before = _lease_row(owner["lease_id"])

    differing = {
        "holder_token": uuid.uuid4().hex,
        "pid": owner["pid"] + 1,
        "boot_id": uuid.uuid4().hex,
        "session_id": f"{session_id}-other",
        "turn_id": "turn-other",
    }[field]
    foreign = dict(owner, **{field: differing})

    time.sleep(1.1)
    _renew(manager, foreign)
    assert _lease_row(owner["lease_id"]) == before, "a foreign renew refreshed a lease it does not own"

    _end(manager, foreign)
    assert _lease_row(owner["lease_id"]) == before, "a foreign end deleted a lease it does not own"

    _renew(manager, owner)
    assert _lease_row(owner["lease_id"])["expires_at"] > before["expires_at"]
    _end(manager, owner)
    assert _lease_row(owner["lease_id"]) is None


# ---------------------------------------------------------------------------
# Slice B2 / review finding 6: one atomic finalization_decision() that
# snapshots every same-session lease BEFORE any prune, records structured
# evidence (reason, all lease rows and their freshness, the claim's heartbeat
# and expiry), and returns preserve-versus-release.
# ---------------------------------------------------------------------------


def test_durable_terminal_reason_classification_is_explicit_and_fail_closed():
    assert core.is_durable_terminal_reason("shutdown") is True
    assert core.is_durable_terminal_reason("session_boundary") is True
    assert core.is_durable_terminal_reason("new_session") is True
    # Automatic-cleanup stamps are never proof the conversation ended.
    assert core.is_durable_terminal_reason("ws_orphan_reap") is False
    assert core.is_durable_terminal_reason("startup_orphan_reap") is False
    assert core.is_durable_terminal_reason("ws_disconnect") is False
    # Unknown / absent reasons fail closed.
    assert core.is_durable_terminal_reason("") is False
    assert core.is_durable_terminal_reason(None) is False
    assert core.is_durable_terminal_reason("something_new") is False


def test_finalization_decision_preserves_on_a_live_same_session_lease(manager):
    session_id = "s-decision-live"
    claim_id = _acquire(session_id)
    lease = _lease(session_id, "turn-live")
    _begin(manager, lease)

    decision = core.finalization_decision(session_id, DURABLE_TERMINAL_REASON, True)

    assert decision["disposition"] == "preserve"
    assert decision["outcome"] == "deferred"
    assert decision["claim_id"] == claim_id
    evidence = decision["evidence"]
    assert evidence["reason"] == DURABLE_TERMINAL_REASON
    assert evidence["durable_terminal"] is True
    assert evidence["lease_counts"] == {"total": 1, "live": 1, "stale": 0, "foreign": 0}
    assert [row["lease_id"] for row in evidence["leases"]] == [lease["lease_id"]]
    assert core.active_claim(session_id) is not None


def test_finalization_decision_snapshots_stale_and_live_rows_before_pruning(manager):
    session_id = "s-decision-snapshot"
    _acquire(session_id)
    live = _lease(session_id, "turn-live")
    stale = _lease(session_id, "turn-stale")
    _begin(manager, live)
    _begin(manager, stale)
    conn = core._connect()
    conn.execute(
        "UPDATE execution_leases SET expires_at=?, last_seen_at=? WHERE lease_id=?",
        (int(time.time()) - 5, int(time.time()) - 305, stale["lease_id"]),
    )
    conn.close()

    decision = core.finalization_decision(session_id, DURABLE_TERMINAL_REASON, True)

    evidence = decision["evidence"]
    by_id = {row["lease_id"]: row for row in evidence["leases"]}
    assert set(by_id) == {live["lease_id"], stale["lease_id"]}, (
        "the snapshot was taken after the prune, losing the stale row as evidence"
    )
    assert by_id[live["lease_id"]]["live"] is True
    assert by_id[stale["lease_id"]]["live"] is False
    assert by_id[stale["lease_id"]]["age_seconds"] >= 300
    assert by_id[stale["lease_id"]]["expires_in_seconds"] < 0
    assert by_id[live["lease_id"]]["turn_id"] == "turn-live"
    assert evidence["lease_counts"] == {"total": 2, "live": 1, "stale": 1, "foreign": 0}
    assert evidence["pruned_lease_ids"] == [stale["lease_id"]]
    # The live row still preserves the claim; the stale one is only evidence.
    assert decision["disposition"] == "preserve"
    assert core.active_claim(session_id) is not None
    # Ownership secrets never enter the audit trail.
    serialized = json.dumps(evidence)
    assert live["holder_token"] not in serialized
    assert by_id[live["lease_id"]]["holder_fingerprint"]


def test_finalization_decision_records_claim_heartbeat_and_expiry_evidence(manager):
    session_id = "s-decision-claim-evidence"
    claim_id = _acquire(session_id)
    conn = core._connect()
    conn.execute(
        "UPDATE claims SET heartbeat_at=? WHERE claim_id=?",
        (int(time.time()) - 120, claim_id),
    )
    conn.close()

    decision = core.finalization_decision(session_id, NON_TERMINAL_REASON, False)

    claim_evidence = decision["evidence"]["claim"]
    assert claim_evidence["claim_id"] == claim_id
    assert claim_evidence["status"] == "active"
    assert claim_evidence["heartbeat_age_seconds"] >= 120
    assert claim_evidence["expires_in_seconds"] > 0
    assert claim_evidence["expired"] is False
    assert claim_evidence["acquired_at"] <= claim_evidence["expires_at"]

    stored = _full_audit_rows(session_id)
    assert len(stored) == 1
    assert json.loads(stored[0]["evidence"])["claim"] == claim_evidence
    assert stored[0]["disposition"] == "preserve"
    assert stored[0]["durable_terminal"] == 0


@pytest.mark.parametrize("lease_state", ["missing", "stale", "foreign"])
def test_absent_or_unowned_lease_state_is_never_terminal_proof(manager, lease_state):
    """No live lease is not proof the conversation ended. Without an explicit
    durable terminal signal the claim must be preserved -- a missing row, an
    expired row, and another process's row are all equally inconclusive."""
    session_id = f"s-not-proof-{lease_state}"
    claim_id = _acquire(session_id, target=f"system:target-{lease_state}")
    if lease_state == "stale":
        lease = _lease(session_id, "turn-stale")
        _begin(manager, lease)
        conn = core._connect()
        conn.execute(
            "UPDATE execution_leases SET expires_at=? WHERE lease_id=?",
            (int(time.time()) - 1, lease["lease_id"]),
        )
        conn.close()
    elif lease_state == "foreign":
        foreign = _lease(session_id, "turn-foreign", pid=999_001, boot_id=uuid.uuid4().hex)
        _begin(manager, foreign)

    decision = core.finalization_decision(session_id, NON_TERMINAL_REASON, False)

    assert decision["disposition"] == "preserve"
    expected = "preserved_live_lease" if lease_state == "foreign" else "preserved_no_durable_terminal"
    assert decision["outcome"] == expected
    assert core.active_claim(session_id)["claim_id"] == claim_id
    assert _deferred_rows() == [], "a preserve decision must never schedule a release"
    # The refusal is recorded, never silent.
    audit = _finalize_audit_rows(session_id)
    assert len(audit) == 1 and audit[0]["outcome"] == expected


def test_normal_terminal_cleanup_releases_the_claim_exactly_once(manager):
    session_id = "s-terminal-cleanup"
    claim_id = _acquire(session_id)
    lease = _lease(session_id, "turn-a")
    _begin(manager, lease)
    _end(manager, lease)

    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id) is None

    # A repeated finalize for the same, already-released session is a no-op.
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)

    conn = core._connect()
    try:
        released = conn.execute(
            "SELECT COUNT(*) AS n FROM claim_events WHERE claim_id=? AND event='released'",
            (claim_id,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert released == 1
    outcomes = [row["outcome"] for row in _finalize_audit_rows(session_id)]
    assert outcomes == ["released", "no_claim"]


def test_stale_reap_timer_firing_after_a_newer_turn_preserves_the_claim(manager):
    """The incident's exact shape: a reap timer armed during turn 1 fires
    after turn 1 ended and turn 2 already started. Turn 1's own end must not
    look like proof the session is finished."""
    session_id = "s-stale-timer"
    claim_id = _acquire(session_id)
    turn_1 = _lease(session_id, "turn-1")
    _begin(manager, turn_1)
    _end(manager, turn_1)
    turn_2 = _lease(session_id, "turn-2")
    _begin(manager, turn_2)

    # The stale timer finally fires, carrying only an automatic reap stamp.
    _finalize(manager, session_id, reason=NON_TERMINAL_REASON)
    assert core.active_claim(session_id)["claim_id"] == claim_id

    # Even a genuine terminal signal must wait for the newer turn to finish.
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id)["claim_id"] == claim_id
    _end(manager, turn_2)
    assert core.active_claim(session_id) is None


def test_finalize_hook_releases_only_on_an_explicit_release_decision(manager, monkeypatch):
    completed: list[tuple] = []
    monkeypatch.setattr(
        _plugin_core(manager), "_kanban_complete", lambda *a, **k: completed.append(a) or None
    )

    session_id = "s-hook-release"
    _acquire(session_id)
    _finalize(manager, session_id, reason=NON_TERMINAL_REASON)
    assert completed == [], "a preserve decision completed the Kanban mirror"
    assert core.active_claim(session_id) is not None

    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert len(completed) == 1
    assert core.active_claim(session_id) is None


def test_finalization_decision_transaction_failure_leaves_no_partial_state(manager):
    session_id = "s-decision-failure"
    claim_id = _acquire(session_id)
    lease = _lease(session_id, "turn-a")
    _begin(manager, lease)

    real_prune = core._prune_expired_leases

    def _boom(conn, now):
        real_prune(conn, now)
        raise sqlite3.OperationalError("database is locked")

    core._prune_expired_leases = _boom
    try:
        with pytest.raises(sqlite3.OperationalError):
            core.finalization_decision(session_id, DURABLE_TERMINAL_REASON, True)
    finally:
        core._prune_expired_leases = real_prune

    assert core.active_claim(session_id)["claim_id"] == claim_id
    assert _deferred_rows() == []
    assert _finalize_audit_rows(session_id) == []
    assert [row["lease_id"] for row in _execution_lease_rows()] == [lease["lease_id"]], (
        "a failed decision transaction was not rolled back"
    )

    # The session is still fully operable afterwards.
    _end(manager, lease)
    _finalize(manager, session_id, reason=DURABLE_TERMINAL_REASON)
    assert core.active_claim(session_id) is None
