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
        assert "child_marker=1" in child_result.get("output", ""), (
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
