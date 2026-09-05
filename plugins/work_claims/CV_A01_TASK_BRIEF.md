# CV-A01: HERMES_KANBAN_TASK unrestricted-mutation defect

**Status: RESOLVED**, over three follow-up slices on top of the premature-
finalization lifecycle fix and the distribution-provenance fix this brief
originally deferred behind. Stage 1 built the process-bound worker identity
(`agent/dispatcher_identity.py`); Stage 2A made that identity the plugin's
only source of dispatcher authority and added OS containment for
`terminal`; Stage 2B moved the file boundary out of the pre-tool check and
into the write itself (`agent/confined_file_ops.py` wired into the real
`write_file`/`patch` implementations). See `README.md`'s "CV-A01:
dispatcher scope (resolved)" section and `PROVENANCE.md`'s "CV-A01 Stage
2A"/"Stage 2B" sections for the shipped fix, and
`tests/plugins/test_work_claims_cv_a01_dispatcher_scope.py`,
`tests/plugins/test_work_claims_terminal_containment.py` plus
`tests/plugins/test_work_claims_file_confinement.py` for its
RED-was-the-bug-described-below -> GREEN test matrix. This file is kept as
the original defect record; nothing below describes current behavior.

The "proposed fix shape" below is superseded in one important respect: it
assumed the calling session's Kanban task could be resolved from
`HERMES_KANBAN_TASK`. It cannot — an environment variable carries no
authority across a process boundary, which is why Stage 1 exists at all.

---

**Original status (superseded):** out of scope for the premature-
finalization lifecycle fix in this candidate. Documented here only, per
explicit instruction not to implement it in this patch and never to add a
test that accepts it as correct behavior. No such test existed anywhere in
that candidate's suite.

## Defect

`core.mutation_allowed()`:

```python
def mutation_allowed(session_id: str, tool_name: str, args: dict[str, Any]) -> tuple[bool, str | None]:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True, None
    ...
```

Any session with `HERMES_KANBAN_TASK` set in its environment bypasses **every**
downstream check in this function — not just the "you need a claim" check,
but also the workspace-scoping enforcement further down (the `path_within`
checks that confine `write_file`/`patch`/`terminal` mutations to the claimed
workspace). A Kanban worker session can therefore mutate any path or run any
terminal command outside its assigned task's workspace, with no claim and no
scoping, as long as the env var is set. The plugin's own README documents the
env var's *intended* scope narrowly ("Kanban workers are already atomically
claimed and do not need a second claim") — the code does not enforce that
narrower intent; it grants a blanket bypass.

## Why it's not fixed in this candidate

1. It is causally unrelated to the premature-finalization defect: it's a
   missing-scope authorization bug in `mutation_allowed`, not a
   session-lifecycle/finalize-timing bug in `release_all_for_session`.
2. A correct fix requires knowing how a Kanban task's *own* workspace/target
   scope is recorded and looked up (so the bypass can be narrowed to "skip
   the claim requirement, but still enforce this task's own workspace scope"
   rather than "skip everything"). That lookup — where a kanban task's
   assigned workspace/targets live and how a worker session's `task_id`
   resolves to them — was not researched as part of this engagement, and
   guessing at it risks either a fail-open scoping bug or breaking legitimate
   kanban worker flows.
3. The user's instruction was explicit: implement it here **only if cleanly
   separable into its own commit and test set**; a fix authored without the
   above research would not be a clean, well-understood, independently
   testable change — it would be a guess. A brief is the honest deliverable.

## Proposed fix shape (for the follow-up task, not implemented here)

- Resolve the calling session's Kanban task record (via `task_id`/`session_id`
  — same resolution `_kanban_create`/`_kanban_complete` already use) and read
  its assigned workspace/targets.
- Replace the unconditional `return True, None` with: skip only the
  *claim-existence* requirement, then continue into the same `workspace`/
  `path_within` scoping checks already applied to claim-holding sessions,
  using the *kanban task's* workspace instead of a claim's.
- New RED test: a `HERMES_KANBAN_TASK` session attempts a `write_file` outside
  its assigned kanban task's workspace and must be blocked (currently
  `allowed=True` — this is the RED case to reproduce first); a mutation
  inside its assigned workspace must still be allowed.
- Regression: existing kanban-worker flows that mutate inside their assigned
  workspace must remain unaffected (no new false positives).

## Acceptance criteria for the follow-up

- A kanban-worker session can no longer mutate paths outside its assigned
  task's workspace merely by having `HERMES_KANBAN_TASK` set.
- No regression to legitimate in-scope kanban worker mutations.
- Ships as its own commit, with its own RED→GREEN test set, independent of
  the lifecycle-fix commit in this candidate.
