"""Cross-session HERMES_SESSION_ID leak via the shared bash snapshot.

Regression coverage for the bug where a single long-lived backend serves many
sessions through ONE ``_active_environments["default"]`` LocalEnvironment (the
messaging gateway, TUI, and desktop/web dashboard all collapse the terminal to
"default"). That environment persists a bash *session snapshot* file and
``source``s it before every command. ``export -p`` dumped the FIRST session's
``HERMES_SESSION_ID`` into the snapshot, so every LATER session ``source``d that
stale value and its ``echo $HERMES_SESSION_ID`` reported a FOREIGN session's id
— overriding the correct per-command Popen env injected by
``_inject_session_context_env``.

The fix strips the per-session bridged vars (HERMES_SESSION_* / UI /
CRON_AUTO_DELIVER_) from the snapshot at both dump sites in
``tools/environments/base.py``; they are re-injected fresh on every command.
"""

import os
import re
import sys

import pytest

from tools.environments.base_session_env import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)


# ---------------------------------------------------------------------------
# Unit: the exclusion regex matches exactly the bridged vars, nothing else.
# ---------------------------------------------------------------------------

def test_regex_matches_bridged_session_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    # Every var the gateway bridges must be excluded.
    from gateway.session_context import _VAR_MAP

    for name in _VAR_MAP:
        line = f'declare -x {name}="whatever"'
        assert rx.search(line), f"{name} should be excluded from the snapshot"


def test_export_snippet_shape():
    snippet = _export_dump_excluding_session_vars('"$__hermes_snap_tmp"')
    assert "export -p" in snippet
    # Unset-by-name (not line-grep): multi-line declare values must not leave
    # continuation lines in the snapshot (issue #71296).
    assert "unset" in snippet
    assert "${!HERMES_SESSION_*}" in snippet
    assert "${!HERMES_CRON_AUTO_DELIVER_*}" in snippet
    assert "${!HERMES_BROWSER_CONTROL_*}" in snippet
    assert "HERMES_UI_SESSION_ID" in snippet
    assert "grep -vE" not in snippet
    assert '"$__hermes_snap_tmp"' in snippet
    # The redirection must be attached to a brace group wrapping the dump,
    # NOT to a pipeline segment: a redirect on a pipeline segment expands the
    # temp-path variable inside that segment's subshell (potentially
    # inconsistently with the parent that expands the follow-up ``mv``
    # operand), silently orphaning the dump and breaking snapshot env
    # persistence entirely.
    assert snippet.lstrip().startswith("{ ")
    assert "|| true; }" in snippet
    assert snippet.rstrip().endswith('> "$__hermes_snap_tmp"')


