"""Execution-turn lease (agent/execution_turn.py) — the unconditional turn
boundary wired into ``AIAgent.run_conversation``.

Uses a real ``hermes_cli.plugins.PluginManager`` (not a mock of the plugin
dispatch layer) so these tests prove the actual caller-thread contract in
``_HOOK_CALLER_THREAD_HOOKS`` and the actual additive-payload dispatch in
``PluginManager.invoke_hook``, not an assumption about them.
"""

from __future__ import annotations

import os
import threading

import pytest

import hermes_cli.plugins as plugins_mod
from agent import dispatcher_identity as di
from agent import execution_turn
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_identity as kbi
from hermes_cli.plugins import PluginManager, RequiredHookError
from run_agent import AIAgent


class _Recorder:
    """Captures every ``on_execution_turn_*`` call, in order, with thread."""

    def __init__(self):
        self.begin = []
        self.renew = []
        self.end = []
        self.threads = []

    def on_begin(self, **kwargs):
        self.threads.append(threading.current_thread())
        self.begin.append(kwargs)
        return {"acknowledged": True}

    def on_renew(self, **kwargs):
        self.threads.append(threading.current_thread())
        self.renew.append(kwargs)
        return {"acknowledged": True}

    def on_end(self, **kwargs):
        self.threads.append(threading.current_thread())
        self.end.append(kwargs)
        return {"acknowledged": True}


@pytest.fixture
def recorder(monkeypatch):
    """Register a real PluginManager as the active one, with begin/end hooks."""
    mgr = PluginManager()
    mgr._discovered = True
    rec = _Recorder()
    mgr._hooks["on_execution_turn_begin"] = [rec.on_begin]
    mgr._hooks["on_execution_turn_renew"] = [rec.on_renew]
    mgr._hooks["on_execution_turn_end"] = [rec.on_end]
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return rec


@pytest.fixture
def no_consumer(monkeypatch):
    """Register a real PluginManager that consumes none of the lease hooks."""
    mgr = PluginManager()
    mgr._discovered = True
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return mgr


@pytest.fixture
def bound_worker(tmp_path):
    """Bind this process as a genuine dispatcher worker, via the real DB path.

    Not a stub of ``get_bound()``: the token is minted by
    ``kanban_db.issue_worker_identity`` and consumed by
    ``dispatcher_identity.bind_token``, so the binding these tests read is the
    one a dispatcher-spawned worker actually holds.
    """
    di.reset_for_tests()
    db_path = tmp_path / "kanban.db"
    workspace = tmp_path / "worker-ws"
    workspace.mkdir(parents=True, exist_ok=True)
    with kbc.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(
            conn, title="lease worker", assignee="tester",
            workspace_path=str(workspace),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        token = kbi.issue_worker_identity(
            conn,
            task_id=task_id,
            run_id=int(claimed.current_run_id),
            workspace_path=str(workspace),
            worker_pid=os.getpid(),
            ttl_seconds=3600,
        )
    di.bind_token(token, db_path=str(db_path))
    assert di.get_bound() is not None
    yield workspace
    di.reset_for_tests()


# -- a bound dispatcher worker may not run without its security consumer ----
#
# ``_consumed()`` exists so an ordinary host with no interested plugin pays
# nothing per turn. For a dispatcher-spawned worker that same early return is
# a fail-open: the work-claims plugin *is* the CV-A01 boundary, and "no plugin
# consumes the hook" describes exactly the process that has no containment.
# A bound worker therefore takes the required-hook path unconditionally, and
# an absent consumer aborts the turn instead of silently skipping the lease.


def test_bound_worker_aborts_when_no_plugin_consumes_the_begin_hook(
    bound_worker, no_consumer
):
    with pytest.raises(RequiredHookError) as exc_info:
        execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)

    assert "on_execution_turn_begin" in str(exc_info.value)
    assert "no registered callbacks" in str(exc_info.value)


def test_bound_worker_aborts_when_the_begin_hook_is_not_acknowledged(
    bound_worker, monkeypatch
):
    """A registered-but-refusing consumer is not admission either.

    Distinguishes "the plugin is absent" from "the plugin is present and
    declined", so the abort cannot be satisfied by a callback that merely
    exists.
    """
    mgr = PluginManager()
    mgr._discovered = True
    mgr._hooks["on_execution_turn_begin"] = [lambda **_kwargs: {"acknowledged": False}]
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)

    with pytest.raises(RequiredHookError, match="acknowledged=true"):
        execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)


