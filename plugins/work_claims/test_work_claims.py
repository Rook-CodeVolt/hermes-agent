from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import core
from . import _MAILBOX_READ_ONLY, _is_mutating, _terminal_is_read_only


class WorkClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    @patch.object(core, "_kanban_create", return_value="t_one")
    def test_conflicting_target_fails_closed(self, _create):
        first = core.acquire("s1", "first", ["external:codevolt-vps"])
        second = core.acquire("s2", "second", ["external:codevolt-vps"])
        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertEqual(second["conflicts"][0]["session_id"], "s1")

    @patch.object(core, "_kanban_create", return_value="t_one")
    def test_profiles_share_one_authoritative_lock_database(self, _create):
        profiles = self.home / "profiles"
        with patch.dict(os.environ, {"HERMES_HOME": str(profiles / "alpha")}, clear=False):
            first = core.acquire("alpha-session", "alpha", ["external:shared-production"])
            self.assertEqual(core.db_path(), self.home.resolve() / "work-claims.db")
        with patch.dict(os.environ, {"HERMES_HOME": str(profiles / "beta")}, clear=False):
            second = core.acquire("beta-session", "beta", ["external:shared-production"])
            self.assertEqual(core.db_path(), self.home.resolve() / "work-claims.db")
        self.assertTrue(first["success"])
        self.assertFalse(second["success"])

    @patch.object(core, "_kanban_complete", return_value=None)
    @patch.object(core, "_kanban_create", return_value="t_one")
    def test_release_allows_later_claim(self, _create, _complete):
        self.assertTrue(core.acquire("s1", "first", ["system:hermes-profile"])["success"])
        self.assertTrue(core.release("s1", "done")["success"])
        self.assertTrue(core.acquire("s2", "second", ["system:hermes-profile"])["success"])

    @patch.object(core, "_kanban_create", return_value="t_one")
    def test_mutation_requires_claim_and_workspace_scope(self, _create):
        allowed, _ = core.mutation_allowed("s1", "write_file", {"path": "/tmp/x"})
        self.assertFalse(allowed)
        conn = core._connect()
        now = 1_900_000_000
        conn.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,'active',?,?,?,?,?)", ("c1", "s1", "test", "/safe", "/repo", "t", now, now, now + 1000, None, None))
        conn.execute("INSERT INTO claim_targets VALUES(?,?)", ("repo:/repo", "c1"))
        conn.close()
        with patch("plugins.work_claims.core.time.time", return_value=now):
            self.assertTrue(core.mutation_allowed("s1", "write_file", {"path": "/safe/file"})[0])
            self.assertFalse(core.mutation_allowed("s1", "write_file", {"path": "/other/file"})[0])
            self.assertFalse(core.mutation_allowed("s1", "terminal", {"workdir": "/repo"})[0])

    def test_target_normalization(self):
        self.assertEqual(core.normalize_target("external:CodeVolt VPS"), "external:codevolt-vps")
        with self.assertRaises(ValueError):
            core.normalize_target("repo:relative")
        with self.assertRaises(ValueError):
            core.normalize_target("unknown:x")

    def test_terminal_read_only_allowlist_is_narrow(self):
        self.assertTrue(_terminal_is_read_only("git status --short"))
        self.assertTrue(_terminal_is_read_only("hermes sessions list --limit 30"))
        self.assertTrue(_terminal_is_read_only("python3 scripts/validate_vault.py"))
        self.assertTrue(_terminal_is_read_only(" ".join(_MAILBOX_READ_ONLY)))
        self.assertFalse(_terminal_is_read_only("git status && rm file"))
        self.assertFalse(_terminal_is_read_only("git show --ext-diff"))
        self.assertFalse(_terminal_is_read_only("python3 arbitrary.py"))
        self.assertFalse(_terminal_is_read_only("hermes config set approvals.mode off"))
        self.assertTrue(_is_mutating("execute_code", {"code": "open('/tmp/x','w').write('x')"}))

    def test_dispatcher_capability_discovery_is_read_only(self):
        workspace = self.home
        st = workspace.stat()
        key = (st.st_dev, st.st_ino)
        self.assertTrue(core._dispatcher_scope_decision("tool_search", {}, workspace, key).allowed)
        self.assertTrue(core._dispatcher_scope_decision("tool_describe", {}, workspace, key).allowed)

    def test_dispatcher_delegation_is_bounded_and_rewritten(self):
        workspace = self.home
        st = workspace.stat()
        key = (st.st_dev, st.st_ino)
        two = core._dispatcher_scope_decision(
            "delegate_task",
            {"tasks": [
                {"goal": "Return the exact marker CHILD-A-OK and do nothing else."},
                {"goal": "Return the exact marker CHILD-B-OK and do nothing else."},
            ]},
            workspace,
            key,
        )
        self.assertTrue(two.allowed)
        self.assertIsNotNone(two.modified_args)
        modified = two.modified_args or {}
        self.assertTrue(modified["_dispatcher_leaf_no_tools"])
        self.assertEqual(len(modified["tasks"]), 2)

        three = core._dispatcher_scope_decision(
            "delegate_task",
            {"tasks": [{"goal": f"Return exact marker CHILD-{i}-OK only."} for i in range(3)]},
            workspace,
            key,
        )
        self.assertFalse(three.allowed)
        self.assertIn("at most 2", three.reason or "")
        self.assertFalse(core._dispatcher_scope_decision("delegate_task", {"action": "list"}, workspace, key).allowed)
        self.assertFalse(core._dispatcher_scope_decision("delegate_task", {"goal": "Return a marker."}, workspace, key).allowed)


if __name__ == "__main__":
    unittest.main()
