"""Adversarial tests for the CodeVolt multi-board liveness guard.

Every test here is written to *break* the guard, not to confirm it. The suite
covers each finding from the independent review block on f2342ab:

  1  unsupported evidence commands        -> SupportedEvidenceTests
  2  missing heartbeat/pid contract       -> LivenessValidationTests
  3  dispatch not bound to an exact task  -> ExactDispatchTests
  4  verdict/protected/dependency gates   -> VerdictAndProtectedTests
  5  dropped/archived records, bad totals -> ReconciliationTests
  6  wrong field names, profile preflight -> CapabilityPreflightTests
  7  collision key fields and composition -> CollisionTests
  8  standalone lease/claim linkage       -> WorkClaimsTests
  9  runway, unique owners, one-lane      -> RunwayTests
 10  alert dedupe/retry, degraded service -> AlertTests / ServiceOutcomeTests

The module is loaded from this repo, never from a live path.
"""
from __future__ import annotations

import importlib.util
import json
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "codevolt_continuity_guard.py"
MANIFEST = SOURCE.parents[1] / "ACTIVATION_MANIFEST.md"

NOW = 1_000_000


def load_module():
    spec = importlib.util.spec_from_file_location("codevolt_continuity_guard", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mk_task(task_id: str, status: str, **kw) -> dict:
    """A task record carrying only real kanban_db.Task field names."""
    task = {
        "id": task_id,
        "title": task_id,
        "status": status,
        "assignee": None,
        "priority": 0,
        "tenant": "codevolt-production",
        "workspace_kind": "path",
        "workspace_path": None,
        "branch_name": None,
        "project_id": None,
        "result": None,
        "skills": None,
        "block_kind": None,
        "block_recurrences": 0,
        "consecutive_failures": 0,
        "claim_lock": None,
        "claim_expires": None,
        "worker_pid": None,
        "last_heartbeat_at": None,
        "current_run_id": None,
        "session_id": None,
        "created_at": NOW - 10_000,
        "started_at": None,
        "completed_at": None,
    }
    task.update(kw)
    return task


def gate_task() -> dict:
    """CV-A01 in the literal accepted state the dispatch gate demands."""
    return mk_task("t_13b90c53", "done", result="CV-A01 verdict: PASS", completed_at=NOW - 500)


def live_run(now: int = NOW, pid: int = 4242, run_id: int = 7, profile: str = "oliver") -> list[dict]:
    return [
        {
            "id": run_id,
            "task_id": None,
            "profile": profile,
            "status": "running",
            "worker_pid": pid,
            "last_heartbeat_at": now,
            "started_at": now - 60,
            "ended_at": None,
            "outcome": None,
            "summary": None,
        }
    ]


def active_claim(claim_id: str, task_id: str | None, now: int = NOW, **kw) -> dict:
    claim = {
        "claim_id": claim_id,
        "session_id": f"s_{claim_id}",
        "kanban_task_id": task_id,
        "status": "active",
        "workspace": None,
        "source_workspace": None,
        "acquired_at": now - 100,
        "heartbeat_at": now,
        "expires_at": now + 600,
        "targets": [],
        "lease": None,
    }
    claim.update(kw)
    return claim


class GuardTestBase(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

        self.boards = {board: [] for board in self.m.BOARDS}
        self.board_errors: dict[str, Exception] = {}
        self.db_paths = {board: f"/tmp/kanban/{board}.db" for board in self.m.BOARDS}
        self.runs: dict[str, list[dict]] = {}
        self.links: dict[str, dict] = {}
        self.profiles: dict[str, dict] = {}
        self.claims = {"available": True, "claims": [], "has_claim_targets": True, "has_execution_leases": False}
        self.claims_error: Exception | None = None
        self.alive = True
        self.alerts: list[str] = []
        self.send_results: list[bool] = []
        self.dispatch_calls: list[tuple[str, str]] = []
        self.dispatch_payload = {"spawned": []}
        self.dispatch_rc = 0
        self.reconcile_calls: list[str] = []

        patches = [
            mock.patch.object(self.m, "STATE_DIR", root),
            mock.patch.object(self.m, "STATE_FILE", root / "state.json"),
            mock.patch.object(self.m, "LOCK_FILE", root / "lock"),
            mock.patch.object(self.m, "LOG_FILE", root / "guard.log"),
            mock.patch.object(self.m, "fetch_board_snapshot", side_effect=self._snapshot),
            mock.patch.object(self.m, "fetch_task_runs", side_effect=self._runs),
            mock.patch.object(self.m, "fetch_task_links", side_effect=self._links),
            mock.patch.object(self.m, "fetch_work_claims", side_effect=self._claims),
            mock.patch.object(self.m, "fetch_profiles", side_effect=lambda: dict(self.profiles)),
            mock.patch.object(self.m, "process_alive", side_effect=lambda pid: self.alive),
            mock.patch.object(self.m, "send_alert", side_effect=self._send),
            mock.patch.object(self.m, "reconcile_active_claims", side_effect=self._reconcile),
            mock.patch.object(self.m, "dispatch_exact_task", side_effect=self._dispatch),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- fakes ---------------------------------------------------------------
    def _snapshot(self, board):
        if board in self.board_errors:
            raise self.board_errors[board]
        tasks = [dict(t) for t in self.boards.get(board, [])]
        return {
            "board": board,
            "db_path": self.db_paths[board],
            "tenant": self.m.BOARD_TENANTS[board],
            "tasks": tasks,
            "source_total": len(tasks),
            "archived_count": sum(1 for t in tasks if t.get("status") == "archived"),
            "out_of_tenant_count": 0,
        }

    def _runs(self, board, task_id):
        return [dict(r) for r in self.runs.get(task_id, [])]

    def _links(self, board, task_id):
        return dict(self.links.get(task_id, {"parents": [], "children": []}))

    def _claims(self):
        if self.claims_error is not None:
            raise self.claims_error
        return json.loads(json.dumps(self.claims))

    def _send(self, message):
        self.alerts.append(message)
        return self.send_results.pop(0) if self.send_results else True

    def _reconcile(self, board):
        self.reconcile_calls.append(board)
        return mock.Mock(returncode=0, stdout="{}")

    def _dispatch(self, board, task_id):
        self.dispatch_calls.append((board, task_id))
        payload = self.dispatch_payload
        return mock.Mock(returncode=self.dispatch_rc, stdout=json.dumps(payload))

    # -- helpers -------------------------------------------------------------
    def profile(self, name, skills=(), toolsets=("terminal", "file"), model="gpt-5.6-sol", ok=True, **kw):
        snapshot = {
            "name": name,
            "key": name.lower(),
            "skills": sorted(skills),
            "toolsets": sorted(toolsets),
            "model": model,
            "provider": "openai-codex",
            "config_ok": ok,
            "config_error": None,
        }
        snapshot.update(kw)
        self.profiles[name.lower()] = snapshot
        return snapshot

    def state(self) -> dict:
        return json.loads(self.m.STATE_FILE.read_text())

    def check(self, now=NOW, token=None) -> int:
        return self.m.check_once(now=now, token=token)


# ---------------------------------------------------------------------------
# 1. Supported evidence interfaces only.
# ---------------------------------------------------------------------------
class SupportedEvidenceTests(GuardTestBase):
    def test_no_unsupported_cli_commands_remain_in_source(self):
        text = SOURCE.read_text()
        for banned in (
            "state\", \"session-leases",
            "process-registry",
            "profile\", \"list",
            "session_turn_leases",
        ):
            self.assertNotIn(banned, text, banned)

    def test_rollback_names_exact_preimage_and_candidate_schemas(self):
        text = MANIFEST.read_text()
        self.assertIn(
            "The activation-step-5 preimage is schema 4; this candidate writes schema 5.",
            text,
        )
        self.assertNotIn("pre-activation schema-3", text)

    def test_board_fetch_uses_supported_connection_module_after_facade_shim_removal(self):
        captured = {"boards": []}

        class FakeTask:
            def __init__(self, **kw):
                for key, value in kw.items():
                    setattr(self, key, value)

        class FakeKB:
            @staticmethod
            def kanban_db_path(board=None):
                return Path("/tmp/fake") / f"{board}.db"

            @staticmethod
            def list_tasks(conn, **kwargs):
                captured.update(kwargs)
                return [
                    FakeTask(id="t_a", status="ready", tenant="codevolt-production"),
                    FakeTask(id="t_b", status="archived", tenant="codevolt-production"),
                    FakeTask(id="t_c", status="done", tenant="other-tenant"),
                ]

        class FakeKBC:
            @staticmethod
            def connect_closing(board=None):
                import contextlib as _c

                @_c.contextmanager
                def _cm():
                    captured["boards"].append(board)
                    yield object()

                return _cm()

        fresh = load_module()  # unpatched copy, so the real body runs
        # FakeKB intentionally has no connect_closing attribute: this models the
        # compatibility facade after its deprecated pointer is removed.
        with mock.patch.object(fresh, "_kanban_modules", return_value=(FakeKB, FakeKBC)):
            snapshot = fresh.fetch_board_snapshot("codevolt-managed-delivery")
        self.assertIs(captured.get("include_archived"), True)
        self.assertEqual(captured["boards"], ["codevolt-managed-delivery"])
        self.assertEqual(snapshot["source_total"], 2)
        self.assertEqual(snapshot["archived_count"], 1)
        self.assertEqual(snapshot["out_of_tenant_count"], 1)

    def test_dispatch_command_is_the_exact_task_bound_contract(self):
        fresh = load_module()
        with mock.patch.object(fresh, "run", return_value=mock.Mock(returncode=0, stdout="{}")) as runner:
            fresh.dispatch_exact_task("codevolt-managed-delivery", "t_x")
        argv = runner.call_args[0][0]
        self.assertEqual(
            argv[1:],
            ["kanban", "--board", "codevolt-managed-delivery", "dispatch", "--task-id", "t_x", "--max", "1", "--json"],
        )

    def test_unsupported_claims_evidence_blocks_dispatch(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.boards[board] = [gate_task(), mk_task("t_ready", "ready", assignee="oliver")]
        self.claims_error = RuntimeError("work-claims db missing")
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertFalse(state["standalone_claims"]["available"])
        self.assertIn("standalone_evidence_unavailable", state["dispatch_blockers"])
        self.assertEqual(self.dispatch_calls, [])
        self.assertEqual(state["status"], "liveness-fault")


# ---------------------------------------------------------------------------
# 5. Whole-board reconciliation: archived included, nothing silently dropped.
# ---------------------------------------------------------------------------
class ReconciliationTests(GuardTestBase):
    def test_archived_tasks_are_classified_not_dropped(self):
        snapshots = {
            "codevolt-managed-delivery": {
                "board": "codevolt-managed-delivery",
                "source_total": 3,
                "tasks": [
                    mk_task("t_a", "archived"),
                    mk_task("t_b", "done"),
                    mk_task("t_c", "ready"),
                ],
            }
        }
        buckets = self.m.classify_all(snapshots)
        self.assertEqual([t["id"] for t in buckets[self.m.BUCKET_HISTORICAL]], ["t_a", "t_b"])
        self.assertEqual(sum(len(v) for v in buckets.values()), 3)

    def test_false_low_source_total_is_rejected(self):
        """A caller that dropped archived rows reports fewer tasks than source."""
        snapshots = {
            "codevolt-managed-delivery": {
                "source_total": 5,
                "tasks": [mk_task("t_a", "ready"), mk_task("t_b", "todo")],
            }
        }
        with self.assertRaises(RuntimeError) as ctx:
            self.m.classify_all(snapshots)
        self.assertIn("dropped records", str(ctx.exception))

    def test_false_high_source_total_is_rejected(self):
        snapshots = {"b": {"source_total": 1, "tasks": [mk_task("t_a", "ready"), mk_task("t_b", "todo")]}}
        with self.assertRaises(RuntimeError):
            self.m.classify_all(snapshots)

    def test_missing_source_total_fails_closed(self):
        with self.assertRaises(RuntimeError):
            self.m.classify_all({"b": {"tasks": []}})

    def test_non_dict_record_becomes_an_explicit_malformed_entry(self):
        snapshots = {"b": {"source_total": 2, "tasks": ["not-a-task", mk_task("t_ok", "ready")]}}
        buckets = self.m.classify_all(snapshots)
        self.assertEqual(len(buckets[self.m.BUCKET_MALFORMED]), 1)
        self.assertEqual(buckets[self.m.BUCKET_MALFORMED][0]["id"], "_unparsable_0")
        self.assertEqual(sum(len(v) for v in buckets.values()), 2)

    def test_malformed_status_and_id_variants(self):
        for case in (
            mk_task("t_x", "not-a-real-status"),
            mk_task("t_x", "cancelled"),  # not in the real status vocabulary
            mk_task("t_x", "superseded"),
            {"id": "no-prefix", "status": "todo"},
            {"status": "todo"},
            {"id": "t_x"},
            {"id": "t_x", "status": 5},
        ):
            self.assertEqual(self.m.classify_task(case), self.m.BUCKET_MALFORMED, case)

    def test_duplicate_id_on_one_board_is_refused(self):
        snapshots = {"b": {"source_total": 2, "tasks": [mk_task("t_dup", "todo"), mk_task("t_dup", "ready")]}}
        with self.assertRaises(RuntimeError):
            self.m.classify_all(snapshots)

    def test_malformed_record_is_a_fault_not_a_completion(self):
        board = "local-model-security-research"
        self.boards[board] = [mk_task("t_bad", "not-a-status")]
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["status"], "liveness-fault")
        self.assertEqual(state["classification_totals"][self.m.BUCKET_MALFORMED], 1)
        self.assertTrue(any("malformed" in a for a in self.alerts))

    def test_state_reports_authoritative_source_totals_per_board(self):
        self.boards["codevolt-managed-delivery"] = [mk_task("t_a", "done"), mk_task("t_b", "archived")]
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(state["source_totals"]["codevolt-managed-delivery"], 2)
        self.assertEqual(state["total_tasks"], 2)
        self.assertTrue(state["reconciled"])


# ---------------------------------------------------------------------------
# 4. Literal verdict / protected / dependency semantics.
# ---------------------------------------------------------------------------
class VerdictAndProtectedTests(GuardTestBase):
    def test_pivot_required_result_is_a_protected_stop_not_ready(self):
        task = mk_task("t_p", "ready", result="Handoff: PIVOT_REQUIRED - approach invalidated")
        self.assertEqual(self.m.classify_task(task), self.m.BUCKET_PROTECTED)

    def test_literal_block_and_reject_tokens_are_protected(self):
        for text in ("verdict: BLOCK", "REJECT: unsafe", "REJECTED by reviewer", "BLOCKED on owner"):
            self.assertEqual(self.m.classify_task(mk_task("t_v", "ready", result=text)), self.m.BUCKET_PROTECTED, text)

    def test_lookalike_words_are_not_verdicts(self):
        for text in ("task UNBLOCKED and resumed", "PASSPORT service", "blocklist rebuilt", "PASSING"):
            self.assertEqual(self.m.verdict_tokens(text), [], text)

    def test_needs_input_and_capability_blocks_are_protected(self):
        for kind in ("needs_input", "capability"):
            task = mk_task("t_b", "blocked", block_kind=kind)
            self.assertEqual(self.m.classify_task(task), self.m.BUCKET_PROTECTED, kind)

    def test_dependency_and_transient_blocks_are_gated_not_protected(self):
        for kind in ("dependency", "transient"):
            task = mk_task("t_b", "blocked", block_kind=kind)
            self.assertEqual(self.m.classify_task(task), self.m.BUCKET_GATED, kind)

    def test_unknown_or_absent_block_kind_fails_closed_to_protected(self):
        for kind in (None, "", "mystery"):
            task = mk_task("t_b", "blocked", block_kind=kind)
            self.assertEqual(self.m.classify_task(task), self.m.BUCKET_PROTECTED, repr(kind))

    def test_unsatisfied_predecessor_blocks_preflight(self):
        self.profile("oliver")
        task = dict(mk_task("t_child", "ready", assignee="oliver"), _board="b")
        gates = {("b", "t_child"): {"parents": [{"id": "t_parent", "status": "review"}], "children": [], "latest_summary": None}}
        problems = self.m.preflight_ready_task(task, self.profiles, gates)
        self.assertIn("unsatisfied_dependency:t_parent:review", problems)

    def test_accepted_predecessor_clears_the_dependency_gate(self):
        self.profile("oliver")
        task = dict(mk_task("t_child", "ready", assignee="oliver"), _board="b")
        gates = {("b", "t_child"): {"parents": [{"id": "t_parent", "status": "done"}], "children": [], "latest_summary": None}}
        self.assertEqual(self.m.preflight_ready_task(task, self.profiles, gates), [])

    def test_missing_dependency_evidence_fails_closed(self):
        self.profile("oliver")
        task = dict(mk_task("t_child", "ready", assignee="oliver"), _board="b")
        problems = self.m.preflight_ready_task(task, self.profiles, {})
        self.assertIn("dependency_evidence_unavailable", problems)
        self.assertIn("successor_evidence_unavailable", problems)

    def test_run_summary_verdict_blocks_preflight(self):
        self.profile("oliver")
        task = dict(mk_task("t_r", "ready", assignee="oliver"), _board="b")
        gates = {("b", "t_r"): {"parents": [], "children": [], "latest_summary": "worker handoff: PIVOT_REQUIRED"}}
        self.assertIn("verdict_block:PIVOT_REQUIRED", self.m.preflight_ready_task(task, self.profiles, gates))

    def test_claim_lock_and_circuit_breaker_block_preflight(self):
        self.profile("oliver")
        gates = {("b", "t_r"): {"parents": [], "children": [], "latest_summary": None}}
        locked = dict(mk_task("t_r", "ready", assignee="oliver", claim_lock="w-1"), _board="b")
        self.assertIn("already_claimed", self.m.preflight_ready_task(locked, self.profiles, gates))
        tripped = dict(mk_task("t_r", "ready", assignee="oliver", consecutive_failures=3), _board="b")
        self.assertIn("circuit_breaker_tripped", self.m.preflight_ready_task(tripped, self.profiles, gates))

    def test_activation_gate_requires_literal_pass(self):
        buckets = {b: [] for b in self.m.BUCKETS}
        buckets[self.m.BUCKET_HISTORICAL] = [dict(gate_task(), _board="b")]
        self.assertTrue(self.m.activation_gate(buckets)["ok"])

    def test_activation_gate_fails_closed_when_missing_or_unaccepted(self):
        empty = {b: [] for b in self.m.BUCKETS}
        self.assertEqual(self.m.activation_gate(empty)["reason"], "gate_task_not_found")

        not_done = {b: [] for b in self.m.BUCKETS}
        not_done[self.m.BUCKET_EXECUTING] = [dict(mk_task("t_13b90c53", "running", result="PASS"), _board="b")]
        self.assertFalse(self.m.activation_gate(not_done)["ok"])

        no_verdict = {b: [] for b in self.m.BUCKETS}
        no_verdict[self.m.BUCKET_HISTORICAL] = [dict(mk_task("t_13b90c53", "done"), _board="b")]
        self.assertEqual(self.m.activation_gate(no_verdict)["reason"], "gate_task_not_pass")

        blocked = {b: [] for b in self.m.BUCKETS}
        blocked[self.m.BUCKET_HISTORICAL] = [dict(mk_task("t_13b90c53", "done", result="PASS but PIVOT_REQUIRED"), _board="b")]
        self.assertIn("gate_task_blocking_verdict", self.m.activation_gate(blocked)["reason"])

    def test_no_dispatch_while_activation_gate_is_unmet(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.boards[board] = [mk_task("t_ready", "ready", assignee="oliver")]  # no gate card at all
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.dispatch_calls, [])
        self.assertIn("activation_gate:gate_task_not_found", self.state()["dispatch_blockers"])


# ---------------------------------------------------------------------------
# 2. Strict fresh worker/run evidence.
# ---------------------------------------------------------------------------
class LivenessValidationTests(GuardTestBase):
    def _executing(self, board="codevolt-managed-delivery", **kw):
        kw.setdefault("last_heartbeat_at", NOW)
        task = mk_task("t_run", "running", assignee="oliver", **kw)
        self.boards[board] = [gate_task(), task]
        self.runs["t_run"] = live_run()
        return task

    def test_healthy_lane_requires_both_task_and_run_heartbeat(self):
        self._executing()
        self.assertEqual(self.check(), 0)
        validation = self.state()["executing_validation"][0]
        self.assertTrue(validation["healthy"])
        self.assertEqual(validation["worker_pid"], 4242)

    def test_missing_task_heartbeat_is_unhealthy(self):
        self._executing(last_heartbeat_at=None)
        self.assertEqual(self.check(), 1)
        self.assertIn("missing_task_heartbeat", self.state()["executing_validation"][0]["reasons"])

    def test_stale_task_heartbeat_is_unhealthy(self):
        self._executing(last_heartbeat_at=NOW - 10_000)
        self.assertEqual(self.check(), 1)
        self.assertIn("stale_task_heartbeat", self.state()["executing_validation"][0]["reasons"])

    def test_future_heartbeat_is_rejected_not_trusted(self):
        self._executing(last_heartbeat_at=NOW + 10_000)
        self.assertEqual(self.check(), 1)
        self.assertIn("future_task_heartbeat", self.state()["executing_validation"][0]["reasons"])

    def test_stale_run_heartbeat_is_unhealthy_even_with_fresh_task_row(self):
        self._executing()
        self.runs["t_run"][0]["last_heartbeat_at"] = NOW - 9_000
        self.assertEqual(self.check(), 1)
        self.assertIn("stale_run_heartbeat", self.state()["executing_validation"][0]["reasons"])

    def test_dead_worker_pid_is_unhealthy(self):
        self._executing()
        self.alive = False
        self.assertEqual(self.check(), 1)
        self.assertIn("dead_worker_pid", self.state()["executing_validation"][0]["reasons"])

    def test_ended_run_row_is_not_a_live_lane(self):
        self._executing()
        self.runs["t_run"][0]["ended_at"] = NOW - 5
        self.assertEqual(self.check(), 1)
        self.assertIn("no_live_run_row", self.state()["executing_validation"][0]["reasons"])

    def test_absent_run_row_is_unhealthy(self):
        self._executing()
        self.runs["t_run"] = []
        self.assertEqual(self.check(), 1)
        self.assertIn("no_run_row", self.state()["executing_validation"][0]["reasons"])

    def test_latest_run_is_chosen_by_start_order_not_list_order(self):
        runs = [
            {"id": 9, "status": "closed", "started_at": NOW - 900, "ended_at": NOW - 800, "worker_pid": 1, "last_heartbeat_at": NOW - 900},
            {"id": 2, "status": "running", "started_at": NOW - 60, "ended_at": None, "worker_pid": 5, "last_heartbeat_at": NOW},
        ]
        self.assertEqual(self.m.latest_run(runs)["id"], 2)

    def test_reconcile_runs_once_and_can_clear_a_stale_claim(self):
        board = "codevolt-managed-delivery"
        self.boards[board] = [gate_task(), mk_task("t_flaky", "running", assignee="oliver", last_heartbeat_at=NOW - 9_999)]
        self.runs["t_flaky"] = live_run()

        def snapshot(b):
            result = self._snapshot(b)
            if b == board and self.reconcile_calls:
                result["tasks"] = [gate_task(), mk_task("t_flaky", "todo", assignee="oliver")]
                result["source_total"] = 2
            return result

        with mock.patch.object(self.m, "fetch_board_snapshot", side_effect=snapshot):
            self.assertEqual(self.check(), 0)
        self.assertEqual(self.reconcile_calls, [board])
        self.assertEqual(self.state()["executing_validation"], [])

    def test_owner_missing_on_an_executing_card_is_unhealthy(self):
        board = "codevolt-managed-delivery"
        self.boards[board] = [gate_task(), mk_task("t_run", "running", assignee=None, last_heartbeat_at=NOW)]
        self.runs["t_run"] = live_run()
        self.assertEqual(self.check(), 1)
        self.assertIn("no_accountable_owner", self.state()["executing_validation"][0]["reasons"])


# ---------------------------------------------------------------------------
# 3. Exact-task dispatch binding.
# ---------------------------------------------------------------------------
class ExactDispatchTests(GuardTestBase):
    def _ready_board(self, board="codevolt-managed-delivery"):
        self.profile("oliver")
        self.boards[board] = [
            gate_task(),
            mk_task("t_low", "ready", assignee="oliver", priority=1, workspace_path="/ws/low"),
        ]
        return board

    def test_dispatch_is_bound_to_the_selected_task_id(self):
        board = self._ready_board()
        self.dispatch_payload = {"spawned": [{"task_id": "t_low", "assignee": "oliver", "workspace": "/ws/low"}]}
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.dispatch_calls, [(board, "t_low")])
        state = self.state()
        self.assertEqual(state["status"], "dispatched-ready")
        self.assertEqual(state["last_dispatch"]["task_id"], "t_low")

    def test_a_higher_priority_task_cannot_steal_the_dispatch(self):
        """The dispatcher answers with a different, higher-priority card."""
        board = self._ready_board()
        self.boards[board].append(
            mk_task("t_high", "ready", assignee="maya", priority=99, workspace_path="/ws/high", skills=["nope"])
        )
        self.profile("maya", skills=())  # maya cannot satisfy the skill, so t_low is selected
        self.dispatch_payload = {"spawned": [{"task_id": "t_high", "assignee": "maya", "workspace": "/ws/high"}]}
        self.assertEqual(self.check(), 1)
        self.assertEqual(self.dispatch_calls, [(board, "t_low")])
        state = self.state()
        self.assertEqual(state["status"], "guard-failed")
        self.assertIn("spawned the wrong task", state["last_error"])
        self.assertIn("requested=t_low", state["last_error"])
        self.assertTrue(any("CRITICAL" in a for a in self.alerts))

    def test_multiple_spawns_for_max_one_is_a_fault(self):
        result = mock.Mock(returncode=0, stdout=json.dumps({"spawned": [{"task_id": "t_a"}, {"task_id": "t_b"}]}))
        with self.assertRaises(RuntimeError) as ctx:
            self.m.assert_exact_spawn(result, "t_a")
        self.assertIn("spawned 2 workers", str(ctx.exception))

    def test_declined_admission_is_a_race_not_a_success(self):
        self._ready_board()
        self.dispatch_payload = {"spawned": [], "skipped_nonspawnable": ["t_low"]}
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertFalse(state["last_dispatch"]["spawned"])
        self.assertIn("dispatch_not_admitted", state["dispatch_blockers"])
        self.assertNotEqual(state["status"], "dispatched-ready")
        self.assertTrue(any("declined" in a for a in self.alerts))

    def test_malformed_dispatch_response_fails_closed(self):
        for stdout in ("[]", '{"ok":true}', "not json"):
            with self.subTest(stdout=stdout):
                result = mock.Mock(returncode=0, stdout=stdout)
                with self.assertRaises(RuntimeError):
                    self.m.assert_exact_spawn(result, "t_a")

    def test_only_one_card_is_dispatched_per_invocation(self):
        board = self._ready_board()
        self.boards[board].append(mk_task("t_second", "ready", assignee="maya", workspace_path="/ws/second"))
        self.profile("maya")
        self.dispatch_payload = {"spawned": [{"task_id": "t_low", "assignee": "oliver"}]}
        self.assertEqual(self.check(), 0)
        self.assertEqual(len(self.dispatch_calls), 1)


# ---------------------------------------------------------------------------
# 6. Capability preflight against real fields and profile-local config.
# ---------------------------------------------------------------------------
class CapabilityPreflightTests(GuardTestBase):
    def setUp(self):
        super().setUp()
        self.gates = {("b", "t_r"): {"parents": [], "children": [], "latest_summary": None}}

    def _task(self, **kw):
        return dict(mk_task("t_r", "ready", assignee="oliver", **kw), _board="b")

    def test_missing_profile_blocks(self):
        self.assertIn("profile_missing:oliver", self.m.preflight_ready_task(self._task(), {}, self.gates))

    def test_missing_forced_skill_blocks_using_the_real_skills_field(self):
        self.profile("oliver", skills=["systematic-debugging"])
        problems = self.m.preflight_ready_task(self._task(skills=["lifecycle-review"]), self.profiles, self.gates)
        self.assertIn("missing_skill:lifecycle-review", problems)

    def test_installed_skill_passes(self):
        self.profile("oliver", skills=["lifecycle-review"])
        self.assertEqual(self.m.preflight_ready_task(self._task(skills=["lifecycle-review"]), self.profiles, self.gates), [])

    def test_workspace_task_needs_a_workspace_capable_toolset(self):
        self.profile("oliver", toolsets=["web", "memory"])
        problems = self.m.preflight_ready_task(self._task(workspace_path="/ws/a"), self.profiles, self.gates)
        self.assertIn("missing_workspace_toolset", problems)

    def test_unreadable_profile_config_fails_closed(self):
        self.profile("oliver", ok=False)
        self.profiles["oliver"]["config_error"] = "ParserError"
        problems = self.m.preflight_ready_task(self._task(), self.profiles, self.gates)
        self.assertTrue(any(p.startswith("profile_unusable:oliver") for p in problems))

    def test_profile_alias_collision_makes_the_alias_unusable(self):
        """Two directories normalizing to one key must not silently race."""
        root = Path(self.temp.name) / "profiles"
        # "oliver" and "oliver " are distinct directories on every filesystem
        # this runs on, and both normalize to the alias "oliver".
        for name in ("oliver", "oliver "):
            directory = root / name
            (directory / "skills" / "cat" / "s1").mkdir(parents=True, exist_ok=True)
            (directory / "skills" / "cat" / "s1" / "SKILL.md").write_text("x")
            (directory / "config.yaml").write_text("model:\n  default: m\nplatform_toolsets:\n  cli: [terminal]\n")
        self.assertEqual(len(list(root.iterdir())), 2)
        fresh = load_module()
        with mock.patch.object(fresh, "PROFILES_DIR", root):
            profiles = fresh.fetch_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertFalse(profiles["oliver"]["config_ok"])
        self.assertIn("alias_collision", profiles["oliver"]["config_error"])
        self.assertEqual(profiles["oliver"]["alias_collision"], ["oliver", "oliver "])

    def test_colliding_alias_blocks_dispatch_rather_than_binding_to_one(self):
        board = "codevolt-managed-delivery"
        self.profiles["oliver"] = {
            "name": "oliver",
            "key": "oliver",
            "skills": [],
            "toolsets": ["terminal"],
            "model": "m",
            "config_ok": False,
            "config_error": "alias_collision:oliver,oliver ",
            "alias_collision": ["oliver", "oliver "],
        }
        self.boards[board] = [gate_task(), mk_task("t_ready", "ready", assignee="oliver")]
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.dispatch_calls, [])
        report = self.state()["ready_preflight"][0]
        self.assertTrue(any("alias_collision" in p for p in report["problems"]))

    def test_profile_scan_reads_local_config_and_skills(self):
        root = Path(self.temp.name) / "profiles-ok"
        directory = root / "maya"
        (directory / "skills" / "security" / "threat-modelling").mkdir(parents=True)
        (directory / "skills" / "security" / "threat-modelling" / "SKILL.md").write_text("x")
        (directory / "skills" / "flat-skill").mkdir(parents=True)
        (directory / "skills" / "flat-skill" / "SKILL.md").write_text("x")
        (directory / "config.yaml").write_text(
            "model:\n  default: gpt-5.6-sol\n  provider: openai-codex\n"
            "agent:\n  disabled_toolsets: [browser]\n"
            "platform_toolsets:\n  cli: [terminal, browser, kanban]\n"
        )
        fresh = load_module()
        with mock.patch.object(fresh, "PROFILES_DIR", root):
            profiles = fresh.fetch_profiles()
        snapshot = profiles["maya"]
        self.assertTrue(snapshot["config_ok"])
        self.assertEqual(snapshot["skills"], ["flat-skill", "threat-modelling"])
        self.assertEqual(snapshot["toolsets"], ["kanban", "terminal"])
        self.assertEqual(snapshot["model"], "gpt-5.6-sol")

    def test_capability_gap_raises_an_immediate_alert(self):
        board = "codevolt-managed-delivery"
        self.boards[board] = [gate_task(), mk_task("t_ready", "ready", assignee="ghost")]
        self.assertEqual(self.check(), 0)
        self.assertTrue(any("capability gap" in a for a in self.alerts))
        self.assertEqual(self.dispatch_calls, [])


# ---------------------------------------------------------------------------
# 7. Independent collision keys.
# ---------------------------------------------------------------------------
class CollisionTests(GuardTestBase):
    def test_same_workspace_different_branch_still_collides(self):
        a = mk_task("t_a", "running", workspace_path="/ws/shared", branch_name="feat-a")
        b = mk_task("t_b", "ready", workspace_path="/ws/shared", branch_name="feat-b")
        self.assertTrue(self.m.collision_keys(a) & self.m.collision_keys(b))

    def test_each_shared_target_is_an_independent_key(self):
        task = mk_task("t_a", "ready", workspace_path="/ws/a", project_id="proj-1")
        keys = self.m.collision_keys(task, {"t_a": {"repo:/ws/a", "external:pagerduty"}})
        self.assertEqual(
            keys,
            {("workspace", "/ws/a"), ("project", "proj-1"), ("target", "repo:/ws/a"), ("target", "external:pagerduty")},
        )

    def test_a_task_colliding_on_only_one_target_is_still_blocked(self):
        occupied = {("target", "external:pagerduty")}
        task = mk_task("t_a", "ready", workspace_path="/ws/free")
        keys = self.m.collision_keys(task, {"t_a": {"external:pagerduty"}})
        self.assertTrue(keys & occupied)

    def test_no_declared_target_cannot_collide(self):
        self.assertEqual(self.m.collision_keys(mk_task("t_a", "ready")), set())

    def test_collision_skips_occupied_workspace_and_dispatches_the_free_card(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.profile("maya")
        self.boards[board] = [
            gate_task(),
            mk_task("t_active", "running", assignee="oliver", workspace_path="/ws/shared", last_heartbeat_at=NOW),
            mk_task("t_collide", "ready", assignee="maya", priority=50, workspace_path="/ws/shared"),
            mk_task("t_safe", "ready", assignee="maya", priority=10, workspace_path="/ws/free"),
        ]
        self.runs["t_active"] = live_run()
        self.links["t_safe"] = {"parents": [], "children": []}
        self.dispatch_payload = {"spawned": [{"task_id": "t_safe", "assignee": "maya"}]}
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.dispatch_calls, [(board, "t_safe")])
        report = {e["task_id"]: e for e in self.state()["ready_preflight"]}
        self.assertFalse(report["t_collide"]["eligible"])
        self.assertIn("collision:workspace:/ws/shared", report["t_collide"]["problems"])

    def test_two_ready_cards_cannot_both_claim_one_target_in_a_pass(self):
        profiles = {"oliver": {"name": "oliver", "config_ok": True, "skills": [], "toolsets": ["terminal"], "model": "m"}}
        gates = {("b", "t_1"): {"parents": [], "children": []}, ("b", "t_2"): {"parents": [], "children": []}}
        ready = [
            dict(mk_task("t_1", "ready", assignee="oliver", priority=5, workspace_path="/ws/x"), _board="b"),
            dict(mk_task("t_2", "ready", assignee="oliver", priority=1, workspace_path="/ws/x"), _board="b"),
        ]
        chosen, report = self.m.select_dispatch_candidate(ready, profiles, set(), gates)
        self.assertEqual(chosen["id"], "t_1")
        second = next(e for e in report if e["task_id"] == "t_2")
        self.assertIn("collision:workspace:/ws/x", second["problems"])


# ---------------------------------------------------------------------------
# 8. Standalone execution evidence via work-claims.
# ---------------------------------------------------------------------------
class WorkClaimsTests(GuardTestBase):
    def test_reads_real_claims_schema_query_only(self):
        db = Path(self.temp.name) / "work-claims.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE claims (
                claim_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, summary TEXT NOT NULL,
                workspace TEXT, source_workspace TEXT, kanban_task_id TEXT,
                status TEXT NOT NULL CHECK(status IN ('active','released','expired')),
                acquired_at INTEGER NOT NULL, heartbeat_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL, released_at INTEGER, release_summary TEXT);
            CREATE TABLE claim_targets (target TEXT PRIMARY KEY, claim_id TEXT NOT NULL);
            """
        )
        conn.execute(
            "INSERT INTO claims VALUES ('c1','s1','work','/ws/a',NULL,'t_run','active',1,2,3,NULL,NULL)"
        )
        conn.execute("INSERT INTO claims VALUES ('c2','s2','old',NULL,NULL,NULL,'released',1,2,3,NULL,NULL)")
        conn.execute("INSERT INTO claim_targets VALUES ('repo:/ws/a','c1')")
        conn.commit()
        conn.close()
        fresh = load_module()
        with mock.patch.object(fresh, "WORK_CLAIMS_DB", db):
            evidence = fresh.fetch_work_claims()
        self.assertTrue(evidence["available"])
        self.assertFalse(evidence["has_execution_leases"])
        self.assertEqual([c["claim_id"] for c in evidence["claims"]], ["c1"])  # released rows excluded
        self.assertEqual(evidence["claims"][0]["targets"], ["repo:/ws/a"])

    def test_missing_claims_db_raises_rather_than_reporting_empty(self):
        fresh = load_module()
        with mock.patch.object(fresh, "WORK_CLAIMS_DB", Path(self.temp.name) / "absent.db"), self.assertRaises(
            RuntimeError
        ):
            fresh.fetch_work_claims()

    def test_linked_claim_corroborates_a_lane_without_creating_one(self):
        board = "codevolt-managed-delivery"
        self.boards[board] = [gate_task(), mk_task("t_run", "running", assignee="oliver", last_heartbeat_at=NOW)]
        self.runs["t_run"] = live_run()
        self.claims["claims"] = [active_claim("c1", "t_run", targets=["repo:/ws/a"])]
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(len(state["standalone_claims"]["linked"]), 1)
        self.assertEqual(state["standalone_claims"]["faults"], [])
        self.assertEqual(state["healthy_lane_count"], 1)
        self.assertEqual(state["accountable_specialists"], ["oliver"])

    def test_claim_holder_is_never_counted_as_an_owner(self):
        """An unhealthy card with a live claim must not manufacture a specialist."""
        board = "codevolt-managed-delivery"
        self.boards[board] = [
            gate_task(),
            mk_task("t_ok", "running", assignee="oliver", last_heartbeat_at=NOW),
            mk_task("t_bad", "running", assignee="maya", last_heartbeat_at=NOW - 9_999),
        ]
        self.runs["t_ok"] = live_run()
        self.runs["t_bad"] = live_run()
        self.claims["claims"] = [active_claim("c1", "t_bad")]
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["accountable_specialists"], ["oliver"])
        self.assertEqual(state["healthy_lane_count"], 1)

    def test_expired_missing_and_future_claims_are_faults(self):
        known = {"t_run": {"_board": "b", "status": "running"}}
        cases = {
            "lease_expired": active_claim("c1", "t_run", expires_at=NOW - 1),
            "missing_expiry": active_claim("c2", "t_run", expires_at=None),
            "future_heartbeat": active_claim("c3", "t_run", heartbeat_at=NOW + 999),
            "stale_heartbeat": active_claim("c4", "t_run", heartbeat_at=NOW - 9_999),
            "unlinked_to_task": active_claim("c5", None),
            "unknown_task": active_claim("c6", "t_elsewhere"),
        }
        for reason, claim in cases.items():
            linked, faults = self.m.link_work_claims([claim], known, {"t_run"}, NOW)
            self.assertEqual(linked, [], reason)
            self.assertIn(reason, faults[0]["reasons"], reason)

    def test_claim_against_a_non_executing_task_is_a_fault(self):
        known = {"t_done": {"_board": "b", "status": "done"}}
        linked, faults = self.m.link_work_claims([active_claim("c1", "t_done")], known, set(), NOW)
        self.assertEqual(linked, [])
        self.assertIn("task_not_executing:done", faults[0]["reasons"])

    def test_claim_fault_blocks_dispatch_and_alerts(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.boards[board] = [gate_task(), mk_task("t_ready", "ready", assignee="oliver")]
        self.claims["claims"] = [active_claim("c-orphan", None)]
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["status"], "liveness-fault")
        self.assertIn("standalone_claim_fault", state["dispatch_blockers"])
        self.assertEqual(self.dispatch_calls, [])
        self.assertTrue(any("work-claims evidence is faulted" in a for a in self.alerts))

    def test_fresh_standalone_execution_is_verified_without_inventing_an_owner(self):
        claim = active_claim("c-standalone", None)
        claim["lease"] = {"pid": 5151, "progress_seq": 9, "observed_at": NOW}
        self.claims["has_execution_leases"] = True
        self.claims["claims"] = [claim]
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(
            state["standalone_claims"]["executing"],
            [{"claim_id": "c-standalone", "pid": 5151, "progress_seq": 9}],
        )
        self.assertEqual(state["accountable_specialists"], [])
        self.assertEqual(state["healthy_worker_count"], 1)
        self.assertEqual(state["status"], "healthy-active")

    def test_standalone_execution_must_advance_after_its_first_observation(self):
        claim = active_claim("c-standalone", None)
        claim["lease"] = {"pid": 5151, "progress_seq": 9, "observed_at": NOW}
        self.claims["has_execution_leases"] = True
        self.claims["claims"] = [claim]
        self.assertEqual(self.check(now=NOW), 0)
        self.assertEqual(self.check(now=NOW + 60), 1)
        state = self.state()
        self.assertEqual(state["standalone_claims"]["executing"], [])
        self.assertIn("execution_not_advancing", state["standalone_claims"]["faults"][0]["reasons"])

    def test_standalone_execution_occupies_workspace_and_target_collision_keys(self):
        claim = active_claim(
            "c-standalone",
            None,
            workspace="/ws/held",
            targets=["external:held"],
            lease={"pid": 5151, "progress_seq": 9, "observed_at": NOW},
        )
        self.claims.update(has_execution_leases=True, claims=[claim])
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.boards[board] = [
            gate_task(),
            mk_task("t_ready", "ready", assignee="oliver", workspace_path="/ws/held"),
        ]
        self.assertEqual(self.check(), 0)
        report = self.state()["ready_preflight"][0]
        self.assertIn("collision:workspace:/ws/held", report["problems"])
        self.assertEqual(self.dispatch_calls, [])


# ---------------------------------------------------------------------------
# 9. Runway, unique owners, one-lane exception, mixed health.
# ---------------------------------------------------------------------------
class RunwayTests(GuardTestBase):
    def _lane(self, board, task_id, owner, workspace, healthy=True):
        task = mk_task(
            task_id,
            "running",
            assignee=owner,
            workspace_path=workspace,
            last_heartbeat_at=NOW if healthy else NOW - 9_999,
        )
        self.boards.setdefault(board, []).append(task)
        self.runs[task_id] = live_run(pid=abs(hash(task_id)) % 30000 + 100)
        return task

    def test_ready_card_is_inventory_never_a_healthy_lane(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.boards[board] = [gate_task(), mk_task("t_ready", "ready", assignee="oliver")]
        self.dispatch_payload = {"spawned": []}
        self.check()
        state = self.state()
        self.assertEqual(state["healthy_lane_count"], 0)
        self.assertEqual(state["accountable_specialists"], [])
        self.assertEqual(state["ready_inventory"], [f"{board}:t_ready"])

    def test_mixed_healthy_and_unhealthy_fails_closed_without_dispatching(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.profile("maya")
        self.profile("clara")
        self.boards[board] = [gate_task()]
        self._lane(board, "t_ok", "oliver", "/ws/1", healthy=True)
        self._lane(board, "t_bad", "maya", "/ws/2", healthy=False)
        self.boards[board].append(mk_task("t_ready", "ready", assignee="clara", workspace_path="/ws/3"))
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["healthy_lane_count"], 1)
        self.assertEqual(state["status"], "liveness-fault")
        self.assertIn("unhealthy_executing_lane", state["dispatch_blockers"])
        self.assertEqual(self.dispatch_calls, [])

    def test_all_lanes_unhealthy_is_a_critical_fault_with_full_evidence(self):
        board = "codevolt-managed-delivery"
        self.profile("clara")
        self.boards[board] = [gate_task()]
        self._lane(board, "t_bad", "maya", "/ws/2", healthy=False)
        self.boards[board].append(mk_task("t_ready", "ready", assignee="clara", workspace_path="/ws/free"))
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["status"], "liveness-fault")
        self.assertTrue(state["no_healthy_owner"])
        self.assertIn("no_healthy_worker_owner", state["dispatch_blockers"])
        self.assertEqual(self.dispatch_calls, [])
        # The diagnosis survives: evidence is recorded, not thrown away.
        self.assertEqual(state["executing_validation"][0]["task_id"], "t_bad")
        self.assertTrue(state["reconciled"])
        self.assertTrue(any("CRITICAL" in a and "no healthy worker owner" in a for a in self.alerts))

    def test_no_dispatch_at_the_three_lane_ceiling(self):
        board = "codevolt-managed-delivery"
        self.profile("clara")
        self.boards[board] = [gate_task()]
        for index, owner in enumerate(("oliver", "maya", "hannah")):
            self._lane(board, f"t_run{index}", owner, f"/ws/{index}")
            self.profile(owner)
        self.boards[board].append(mk_task("t_ready", "ready", assignee="clara", workspace_path="/ws/free"))
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(state["healthy_lane_count"], self.m.RUNWAY_MAX_LANES)
        self.assertIn("runway_at_ceiling", state["dispatch_blockers"])
        self.assertEqual(state["status"], "runway-at-target")
        self.assertEqual(self.dispatch_calls, [])

    def test_one_healthy_lane_requires_a_proven_exception(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.profile("maya")
        self.boards[board] = [gate_task()]
        self._lane(board, "t_lane", "oliver", "/ws/shared")
        self.boards[board].append(
            mk_task("t_ready", "ready", assignee="maya", workspace_path="/ws/shared")  # same target -> unproven
        )
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(self.dispatch_calls, [])
        report = {e["task_id"]: e for e in state["ready_preflight"]}
        self.assertIn("collision:workspace:/ws/shared", report["t_ready"]["problems"])

    def test_one_lane_exception_proof_is_deterministic_and_recorded(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.profile("maya")
        self.boards[board] = [gate_task()]
        self._lane(board, "t_lane", "oliver", "/ws/lane")
        self.boards[board].append(mk_task("t_ready", "ready", assignee="maya", workspace_path="/ws/other"))
        self.dispatch_payload = {"spawned": [{"task_id": "t_ready", "assignee": "maya"}]}
        first = self.check()
        proof = self.state()["one_lane_exception"]
        self.assertEqual(first, 0)
        self.assertTrue(proof["applies"])
        self.assertTrue(all(proof["clauses"].values()))
        self.assertEqual(self.dispatch_calls, [(board, "t_ready")])

        # Deterministic: an identical second evaluation reaches the same proof.
        again = self.m.one_lane_exception(
            dict(mk_task("t_ready", "ready", assignee="maya", workspace_path="/ws/other"), _board=board),
            [{"board": board, "task_id": "t_lane", "assignee": "oliver"}],
            {(board, "t_lane"): dict(mk_task("t_lane", "running", workspace_path="/ws/lane"), _board=board)},
            {(board, "t_ready"): {"parents": [], "children": []}, (board, "t_lane"): {"parents": [], "children": []}},
        )
        self.assertEqual(again[1]["clauses"], proof["clauses"])

    def test_one_lane_exception_refuses_the_same_owner(self):
        candidate = dict(mk_task("t_ready", "ready", assignee="oliver", workspace_path="/ws/other"), _board="b")
        ok, proof = self.m.one_lane_exception(
            candidate,
            [{"board": "b", "task_id": "t_lane", "assignee": "oliver"}],
            {("b", "t_lane"): dict(mk_task("t_lane", "running", workspace_path="/ws/lane"), _board="b")},
            {("b", "t_ready"): {"parents": [], "children": []}, ("b", "t_lane"): {"parents": [], "children": []}},
        )
        self.assertFalse(ok)
        self.assertFalse(proof["clauses"]["distinct_owner"])

    def test_one_lane_exception_refuses_a_successor_of_the_running_lane(self):
        candidate = dict(mk_task("t_ready", "ready", assignee="maya", workspace_path="/ws/other"), _board="b")
        ok, proof = self.m.one_lane_exception(
            candidate,
            [{"board": "b", "task_id": "t_lane", "assignee": "oliver"}],
            {("b", "t_lane"): dict(mk_task("t_lane", "running", workspace_path="/ws/lane"), _board="b")},
            {
                ("b", "t_ready"): {"parents": [], "children": []},
                ("b", "t_lane"): {"parents": [], "children": [{"id": "t_ready", "status": "ready"}]},
            },
        )
        self.assertFalse(ok)
        self.assertFalse(proof["clauses"]["candidate_is_not_lane_successor"])

    def test_owner_already_holding_a_lane_cannot_take_a_second(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.boards[board] = [gate_task()]
        self._lane(board, "t_lane", "oliver", "/ws/lane")
        self.boards[board].append(mk_task("t_ready", "ready", assignee="oliver", workspace_path="/ws/other"))
        self.assertEqual(self.check(), 0)
        report = {e["task_id"]: e for e in self.state()["ready_preflight"]}
        self.assertIn("owner_already_holds_a_lane:oliver", report["t_ready"]["problems"])
        self.assertEqual(self.dispatch_calls, [])

    def test_successor_currently_executing_blocks_its_predecessor(self):
        self.profile("oliver")
        task = dict(mk_task("t_pred", "ready", assignee="oliver"), _board="b")
        gates = {("b", "t_pred"): {"parents": [], "children": [{"id": "t_succ", "status": "running"}]}}
        problems = self.m.preflight_ready_task(task, self.profiles, gates, set(), {("b", "t_succ")})
        self.assertIn("successor_executing:t_succ", problems)

    def test_runway_bounds_are_recorded_for_the_consumer(self):
        self.boards["codevolt-managed-delivery"] = [gate_task()]
        self.assertEqual(self.check(), 0)
        runway = self.state()["runway"]
        self.assertEqual(runway["min_lanes"], self.m.RUNWAY_MIN_LANES)
        self.assertEqual(runway["max_lanes"], self.m.RUNWAY_MAX_LANES)
        self.assertEqual(runway["current_lanes"], 0)

    def test_repeat_check_is_deterministic_and_does_not_redispatch(self):
        board = "codevolt-managed-delivery"
        self.profile("oliver")
        self.boards[board] = [gate_task(), mk_task("t_ready", "ready", assignee="oliver", workspace_path="/ws/a")]
        self.dispatch_payload = {"spawned": [{"task_id": "t_ready", "assignee": "oliver"}]}
        self.assertEqual(self.check(now=NOW), 0)
        first = self.state()

        # The card is now running with a live worker: the same checker must
        # settle, not dispatch it again.
        self.boards[board] = [gate_task(), mk_task("t_ready", "running", assignee="oliver", workspace_path="/ws/a", last_heartbeat_at=NOW + 60)]
        self.runs["t_ready"] = live_run(now=NOW + 60)
        self.assertEqual(self.check(now=NOW + 60), 0)
        second = self.state()
        self.assertEqual(len(self.dispatch_calls), 1)
        self.assertEqual(second["healthy_lane_count"], 1)
        self.assertEqual(first["status"], "dispatched-ready")
        self.assertEqual(second["status"], "healthy-active")

    def test_unique_specialists_dedupes_and_ignores_blanks(self):
        self.assertEqual(self.m.unique_specialists(["oliver", "oliver ", None, "", "maya", 5]), ["maya", "oliver"])

    def test_two_workers_owned_by_one_specialist_are_one_runway_lane(self):
        board = "platform-command-centre"
        self.profile("maya")
        self.boards[board] = [gate_task()]
        self._lane(board, "t_one", "maya", "/ws/one")
        self._lane(board, "t_two", "maya", "/ws/two")
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(state["healthy_worker_count"], 2)
        self.assertEqual(state["healthy_lane_count"], 1)
        self.assertEqual(state["accountable_specialists_count"], 1)


class SuccessorTests(GuardTestBase):
    def _critical_path_with_children(self, children):
        board = "platform-command-centre"
        self.profile("maya")
        task = mk_task("t_active", "running", assignee="maya", last_heartbeat_at=NOW)
        task["title"] = "[critical-path] release gate"
        self.boards[board] = [gate_task(), task]
        self.runs["t_active"] = live_run(profile="maya")
        self.links["t_active"] = {"parents": [], "children": children}
        return board

    def assert_children_do_not_stage_successor(self, children):
        board = self._critical_path_with_children(children)
        self.assertEqual(self.check(), 1)
        state = self.state()
        key = f"{board}:t_active"
        self.assertEqual(state["immediate_successors"], {key: []})
        self.assertEqual(state["missing_immediate_successors"], [key])
        self.assertIn("missing_critical_path_successor", state["dispatch_blockers"])

    def test_state_emits_only_minimized_explicitly_actionable_successors(self):
        for status in sorted(self.m.ACTIONABLE_SUCCESSOR_STATUSES):
            with self.subTest(status=status):
                board = self._critical_path_with_children(
                    [{"id": "t_successor", "status": status, "untrusted_extra": "/private/evidence"}]
                )
                self.check()
                self.assertEqual(
                    self.state()["immediate_successors"],
                    {f"{board}:t_active": [{"id": "t_successor", "status": status}]},
                )
                self.assertEqual(self.state()["missing_immediate_successors"], [])

    def test_done_child_does_not_stage_a_successor(self):
        self.assert_children_do_not_stage_successor([{"id": "t_old", "status": "done"}])

    def test_archived_child_does_not_stage_a_successor(self):
        self.assert_children_do_not_stage_successor([{"id": "t_old", "status": "archived"}])

    def test_missing_child_row_does_not_stage_a_successor(self):
        self.assert_children_do_not_stage_successor([{"id": "t_missing", "status": None}])

    def test_malformed_child_rows_do_not_stage_a_successor(self):
        for child in (None, "not-a-row", {}, {"id": "bad", "status": "todo"}, {"id": "t_bad", "status": "unknown"}):
            with self.subTest(child=child):
                self.assert_children_do_not_stage_successor([child])

    def test_explicit_critical_path_card_without_successor_blocks_and_alerts(self):
        board = "platform-command-centre"
        self.profile("maya")
        task = mk_task("t_active", "running", assignee="maya", last_heartbeat_at=NOW)
        task["title"] = "[critical-path] release gate"
        self.boards[board] = [gate_task(), task]
        self.runs["t_active"] = live_run(profile="maya")
        self.links["t_active"] = {"parents": [], "children": []}
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["missing_immediate_successors"], [f"{board}:t_active"])
        self.assertIn("missing_critical_path_successor", state["dispatch_blockers"])
        self.assertTrue(any("critical-path" in alert and "missing" in alert for alert in self.alerts))


# ---------------------------------------------------------------------------
# Unowned review cards.
# ---------------------------------------------------------------------------
class OwnerlessReviewTests(GuardTestBase):
    def test_unowned_review_card_is_a_fault_that_blocks_dispatch(self):
        board = "platform-command-centre"
        self.profile("oliver")
        self.boards[board] = [gate_task(), mk_task("t_rev", "review", assignee="", last_heartbeat_at=NOW)]
        self.boards["codevolt-managed-delivery"] = [mk_task("t_ready", "ready", assignee="oliver")]
        self.runs["t_rev"] = live_run()
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["ownerless_reviews"], [{"board": board, "task_id": "t_rev"}])
        self.assertIn("ownerless_review", state["dispatch_blockers"])
        self.assertEqual(self.dispatch_calls, [])
        self.assertTrue(any("no accountable owner" in a for a in self.alerts))

    def test_owned_review_card_is_not_ownerless(self):
        board = "platform-command-centre"
        self.boards[board] = [gate_task(), mk_task("t_rev", "review", assignee="maya", last_heartbeat_at=NOW)]
        self.runs["t_rev"] = live_run()
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.state()["ownerless_reviews"], [])


# ---------------------------------------------------------------------------
# 10. Alert dedupe with retry; explicit degraded/unknown service state.
# ---------------------------------------------------------------------------
class AlertTests(GuardTestBase):
    def _backlog(self):
        self.boards["codevolt-managed-delivery"] = [mk_task("t_todo", "todo")]

    def test_unchanged_signature_does_not_realert(self):
        self._backlog()
        self.check(now=NOW)
        self.check(now=NOW + 60)
        self.assertEqual(len(self.alerts), 1)

    def test_changed_signature_realerts(self):
        self._backlog()
        self.check(now=NOW)
        self.boards["local-model-security-research"] = [mk_task("t_todo2", "todo")]
        self.check(now=NOW + 60)
        self.assertEqual(len(self.alerts), 2)

    def test_failed_send_is_never_recorded_as_delivered_and_is_retried(self):
        self._backlog()
        self.send_results = [False]
        self.check(now=NOW)
        state = self.state()
        self.assertNotIn("manager_attention", state["alert_signatures"])
        self.assertIn("manager_attention", state["pending_alerts"])
        self.assertEqual(state["pending_alerts"]["manager_attention"]["attempts"], 1)

        # Next cycle: identical fault, but the pending alert must be resent.
        self.check(now=NOW + 60)
        state = self.state()
        self.assertEqual(state["resent_alerts"], ["manager_attention"])
        self.assertIn("manager_attention", state["alert_signatures"])
        self.assertEqual(state["pending_alerts"], {})
        self.assertEqual(len(self.alerts), 2)

        # Third cycle: now genuinely deduped.
        self.check(now=NOW + 120)
        self.assertEqual(len(self.alerts), 2)

    def test_repeated_send_failures_keep_retrying_and_count_attempts(self):
        self._backlog()
        self.send_results = [False, False, False]
        self.check(now=NOW)
        self.check(now=NOW + 60)
        state = self.state()
        self.assertIn("manager_attention", state["pending_alerts"])
        self.assertGreaterEqual(len(self.alerts), 2)

    def test_distinct_fault_categories_are_independent(self):
        board = "platform-command-centre"
        self.boards[board] = [gate_task(), mk_task("t_rev", "review", assignee="", last_heartbeat_at=NOW)]
        self.boards["codevolt-managed-delivery"] = [mk_task("t_todo", "todo")]
        self.runs["t_rev"] = live_run()
        self.check(now=NOW)
        self.check(now=NOW + 30)
        categories = set(self.state()["alert_signatures"])
        self.assertIn("ownerless_review", categories)
        self.assertIn("manager_attention", categories)
        self.assertEqual(len(self.alerts), len(categories))

    def test_fault_signature_is_a_hash_of_category_and_payload(self):
        a = self.m.fault_signature("x", {"k": 1})
        self.assertEqual(a, self.m.fault_signature("x", {"k": 1}))
        self.assertNotEqual(a, self.m.fault_signature("y", {"k": 1}))
        self.assertNotEqual(a, self.m.fault_signature("x", {"k": 2}))
        self.assertEqual(len(a), 64)

    def test_alerts_do_not_expose_session_capabilities_or_absolute_paths(self):
        claim = active_claim(
            "c-orphan",
            None,
            session_id="opaque-session-capability",
            workspace="/Users/rook/private/project",
            targets=["repo:/Users/rook/private/project"],
        )
        self.claims["claims"] = [claim]
        self.check()
        rendered = "\n".join(self.alerts)
        self.assertNotIn("opaque-session-capability", rendered)
        self.assertNotIn("/Users/rook/private/project", rendered)
        self.assertNotIn("repo:/Users", rendered)


class ServiceOutcomeTests(GuardTestBase):
    def test_unreadable_board_degrades_the_service_and_never_reports_success(self):
        self.boards["codevolt-managed-delivery"] = [gate_task()]
        self.board_errors["local-model-security-research"] = RuntimeError("db locked")
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["status"], "degraded")
        self.assertFalse(state["reconciled"])
        self.assertEqual(state["degraded_boards"], ["local-model-security-research"])
        self.assertNotIn("classification", state)
        self.assertEqual(self.dispatch_calls, [])
        self.assertTrue(any("DEGRADED" in a for a in self.alerts))

    def test_board_alias_collision_degrades_both_boards(self):
        shared = "/tmp/kanban/same.db"
        self.db_paths["codevolt-managed-delivery"] = shared
        self.db_paths["local-model-security-research"] = shared
        self.assertEqual(self.check(), 1)
        state = self.state()
        self.assertEqual(state["status"], "degraded")
        self.assertEqual(
            sorted(state["degraded_boards"]), ["codevolt-managed-delivery", "local-model-security-research"]
        )
        for board in state["degraded_boards"]:
            self.assertIn("board_alias_collision", state["board_evidence"][board]["error"])

    def test_all_boards_reconciled_and_complete_is_the_only_quiet_success(self):
        for board in self.m.BOARDS:
            self.boards[board] = [mk_task(f"t_{board[:4]}", "done")]
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(state["status"], "complete")
        self.assertTrue(state["reconciled"])
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.dispatch_calls, [])

    def test_success_decisions_exclude_every_fault_state(self):
        for faulted in ("degraded", "guard-failed", "liveness-fault"):
            self.assertNotIn(faulted, self.m.SUCCESS_DECISIONS)

    def test_state_exposes_the_consumer_contract_fields(self):
        self.boards["codevolt-managed-delivery"] = [gate_task()]
        self.assertEqual(self.check(), 0)
        state = self.state()
        self.assertEqual(state["consumer_contract"]["schema_version"], self.m.SCHEMA_VERSION)
        for field in state["consumer_contract"]["fields"]:
            self.assertIn(field, state, field)


# ---------------------------------------------------------------------------
# Wake paths: one deterministic checker, no governor, no nested manager.
# ---------------------------------------------------------------------------
class WakePathTests(GuardTestBase):
    def test_three_boards_are_covered(self):
        self.assertEqual(
            set(self.m.BOARDS),
            {"platform-command-centre", "codevolt-managed-delivery", "local-model-security-research"},
        )

    def test_event_token_accepts_each_board_and_rejects_others(self):
        for board in self.m.BOARDS:
            payload = {"hook_event_name": "kanban_task_completed", "extra": {"board": board, "task_id": "t_a", "run_id": 1}}
            self.assertIsNotNone(self.m.event_token(payload), board)
        self.assertIsNone(
            self.m.event_token(
                {"hook_event_name": "kanban_task_completed", "extra": {"board": "other", "task_id": "t_a"}}
            )
        )
        self.assertIsNone(self.m.event_token({"hook_event_name": "unrelated", "extra": {"board": self.m.BOARDS[0]}}))

    def test_hook_path_spawns_only_this_scripts_watchdog(self):
        payload = {"hook_event_name": "kanban_task_blocked", "extra": {"board": self.m.BOARDS[0], "task_id": "t_x", "run_id": 3}}
        with mock.patch.object(self.m.json, "load", return_value=payload), mock.patch.object(
            self.m.subprocess, "Popen"
        ) as popen:
            self.assertEqual(self.m.hook_main(), 0)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:2], [str(self.m.PYTHON), str(self.m.SCRIPT)])
        self.assertEqual(argv[2], "--watchdog")
        self.assertEqual(len(popen.call_args_list), 1)

    def test_timer_and_event_paths_run_the_same_checker(self):
        with mock.patch.object(self.m, "check_once", return_value=0) as checker:
            with mock.patch.object(self.m.sys, "argv", ["guard", "--watchdog"]):
                self.assertEqual(self.m.main(), 0)
            with mock.patch.object(self.m.sys, "argv", ["guard", "--watchdog", "--event-token", "tok"]):
                self.assertEqual(self.m.main(), 0)
        self.assertEqual([c.kwargs["token"] for c in checker.call_args_list], [None, "tok"])

    def test_concurrent_invocation_is_a_no_op(self):
        with mock.patch.object(self.m.fcntl, "flock", side_effect=BlockingIOError):
            self.assertEqual(self.check(), 0)
        self.assertFalse(self.m.STATE_FILE.exists())

    def test_legacy_state_keys_are_pruned_on_schema_bump(self):
        self.m.atomic_json(
            self.m.STATE_FILE,
            {
                "schema_version": 3,
                "last_governor_trigger_at": 90,
                "governor_runner_pid": 123,
                "healthy_active_owners": 9,
                "standalone_links": {"linked": []},
                "counts": {},
                "last_active_reconciliation": {"spawned": ["stale-task"], "reclaimed": 4},
            },
        )
        self.boards["codevolt-managed-delivery"] = [mk_task("t_d", "done")]
        self.assertEqual(self.check(), 0)
        state = self.state()
        for key in (
            "last_governor_trigger_at",
            "governor_runner_pid",
            "healthy_active_owners",
            "standalone_links",
            "counts",
            "last_active_reconciliation",
        ):
            self.assertNotIn(key, state)
        self.assertEqual(state["schema_version"], self.m.SCHEMA_VERSION)


class LaunchdCanaryHarnessTests(unittest.TestCase):
    def test_harness_builds_a_separate_run_at_load_and_sixty_second_job(self):
        harness_path = SOURCE.parent / "tests" / "run_launchd_canary.py"
        spec = importlib.util.spec_from_file_location("run_launchd_canary", harness_path)
        assert spec and spec.loader
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        payload = harness.build_plist(
            "com.codevolt.continuity-guard.canary.test",
            Path("/usr/bin/python3"),
            Path("/tmp/worker.py"),
            Path("/tmp/guard.py"),
            Path("/tmp/fixture"),
        )
        encoded = plistlib.dumps(payload)
        decoded = plistlib.loads(encoded)
        self.assertTrue(decoded["RunAtLoad"])
        self.assertEqual(decoded["StartInterval"], 60)
        self.assertEqual(decoded["Label"], "com.codevolt.continuity-guard.canary.test")
        self.assertIn("/tmp/fixture", decoded["ProgramArguments"])

    def test_harness_retries_one_absent_rc5_bootstrap(self):
        harness_path = SOURCE.parent / "tests" / "run_launchd_canary.py"
        spec = importlib.util.spec_from_file_location("run_launchd_canary", harness_path)
        assert spec and spec.loader
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        failed = subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5")
        absent = subprocess.CompletedProcess([], 113, "", "service not found")
        succeeded = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(harness, "invoke", side_effect=[failed, absent, succeeded]) as invoke, mock.patch.object(
            harness.time, "sleep"
        ) as sleep:
            result, attempts, unexpectedly_loaded = harness.bootstrap_with_bounded_retry(
                "gui/501",
                "gui/501/com.codevolt.continuity-guard.canary.test",
                Path("/tmp/canary.plist"),
            )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(unexpectedly_loaded)
        self.assertEqual([attempt["rc"] for attempt in attempts], [5, 0])
        self.assertEqual(attempts[0]["post_failure_print_rc"], 113)
        self.assertEqual(invoke.call_count, 3)
        sleep.assert_called_once_with(1.0)

    def test_harness_does_not_retry_if_failed_bootstrap_left_a_service(self):
        harness_path = SOURCE.parent / "tests" / "run_launchd_canary.py"
        spec = importlib.util.spec_from_file_location("run_launchd_canary", harness_path)
        assert spec and spec.loader
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        failed = subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5")
        present = subprocess.CompletedProcess([], 0, "service present", "")
        with mock.patch.object(harness, "invoke", side_effect=[failed, present]) as invoke, mock.patch.object(
            harness.time, "sleep"
        ) as sleep:
            result, attempts, unexpectedly_loaded = harness.bootstrap_with_bounded_retry(
                "gui/501",
                "gui/501/com.codevolt.continuity-guard.canary.test",
                Path("/tmp/canary.plist"),
            )
        self.assertEqual(result.returncode, 5)
        self.assertTrue(unexpectedly_loaded)
        self.assertEqual(attempts[0]["post_failure_print_rc"], 0)
        self.assertEqual(invoke.call_count, 2)
        sleep.assert_not_called()

    def test_harness_does_not_retry_if_absence_check_is_indeterminate(self):
        harness_path = SOURCE.parent / "tests" / "run_launchd_canary.py"
        spec = importlib.util.spec_from_file_location("run_launchd_canary", harness_path)
        assert spec and spec.loader
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        failed = subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5")
        indeterminate = subprocess.CompletedProcess([], 5, "", "Input/output error")
        succeeded = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(harness, "invoke", side_effect=[failed, indeterminate, succeeded]) as invoke, mock.patch.object(
            harness.time, "sleep"
        ) as sleep:
            result, attempts, unexpectedly_loaded = harness.bootstrap_with_bounded_retry(
                "gui/501",
                "gui/501/com.codevolt.continuity-guard.canary.test",
                Path("/tmp/canary.plist"),
            )
        self.assertEqual(result.returncode, 5)
        self.assertFalse(unexpectedly_loaded)
        self.assertEqual(attempts[0]["post_failure_print_rc"], 5)
        self.assertEqual(invoke.call_count, 2)
        sleep.assert_not_called()

    def test_harness_only_accepts_rc113_as_proven_absence(self):
        harness_path = SOURCE.parent / "tests" / "run_launchd_canary.py"
        spec = importlib.util.spec_from_file_location("run_launchd_canary", harness_path)
        assert spec and spec.loader
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        self.assertTrue(harness.is_label_absent(subprocess.CompletedProcess([], 113, "", "service not found")))
        self.assertFalse(harness.is_label_absent(subprocess.CompletedProcess([], 5, "", "Input/output error")))
        self.assertFalse(harness.is_label_absent(subprocess.CompletedProcess([], 0, "service present", "")))

    def test_harness_help_is_runnable_without_touching_launchd(self):
        harness_path = SOURCE.parent / "tests" / "run_launchd_canary.py"
        result = subprocess.run(
            ["python3", str(harness_path), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("isolated launchd canary", result.stdout)


class SubprocessSurfaceTests(GuardTestBase):
    """The guard's entire outbound surface, enumerated and pinned.

    Nothing here may mutate a board, and nothing may start a governor, a
    manager, or any agent loop.
    """

    ALLOWED: ClassVar[dict] = {
        "dispatch_exact_task": ["kanban", "--board", "B", "dispatch", "--task-id", "T", "--max", "1", "--json"],
        "reconcile_active_claims": ["kanban", "--board", "B", "dispatch", "--max", "0", "--json"],
        "send_alert": ["send", "--to"],
    }

    def test_only_three_hermes_commands_exist_and_none_mutate_a_board(self):
        source = SOURCE.read_text()
        self.assertEqual(source.count("str(HERMES)"), len(self.ALLOWED))

        fresh = load_module()
        with mock.patch.object(fresh, "run", return_value=mock.Mock(returncode=0, stdout="{}")) as runner:
            fresh.dispatch_exact_task("B", "T")
            fresh.reconcile_active_claims("B")
            fresh.send_alert("hello")
        argvs = [call[0][0] for call in runner.call_args_list]
        self.assertEqual(len(argvs), 3)
        for argv, expected in zip(argvs, self.ALLOWED.values()):
            self.assertEqual(argv[0], str(fresh.HERMES))
            self.assertEqual(argv[1 : 1 + len(expected)], expected)
        # No board-mutating verb appears in any command the guard can issue.
        words = {word for argv in argvs for word in argv}
        for mutator in ("move", "create", "block", "unblock", "link", "delete", "complete", "assign", "edit"):
            self.assertNotIn(mutator, words, mutator)

    def test_no_governor_or_agent_loop_is_spawned(self):
        fresh = load_module()
        payload = {"hook_event_name": "kanban_task_completed", "extra": {"board": fresh.BOARDS[0], "task_id": "t_a", "run_id": 1}}
        with mock.patch.object(fresh.json, "load", return_value=payload), mock.patch.object(
            fresh.subprocess, "Popen"
        ) as popen:
            fresh.hook_main()
        argv = popen.call_args[0][0]
        # The only process the guard ever starts is this same script's watchdog.
        self.assertEqual(argv[0], str(fresh.PYTHON))
        self.assertEqual(argv[1], str(fresh.SCRIPT))
        self.assertIn("--watchdog", argv)
        source_functions = {name for name in dir(fresh) if callable(getattr(fresh, name))}
        self.assertFalse([n for n in source_functions if "governor" in n.lower()])
        self.assertFalse([n for n in source_functions if "manager" in n.lower() and n != "MANAGER"])


if __name__ == "__main__":
    unittest.main()