def test_bound_worker_opens_the_lease_when_the_security_plugin_is_loaded(
    bound_worker, recorder
):
    """The positive control: admission happens, and the turn proceeds."""
    lease = execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)

    assert lease is not None
    assert len(recorder.begin) == 1
    assert recorder.begin[0]["session_id"] == "s1"

    execution_turn.end(lease, outcome="success")
    assert len(recorder.end) == 1


def test_unbound_process_with_no_consumer_still_skips_the_lease(no_consumer):
    """Ordinary sessions are untouched -- the cost gate still applies to them."""
    di.reset_for_tests()

    assert execution_turn.begin("s1", "s1:t1:aaaa") is None


def test_a_suppressed_scope_inside_a_worker_skips_the_lease(bound_worker, no_consumer):
    """``delegate_task`` children and in-process cron hold no authority.

    ``get_bound()`` is already ``None`` for them, so they must not inherit the
    worker's admission requirement any more than they inherit its authority.
    """
    with di.suppressed():
        assert execution_turn.begin("s1", "s1:t1:aaaa") is None


# -- module-level: begin/rebind/close against a real PluginManager ----------


def test_begin_returns_none_without_a_consumer(monkeypatch):
    """No plugin handles the hooks: begin() must no-op, not open a lease."""
    mgr = PluginManager()
    mgr._discovered = True
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)

    lease = execution_turn.begin("s1", "s1:t1:aaaa")

    assert lease is None
    # end() on the None result must be a safe no-op too.
    execution_turn.end(lease, outcome="success")


def test_begin_and_end_run_on_the_caller_thread(recorder):
    caller = threading.current_thread()

    lease = execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)
    assert lease is not None
    execution_turn.end(lease, outcome="success")

    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1
    assert all(t is caller for t in recorder.threads)


def test_begin_payload_and_end_outcome(recorder):
    lease = execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)
    execution_turn.end(lease, outcome="failed")

    begin_payload = recorder.begin[0]
    end_payload = recorder.end[0]
    assert begin_payload["session_id"] == "s1"
    assert begin_payload["turn_id"] == "s1:t1:aaaa"
    assert begin_payload["lease_id"] == end_payload["lease_id"]
    assert begin_payload["holder_token"] == end_payload["holder_token"]
    assert begin_payload["boot_id"] == end_payload["boot_id"]
    assert end_payload["outcome"] == "failed"


def test_close_is_exactly_once(recorder):
    lease = execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)
    lease.close(outcome="success")
    lease.close(outcome="success")
    lease.close(outcome="success")

    assert len(recorder.end) == 1


def test_end_helper_is_exactly_once(recorder):
    lease = execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)
    execution_turn.end(lease, outcome="success")
    # A second call from a defensive second finally-guard must not re-fire
    # the hook or raise.
    execution_turn.end(lease, outcome="success")

    assert len(recorder.end) == 1


def test_rebind_closes_old_lease_and_opens_symmetric_new_one(recorder):
    lease = execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)
    lease.rebind("s2")

    assert len(recorder.begin) == 2
    assert len(recorder.end) == 1
    assert recorder.begin[0]["session_id"] == "s1"
    assert recorder.end[0]["session_id"] == "s1"
    assert recorder.end[0]["outcome"] == "rebound"
    assert recorder.begin[1]["session_id"] == "s2"
    # A fresh lease/holder identity — a successor turn must never be able to
    # close its predecessor's lease and vice versa.
    assert recorder.begin[1]["lease_id"] != recorder.begin[0]["lease_id"]
    assert recorder.begin[1]["holder_token"] != recorder.begin[0]["holder_token"]

    lease.close(outcome="success")
    assert len(recorder.end) == 2
    assert recorder.end[1]["session_id"] == "s2"
    assert recorder.end[1]["lease_id"] == recorder.begin[1]["lease_id"]


def test_rebind_to_same_session_is_a_noop(recorder):
    lease = execution_turn.begin("s1", "s1:t1:aaaa", renew_interval_seconds=3600.0)
    lease.rebind("s1")

    assert len(recorder.begin) == 1
    assert len(recorder.end) == 0


