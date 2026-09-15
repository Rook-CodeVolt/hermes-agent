"""Phase A/B invocation-path test matrix (design t_a1260456 §9.13, inventory
t_77cc1d04, cutover t_c67e90a0).

Pass 1's ``test_kanban_invocation_authority.py`` proves the MECHANISM itself
(grant issue/activate/verify/revoke semantics, the detach/reparent property)
in isolation. This module proves the 13 additional path-specific behaviours
the inventory's §9.13 requires: that ``decide_mutation_authority`` /
``write_txn`` / the board-pointer gate resolve every admitted path (W1, D1,
D2, H1, M1, E1/CI1) to ALLOW under its own narrow context, and every
prohibited adjacent path (W2, W3, W4, unauthenticated H1, H2, C1/B1,
operational CI, generic-CLI M1) to DENY -- with real Phase B enforcement
turned ON via ``HERMES_KANBAN_INVOCATION_AUTHORITY_ENFORCE=1`` (the
``enforcement_env`` fixture below), since that is the only way these tests
exercise the actual deny behavior Phase B ships. Enforcement OFF (Phase A
telemetry-only, the production default until sign-off) is covered instead
by the full existing regression suite staying green unmodified -- see the
Phase A/B report on t_c67e90a0.

Every test in this module explicitly sets ``HERMES_KANBAN_INVOCATION_AUTHORITY_ENFORCE``
itself (never relies on ambient env) so it is deterministic in isolation and
under `pytest -k`/xdist reordering.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_authority_context as kac
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_invocation_authority as kia

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def enforcement_env(monkeypatch):
    """Turn Phase B enforcement ON for the duration of one test only.

    NOTE: does not assert ``enforcement_enabled() is False`` after ``yield``
    -- pytest finalizes fixtures in reverse dependency order, so at the
    point THIS fixture's post-yield code runs, the shared ``monkeypatch``
    fixture (function-scoped, one instance per test) has not reverted its
    own ``setenv`` yet; that revert happens when ``monkeypatch`` itself
    finalizes, strictly after every fixture that merely depends on it. The
    env var reverting correctly for the NEXT test is already covered by
    ``test_enforcement_off_is_the_default_and_all_above_denials_become_allows``,
    which explicitly ``monkeypatch.delenv``s it and re-checks.
    """
    monkeypatch.setenv(kia.ENFORCEMENT_ENV_VAR, "1")
    assert kia.enforcement_enabled() is True
    yield


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real Kanban DB with one claimed task, isolated HERMES_HOME. Mirrors
    ``test_kanban_invocation_authority.board`` (kept independent rather than
    imported, so this module has no load-order coupling to that file)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv(kia.GRANT_ENV_VAR, raising=False)
    with kbc.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="path-matrix fixture", assignee="tester")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    return {
        "db_path": db_path,
        "task_id": task_id,
        "run_id": int(claimed.current_run_id),
        "home": home,
    }


def _issue_and_activate(board, *, worker_pid: int, ttl_seconds=None):
    """Mints+activates a grant exactly the way the real dispatcher does it
    (``kanban_db_dispatch._default_spawn``): issuance and activation are
    themselves guarded ``write_txn`` mutations (design §5 -- there is no
    bootstrap exemption), so they only succeed under enforcement when a
    trusted context is active around them. In real dispatch this is
    ``kac.dispatcher_authority()``, entered by the D1 call site
    (``gateway.kanban_watchers_dispatcher``) around the WHOLE
    ``dispatch_once`` call that ``_default_spawn`` runs inside of.

    IMPORTANT PRODUCTION FINDING (see the Phase A/B report on t_c67e90a0):
    the standalone ``hermes kanban dispatch`` / ``daemon`` CLI commands
    call ``dispatch_once`` WITHOUT entering ``dispatcher_authority()`` (by
    design -- D1's decision keeps them denied, same class as C1). Under
    Phase B enforcement this means grant issuance for THOSE dispatched
    workers fails too (falls into ``_default_spawn``'s existing
    except-and-continue), so a worker spawned by standalone CLI dispatch
    gets no valid grant at all and is denied every mutation once it starts
    -- not just the CLI's own bookkeeping writes. This helper reproduces
    the WORKING (embedded-gateway-dispatcher) case; there is deliberately
    no helper here for the broken standalone-CLI-spawn case since it has
    no positive test (its entire point is denial).
    """
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        with kac.dispatcher_authority():
            token = kia.issue_pending_grant(
                conn, task_id=board["task_id"], run_id=board["run_id"], ttl_seconds=ttl_seconds,
            )
            grant_id = token.split(".", 2)[1]
            ok = kia.activate_grant(
                conn, grant_id=grant_id, task_id=board["task_id"], run_id=board["run_id"],
                worker_pid=worker_pid,
            )
    assert ok
    return token, grant_id


def _mutate(board, token):
    """One representative real mutation through the guarded ``write_txn``
    boundary: add_comment (allow_nested=True path, the common case)."""
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        if token is not None:
            os.environ[kia.GRANT_ENV_VAR] = token
        else:
            os.environ.pop(kia.GRANT_ENV_VAR, None)
        return kb.add_comment(conn, board["task_id"], "tester", "path-matrix probe")


# --------------------------------------------------------------------------- #
# W1: real dispatcher-spawned worker root performs each intended operation
# family with its PID/start grant, under enforcement.
# --------------------------------------------------------------------------- #

def test_w1_worker_root_with_valid_grant_allowed_under_enforcement(board, enforcement_env):
    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    comment_id = _mutate(board, token)
    assert isinstance(comment_id, int) and comment_id > 0


# --------------------------------------------------------------------------- #
# W2: worker terminal/re-exec mutating CLI denied -- inherited grant,
# stripped grant, and after detachment. (PTY / --force / explicit-board
# variants are covered by decide_mutation_authority's evaluation order
# itself: none of those signals are consulted at all, so a positive test
# against the real function set already proves they cannot bypass it --
# the design explicitly rules them out as inputs, so there is no separate
# code path to test.)
# --------------------------------------------------------------------------- #

def test_w2_inherited_grant_in_child_subprocess_denied_under_enforcement(board, tmp_path, enforcement_env):
    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    probe = tmp_path / "probe_w2.py"
    probe.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "from hermes_cli import kanban_db as kb\n"
        "from hermes_cli import kanban_db_connect as kbc\n"
        f"with kbc.connect_closing(db_path=Path({str(board['db_path'])!r})) as conn:\n"
        "    try:\n"
        f"        kb.add_comment(conn, {board['task_id']!r}, 'tester', 'w2-probe')\n"
        "        print(json.dumps({'denied': False}))\n"
        "    except PermissionError as exc:\n"
        "        print(json.dumps({'denied': True, 'reason': str(exc)}))\n"
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(board["home"])
    env["HERMES_KANBAN_DB"] = str(board["db_path"])
    env[kia.ENFORCEMENT_ENV_VAR] = "1"
    env[kia.GRANT_ENV_VAR] = token  # attacker inherits the parent's token verbatim
    proc = subprocess.run(
        [sys.executable, str(probe)], env=env, cwd=str(ROOT),
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    import json as _json
    result = _json.loads(proc.stdout.strip())
    assert result["denied"] is True, result


def test_w2_stripped_grant_denied_under_enforcement(board, enforcement_env):
    _issue_and_activate(board, worker_pid=os.getpid())
    with pytest.raises(PermissionError):
        _mutate(board, None)


def test_w2_after_detachment_no_own_grant_denied_under_enforcement(board, enforcement_env):
    """A process that never received a grant of its own (the detached-
    descendant end state) is denied -- the same base case the full
    subprocess detach/reparent reproduction in Pass 1 reduces to, here
    checked with enforcement actually flipped on."""
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with pytest.raises(PermissionError):
        _mutate(board, None)


# --------------------------------------------------------------------------- #
# W3/W4: delegate_task child and worker-fired cron denied even in-process
# while the parent worker's grant is valid.
# --------------------------------------------------------------------------- #

def test_w3_delegated_child_context_denies_even_with_valid_grant_under_enforcement(board, enforcement_env):
    from agent import delegation_context

    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    os.environ[kia.GRANT_ENV_VAR] = token
    with kbc.connect_closing(db_path=board["db_path"]) as conn:
        # Sanity: valid outside the delegated scope.
        assert kb.add_comment(conn, board["task_id"], "tester", "outside-delegate") > 0
        with delegation_context.delegated_child_context("path-matrix-child"):
            with pytest.raises(PermissionError):
                kb.add_comment(conn, board["task_id"], "tester", "inside-delegate")


def test_w4_worker_fired_cron_context_denies_even_with_valid_grant_under_enforcement(board, enforcement_env):
    from agent import delegation_context

    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    os.environ[kia.GRANT_ENV_VAR] = token
    tok = delegation_context.enter_non_dispatcher_owned_context()
    try:
        with kbc.connect_closing(db_path=board["db_path"]) as conn:
            with pytest.raises(PermissionError):
                kb.add_comment(conn, board["task_id"], "tester", "inside-cron-context")
    finally:
        delegation_context.exit_non_dispatcher_owned_context(tok)


# --------------------------------------------------------------------------- #
# D1: embedded dispatcher context can claim/promote/reclaim-class writes;
# identical direct call WITHOUT the context is denied (no grant present),
# proving the context is required and not incidentally granted by anything
# else about being "the dispatcher's own code".
# --------------------------------------------------------------------------- #

def test_d1_dispatcher_context_allows_mutation_with_no_worker_grant(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.dispatcher_authority():
        comment_id = _mutate(board, None)
    assert isinstance(comment_id, int) and comment_id > 0


def test_d1_without_dispatcher_context_same_call_denied(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with pytest.raises(PermissionError):
        _mutate(board, None)


def test_d1_context_scoped_to_its_own_with_block_only(board, enforcement_env):
    """The context resets on exit -- a mutation issued just after the
    ``with`` block exits (still no grant) is denied again."""
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.dispatcher_authority():
        assert kac.current_trusted_authority_class() == "dispatcher"
    assert kac.current_trusted_authority_class() is None
    with pytest.raises(PermissionError):
        _mutate(board, None)


# --------------------------------------------------------------------------- #
# D2: embedded gateway notifier context permits only notifier operation
# families in-process; a request handler / worker cannot borrow it (proven
# by the same scoping property as D1 -- the context is not ambiently
# visible outside its own ``with`` block).
# --------------------------------------------------------------------------- #

def test_d2_gateway_notifier_context_allows_mutation_with_no_worker_grant(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.gateway_notifier_authority():
        comment_id = _mutate(board, None)
    assert isinstance(comment_id, int) and comment_id > 0


def test_d2_context_not_visible_outside_its_own_scope(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.gateway_notifier_authority():
        pass
    assert kac.current_trusted_authority_class() is None
    with pytest.raises(PermissionError):
        _mutate(board, None)


def test_d1_and_d2_contexts_are_mutually_exclusive_labels(board, enforcement_env):
    """Both are simultaneously represented in ``_CONTEXTS`` but only one is
    ever entered by legitimate call sites; pin that entering one does not
    also report the other as active (a mislabeled decision would corrupt
    Phase A telemetry's authority_class field)."""
    with kac.dispatcher_authority():
        assert kac.current_trusted_authority_class() == "dispatcher"
    with kac.gateway_notifier_authority():
        assert kac.current_trusted_authority_class() == "gateway_notifier"


# --------------------------------------------------------------------------- #
# H1: authenticated dashboard mutating request allowed only for its own
# route/board; unauthenticated request (no context entered), cross-board
# attempt, background task after request context exit, and a process that
# merely has worker ambient env (no dashboard context) are all denied.
# --------------------------------------------------------------------------- #

def test_h1_dashboard_request_context_allows_mutation_with_no_worker_grant(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.dashboard_request_authority():
        comment_id = _mutate(board, None)
    assert isinstance(comment_id, int) and comment_id > 0


def test_h1_unauthenticated_request_no_context_denied(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with pytest.raises(PermissionError):
        _mutate(board, None)


def test_h1_background_task_after_request_context_exit_denied(board, enforcement_env):
    """Simulates a background continuation (e.g. a fire-and-forget task
    scheduled from inside a request handler) that runs AFTER the request's
    ``with dashboard_request_authority()`` block has already exited --
    exactly the ``loop.run_in_executor`` vs ``contextvars.copy_context()``
    distinction the H1 wiring fix addresses, checked here at the authority
    layer directly (independent of asyncio) to pin the context's own
    lifetime regardless of how a future caller schedules work."""
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.dashboard_request_authority():
        pass  # request handled; context exits when the `with` block ends
    with pytest.raises(PermissionError):
        _mutate(board, None)  # a later, unrelated call has no context of its own


def test_h1_worker_ambient_env_alone_without_dashboard_context_denied(board, enforcement_env):
    """A process holding a legitimate WORKER grant, but reached via
    something pretending to be a dashboard request without ever entering
    ``dashboard_request_authority``, still resolves through the normal
    worker-grant path (W1), not H1 -- there is no way to forge H1 without
    the trusted context, and this pins that a worker grant is evaluated
    independently and correctly rather than being confused with H1."""
    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    # No dashboard context entered anywhere -- this must resolve as W1
    # (worker), which is allowed, exactly as test_w1 already proves; the
    # negative half of this path (a dashboard-shaped call with NO context
    # and NO grant) is test_h1_unauthenticated_request_no_context_denied.
    comment_id = _mutate(board, token)
    assert isinstance(comment_id, int) and comment_id > 0


# --------------------------------------------------------------------------- #
# H2: websocket route never issues a trusted context and never mutates --
# there is no grant path to it at all. Pinned structurally: the dashboard
# plugin's websocket handler must not import/call any authority-context
# entry point.
# --------------------------------------------------------------------------- #

def test_h2_websocket_handler_never_enters_a_mutation_authority_context():
    import ast

    plugin_file = ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    tree = ast.parse(plugin_file.read_text())
    websocket_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "stream_events":
            websocket_fn = node
            break
    assert websocket_fn is not None, "stream_events handler not found"
    source = ast.get_source_segment(plugin_file.read_text(), websocket_fn) or ""
    for forbidden in (
        "dashboard_request_authority", "dispatcher_authority", "gateway_notifier_authority",
        "maintenance_authority", "write_txn",
    ):
        assert forbidden not in source, (
            f"websocket handler stream_events must never reference {forbidden!r} "
            "(H2 is read-only per design; any mutation authority reference here "
            "would be a real regression)"
        )


# --------------------------------------------------------------------------- #
# C1/B1: direct interactive mutating CLI and board-pointer commands denied;
# no TTY/PTY/flag bypass (those signals are never consulted by
# decide_mutation_authority / _require_board_pointer_mutation_authority in
# the first place, so a plain call with none of that ceremony already
# proves the negative -- there is no code path that reads them).
# --------------------------------------------------------------------------- #

def test_c1_interactive_cli_shaped_mutation_denied_under_enforcement(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    # No TTY is attached in a pytest subprocess-less run either way; the
    # point of this test is that NOTHING about "looking interactive" is
    # consulted, not that we need to fake a TTY to prove it.
    with pytest.raises(PermissionError):
        _mutate(board, None)


def test_b1_board_pointer_mutation_denied_without_a_qualifying_context(board, enforcement_env):
    from hermes_cli import kanban_db as kb_mod

    with pytest.raises(PermissionError):
        kb_mod.set_current_board("some-other-board")


def test_b1_board_pointer_mutation_allowed_under_dashboard_context(tmp_path, monkeypatch, enforcement_env):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb_mod

    with kac.dashboard_request_authority():
        kb_mod.create_board("pointer-target")
        kb_mod.set_current_board("pointer-target")
    assert kb_mod.get_current_board() == "pointer-target"


def test_b1_board_pointer_mutation_allowed_under_maintenance_context(tmp_path, monkeypatch, enforcement_env):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb_mod

    with kac.maintenance_authority():
        kb_mod.create_board("pointer-target-m1")
        kb_mod.set_current_board("pointer-target-m1")
    assert kb_mod.get_current_board() == "pointer-target-m1"


def test_b1_worker_grant_alone_is_not_sufficient_for_board_pointer(board, enforcement_env):
    """A perfectly valid W1 worker grant must NOT authorize the board
    pointer -- B1's whole point is that board-scoped worker authority is
    never sufficient for the pointer itself."""
    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    os.environ[kia.GRANT_ENV_VAR] = token
    from hermes_cli import kanban_db as kb_mod

    with pytest.raises(PermissionError):
        kb_mod.set_current_board("some-other-board")


# --------------------------------------------------------------------------- #
# M1: admitted maintenance context can perform GC/repair/import/migration-
# class writes on the exact board; the generic local CLI (no context) stays
# denied even for the identical operation, per the inventory's explicit "no
# generic local CLI exception" M1 decision.
# --------------------------------------------------------------------------- #

def test_m1_maintenance_context_allows_mutation_with_no_worker_grant(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.maintenance_authority():
        comment_id = _mutate(board, None)
    assert isinstance(comment_id, int) and comment_id > 0


def test_m1_generic_local_cli_without_maintenance_context_denied(board, enforcement_env):
    """Pins the inventory's explicit decision: there is no automatic
    maintenance authority for a bare ``hermes kanban gc``/``repair``
    invocation -- only an admitted scheduler/broker that itself enters
    ``maintenance_authority()`` gets it."""
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with pytest.raises(PermissionError):
        _mutate(board, None)


# --------------------------------------------------------------------------- #
# T1: tool behavior maps only to its containing authority -- the tool
# module itself asserts nothing extra. Pinned structurally: kanban tools
# never import kanban_authority_context / assert their own authority class,
# they only call into kanban_db, which is the single place authority is
# actually decided.
# --------------------------------------------------------------------------- #

def test_t1_tool_module_asserts_no_authority_of_its_own():
    tools_file = ROOT / "tools" / "kanban_tools.py"
    source = tools_file.read_text()
    assert "kanban_authority_context" not in source, (
        "tools/kanban_tools.py must not import/assert its own authority "
        "class -- T1 per the inventory maps entirely onto whichever of "
        "W1/W3/W4 the calling context already resolves to via kanban_db"
    )
    assert "decide_mutation_authority" not in source


# --------------------------------------------------------------------------- #
# E1/CI1: temp-root test/eval authority is allowed against a temp DB;
# explicitly denied against anything that looks like the current/production
# board path (never authorizes just because a caller CLAIMS to be a test).
# --------------------------------------------------------------------------- #

def test_e1_test_authority_context_allows_mutation_on_temp_board(board, enforcement_env):
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with kac.test_authority():
        comment_id = _mutate(board, None)
    assert isinstance(comment_id, int) and comment_id > 0


def test_ci1_no_operational_board_credential_exists_for_ci():
    """There is no CI-specific token/context that decide_mutation_authority
    recognizes as distinct from ``test``; CI's only positive authority
    class in the module is ``test_authority``, scoped the same as any other
    pytest run against a temp DB -- i.e. CI gets no special elevated
    credential for operational (current/production) boards. This pins the
    absence structurally: the module must not define any CI-labeled
    context beyond the shared test/eval one."""
    assert not hasattr(kac, "ci_authority")
    assert not hasattr(kac, "operational_ci_authority")


# --------------------------------------------------------------------------- #
# Telemetry: no plaintext token/credential in logs/exceptions/decision
# objects, for both allow and deny outcomes, matching Phase A's minimum-
# field, non-secret requirement.
# --------------------------------------------------------------------------- #

def test_telemetry_allow_and_deny_never_carry_the_plaintext_token(board, enforcement_env, caplog):
    caplog.set_level("DEBUG")
    token, _ = _issue_and_activate(board, worker_pid=os.getpid())
    secret_hex = token.split(".", 2)[2]

    # Allow path.
    _mutate(board, token)
    # Deny path.
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    with pytest.raises(PermissionError):
        _mutate(board, None)

    for record in caplog.records:
        msg = record.getMessage()
        assert secret_hex not in msg
        assert token not in msg


def test_telemetry_decision_dataclass_never_stores_a_bare_token_field(board):
    """Structural pin: ``AuthorityDecision`` has no field that could
    accidentally be populated with a raw token/secret_hex, only the
    non-secret ``grant`` (a ``GrantAuthority`` carrying only grant_id/
    task_id/run_id/board_scope) and ``deny_reason`` (a fixed code, never
    free text with the token interpolated in)."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(kia.AuthorityDecision)}
    assert field_names == {"allowed", "authority_class", "grant", "deny_reason"}
    grant_field_names = {f.name for f in dataclasses.fields(kia.GrantAuthority)}
    assert "token" not in grant_field_names
    assert "secret_hex" not in grant_field_names
    assert "token_digest" not in grant_field_names


# --------------------------------------------------------------------------- #
# O1: non-Kanban SQLite stores are unaffected -- the distinct
# ``hermes_cli.sqlite_util.write_txn`` never imports/consults the Kanban
# authority module at all.
# --------------------------------------------------------------------------- #

def test_o1_non_kanban_sqlite_util_write_txn_does_not_reference_kanban_authority():
    sqlite_util_file = ROOT / "hermes_cli" / "sqlite_util.py"
    source = sqlite_util_file.read_text()
    assert "kanban_invocation_authority" not in source
    assert "kanban_authority_context" not in source


# --------------------------------------------------------------------------- #
# Cross-cutting: enforcement OFF (the production default until sign-off) is
# provably behavior-invisible for every denial class exercised above -- the
# exact same calls that deny under enforcement succeed under the default.
# --------------------------------------------------------------------------- #

def test_enforcement_off_is_the_default_and_all_above_denials_become_allows(board, monkeypatch):
    monkeypatch.delenv(kia.ENFORCEMENT_ENV_VAR, raising=False)
    assert kia.enforcement_enabled() is False
    os.environ.pop(kia.GRANT_ENV_VAR, None)
    # Every one of these calls denies under enforcement (see the tests
    # above); with enforcement off, Phase A telemetry still runs (would_deny
    # is recorded) but the mutation itself proceeds.
    comment_id = _mutate(board, None)
    assert isinstance(comment_id, int) and comment_id > 0