def test_regex_matches_delegated_child_kanban_identity_and_enforcement_vars():
    """Exact-name contract for the t_594fe921 + t_99ee91ca exclusions: every one of
    DELEGATED_CHILD_ENV_MARKER / KANBAN_ENV_KEYS / ENFORCEMENT_ENV_VAR must match the regex, and
    a name that merely shares a prefix (not an exact match) must NOT -- this is a positive
    allowlist of exact identity/policy vars, not a blanket HERMES_KANBAN_* strip (HERMES_KANBAN_
    BOARD/DB are board/location, not identity, and must keep surviving in the snapshot)."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, KANBAN_ENV_KEYS
    from hermes_cli.kanban_invocation_authority import ENFORCEMENT_ENV_VAR

    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    for name in (DELEGATED_CHILD_ENV_MARKER, ENFORCEMENT_ENV_VAR, *KANBAN_ENV_KEYS):
        line = f'declare -x {name}="whatever"'
        assert rx.search(line), f"{name} should be excluded from the snapshot"

    for untouched in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB"):
        line = f'declare -x {untouched}="whatever"'
        assert not rx.search(line), (
            f"{untouched} identifies board/location, not identity/policy, and must "
            f"survive in the snapshot -- a blanket HERMES_KANBAN_* prefix must not be used"
        )


# ---------------------------------------------------------------------------
# Integration: real LocalEnvironment, two sessions, no cross-contamination.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_no_cross_session_leak(tmp_path):
    import threading

    from gateway.session_context import _VAR_MAP, _UNSET, set_session_vars
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        def run_as(sid):
            out = {}

            def worker():
                for v in _VAR_MAP.values():
                    v.set(_UNSET)
                set_session_vars(session_key="k" + sid, session_id=sid, source="desktop")
                out["r"] = env.execute('echo "[$HERMES_SESSION_ID]"')

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            return out["r"].get("output", "")

        out_a = run_as("SIDAAA")
        out_b = run_as("SIDBBB")

        assert "SIDAAA" in out_a, f"session A saw {out_a!r}"
        # The core assertion: B must see its OWN id, not A's leaked via snapshot.
        assert "SIDBBB" in out_b, f"session B saw {out_b!r}"
        assert "SIDAAA" not in out_b, f"session B leaked A's id: {out_b!r}"

        # And the snapshot file must not carry the session id at all.
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                assert "HERMES_SESSION_ID" not in f.read()
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# Regression: HERMES_DELEGATED_CHILD_CONTEXT / KANBAN_ENV_KEYS leak via the
# shared bash snapshot (t_594fe921, design review t_5f704f40).
#
# Root cause: identical shape to the HERMES_SESSION_ID leak above, but for the
# delegate_task child-process marker and the 5 kanban worker-identity vars. A
# delegate_task child's real subprocess env carries HERMES_DELEGATED_CHILD_CONTEXT=1
# (injected by agent.delegation_context.delegated_child_subprocess_env once the
# ContextVar set by ``delegated_child_context()`` is observed). If a command run
# inside that scope is the one that (re-)dumps the shared snapshot, every LATER
# genuinely-top-level command sharing the same cached bash session would source
# that stale marker and be wrongly classified as a delegated child (e.g. blocking
# Kanban writes) — this was confirmed live: 34/81 hermes-snap-*.sh files on the
# operating host carried the stale marker before this fix.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_no_delegated_child_or_kanban_identity_leak(tmp_path):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, KANBAN_ENV_KEYS, delegated_child_context
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        # First command runs INSIDE a delegate_task child context, exactly as a
        # delegate_task() call would execute its child's tool commands. The
        # command's own subprocess env picks up the marker (and has the kanban
        # identity vars scrubbed) via delegated_child_subprocess_env; the snapshot
        # re-dump at the end of this command must not persist the marker.
        with delegated_child_context():
            child_result = env.execute(
                'echo "child_marker=${HERMES_DELEGATED_CHILD_CONTEXT:-NOT_SET}"'
            )
        child_output = child_result.get("output", "")
        # The marker's value is the fenced kanban board ROOT (agent.delegation_context.
        # scrub_kanban_env), not a literal "1" -- a later fix (t_11e8c077) generalized it from a
        # bare flag to a path so descendant fencing can be scoped to the lineage's own board. The
        # sanity check only needs "the child saw *some* marker value", not its exact shape.
        assert "child_marker=NOT_SET" not in child_output and "child_marker=" in child_output, (
            f"sanity check failed: delegated_child_context() should make the child "
            f"command's own subprocess see the marker: {child_result!r}"
        )

        # Second command is genuinely top-level (scope has exited, ContextVar reset)
        # on the SAME session/snapshot file. It must see the marker and every kanban
        # identity var as unset, not the delegated child's leaked values.
        checks = " ".join(
            f'echo "{name}=${{{name}:-NOT_SET}}"' for name in (DELEGATED_CHILD_ENV_MARKER, *KANBAN_ENV_KEYS)
        )
        top_level_result = env.execute(checks)
        output = top_level_result.get("output", "")
        for name in (DELEGATED_CHILD_ENV_MARKER, *KANBAN_ENV_KEYS):
            assert f"{name}=NOT_SET" in output, (
                f"snapshot leaked {name} into a genuinely top-level command: {output!r}"
            )

        # And the on-disk snapshot file itself must not carry any of these names.
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                contents = f.read()
            for name in (DELEGATED_CHILD_ENV_MARKER, *KANBAN_ENV_KEYS):
                assert name not in contents, (
                    f"snapshot file {snap!r} still contains {name!r}: leaks into every "
                    f"later top-level command sharing this cached bash session"
                )
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# Regression: HERMES_KANBAN_INVOCATION_AUTHORITY_ENFORCE leak via the shared bash snapshot
# (t_99ee91ca, same leak class as t_594fe921 above but for a var that did not exist yet when that
# fix shipped).
#
# Root cause: identical shape to the leak above, but for the Phase B kanban invocation-authority
# global enforcement flag (hermes_cli.kanban_invocation_authority.ENFORCEMENT_ENV_VAR). Unlike the
# per-worker identity vars above, this flag is a global POLICY setting meant to be uniformly set (or
# not) via every profile's ~/.hermes/.env -- but if a command that happens to run with the var set
# in its own environment is the one that (re-)dumps the shared snapshot, every LATER command sharing
# that cached session sources that stale value regardless of the var's actual current .env-driven
# state. Hit live (Rook's own persistent session, t_255d78c0 comment @ 2026-09-16 07:57): after the
# var was rolled into every profile .env as part of closing the C1 interactive-CLI gap, the session's
# snapshot picked it up via the standard export -p re-dump and began re-exporting it into every
# subsequent tool-call subprocess, incorrectly denying ordinary kanban administration.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_no_invocation_authority_enforce_leak(tmp_path):
    from hermes_cli.kanban_invocation_authority import ENFORCEMENT_ENV_VAR
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        # First command exports the enforcement flag in ITS OWN bash session (simulating a caller
        # that explicitly set/tested it, or a stale ambient value inherited from an outer process)
        # -- not via the snapshot. The snapshot re-dump at the END of this command is the actual
        # leak surface under test: it must not persist the export the command just made.
        set_result = env.execute(
            f'export {ENFORCEMENT_ENV_VAR}=1; echo "enforce_marker=${{{ENFORCEMENT_ENV_VAR}:-NOT_SET}}"'
        )
        assert "enforce_marker=1" in set_result.get("output", ""), (
            f"sanity check failed: the command's own shell should see the var it just "
            f"exported: {set_result!r}"
        )

        # Second command is genuinely top-level on the SAME session/snapshot file, with no var set
        # of its own. It must see the flag as unset, not the first command's leaked value -- a leak
        # here would silently flip Kanban mutation enforcement on or off for every later command.
        top_level_result = env.execute(f'echo "enforce_marker=${{{ENFORCEMENT_ENV_VAR}:-NOT_SET}}"')
        output = top_level_result.get("output", "")
        assert "enforce_marker=NOT_SET" in output, (
            f"snapshot leaked {ENFORCEMENT_ENV_VAR} into a genuinely top-level command: {output!r}"
        )

        # And the on-disk snapshot file itself must not carry the name.
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                contents = f.read()
            assert ENFORCEMENT_ENV_VAR not in contents, (
                f"snapshot file {snap!r} still contains {ENFORCEMENT_ENV_VAR!r}: leaks into "
                f"every later top-level command sharing this cached bash session, silently "
                f"changing whether kanban mutations are enforced/denied for that session"
            )
    finally:
        env.cleanup()