def test_required_begin_precedes_observer_and_return(monkeypatch):
    events = []
    monkeypatch.setattr(execution_turn, "_consumed", lambda: True)
    monkeypatch.setattr(
        execution_turn,
        "_invoke_required",
        lambda name, payload: events.append(("required", name, payload["turn_id"])),
    )
    monkeypatch.setattr(
        execution_turn,
        "_observe",
        lambda name, payload: events.append(("observer", name, payload["turn_id"])),
    )

    lease = execution_turn.begin("s1", "turn-1", renew_interval_seconds=3600.0)

    assert events == [
        ("required", execution_turn.BEGIN_HOOK, "turn-1"),
        ("observer", execution_turn.BEGIN_HOOK, "turn-1"),
    ]
    execution_turn.end(lease, outcome="success")


def test_renew_and_end_route_through_required_path_exactly_once(monkeypatch):
    calls = []
    monkeypatch.setattr(execution_turn, "_consumed", lambda: True)
    monkeypatch.setattr(
        execution_turn,
        "_invoke_required",
        lambda name, payload: calls.append((name, payload.copy())),
    )
    monkeypatch.setattr(execution_turn, "_observe", lambda *_args: None)

    lease = execution_turn.begin("s1", "turn-1", renew_interval_seconds=3600.0)
    assert lease is not None
    execution_turn._invoke(execution_turn.RENEW_HOOK, lease.payload())
    execution_turn.end(lease, outcome="success")
    execution_turn.end(lease, outcome="success")

    assert [name for name, _payload in calls] == [
        execution_turn.BEGIN_HOOK,
        execution_turn.RENEW_HOOK,
        execution_turn.END_HOOK,
    ]
    assert calls[-1][1]["outcome"] == "success"


def test_end_failure_propagates_but_dispatches_exactly_once(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    end_calls = []
    manager._hooks[execution_turn.BEGIN_HOOK] = [
        lambda **_kwargs: {"acknowledged": True}
    ]

    def fail_end(**_kwargs):
        end_calls.append("end")
        raise RuntimeError("end persistence failed")

    manager._hooks[execution_turn.END_HOOK] = [fail_end]
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    lease = execution_turn.begin("s1", "turn-1", renew_interval_seconds=3600.0)

    with pytest.raises(RequiredHookError, match="callback.*raised"):
        execution_turn.end(lease, outcome="success")
    execution_turn.end(lease, outcome="success")

    assert end_calls == ["end"]


# -- integration: the three run_agent.py call sites --------------------------


class _DB:
    def __init__(self, session_exists=True, acquire_result=True, resolved_session_id=None):
        self.events = []
        self.session_exists = session_exists
        self.acquire_result = acquire_result
        self.resolved_session_id = resolved_session_id

    def get_session(self, session_id):
        return {"id": session_id} if self.session_exists else None

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        self.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None and self.acquire_result is False:
            on_wait(0.0)
        return self.acquire_result

    def resolve_resume_session_id(self, session_id):
        self.events.append(("resolve", session_id))
        return self.resolved_session_id or session_id

    def get_messages_as_conversation(self, session_id, **kwargs):
        self.events.append(("reload", session_id, kwargs))
        return [{"role": "user", "content": "durable latest"}]

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        self.events.append(("release", session_id, holder))


def _agent_with_db(db, *, session_id="stale-parent", platform="desktop"):
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent.platform = platform
    agent.model = "test-model"
    agent._session_db = db
    agent._session_db_created = True
    agent._persist_disabled = False
    agent._parent_session_id = None
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent._conversation_root_id = lambda: session_id
    agent.log_prefix = ""
    agent._vprint = lambda *a, **k: None
    agent.status_callback = None
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._pending_redirect = None
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    return agent


def test_begin_failure_aborts_before_agent_and_balances_cleanup(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    events = []

    def fail_begin(**_kwargs):
        events.append("begin")
        raise RuntimeError("begin persistence failed")

    def acknowledge_end(**kwargs):
        events.append(("end", kwargs["outcome"]))
        return {"acknowledged": True}

    manager._hooks[execution_turn.BEGIN_HOOK] = [fail_begin]
    manager._hooks[execution_turn.END_HOOK] = [acknowledge_end]
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("agent loop must not start")
        ),
    )
    agent = _agent_with_db(_DB())

    with pytest.raises(RequiredHookError, match="callback.*raised"):
        AIAgent.run_conversation(
            agent,
            "hi",
            conversation_history=[{"role": "user", "content": "seed"}],
        )

    assert events == ["begin", ("end", "begin_failed")]


