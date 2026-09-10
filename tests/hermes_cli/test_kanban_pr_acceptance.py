"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as pr_acceptance
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


def test_exact_pr_uses_all_head_checks_when_private_repository_rules_are_unavailable(monkeypatch):
    conclusion = "success"
    sha = "a" * 40

    def fake_api(endpoint, *, query=None, paginate=False):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": sha,
                "baseRefName": "main",
                "state": "MERGED",
                "baseRef": {"branchProtectionRule": None},
            }}}}
        if "/rules/branches/" in endpoint:
            raise subprocess.CalledProcessError(1, ["gh", "api"])
        if "/check-runs" in endpoint:
            return [{"total_count": 2, "check_runs": [
                {
                    "id": 42,
                    "name": "verify",
                    "head_sha": sha,
                    "app": {"id": 1},
                    "status": "completed",
                    "conclusion": conclusion,
                    "html_url": "https://github.com/acme/repo/actions/runs/42",
                },
                {
                    "id": 43,
                    "name": "optional-platform-build",
                    "head_sha": sha,
                    "app": {"id": 1},
                    "status": "completed",
                    "conclusion": "skipped",
                    "html_url": "https://github.com/acme/repo/actions/runs/43",
                },
            ]}]
        if "/statuses" in endpoint:
            return [[]]
        if "/pulls/" in endpoint:
            return {"head": {"sha": sha}, "base": {"ref": "main"},
                    "state": "closed", "merged": True}
        raise AssertionError(endpoint)

    monkeypatch.setattr(pr_acceptance, "_api", fake_api)
    receipt = pr_acceptance.collect_acceptance(
        "https://github.com/acme/repo/pull/7", None,
    )
    assert receipt["ok"] is True
    assert receipt["required_source"] == "current_head_ci_fallback"
    assert receipt["repository_rules_available"] is False
    assert receipt["ignored_non_evidence_checks"] == 1
    assert receipt["required"] == [{"context": "verify", "app_id": 1}]

    conclusion = "skipped"
    receipt = pr_acceptance.collect_acceptance(
        "https://github.com/acme/repo/pull/7", None,
    )
    assert receipt["ok"] is False
    assert receipt["classification"] == "missing"


def test_exact_pr_without_repository_policy_or_head_checks_fails_closed(monkeypatch):
    sha = "a" * 40

    def fake_api(endpoint, *, query=None, paginate=False):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": sha,
                "baseRefName": "main",
                "state": "OPEN",
                "baseRef": {"branchProtectionRule": None},
            }}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/check-runs" in endpoint:
            return [{"total_count": 0, "check_runs": []}]
        if "/statuses" in endpoint:
            return [[]]
        if "/pulls/" in endpoint:
            return {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
        raise AssertionError(endpoint)

    monkeypatch.setattr(pr_acceptance, "_api", fake_api)
    receipt = pr_acceptance.collect_acceptance(
        "https://github.com/acme/repo/pull/7", None,
    )
    assert receipt["ok"] is False
    assert receipt["classification"] == "missing"
    assert receipt["required"] == []