def test_success_outcome_closes_lease_exactly_once(recorder, monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        assert len(recorder.begin) == 1, "begin must be acknowledged before agent run"
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
    )

    assert result["final_response"] == "ok"
    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1
    assert recorder.end[0]["outcome"] == "success"


def test_empty_and_tool_only_outcomes_still_close_the_lease(recorder, monkeypatch):
    """No interrupted/failed flag: run_agent.py still classifies and closes."""
    db = _DB()
    agent = _agent_with_db(db)

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        return {"final_response": "", "messages": history, "tool_calls": 1}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    AIAgent.run_conversation(
        agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
    )

    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1
    assert recorder.end[0]["outcome"] == "success"


def test_interrupted_outcome_closes_lease_exactly_once(recorder, monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        return {
            "final_response": "",
            "messages": history,
            "completed": False,
            "interrupted": True,
        }

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
    )

    assert result.get("interrupted") is True
    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1
    assert recorder.end[0]["outcome"] == "cancelled"


def test_failed_outcome_closes_lease_exactly_once(recorder, monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        return {"final_response": "", "messages": history, "failed": True}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
    )

    assert result.get("failed") is True
    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1
    assert recorder.end[0]["outcome"] == "failed"


def test_escaping_exception_still_closes_the_lease(recorder, monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)

    def raising_run(_agent, _message, _system, history, *_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", raising_run)
    with pytest.raises(RuntimeError):
        AIAgent.run_conversation(
            agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
        )

    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1


def test_lease_wait_timeout_early_return_still_closes_the_lease(recorder, monkeypatch):
    db = _DB(acquire_result=False)
    agent = _agent_with_db(db)

    def boom(*_args, **_kwargs):
        raise AssertionError("turn must not start without the durable lease")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", boom)
    result = AIAgent.run_conversation(
        agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
    )

    assert result["failed"] is True
    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1
    assert recorder.end[0]["outcome"] == "timed_out"


def test_lease_wait_interrupt_early_return_still_closes_the_lease(recorder, monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)

    def acquire_with_abort(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        should_abort = kwargs.get("should_abort")
        agent._interrupt_requested = True
        agent._interrupt_message = "follow-up while waiting"
        assert should_abort()
        return False

    db.acquire_session_turn_lease = acquire_with_abort

    def boom(*_args, **_kwargs):
        raise AssertionError("turn must not start when lease wait is aborted")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", boom)
    result = AIAgent.run_conversation(
        agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
    )

    assert result.get("interrupted") is True
    assert len(recorder.begin) == 1
    assert len(recorder.end) == 1
    assert recorder.end[0]["outcome"] == "cancelled"


def test_rebind_when_session_id_rotates_mid_turn(recorder, monkeypatch):
    """A contended lease wait resolves to a compressed-transcript tip:
    the lease must close on the original session_id and reopen, symmetric,
    on the rotated one — and the final close must target the rotated id."""
    db = _DB(resolved_session_id="compressed-tip")
    agent = _agent_with_db(db, session_id="stale-parent")

    def acquire_with_wait(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(0.0)
        return True

    db.acquire_session_turn_lease = acquire_with_wait

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent, "hi", conversation_history=[{"role": "user", "content": "seed"}]
    )

    assert result["final_response"] == "ok"
    # begin(stale-parent) at turn entry, rebind() closes it and opens
    # (compressed-tip); the outer finally closes (compressed-tip) once more.
    assert len(recorder.begin) == 2
    assert len(recorder.end) == 2
    assert recorder.begin[0]["session_id"] == "stale-parent"
    assert recorder.end[0]["session_id"] == "stale-parent"
    assert recorder.end[0]["outcome"] == "rebound"
    assert recorder.begin[1]["session_id"] == "compressed-tip"
    assert recorder.end[1]["session_id"] == "compressed-tip"
    assert recorder.end[1]["outcome"] == "success"
    assert recorder.begin[1]["lease_id"] != recorder.begin[0]["lease_id"]
