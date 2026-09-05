# Provenance

## Version 1.6.0 candidate (2026-09-03)

This branch adds the previously external canonical work-claims source and its
transactional installer to the Hermes repository so the dispatcher-capability
change can be reviewed and promoted as one versioned unit. The candidate was
built in isolated branch
`claim/fix-bounded-dispatcher-capability-ad-fc0f5084` from Hermes commit
`63279301bcbdc185c1b07b98a9312eb0c862f26d`.

The active production source was copied byte-for-byte before the 1.6.0 edits.
The candidate admits read-only `tool_search`/`tool_describe` and only a
host-enforced synchronous, maximum-two, three-iteration, empty-tool-surface
`delegate_task` mode for genuine dispatcher workers. It is not promoted by
this commit; production installation, profile admission, restart, and the real
Daniel dispatch canary remain gated on independent security review.

The historical provenance below is retained as the lineage of the 1.5.0
source copied into this candidate. Statements there saying “this copy” was not
live describe those earlier review slices, not the status of the later 1.5.0
production deployment.

## Historical 1.5.0 lineage

This directory is a **review candidate**, built and tested entirely inside the
isolated git worktree/branch `claim/attempt-3-correct-premature-session--a2afbf70`
of the `hermes-agent` repository. It is not wired into that repository's
plugin loader or any CI path, and it was never copied to, or executed
against, any live Hermes profile. The live orchestrator plugin at
`~/.hermes/plugins/work_claims/` (canonical source, distributed by
`~/.hermes/scripts/install_work_claims.py` to the `rook`, `hannah`, `clara`,
`daniel`, `maya`, `oliver`, `sophie` profiles — a roster that omits `elias`,
which enables the plugin; see *Correction generation 4*) was **not
modified**.

## Baseline hashes (captured 2026-09-02, before any edit in this candidate)

Live source: `/Users/rook/.hermes/plugins/work_claims/`

| file | sha256 | live mtime |
|---|---|---|
| `plugin.yaml`  | `2406b0d4798a72946f704b929e9d18975a14b59832d926f54a3f2fab34b72e08` | 2026-08-26 11:46:23 |
| `__init__.py`  | `d2a8312154e551789c374246403ab957b152913fbb0022d8a88a4703d744dd3e` | 2026-08-26 12:21:02 |
| `core.py`      | `71df769f5e9393ac2634e3d30b13f8ac54235c37b218f4429ef8cd3ddb4662cc` | 2026-08-26 12:13:37 |
| `README.md`    | `bc7a5a05d3224a39c72916495572674e8e2f047f5a726e33a78a9c5e1c58a9da` | 2026-08-26 11:48:25 |
| `test_work_claims.py` | `3c97d322bf3ee99be67799ca11df79e485a8ee690f5efb0f44e47e3c23f9b146` | 2026-08-26 12:21:02 |

These five files were `cp`'d byte-for-byte from the live source into this
candidate directory first, and the copy was hash-verified equal to the table
above before any fix code was written (the git history of this branch
therefore starts from an exact live baseline). All fix work happened only on
top of that verified baseline, in this worktree.

## Incident evidence

Root-caused in
`/Users/rook/.hermes/cache/delegation/subagent-summary-0-20260902_174240_738444.txt`
(verdict: `PROVEN`). Summary: a stale `ws_orphan_reap` timer for session
`20260902_171806_a57d9d` fired `on_session_finalize` at 17:24:26 BST while an
in-process detached turn (WS 58980, `Thread-15`) was still running; the claim
was released 438s before the session actually ended (17:31:43 BST). The
host's own preserve-guard (`tui_gateway/server.py::_other_runtime_lease_guard`,
backed by `hermes_cli/active_sessions.py::active_session_liveness_guard`) only
tracks WS-connection-scoped, PID-liveness leases — a same-process detached
turn that has outlived its WS handle is invisible to it, so the plugin's
`_on_session_finalize` released the claim unconditionally.

## TDD provenance: RED before GREEN

`test_lifecycle_finalize.py` was written against the untouched baseline
(hashes above) before any line of `core.py`/`__init__.py` was changed. The
first run, `PYTHONPATH=plugins python3 -m unittest -v work_claims.test_lifecycle_finalize`,
against that baseline:

```
Ran 13 tests in 0.081s
FAILED (errors=11)
```

The primary incident-reproduction test,
`PrematureFinalizeRedTest.test_stale_orphan_reap_does_not_release_claim_during_active_detached_turn`,
failed at `core.release_all_for_session(session_id, ..., reason="ws_orphan_reap")`
with `TypeError: release_all_for_session() got an unexpected keyword argument
'reason'` — i.e. the baseline had no concept of a `reason` or of a live turn
at all, so it could not do anything other than release unconditionally
(the exact proven defect). The remaining ten regression-matrix tests in the
same file errored identically or on `AttributeError: module 'work_claims.core'
has no attribute 'begin_turn'`, since none of the new liveness API existed
yet. Only `test_target_normalization`-style tests unrelated to the new API
were unaffected.

After implementing `begin_turn`/`end_turn`/`session_has_live_turn`/
`pre_finalize_audit`/`_defer_finalize` and the `reason=`-aware
`release_all_for_session` in `core.py`, plus the `pre_llm_call`/
`post_llm_call` hook wiring in `__init__.py`, the same file plus the full
suite (`test_work_claims.py`, `test_lifecycle_finalize.py`,
`test_installer_distribution.py`, `test_hook_wiring.py`) is GREEN: **26
passed, 0 failed** under `hermes-agent/venv/bin/python3.11` — see the test
run log referenced in the final delivery report for this candidate.

## What changed vs. the live baseline

See `git log` on this branch for the exact commit(s). In short: `core.py`
gained an `active_turns` / `deferred_finalizes` liveness ledger and
`begin_turn` / `end_turn` / `session_has_live_turn` / `pre_finalize_audit`;
`release_all_for_session` now defers (never skips or leaks) a release while a
live turn is registered; `__init__.py` wires `pre_llm_call` / `post_llm_call`
to `begin_turn` / `end_turn`. Full design rationale: `RECOVERY.md` in this
directory.

## Follow-up correction: host execution-turn lease (this slice)

An independent review of the commit above (`lease-review-block-87ed3514`)
found that liveness was bound to the hook-dispatcher's short-lived
`hermes-hook-*` callback thread, not the turn itself — so `begin_turn`'s
record could be pruned by `session_has_live_turn` the instant the
`pre_llm_call` callback returned, before the turn it described had actually
finished (finding 1, CRITICAL), plus five further gaps: `deferred_finalizes`
keyed only by `session_id` (finding 2), `post_llm_call`'s
`final_response`-and-`not interrupted` gate silently skipping interrupted/
failed/empty/tool-only turns (finding 3), a non-atomic multi-transaction
`release_all_for_session` (finding 4), PID-only foreign liveness with no
holder identity or renewable expiry (finding 5), and an audit trail that
only existed when a claim did (finding 6).

This slice lands on top of the accepted host-side fix (commit
`0b6bd24df3a46f3c61c6083e62b8d8e07d9d834c`, `agent/execution_turn.py` +
its three call sites in `run_agent.py`) and rewrites `core.py`/`__init__.py`
to consume that host boundary instead of `pre_llm_call`/`post_llm_call`:
`active_turns` is replaced by a renewable, holder-token-owned
`execution_leases` table; `deferred_finalizes` is re-keyed to `claim_id`;
every finalize decision (observe → audit → defer-or-release → claim CAS) is
one `BEGIN IMMEDIATE` transaction; a new `finalize_audit` table records every
attempt, including "no active claim". Full design: `RECOVERY.md`. Findings 7
(installer) and 9 (CV-A01) are explicitly out of scope for this slice, as
they were for the one before it.

Superseded in-tree tests `test_lifecycle_finalize.py` and
`test_hook_wiring.py` (they exercised the removed `begin_turn`/`end_turn`/
`pre_llm_call`/`post_llm_call` API) were deleted; their coverage, extended
with the concurrency/ownership/crash-recovery cases the review also asked
for, now lives in `tests/plugins/test_work_claims_lifecycle.py`, driven
through a real, discovery-loaded `PluginManager` rather than direct
`core.py` calls. `test_work_claims.py`'s `@patch("work_claims.core...")`
decorators were also fixed to `@patch("plugins.work_claims.core...")` —
finding 8's pytest import-identity bug (pytest imports this package as
`plugins.work_claims`, not `work_claims`).

## Follow-up correction: finding 7, distribution provenance (this slice)

Finding 7 of `lease-review-block-87ed3514` held that `installer.py` (the
candidate's deterministic mirror of the live
`~/.hermes/scripts/install_work_claims.py`) was not tied to the production
installer by any pinned hash, deleted each destination before validating
anything, copied files directly into the live destination with no staging or
atomicity, had no multi-profile rollback, trusted only a dynamic file-set
equality check with no committed/approved manifest, distributed only four
files with no immutable manifest of its own, and left `plugin.yaml`'s
version unchanged. It explicitly scoped this out of the prior slice ("Do not
implement CV-A01 ... it follows as a separate commit" applied by extension
to finding 7 as well, per the task brief for this slice).

This slice replaces `installer.py` with a fail-closed distribution pipeline
and adds a committed `MANIFEST.json` covering the complete plugin source set
(`plugin.yaml`, `__init__.py`, `core.py`, `README.md`, `test_work_claims.py`).
`plugin.yaml` is bumped `1.1.0` -> `1.2.0` for this slice. Full design:
`README.md`'s "Distribution" section and the `installer.py` module
docstring. **No file under `~/.hermes/` (live plugin, live installer, live
profiles) was read for anything other than a one-time, read-only sha256 of
`~/.hermes/scripts/install_work_claims.py`, and no live file was written.**

### Pinned provenance hashes (captured 2026-09-02, this candidate commit)

| artifact | sha256 |
|---|---|
| `~/.hermes/scripts/install_work_claims.py` (production installer, read-only input) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |
| `plugins/work_claims/MANIFEST.json` (this candidate's committed, approved manifest) | `e64335a7c5c9714d5d9c1cfb3b872eb94e53b906db12ad881f33cd778bfb2fdb` |

Both hashes are pinned as constants in `installer.py`
(`PRODUCTION_INSTALLER_SHA256`, `APPROVED_MANIFEST_SHA256`) and re-verified
by `test_installer_distribution.py::ProvenancePinTest` against the real
production installer file and the real committed manifest file, so either
drifting silently fails the test suite rather than being silently trusted.

### TDD provenance: installer tests written against the new API, RED->GREEN

`test_installer_distribution.py` was rewritten end-to-end against the new
`installer.py` module (`ProvenanceError`, `InstallTransactionError`,
`load_manifest`, `verify_source_complete`, `distribute`, `recover`) that did
not exist under the old four-argument `FILES`-tuple API; the previous test
file's assertions (e.g. `installer.FILES`) fail immediately against the new
module, and the new test file's assertions (e.g. `installer.ProvenanceError`,
`installer.recover`) failed immediately against the old module -- the two
were mutually RED until both were rewritten together. All 20 tests in
`plugins/work_claims/test_installer_distribution.py` are GREEN against the
final `installer.py`, alongside the full `plugins/work_claims` unit suite,
`tests/plugins/test_work_claims_lifecycle.py`, and the host execution-turn
boundary regressions (`tests/agent/test_execution_turn_lease.py`,
`tests/run_agent/test_cross_process_turn_lease.py`) -- see the final delivery
report for this slice for the exact combined count.

Finding 9 (CV-A01) remains explicitly out of scope for this slice, unchanged
from the prior one -- see `CV_A01_TASK_BRIEF.md`.

## CV-A01: dispatcher workspace authorization (this slice)

Closes finding 9 / `CV_A01_TASK_BRIEF.md`: `mutation_allowed()` granted an
unconditional bypass -- claim existence *and* the `path_within` workspace
scoping applied to claim holders -- to any session with `HERMES_KANBAN_TASK`
set. `plugin.yaml` is bumped `1.2.0` -> `1.3.0` for this slice.

The blanket bypass is replaced with a narrow, verified grant. A session is
treated as a genuine claimless dispatcher worker only when all of the
following hold, re-derived fresh on every `mutation_allowed()` call (nothing
here is cached across calls, so a swap between two calls is re-caught by the
next one, not trusted from a stale prior result):

1. `agent.delegation_context.is_dispatcher_owned_worker_context()` is true.
   `delegate_task` children and in-process cron jobs legitimately inherit
   the worker's `HERMES_KANBAN_*` env but are not the dispatcher's task
   owner; this predicate is false for both, and `mutation_allowed()` falls
   through to ordinary claim enforcement for them, exactly as it would for
   a non-Kanban session.
2. `HERMES_KANBAN_TASK` resolves, via `hermes_cli.kanban_db.connect_closing()`
   and `get_task()`, to an existing task whose `workspace_path` is an
   absolute, existing directory that passes a canonical ancestor walk (see
   below).
3. `HERMES_KANBAN_WORKSPACE` from the environment independently passes the
   same walk and matches the database's workspace by `(st_dev, st_ino)` --
   not by string/lexical equality, which a symlink or case-variant alias
   could spoof.

Only then is claimless mutation permitted, and only for `write_file`/`patch`
targets and `terminal` `workdir`s that pass `_confined_by_ancestor_walk()`:
a component-by-component walk from the verified workspace root (never
`Path.resolve()`, which silently normalizes both symlinks and, on a
case-insensitive-but-preserving filesystem like APFS, case) that rejects:

- a target that is itself a symlink, or any intermediate directory
  component that is a symlink (including one used to escape the workspace)
- a path component whose lexical case differs from the real on-disk
  directory entry, even when the filesystem would resolve it to the same
  file (APFS case alias)
- an existing regular file write target with `st_nlink > 1` (a hard link
  potentially writable/visible from outside the workspace)
- any component whose device (`st_dev`) differs from the workspace root's
  own -- a mount or bind-mount alias grafted onto a subdirectory
- a missing, malformed, or renamed ancestor (any non-final path component
  that does not exist)

Genuinely dispatcher-owned context with missing, malformed, or mismatched
Kanban metadata (no `HERMES_KANBAN_WORKSPACE`, nonexistent task, task with
no recorded workspace, env/DB workspace mismatch) is rejected outright with
a descriptive reason -- it is never silently treated as "not applicable"
and passed through to the generic no-claim message, so an operator can tell
a spoofed/broken dispatcher identity apart from an ordinary unclaimed
session.

Ordinary claim-holder enforcement (`path_within`, and the whole non-Kanban
branch of `mutation_allowed()`) is unchanged.

### TDD provenance: RED before GREEN

`tests/plugins/test_work_claims_cv_a01_dispatcher_scope.py` reproduces the
exact defect `CV_A01_TASK_BRIEF.md` recorded (`HERMES_KANBAN_TASK` set ->
`mutation_allowed()` returns `True` unconditionally, including for a
`write_file` target outside the task's own workspace) against the baseline
`core.py` before this slice's fix, then exercises the narrowed replacement:
in-workspace write/terminal allow, outside-workspace deny, an APFS
case-variant alias, a symlinked target file, a symlinked intermediate
directory used to escape the workspace, a hard-linked write target, an
env/DB workspace mismatch, missing/malformed Kanban metadata (missing
`HERMES_KANBAN_WORKSPACE`, nonexistent task, task with no recorded
workspace), a delegated-child and an in-process-cron context both
inheriting the worker's env without owning it, a TOCTOU ancestor swap
between two calls, and two real subprocess dispatcher workers sharing one
Kanban database but assigned to separate workspaces, each confined to its
own. All 16 cases are driven through a real, discovery-loaded
`hermes_cli.plugins.PluginManager`'s `pre_tool_call` hook, not a direct
`core.mutation_allowed()` call -- see the final delivery report for this
slice for the exact combined suite count run against the fixed `core.py`.

### Regenerated manifest (this slice)

`core.py` and `README.md` changed; `plugin.yaml`'s version changed
(`1.2.0` -> `1.3.0`). `MANIFEST.json`'s per-file hashes were regenerated for
the three changed files (`core.py`, `README.md`, `plugin.yaml`);
`__init__.py` and `test_work_claims.py` are unchanged from the prior slice
and keep their existing hashes. `installer.APPROVED_MANIFEST_SHA256` was
updated to match the regenerated `MANIFEST.json`'s own sha256.
`installer.PRODUCTION_INSTALLER_SHA256` is unchanged -- this slice never
read or touched `~/.hermes/scripts/install_work_claims.py` or any other
file under `~/.hermes/`.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/MANIFEST.json` (this slice's committed, approved manifest) | `ad2264148541cf3956bd18e577f5b7a74179aa8f9283077f552b4271d0cc77d2` |

Pinned as `installer.APPROVED_MANIFEST_SHA256` and re-verified by
`test_installer_distribution.py::ProvenancePinTest` against the real
committed manifest file, exactly as the prior slice's pin was.

## Follow-up correction: pytest import-identity patch targets (this slice)

`test_work_claims.py`'s `@patch(...)` decorators were fixed to target
`plugins.work_claims.core...` (matching finding 8's pytest import-identity
fix already applied elsewhere in this candidate), not `work_claims.core...`.
Only `test_work_claims.py` changed; `MANIFEST.json`'s hash for that file was
regenerated and `installer.APPROVED_MANIFEST_SHA256` was updated to match
the regenerated `MANIFEST.json`'s own sha256.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/test_work_claims.py` | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/MANIFEST.json` (this slice's committed, approved manifest) | `b2b3ecfae518dfbeee033c1f5f8f0d91c1bdb7403e79aeb6896e14725eee941b` |

Pinned as `installer.APPROVED_MANIFEST_SHA256` and re-verified by
`test_installer_distribution.py::ProvenancePinTest` against the real
committed manifest file.

## Follow-up correction: lifecycle slice B2 (this slice)

Closes findings 5 and 6 of `work-claims-review-block-e37cd649` (finding 4's
fail-closed acknowledged begin/end contract was landed by the two commits
this slice sits on: `46dfd5646` and `980ce9093`, verified present at the
clean HEAD this slice started from). `plugin.yaml` is bumped `1.3.0` ->
`1.4.0`.

**Finding 5 — foreign begin could take over a lease.**
`on_execution_turn_begin` used `INSERT OR REPLACE` keyed on `lease_id` alone,
so anything replaying a live lease id silently became its owner and left the
real owner unable to renew or end its own turn. It is now a create-only
compare-and-insert/update inside one explicit `BEGIN IMMEDIATE`: the stored
row is compared against the full incoming holder identity
(`lease_id`, `session_id`, `turn_id`, `holder_token`, `pid`, `boot_id`) and
the call either inserts, idempotently refreshes for the exact same identity,
or raises `LeaseIdentityConflict` and fails the required hook closed.
`on_execution_turn_renew` and `on_execution_turn_end` validate that same full
identity, so a forged token, a replayed lease id from another process, and a
PID reused after a restart all match no row. `pid`/`boot_id` are now enforced
as part of identity (previously `pid` was stored but unused and `boot_id` was
this process's own constant, discarding the host-supplied value). Holder
tokens never appear in an error message or an audit record — only a
truncated SHA-256 fingerprint.

**Finding 6 — finalize decision receipts were incomplete.**
`core.finalization_decision(session_id, reason, durable_terminal)` is one
atomic transaction that snapshots every same-session lease **before any
prune**, snapshots the claim's own heartbeat/expiry, prunes, sweeps, records
structured `evidence` (reason, durable-terminal signal, every lease row with
its age/remaining TTL/live/stale/foreign flags, the pruned ids, and the claim
heartbeat/expiry) into an extended `finalize_audit` (new `disposition`,
`durable_terminal`, `evidence` columns, added by in-place `ALTER TABLE` so no
recorded decision is dropped), and returns preserve-versus-release.
A live same-session lease always preserves. Nothing else is proof: a stale
lease, another process's lease, and a missing row are equally inconclusive,
so a claim is released only on an explicit durable terminal signal
(`core.is_durable_terminal_reason` — a deliberate boundary such as
`shutdown`/`session_boundary`/`session_reset`, cross-checked against
`hermes_state_common.is_automatic_end_reason`, never `ws_orphan_reap`, never
an unrecognised or absent reason). `_on_session_finalize` calls that decision
and completes the Kanban mirror only on an explicit release disposition.

### TDD provenance: RED before GREEN

The lifecycle suite's new cases were written and run against the unchanged
`core.py`/`__init__.py` first: **20 failed, 3 passed** (the three that passed
were already-correct sub-cases: idempotent re-begin, `holder_token`-only
renew/end checking). They cover foreign begin collision on each identity
field, same-holder idempotent re-begin, full-identity renew/end validation,
live-versus-stale snapshot before prune, missing/stale/foreign state rejected
as non-terminal proof, normal terminal cleanup exactly once, a stale reap
timer firing after a newer turn started, claim heartbeat/expiry evidence,
release only on an explicit release disposition, and a failed decision
transaction leaving no partial state. After the fix the same suite is green
at **74 passed** (was 50 before this slice) — see the delivery report for the
exact combined counts across the interacting host suites.

The suite also now resolves the plugin module the hooks really run in
(`PluginManager` loads it by file location, a different module object from
the test's `plugins.work_claims` import), so hook-path assertions and the
Kanban-mirror patch apply to the instance actually executing.

### Regenerated manifest (this slice)

`core.py`, `__init__.py`, `README.md` and `plugin.yaml` changed;
`test_work_claims.py` is unchanged and keeps its existing hash.
`MANIFEST.json`'s per-file hashes were regenerated for the four changed files
and `installer.APPROVED_MANIFEST_SHA256` updated to match the regenerated
manifest's own sha256. `installer.PRODUCTION_INSTALLER_SHA256` is unchanged —
this slice never read or wrote any file under `~/.hermes/` outside this
worktree.

Findings 1 (CV-A01), 2 (production installer), 3 (`recover()` participants),
7 (self-referential `generated_from_candidate_commit`) and 8 (true
subprocess/abrupt-kill tests) remain out of scope for this slice and
unaddressed.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` | `b0f5b0531081cc5dda456e030d8fe1265016c84c14f1a357349c0ad24fdc47a4` |
| `plugins/work_claims/__init__.py` | `6813e78b4b650144939d978e87c8e4eed960ba7ed0bb7b93b1fb36ad9f7eb9d1` |
| `plugins/work_claims/core.py` | `79a552cf27f1cd6652461eaf23d976a05c178b593af11312d69a73e59f37630f` |
| `plugins/work_claims/README.md` | `16fdb3133a2f03ecb192ddf1d7fbe866900a9e7e6cecf766f3c3df68c66fcf53` |
| `plugins/work_claims/MANIFEST.json` (this slice's committed, approved manifest) | `44350b8fb3c554dbc7e0941204c8e2c38b033b3f1f09d021ee9fe9f7f076142e` |

Pinned as `installer.APPROVED_MANIFEST_SHA256` and re-verified by
`test_installer_distribution.py::ProvenancePinTest` against the real
committed manifest file.

## External release attestation (findings 2, 3, 7, 8 — this slice)

This section is the plugin's **external, non-manifest attestation**.
`PROVENANCE.md` is deliberately *not* listed in `MANIFEST.json`'s `files`,
is not distributed to any profile, and is not covered by
`installer.APPROVED_MANIFEST_SHA256`. Commit and tree identity live here,
outside the distributed set, because a manifest cannot reliably record the
commit that contains it (finding 7).

### Reviewed identity (independent review block `work-claims-review-block-e37cd649`)

| item | value |
|---|---|
| reviewed commit | `e37cd649b6cab76a8fec5b863a82b4cd1e514d6b` |
| reviewed tree | `03602198995689c74943145820831f57dd77ee99` |
| reviewed `work_claims` subtree | `07d3ac092addff9e2401e400a81431b76eac1549` |
| review date | 2026-09-02 |
| `MANIFEST.json` sha256 at the reviewed commit | `b2b3ecfae518dfbeee033c1f5f8f0d91c1bdb7403e79aeb6896e14725eee941b` |
| production installer sha256 at review time | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

The reviewed commit is an ancestor of this slice; two further slices
(`46dfd5646`/`980ce9093`/`480618b1b`, then `ac4de5cbd`/`182b1f4f2`) landed on
top of it, so the manifest hash recorded above is the reviewed commit's, not
this slice's — the current pins are in the table further down. This slice
starts from `182b1f4f2649133432e3b2f02b038c2d8475bcf9`.

Containment is unchanged: nothing under `~/.hermes/` was written. The only
live file this slice touched at all is
`~/.hermes/scripts/install_work_claims.py`, and only to read its bytes for a
sha256 — it still hashes to `f7a606fa…`, exactly as at review time.

### Finding 7 — the self-referential commit field is gone

`MANIFEST.json`'s `generated_from_candidate_commit` field (last holding
`980ce909…`, already stale by two commits, and originally `92351aef…` where
the reviewed commit was `e37cd649…`) is **removed**. The manifest's keys are
now exactly `plugin_version`, `production_installer_path`,
`production_installer_sha256`, `files` — file-level hashes and nothing that
tries to name its own commit.
`test_installer_distribution.py::ProvenancePinTest` enforces all three
properties semantically: the field's absence, the exact allowed key set, and
that this file (outside the manifest) carries the reviewed commit/tree/
subtree identities above.

### Finding 2 — the production entrypoint now runs the reviewed transaction

`scripts/install_work_claims.py` is added as the repository-owned production
entrypoint. It contains no distribution logic whatsoever — a test asserts
its source contains no `shutil.copy`, `shutil.rmtree`, `os.replace` or
`mkdir(` — and instead:

1. verifies the canonical `plugins/work_claims/installer.py` against the
   pinned `CANONICAL_INSTALLER_SHA256` *before* executing it (the pin runs
   in this direction because a script cannot meaningfully attest to its own
   bytes: a tampered copy would simply omit the check);
2. executes it via `importlib.util.spec_from_file_location` under the
   synthetic name `_work_claims_installer_impl`, never inserted into
   `sys.modules` and never on `sys.path`, so it can neither shadow nor be
   shadowed by an imported `plugins.work_claims.installer` — a test asserts
   the two are distinct module objects with distinct names that nevertheless
   share identical pins, and that loading refuses to proceed if that
   synthetic name is already registered;
3. classifies, read-only, whatever occupies the production installer path:
   the **admitted migration source** (`PRODUCTION_INSTALLER_SHA256`,
   `f7a606fa…` — the exact live installer these bytes replace), a copy of
   itself (migration already applied), or absent. Anything else is an
   unknown installer at the production path and aborts before any profile is
   touched;
4. delegates the entire transaction to `installer.distribute()`.

The live `~/.hermes/scripts/install_work_claims.py` is **not** replaced by
this slice. Nothing is deployed, activated, or copied to a profile: the
replacement lives in the repository, is exercised end-to-end in real
subprocesses against temp directories, and the migration is a separate,
explicitly gated step.

### Finding 3 — marker-recorded participants are authoritative

`installer.recover()` previously iterated the *caller's* profile list and
then deleted the marker unconditionally, so `recover(root, [])` erased the
marker while a profile's `.previous-*` still held un-restored content. Now:

- the marker's own `profiles` list is authoritative and is unioned with the
  caller's list (which may be empty, a subset, or a superset);
- a marker that cannot be parsed — truncated, empty, not JSON, not an
  object, no `profiles` key, non-string entries, or a symlink — raises
  `installer.RecoveryError` and is **left exactly as found**, together with
  every artifact; it is the evidence of what the killed run was doing, and
  `distribute()` refuses to start behind it;
- duplicate `.previous-*`/`.staging-*` siblings from more than one
  interrupted run are ordered deterministically, oldest first by
  `st_mtime_ns` with the filename as a total-order tiebreak. The uuid4 token
  in the name carries no ordering, so a lexical sort would pick an arbitrary
  snapshot; the oldest is the one holding the true pre-transaction content.
  The rest are discarded;
- the marker is deleted only after every participant resolves. If any
  participant still holds an artifact that could not be removed, the marker
  and the artifacts survive and `RecoveryError` is raised.

### Finding 8 — real subprocess SIGKILL tests at every durable transition

`test_installer_kill_recovery.py` spawns the **actual** production
entrypoint in a real child process against a throwaway Hermes home, parks it
at one durable transition, and kills it with `SIGKILL` (asserting the child
died by signal, `returncode == -9`, not by a clean exit). No exception
injection, no `finally` cleanup, no interpreter shutdown hooks — the
evidence left on disk is genuine crash evidence. The parked checkpoints are:

| checkpoint | state after the kill | recovery |
|---|---|---|
| `after_marker` | marker written, nothing staged | marker read, nothing to restore, marker removed |
| `mid_staging` | half-populated `.staging-*`, no swap | staging discarded, destinations untouched |
| `between_renames` | destination **absent**, `.previous-*` holds it | destination restored from `.previous-*` |
| `after_first_swap` | profile 0 swapped, profile 1 untouched | all-or-nothing rollback of profile 0 |
| `after_all_swaps` | every profile swapped, marker not yet deleted | every profile rolled back |

Because the distributed bytes are fixed by the manifest, "rolled back" and
"left holding the new copy" are byte-identical; each destination is
therefore seeded with a non-manifest `PRE_EXISTING` sentinel file that
survives a rollback and cannot survive a completed swap. The suite also
proves that re-running the real entrypoint after a kill recovers first and
then installs cleanly, that recovery is idempotent, that
`recover(root, [])` — a caller that knows about no profiles at all — still
rolls back every participant the killed process recorded, and that a
recovery which cannot finish leaves the marker in place.

### TDD provenance: RED before GREEN

Every test above was written and run against the unmodified
`installer.py`/absent entrypoint first: **31 failed, 22 passed**. The
failures were exactly the new contract — `AttributeError:
module 'plugins.work_claims.installer' has no attribute 'RecoveryError'`,
`FileNotFoundError` for `scripts/install_work_claims.py` in every entrypoint
and kill test, the marker-authority cases deleting the marker instead of
restoring, and the two semantic manifest assertions failing on the
still-present `generated_from_candidate_commit`.

### Regenerated manifest and pins (this slice, after final bytes)

`README.md` changed (the Distribution section documents the entrypoint,
the pin direction and the marker-authority contract); `plugin.yaml`,
`__init__.py`, `core.py` and `test_work_claims.py` are unchanged and keep
their hashes. No runtime plugin behaviour changed in this slice, so
`plugin.yaml`'s version stays at `1.4.0`.

Pins were recomputed strictly in dependency order after the final bytes of
each artifact: `README.md` → `MANIFEST.json` → `installer.APPROVED_MANIFEST_SHA256`
→ `installer.py` → the entrypoint's `CANONICAL_INSTALLER_SHA256`. Nothing is
circular: the manifest does not hash `installer.py`, and `installer.py` does
not hash the entrypoint.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` (unchanged) | `b0f5b0531081cc5dda456e030d8fe1265016c84c14f1a357349c0ad24fdc47a4` |
| `plugins/work_claims/__init__.py` (unchanged) | `6813e78b4b650144939d978e87c8e4eed960ba7ed0bb7b93b1fb36ad9f7eb9d1` |
| `plugins/work_claims/core.py` (unchanged) | `79a552cf27f1cd6652461eaf23d976a05c178b593af11312d69a73e59f37630f` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/README.md` | `74348b73e62df9e014a036c7cee5c2f6790cf0017c808cfb06ebb067a70b59ac` |
| `plugins/work_claims/MANIFEST.json` (committed, approved manifest) | `58552820c02e8c17c6a95ed084b735d5790561df58ef68cf90b2d3de8126c8a0` |
| `plugins/work_claims/installer.py` (canonical implementation) | `221035b89ab1fc6f0636b1ff7ce7554b887c747d29b5d1eb34ebd45c96d8c737` |
| `scripts/install_work_claims.py` (production entrypoint) | `33073967e35ffd9e39ab4685dc2019ddc15b69fc0590565d3bea52811ad3c496` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

`MANIFEST.json`'s sha256 is pinned as `installer.APPROVED_MANIFEST_SHA256`;
`installer.py`'s sha256 is pinned as the entrypoint's
`CANONICAL_INSTALLER_SHA256`; the migration source's sha256 remains pinned
as `installer.PRODUCTION_INSTALLER_SHA256`. All three are re-verified
against the real files by `test_installer_distribution.py::ProvenancePinTest`.

Findings 1 (CV-A01), 4, 5 and 6 are out of scope for this slice; CV-A01 and
the lifecycle semantics were not touched.
## CV-A01 Stage 2A: dispatcher scope is a bound identity only (this slice)

Stage 1 (commit `deb699fb4`) established the process-bound worker identity
in `agent/dispatcher_identity.py` but changed no tool behaviour: it said in
its own docstring that the work-claims mutation gate *would* depend on it.
This slice makes that true and removes what it replaces.

### What changed

`plugins/work_claims/core.py`

* `_resolve_dispatcher_scope()` no longer takes a task id and no longer
  reads `HERMES_KANBAN_TASK`, `HERMES_KANBAN_WORKSPACE`, or
  `delegation_context.is_dispatcher_owned_worker_context()` as authority.
  It resolves `dispatcher_identity.get_bound()` and nothing else. The env
  vars are not read by the gate at all any more; the suppression
  ContextVars still matter, but only *inside* `get_bound()`, where they
  remove authority rather than granting it.
* `dispatcher_identity.revalidate()` runs on **every** decision — token
  expiry, this process's PID and kernel start time, and, against the
  database, task existence, current-run identity and recorded workspace.
  The workspace is then re-walked and re-matched by `(st_dev, st_ino)` to
  the directory the identity was bound to.
* A bound-but-revoked identity denies rather than falling through to claim
  enforcement, and denies on the *same* default-deny terms a valid one uses
  — revocation must not widen scope by handing the decision back to the
  host's looser mutating/non-mutating classification.
* `_dispatcher_scope_decision()` is default-deny. `write_file`/`patch` are
  confined by the existing ancestor walk; `terminal` is confined by the OS;
  `_DISPATCHER_DENIED_TOOLS`/`_DISPATCHER_DENIED_PREFIXES` name the
  deliberately-excluded families; `_DISPATCHER_READ_ONLY_TOOLS` plus the
  `kanban_*` reporting family are the only other things that proceed. An
  unrecognised tool is denied.
* `pre_tool_decision()` replaces `mutation_allowed()` as the entry point and
  can carry a required rewrite. `mutation_allowed()` remains as a
  back-compatible tuple wrapper that reports any decision requiring a
  rewrite as *denied*, since a tuple cannot carry one.

`plugins/work_claims/__init__.py`

* `_pre_tool_call` gates every tool when an identity is bound, and emits the
  containment rewrite as a `pre_tool_call` `{"action": "modify"}` directive.
  Ordinary sessions are unchanged: a non-mutating call still returns
  immediately.

`agent/command_containment.py` (new, repository-side, not distributed)

* Generates a per-workspace Apple sandbox profile — reads unrestricted,
  `file-write*` only under the workspace subpath, `file-write-data`/
  `file-ioctl` under `/dev` for `/dev/null` and `/dev/tty` — and rewrites
  the command to `/usr/bin/sandbox-exec -f <profile> /bin/sh -c <command>`.
* Fails closed: a missing or non-executable (or symlinked) `sandbox-exec`,
  a workspace path that is not canonical, and a workspace path containing
  a quote, backslash, newline or NUL all raise rather than returning an
  unconfined command. Profiles are written mode `0600` into a `0700`
  directory **outside** every workspace, fresh per call, and pruned by age.
* Always wraps, even when the model's own command already names
  `sandbox-exec`: nesting Apple sandboxes intersects policies, so a
  self-supplied permissive wrapper buys nothing.

### The sandbox real-path finding

The spike profile matched on the literal workspace string. Re-running it
against a `/tmp/...` workspace showed the in-workspace write being denied
along with every escape: macOS `/tmp` is a symlink to `/private/tmp`, and
the sandbox matches `subpath` against the **real** path. Two consequences
are now enforced rather than assumed:

* `generate_profile()` refuses any workspace where
  `os.path.realpath(p) != p`, so the profile can never confine a different
  subtree than the one the identity verified. This costs nothing in
  practice — `canonical_walk` already rejects a symlinked component, so a
  workspace that can be bound is already canonical.
* The profile allows `file-read*` unrestricted rather than the spike's
  enumerated read subpaths. With enumerated reads the shell could not
  `getcwd()` its own starting directory and path resolution broke before
  any policy question was reached. Containment is a write boundary; a
  worker that cannot read its toolchain cannot do its task.

### TDD provenance: RED before GREEN

Both suites were written against the Stage-1 code and run first:
`tests/plugins/test_work_claims_cv_a01_dispatcher_scope.py` — **35 failed,
5 passed** (the 5 being the controls that were already correct: ordinary
claimed-session behaviour, read-only tools, the ContextVar-alone case and
the delegated-child env marker). `test_work_claims_terminal_containment.py`
did not collect at all — `ImportError: cannot import name
'command_containment'` — which is the honest RED for a module that does not
exist.

The failures were exactly the new contract: a perfect `HERMES_KANBAN_*`
env granting scope where it must grant nothing, no scope at all for a
genuinely bound identity, no revalidation of run/workspace/expiry, and
every non-path mutator proceeding.

Two RED assertions were corrected rather than the code bent to fit them:
an expiry test raced the whole-second TTL truncation boundary (now sleeps
past it), and one case asserted an unrecognised *read-shaped* tool should
be allowed, which contradicts default-deny — it now asserts the denial.

### Real-subprocess evidence

`test_work_claims_terminal_containment.py` executes the gate's *rewritten*
command as a real subprocess and asserts against the real filesystem, with
positive controls first so no deny can pass for the trivial reason that
nothing ran: an in-workspace write succeeds and a read of `/etc/hosts`
succeeds. Then, all denied and all verified by the file not existing:
absolute path outside, `../` traversal, `>>` redirect, `tee`, `cp`, a
`python -c "subprocess.run([...])"` grandchild, a `hermes config`-shaped
write into `HERMES_HOME`, and a write through a symlink planted *inside*
the workspace. Plus: background denied, missing/outside/nonexistent/
not-a-directory `workdir` denied, the read-only allowlist still contained,
a self-supplied `sandbox-exec` prefix still wrapped, a missing sandbox
binary denied, profile-generation failure denied, and the host's own
`hermes_cli.plugins._dispatch_pre_tool_call_hooks` proven to carry the
rewrite into the args the tool receives.

Identity suppression is proven across a real process boundary too: a
subprocess inheriting the complete `HERMES_KANBAN_*` env binds nothing, and
a second process handed the raw token verbatim fails with a PID mismatch.

### Regenerated manifest and pins (this slice, after final bytes)

`plugin.yaml` (version `1.4.0` -> `1.5.0`, since runtime authorization
behaviour changed), `__init__.py`, `core.py` and `README.md` changed;
`test_work_claims.py` is unchanged and keeps its hash.
`agent/command_containment.py` is repository-side and deliberately **not**
added to the manifest: the distributed set stays exactly the five files
`test_manifest_covers_the_complete_plugin_source_set` pins.

Pins were recomputed strictly in dependency order after the final bytes of
each artifact: `plugin.yaml`/`__init__.py`/`core.py`/`README.md` ->
`MANIFEST.json` -> `installer.APPROVED_MANIFEST_SHA256` -> `installer.py`
-> the entrypoint's `CANONICAL_INSTALLER_SHA256`.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` | `e662f8031ad77c50d4ee73f8159110e36d9edeaacb586a596a8b5afe47a087fa` |
| `plugins/work_claims/__init__.py` | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` | `043558e7cf53d4d7207cc3a629addd5c39064ee0fa4d67652cf4177ed95146c7` |
| `plugins/work_claims/README.md` | `e0625b3dd79d7680edb6c1a701f863bbda693741205797d42b2a85ae05ae45eb` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/MANIFEST.json` (committed, approved manifest) | `86704d376656189df9ea35e4a8bf65ad440662fb46e48af61d78bac805b09450` |
| `plugins/work_claims/installer.py` (canonical implementation) | `a9dd080cb9b879f750ad0769d4a65f617c74847e5f3116b716556f292d3b4783` |
| `scripts/install_work_claims.py` (production entrypoint) | `b2e1be5bf645537321393f67837463cf9c4ad52d050714869e1ccd30ed9e9a3e` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

Nothing was installed, promoted or deployed in this slice, and no live
state was touched.

### Still out of scope (at the end of Stage 2A)

`write_file`/`patch` remain confined by the pre-call ancestor walk only.
The descriptor-relative `O_NOFOLLOW_ANY` primitives in
`agent/workspace_confinement.py` are proven and tested but still not wired
into the actual file operation; that is the next slice. **Closed by Stage
2B below.**

## CV-A01 Stage 2B: the file boundary moved into the operation (this slice)

Stage 2A confined `write_file`/`patch` with a pre-tool ancestor walk. That
walk describes the filesystem as it was when the hook ran; the write
happens afterwards, and the shipped writer then does something else
entirely. `ShellFileOperations._atomic_write` contains, verbatim:

```
if [ -L "$t" ]; then rt="$(readlink -f "$t" ...)"; [ -n "$rt" ] && { t="$rt"; ... }; fi
```

— it *deliberately* resolves a symlinked target and writes to whatever it
points at. So a worker that passed the Stage 2A walk with a plain file, and
replaced that file with a symlink before the write, wrote outside its
workspace. The pre-tool check was never the enforcement; it only described
one moment.

### What this slice changes

`agent/confined_file_ops.py` (new, repository-side) resolves a
`ConfinedScope` from `dispatcher_identity.get_bound()`, and the real
implementations in `tools/file_operations.py` route every mutation through
it: `write_file`, `patch_replace` (and `patch_v4a` through it), plus
`_python_delete` and `move_file`, which V4A `*** Delete File:` and
`*** Move File:` reach through the `patch` tool. Under a scope the write is
performed by `agent/workspace_confinement.py`'s descriptor-relative
primitives instead of the shell script above.

What that closes, each proven by executing the vector and asserting against
the real filesystem:

* **symlinked final component** — the target is opened `O_NOFOLLOW_ANY`
  (Darwin) / `O_NOFOLLOW` relative to the verified parent descriptor, and
  an existing symlink is refused rather than resolved;
* **symlinked ancestor** — every intermediate component is opened
  descriptor-relative under the same flag, so the chain fails at the link
  rather than descending through it;
* **hardlink / inode alias** — a target with `st_nlink > 1` is refused, and
  an existing target that *is* a control-plane file under another name is
  caught by `(st_dev, st_ino)` rather than by spelling;
* **case-variant alias** — a name that resolves only because APFS
  case-folds is refused by comparing against `os.listdir(parent_fd)`, so
  `NOTES.TXT` cannot silently replace `notes.txt`;
* **TOCTOU between check and write** — the target is re-inspected
  immediately before the `renameat`, inside the same descriptor chain, so a
  swap after every path check still refuses;
* **patch preimage substitution** — `patch_replace` records the
  `(st_dev, st_ino)` its preimage came from and passes it as
  `expect_key`; a target replaced between the read and the write is refused
  rather than receiving content derived from a file it is not;
* **control-plane self-overwrite** — a worker whose workspace is the
  checkout it runs from is refused the gate module it is authorized by (by
  the `sys.modules` origin, so what is protected is what this process
  actually loaded), the plugin directory, the installer entrypoint, the
  `HERMES_HOME` `plugins/`/`scripts/` trees and config, and the Kanban
  database its identity is recorded in;
* **stale authorization** — the identity is revalidated *in* the operation,
  so a run that advanced or a workspace re-matched by `(st_dev, st_ino)` to
  a different directory stops the write itself, not merely the next hook.

Fail-closed throughout: a platform without `os.supports_dir_fd` for the
required calls refuses the write rather than degrading to a following
`open()`, and a revoked identity or unverifiable workspace refuses it too.
There is deliberately no third outcome between "confined" and "refused".

The operation is exactly as permissive as the Stage 2A gate and never more:
`create_dirs=False`, so only a not-yet-existing *final* component is
allowed and the writer creates no directory the verification never walked.
Widening both layers to allow nested creation is a separate decision and
was not taken here.

Processes with no binding are untouched — ordinary sessions, `delegate_task`
children, in-process cron and spawned subprocesses resolve no scope, take
the unchanged shell path (including its documented symlink-following), and
remain bounded by the claim gate and the OS sandbox. A descendant cannot
inherit a bypass because it cannot inherit the binding; confinement is a
restriction, never a grant.

### Tests

`tests/plugins/test_work_claims_file_confinement.py` (41 new) drives the
real `tools.file_tools.write_file_tool` / `patch_tool` against the real
filesystem — positive controls first (create, replace-preserving-mode,
subdirectory write, patch, CRLF, BOM, the JSON syntax gate), then each
escape executed and proven not to have written. RED first: the suite did
not collect at all against Stage 2A (no `agent.confined_file_ops`). A
second control run with `active_scope()` neutered to return `None` failed
22 of the 41 — every escape case — proving none of them passes because
some other guard denied it.

Adjacent, all green: 191 across the Stage 1/2A identity, workspace-
confinement, dispatcher-scope, terminal-containment and lifecycle suites;
133 across the plugin's own tests, installer distribution, patch parser and
the remaining file-tool suites; 66 LSP. A 1670-test `tests/tools` sweep has
the identical pass/fail counts before and after this change (8 pre-existing
failures, verified against `HEAD`: macOS `/tmp` -> `/private/tmp`
resolution, search-probe and voice-mode cases untouched by this slice).

### Regenerated manifest and pins (this slice, after final bytes)

`README.md` changed (the CV-A01 section gains "Stage 2B" and item 4 now
says only the final component may be new); `plugin.yaml`, `__init__.py`,
`core.py` and `test_work_claims.py` are unchanged and keep their hashes.
No runtime plugin behaviour changed in this slice — the gate is untouched —
so `plugin.yaml`'s version stays at `1.5.0`.
`agent/confined_file_ops.py` and the additions to
`agent/workspace_confinement.py` are repository-side and deliberately
**not** added to the manifest: the distributed set stays exactly the five
files `test_manifest_covers_the_complete_plugin_source_set` pins.

Pins were recomputed strictly in dependency order after the final bytes of
each artifact: `README.md` -> `MANIFEST.json` ->
`installer.APPROVED_MANIFEST_SHA256` -> `installer.py` -> the entrypoint's
`CANONICAL_INSTALLER_SHA256`.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` (unchanged) | `e662f8031ad77c50d4ee73f8159110e36d9edeaacb586a596a8b5afe47a087fa` |
| `plugins/work_claims/__init__.py` (unchanged) | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` (unchanged) | `043558e7cf53d4d7207cc3a629addd5c39064ee0fa4d67652cf4177ed95146c7` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/README.md` | `9d1304dcb0674a70c0ae041225ca0a586ed748fba8066255e3ea315a1b4b18bc` |
| `plugins/work_claims/MANIFEST.json` (committed, approved manifest) | `058f8ee0ff68090869c0bed401fe902bdbcfc878e745b87f92d083b6a794f958` |
| `plugins/work_claims/installer.py` (canonical implementation) | `d549935d1c23137713723212d99afc85f9e14bf11ce615b87488dd6aa503ec9f` |
| `scripts/install_work_claims.py` (production entrypoint) | `0d6c8b7f4564b3830e104494bd0495ee2fa9a47c164cc13d9aed4429308d9fa7` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

Nothing was installed, promoted or deployed in this slice, and no live
state was touched.


## Correction generation 1 — safe mode disarmed the gate; the marker was not durable

Independent BLOCK of frozen commit `4abfa3fb8439a449d0284ba4656aad8e0bc01a57`
(tree `e3166924e3cd41d8f611c36f4f800c086ebceca2`, work_claims subtree
`17002d9a9bf9685bcedebf29c97178913212ffc2`) returned two findings. Both are
corrected here, in one commit, with no other change of scope.

### HIGH — a worker could inherit safe mode, and the containment failed open

Every boundary Stages 1–2B built is enforced by hooks this plugin registers,
which made all of them rest on one thing nobody had checked: that the worker
loaded the plugin. `hermes_cli/plugins.py` returns from
`discover_and_load()` before scanning anything when `HERMES_SAFE_MODE` is
set, and `kanban_db._default_spawn` built the worker environment with
`env = dict(os.environ)`. A dispatcher started in safe mode therefore handed
every worker that variable, and each worker ran its task with no claim gate,
no file confinement and no terminal sandbox. Nothing logged it, nothing
failed — the boundary failed **open**.

Two independent halves, because either alone is a single point of failure:

1. `kanban_db._scrub_worker_env` (new, module-level and therefore directly
   testable) removes `HERMES_SAFE_MODE` from the child environment, alongside
   the pre-existing session-context routing scrub and the `HERMES_TUI` pop
   that moved into it. Safe mode itself is **not** weakened: it remains fully
   honoured by the process the user started, and only its inheritance across
   the dispatcher's spawn boundary is severed. Verified positively — the
   dispatcher's own `HERMES_SAFE_MODE` is asserted still set after the spawn.
2. `agent/execution_turn.py` gained `_admission_required()`. `begin()`'s
   `if not _consumed(): return None` is a cost gate for uninterested hosts;
   for a process holding a `BoundIdentity` it is the fail-open, because "no
   plugin consumes the hook" describes exactly the worker with no
   containment. A bound worker now takes the required-hook path
   unconditionally and aborts through the **existing** `RequiredHookError`
   — no new error type, no new control flow. That backstop covers every
   other way discovery can come up empty: a disabled plugin, a failed scan,
   a partial install. `get_bound()` is already `None` for `delegate_task`
   children, in-process cron and suppressed scopes, so they inherit neither
   the authority nor the requirement; ordinary sessions still skip the lease.

### MEDIUM — the transaction marker was neither atomic nor durable

`_write_txn_marker` used `Path.write_text` directly. The marker is the only
record of an interrupted run's participants and `_read_marker_participants`
treats anything unparseable as fatal, so a `SIGKILL` between create and write
left a zero-byte file *at the marker's own name* and every later
`distribute()` refused to start behind it — a recoverable interruption turned
into a permanent `RecoveryError`. The write was not durable either.

It now writes and `fsync`s a same-directory temp file
(`.work_claims_install_txn.tmp-<token>`), `os.replace`s it onto the final
name (the only way that name is ever created), and `fsync`s the root
directory. `_remove_txn_marker` persists the removal the same way — cleanup a
crash can undo is not cleanup. `recover()` discards stray temp files
whatever their contents and regardless of the caller's profile list, and
reports them as unresolved if they survive.

### Tests

RED first, each with a negative control proving it fails against the frozen
candidate:

- `tests/hermes_cli/test_kanban_worker_safe_mode_env.py` (new, 11) drives the
  real `_default_spawn` with a fake `Popen` and asserts on the environment it
  actually hands the child — removal for every truthy/falsy spelling, the
  dispatcher's own safe mode preserved, and the pre-existing routing/TUI
  scrubs intact after the refactor.
- `tests/plugins/test_work_claims_terminal_containment.py` (+2) runs the
  whole chain in a **real second process** launched from an environment a
  safe-mode dispatcher really produces: the child discovers plugins for
  itself, binds its own dispatcher identity, opens a real execution-turn
  lease (proving admission through the required hook), and has an
  absolute-path write outside its workspace denied by the sandbox — asserted
  against the filesystem, not the directive. The companion test undoes the
  scrub and shows the other half fail closed: discovery skipped, no gate
  registered, `RequiredHookError` naming `on_execution_turn_begin`, turn
  refused. Reverting either fix fails exactly one of the two.
- `tests/agent/test_execution_turn_lease.py` (+5) binds a genuine identity
  through `kanban_db.issue_worker_identity` (not a stubbed `get_bound`) and
  covers: absent consumer aborts, registered-but-unacknowledging consumer
  aborts, loaded plugin admits, unbound process still skips, suppressed
  scope still skips.
- `plugins/work_claims/test_installer_kill_recovery.py` (+1) adds a real
  `SIGKILL` checkpoint **inside** `_write_txn_marker`, parked between the
  temp file's `fsync` and the rename. It asserts the marker's final name was
  never created, that the one surviving temp file is complete, that no
  destination was touched, that `recover(root, [])` — the hardest case, no
  marker and no caller participants — resolves deterministically and removes
  it, and that the next real run completes leaving nothing behind. Against
  the non-atomic write the checkpoint is never reached at all.
- `plugins/work_claims/test_installer_distribution.py` (+7,
  `MarkerPersistenceTest`) records the real `os.replace`/`os.fsync` calls a
  distribution makes and asserts the final name never existed before the
  rename, that temp and destination are siblings, that both the bytes and the
  directory entry were flushed (creation *and* removal), and that a failed
  write leaves no temp file behind.

Counts, all green: 320 across the focused review set (required hooks,
execution-turn lease, lifecycle, CV-A01 dispatcher scope, terminal
containment, file confinement, dispatcher identity, workspace confinement,
exact dispatch, liveness JSON, and the new safe-mode env suite); 68 across
the plugin's own unittest, installer distribution and SIGKILL kill-recovery
suites; 91 passed / 2 skipped across the adjacent safe-mode and kanban
worker-spawn suites; 46 across the lifecycle/webhook hook suites. A full
`tests/plugins` sweep has a byte-identical failure list before and after this
change (17 pre-existing failures in `memory/test_hindsight_provider.py`,
`test_a2a_plugin.py` and `video_gen/test_fal_plugin.py`, verified against a
detached worktree at the frozen commit).

### Regenerated manifest and pins (this slice, after final bytes)

`README.md` changed (a new "Stage 2C" section, and the CV-A01 preamble now
says four stages); `plugin.yaml`, `__init__.py`, `core.py` and
`test_work_claims.py` are unchanged and keep their hashes. No runtime
behaviour of the five *distributed* files changed in this slice — both fixes
live in repository-side host code (`hermes_cli/kanban_db.py`,
`agent/execution_turn.py`) and in `installer.py`, which is deliberately not
part of the distributed set — so `plugin.yaml`'s version stays at `1.5.0`.

Pins were recomputed strictly in dependency order after the final bytes of
each artifact: `README.md` -> `MANIFEST.json` ->
`installer.APPROVED_MANIFEST_SHA256` -> `installer.py` -> the entrypoint's
`CANONICAL_INSTALLER_SHA256`.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` (unchanged) | `e662f8031ad77c50d4ee73f8159110e36d9edeaacb586a596a8b5afe47a087fa` |
| `plugins/work_claims/__init__.py` (unchanged) | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` (unchanged) | `043558e7cf53d4d7207cc3a629addd5c39064ee0fa4d67652cf4177ed95146c7` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/README.md` | `3ab20c1266cc611a0f2589aa641e89bfb541d416963b04f10381c715d25d88f0` |
| `plugins/work_claims/MANIFEST.json` (committed, approved manifest) | `bd4a7ee1f4b6cd9b92ce680eb56d8dcd4c04039c7d7fc43cef8bbe120a630097` |
| `plugins/work_claims/installer.py` (canonical implementation) | `a2377df45b58544bbaf32358fc744ac0006e0e71cfed4932664602a5137737a8` |
| `scripts/install_work_claims.py` (production entrypoint) | `be7eec946a3bc2a3157e54aa78001c154f8bb18aec0f7653aa416c0fe2c2fada` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

Nothing was installed, promoted, canaried or deployed in this slice. No live
profile, board, service or process was touched, and the admitted migration
source was only ever read.

## Correction generation 2 — the marker write and its directory flush

Independent BLOCK of frozen commit `e84ea2cbcff2f9491879f9e0d3fb71dbe7e7d425`
(tree `3f3ded4a7d428cd5465e71ee5ed37b4a43458f30`, work_claims subtree
`04b5d992058a9ca5f5b1999ccf35cc506338b1d0`) returned two remaining installer
durability findings. Both are corrected here, in one commit, with no other
change of scope. The generation-1 safe-mode finding is accepted closed and is
untouched.

Both findings are the same mistake in two places: generation 1 built the
marker's atomicity out of a temp file and a rename, and then let two
operations that make that construction *mean* something report success
without having done their job.

### The marker payload was written with a single `os.write`

`os.write` may accept fewer bytes than it is handed; a short write is a legal
result on a regular file, not an error. Writing under a temp name buys
atomicity of *publication* and nothing else — whatever the temp file holds is
exactly what `os.replace` installs at the marker's own name — so a short write
published truncated JSON under that name. That is precisely the permanent
`RecoveryError` the temp file was introduced to prevent:
`_read_marker_participants` treats anything unparseable as fatal evidence, and
`distribute()` runs `recover()` first, so every later run refuses to start.

`_write_all` now advances a `memoryview` until every byte is accepted. A call
reporting zero bytes of progress raises `OSError` rather than being retried,
because it is the one short-write result a loop cannot advance on and the
alternative is spinning forever. The `fsync` follows the *complete* payload —
flushing a partial file only makes a truncated marker durably wrong — and the
rename still happens only after that flush, so the marker's final name is
never created except over a whole, durable payload.

### The directory flush swallowed both of its failure modes

`_fsync_dir` returned silently when the directory could not be opened and
passed on an `fsync` error, so `distribute()` could report success over a
marker whose publication — or whose removal — a crash could still undo. The
comment justified this as "best-effort by platform capability, not by
outcome", but it was in fact best-effort by outcome: a genuine `EIO` on a
local disk was indistinguishable from an unsupported operation.

The two are now separated. `_directory_fsync_required()` returned true on
POSIX, where a directory can be opened read-only and `fsync`ed; there the
flush is required and any failure propagates. On a non-POSIX platform
(Windows) a directory cannot be opened for flushing at all, so the flush is
*unavailable* rather than failed and is skipped — the only behaviour that
platform admits. There is no "the flush failed, carry on" path on any
platform, and macOS and Linux attempt exactly what they did before while
accepting strictly less.

> **Superseded.** The boolean predicate described here was replaced by a
> total policy mapping — see *The platform decision is a total policy, not a
> boolean (pivot)* in `RECOVERY.md`. The outcome-vs-capability separation it
> established stands; the boolean *shape* of the platform test did not.

Failures are handled where they happen:

- **Publish side.** The marker has already been renamed on, so it is left in
  place: it is exactly the evidence `recover()` reads, and the run aborts
  before any destination is touched.
- **Removal side, `recover()`.** Raises `RecoveryError` — every participant
  was resolved, but the resolution could not be recorded.
- **Removal side, `distribute()`.** Retiring the marker is the transaction's
  commit point, so it now happens *before* each profile's `.previous-*`
  content is discarded rather than after. That reordering is what makes the
  failure recoverable: the call rolls every profile back and raises
  `InstallTransactionError` instead of returning a success a later
  `recover()` would undo. The rollback path likewise no longer lets a failed
  marker retirement mask the original error.

### Tests

RED first — all 11 fail against the frozen candidate, each with a negative
control that pins *why*:

- `test_installer_distribution.py::MarkerCompleteWriteTest` (new, 5) drives
  `_write_txn_marker` through an `os` whose `write` is capped to a legal short
  write, and asserts the published marker is complete, parses, and reads back
  the right participants. The cap is asserted directly
  (`max(writes) == chunk`, `len(writes) > 1`), so the test proves a
  single-call write would have published a fragment rather than assuming it.
  Also: a zero-progress write must raise and publish nothing and leave no temp
  file; a failure *inside* the payload after a chunk was accepted — a state
  only a looping write can reach — publishes nothing; the `fsync` sees the
  whole payload; and, end to end, a run that published a short-written marker
  and died does not stop the next run from recovering and installing.
- `test_installer_distribution.py::DirectoryDurabilityTest` (new, 6) fails the
  directory half deterministically, refusing the open or refusing the flush,
  distinguishing directory fds by `fstat`. It asserts the install raises
  rather than reporting success, that no destination was touched, that the
  published marker is still parseable evidence a subsequent `recover()`
  resolves, that a failure retiring the marker rolls the call back to the
  pre-swap destination (verified with a non-manifest `SENTINEL` file, since
  the distributed bytes are fixed by the manifest), and that `recover()`
  raises `RecoveryError` when it cannot record its own completion. Two tests
  pin the platform carve-out: the flush is required under a POSIX `os.name`,
  and the identical open failure that is skipped under `nt` raises under
  `posix`.

Counts, all green: 79 across the plugin's own unittest, installer
distribution and real-`SIGKILL` kill-recovery suites (68 before, +11 new);
320 across the prior focused review matrix, unchanged; 45 across the adjacent
spawn/safe-mode/worktree suites; 76 across the lifecycle/review/webhook hook
suites. No test was modified or removed — the 11 are additions.

### Regenerated manifest and pins (this slice, after final bytes)

`README.md` changed (point 5 of the distribution section now states the
fail-closed marker-write and directory-flush contract); `plugin.yaml`,
`__init__.py`, `core.py` and `test_work_claims.py` are unchanged and keep
their hashes. Both fixes live in `installer.py`, which is deliberately not
part of the distributed set, so no distributed file's runtime behaviour
changed and `plugin.yaml`'s version stays at `1.5.0`.

Pins were recomputed strictly in dependency order after the final bytes of
each artifact: `README.md` -> `MANIFEST.json` ->
`installer.APPROVED_MANIFEST_SHA256` -> `installer.py` -> the entrypoint's
`CANONICAL_INSTALLER_SHA256`.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` (unchanged) | `e662f8031ad77c50d4ee73f8159110e36d9edeaacb586a596a8b5afe47a087fa` |
| `plugins/work_claims/__init__.py` (unchanged) | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` (unchanged) | `043558e7cf53d4d7207cc3a629addd5c39064ee0fa4d67652cf4177ed95146c7` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/README.md` | `3590303dee4063445b567bce6887a1f2a36d9ccf19f1c1bd593db641014bfdde` |
| `plugins/work_claims/MANIFEST.json` (committed, approved manifest) | `3902b79e13e79cdccbc9d4908b95853ded70b64f2287d4899ff3e49b2189d9ca` |
| `plugins/work_claims/installer.py` (canonical implementation) | `1f7ec9124443e792f0752399d84e99733c148a9d87bcb0b9d76160d1d1d3d65d` |
| `scripts/install_work_claims.py` (production entrypoint) | `d79a6b1fff2360f3edd2b6b11425f46e0275273468c69f32719980376e28ebf1` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

Nothing was installed, promoted, canaried or deployed in this slice. No live
profile, board, service or process was touched, and the admitted migration
source was only ever read.

## Correction generation 3 — the platform decision becomes a total policy

`_directory_fsync_required()`'s boolean was replaced by
`_directory_fsync_policy()`, a total mapping over `os.name` (`posix` ->
required, `nt` -> unavailable, anything else -> `installer.UnsupportedPlatformError`)
so an unrecognised platform can no longer fall through to the branch that
skips the durability flush and reports success. Full design: `RECOVERY.md`,
*The platform decision is a total policy, not a boolean (pivot)*.
`README.md` and this file's own account of the generation-2 fix were updated
to describe the policy in the present tense as superseded, rather than
silently going stale next to the corrected code. `plugin.yaml`,
`__init__.py`, `core.py` and `test_work_claims.py` are unchanged and keep
their hashes; the fix lives entirely in `installer.py`, which is
deliberately not part of the distributed set, so `plugin.yaml`'s version
stays at `1.5.0`.

Pins were recomputed strictly in dependency order after the final bytes of
each artifact: `README.md` -> `MANIFEST.json` ->
`installer.APPROVED_MANIFEST_SHA256` -> `installer.py` -> the entrypoint's
`CANONICAL_INSTALLER_SHA256`. All hashes below were recomputed mechanically
(`shasum -a 256`) from the bytes at this file's own commit.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` (unchanged) | `e662f8031ad77c50d4ee73f8159110e36d9edeaacb586a596a8b5afe47a087fa` |
| `plugins/work_claims/__init__.py` (unchanged) | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` (unchanged) | `043558e7cf53d4d7207cc3a629addd5c39064ee0fa4d67652cf4177ed95146c7` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/README.md` | `d350f06ea610ae8eac3c3b664322aeca86fa53d97904e90096da9ffa03bc9577` |
| `plugins/work_claims/RECOVERY.md` | `aa03dc159e8cc35b17362f7d014ca9f1980c78009da3d704cd01b04990196014` |
| `plugins/work_claims/MANIFEST.json` (committed, approved manifest) | `66060be08a7c52a758a4cdc9feef24bbddea2dce36e7a8305b86b4baec93b7d6` |
| `plugins/work_claims/installer.py` (canonical implementation) | `8729fbcac757902c64d7dbd9084bb2dfa9f3889a09cdd6ccb4fce4085c4d3bce` |
| `scripts/install_work_claims.py` (production entrypoint) | `c28547ce3c735fa510a81b88433302968029c0a9c074149fbaace948efbfa422` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

Nothing was installed, promoted, canaried or deployed in this slice. No live
profile, board, service or process was touched, and the admitted migration
source was only ever read.

## Correction generation 4 — Elias production-roster completeness

### Observed omission

`PROFILES` in `scripts/install_work_claims.py` listed seven profiles (`rook`,
`hannah`, `clara`, `daniel`, `maya`, `oliver`, `sophie`) but omitted `elias`,
who has `work-claims` enabled in their config. Because the installer succeeds
for every profile it is given without reporting omissions, the partial roster
looked indistinguishable from a complete one across all prior runs and test
matrices. The defect was silent by design: nothing fails when a profile is
absent.

### Changed files

| file | change |
|---|---|
| `scripts/install_work_claims.py` | `elias` appended to `PROFILES`; added comment explaining roster is pinned |
| `plugins/work_claims/test_installer_distribution.py` | `ProductionRosterTest` class added: pins the exact eight-member fleet, asserts `elias` is present, asserts no duplicates, and asserts an unparameterised `main()` distributes to every pinned member |
| `plugins/work_claims/PROVENANCE.md` | top paragraph corrected to note the omission; this section appended |

### Hashes (recomputed mechanically after final bytes of each artifact)

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` (unchanged) | `e662f8031ad77c50d4ee73f8159110e36d9edeaacb586a596a8b5afe47a087fa` |
| `plugins/work_claims/__init__.py` (unchanged) | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` (unchanged) | `043558e7cf53d4d7207cc3a629addd5c39064ee0fa4d67652cf4177ed95146c7` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/README.md` (unchanged) | `d350f06ea610ae8eac3c3b664322aeca86fa53d97904e90096da9ffa03bc9577` |
| `plugins/work_claims/RECOVERY.md` (unchanged) | `aa03dc159e8cc35b17362f7d014ca9f1980c78009da3d704cd01b04990196014` |
| `plugins/work_claims/MANIFEST.json` (committed, approved manifest — unchanged) | `66060be08a7c52a758a4cdc9feef24bbddea2dce36e7a8305b86b4baec93b7d6` |
| `plugins/work_claims/installer.py` (canonical implementation — unchanged) | `8729fbcac757902c64d7dbd9084bb2dfa9f3889a09cdd6ccb4fce4085c4d3bce` |
| `plugins/work_claims/test_installer_distribution.py` | `122db59ce86f34ef5de1e4d057c4d2af1851db632735b46a140c7312cfd3ee35` |
| `scripts/install_work_claims.py` (production entrypoint) | `fe1be05ce8c402c721d20a8c8ea5b605509a502c4908b416f0888ef4df8200ce` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

### Tests

Full `plugins/work_claims/` matrix: **92 passed, 6 subtests passed**.
`ProductionRosterTest` contributes four new cases.

Nothing was installed, promoted, canaried or deployed in this slice. No live
profile, board, service or process was touched, and the admitted migration
source was only ever read.

## Correction generation 5 — hardcoded production-roster omissions: dynamic discovery

### Root cause

The hardcoded `PROFILES` tuple in `scripts/install_work_claims.py` is the
single source of truth for which profiles receive the plugin. When a profile
is enabled (gains `work-claims` in `plugins.enabled`) without a simultaneous
`PROFILES` edit, the installer succeeds silently: every profile it was asked
about is installed, and nothing reports that others were skipped. This is how
`elias` was missed in the first place (corrected in generation 4), and it is
structurally guaranteed to happen again for the next newly-enabled profile.

### Changed files

| file | change |
|---|---|
| `scripts/install_work_claims.py` | Added `discover_enabled_profiles(root)` function; updated `main(profiles=None)` to union PROFILES baseline with filesystem discovery; added `ConfigDiscoveryError`; added `import yaml`; expanded module docstring |
| `plugins/work_claims/test_installer_distribution.py` | Added `DefaultInstallDiscoveryTest` (5 new tests) |
| `plugins/work_claims/PROVENANCE.md` | This section appended |

### Design

`discover_enabled_profiles(root)` scans `<root>/profiles/*/config.yaml`,
parses each with `yaml.safe_load`, and returns profile names where
`plugins.enabled` contains `"work-claims"` and `plugins.disabled` does not.
Enablement is decided by config semantics, not name matching.

When `main(profiles=None)` is called (the production CLI path), it unions
the hardcoded `PROFILES` baseline with `discover_enabled_profiles(root)`:
baseline members appear first in their original order; newly discovered names
are appended after. A name in both appears exactly once.

Fail-closed: an unreadable `config.yaml`, a YAML parse error, or a
`plugins` block with a non-list `enabled`/`disabled` value raises
`ConfigDiscoveryError` before any distribution mutation begins.

Explicit `profiles=` is unchanged: when a caller supplies a list, it is
used verbatim with no filesystem scan (staged drtest canaries, targeted
single-profile installs).

`installer.py` and `MANIFEST.json` (the distributed set) are unchanged.
`CANONICAL_INSTALLER_SHA256` and `APPROVED_MANIFEST_SHA256` are unchanged.

### Tests (5 new, all RED→GREEN)

| test | proves |
|---|---|
| `test_newly_enabled_future_profile_is_included_in_default_install` | A profile not in PROFILES but with work-claims enabled is auto-discovered and distributed to |
| `test_disabled_profile_is_excluded_by_config_semantics` | A profile with work-claims in `plugins.disabled` is excluded; a profile that never enables it is excluded |
| `test_malformed_config_fails_before_any_distribution_mutation` | Malformed config.yaml raises `ConfigDiscoveryError` before any profile is touched |
| `test_explicit_profiles_list_is_exact_and_skips_discovery` | Explicit `profiles=` is used verbatim; filesystem-enabled profiles outside the list are not installed |
| `test_current_8_profile_fleet_is_pinned_and_all_installed_by_default` | All eight live profiles (clara, daniel, elias, hannah, maya, oliver, rook, sophie) are installed on a default run; drtest-rook (no config.yaml) is excluded |

Full `plugins/work_claims/` matrix: **97 passed, 6 subtests passed**
(was 92 passed, 6 subtests passed before this slice; +5 new cases).

### Hashes (recomputed mechanically after final bytes of each artifact)

The distributed set (`plugin.yaml`, `__init__.py`, `core.py`,
`test_work_claims.py`, `README.md`) and `MANIFEST.json` and `installer.py`
are unchanged from generation 4 and keep their hashes.

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` (unchanged) | `e662f8031ad77c50d4ee73f8159110e36d9edeaacb586a596a8b5afe47a087fa` |
| `plugins/work_claims/__init__.py` (unchanged) | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` (unchanged) | `043558e7cf53d4d7207cc3a629addd5c39064ee0fa4d67652cf4177ed95146c7` |
| `plugins/work_claims/test_work_claims.py` (unchanged) | `6830919dbd9d7e22e8564e0921bc54ca6c88b41c4141fae423fe59fe0623ed20` |
| `plugins/work_claims/README.md` (unchanged) | `d350f06ea610ae8eac3c3b664322aeca86fa53d97904e90096da9ffa03bc9577` |
| `plugins/work_claims/RECOVERY.md` (unchanged) | `aa03dc159e8cc35b17362f7d014ca9f1980c78009da3d704cd01b04990196014` |
| `plugins/work_claims/MANIFEST.json` (unchanged) | `66060be08a7c52a758a4cdc9feef24bbddea2dce36e7a8305b86b4baec93b7d6` |
| `plugins/work_claims/installer.py` (unchanged) | `8729fbcac757902c64d7dbd9084bb2dfa9f3889a09cdd6ccb4fce4085c4d3bce` |
| `plugins/work_claims/test_installer_distribution.py` | `8137948d609389b373f9e1824a9ca3dacc93b752b36e633fa84f2b324fab0dfb` |
| `scripts/install_work_claims.py` (production entrypoint) | `dc20b4cdc08cf605d5183b2167c4da592c9ad5ad4ada3c3861826f101367f91e` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only, unchanged) | `f7a606fa9f8837f47b5fe272a709699a192163087366a24df6c139c12cda35b1` |

Nothing was installed, promoted, canaried or deployed in this slice. No live
profile, board, service or process was touched, and the admitted migration
source was only ever read.

## Capability admission generation 6 — read-only dispatcher discovery

### Decision and boundary

Rook selected the narrow alternative after security review rejected dispatched
`delegate_task`: dispatched Kanban workers may call only `tool_search` and
`tool_describe` to inspect the schemas already present in their admitted tool
catalog. `tool_call` remains outside `_DISPATCHER_READ_ONLY_TOOLS` and therefore
fails the default-deny dispatcher gate. `delegate_task` remains explicitly
listed in `_DISPATCHER_DENIED_TOOLS`; no delegation runtime, middleware marker,
background process, child tool surface, or authority limit changed.

### Change and rollback

`core.py` adds exactly the two catalog-read names to the dispatcher read-only
allowlist. `plugin.yaml` and `MANIFEST.json` identify release 1.5.1. The manifest
and installer pin the current already-migrated production entrypoint
(`dc20b4cd…`) observed during the pre-check. The README
records the boundary, and the dispatcher-scope integration test exercises the
real plugin-manager pre-tool hook: both catalog reads are allowed while
`tool_call` and `delegate_task` are blocked. Rollback is the installer's atomic
restore of the pre-1.5.1 profile directories, or redistribution of the prior
1.5.0 manifest-approved artifact.

### Candidate hashes

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` | `b84f65bcc7184dc27b2af9eb64c339ff5930d89db6f2fc8a712458ddd344ca34` |
| `plugins/work_claims/core.py` | `1e36823fc9080bda112d4496fe7883024aa39a06f2ebef0746313150353f1932` |
| `plugins/work_claims/README.md` | `de36713f2dc8f7c2acec6c2e94ce232a3d83657f7648d7dd8fca6e2fecbca098` |
| `plugins/work_claims/MANIFEST.json` | `73bc03ca0783932a32236202d4c4f9f3a206d15162c0f1b172397020c11b8e75` |
| `plugins/work_claims/installer.py` | `88e25445ac0429e8a4c3bec3aef05d9d24823cc4623f49d2f4985faa6267eb08` |
| `scripts/install_work_claims.py` | `c4c0270deea9d1fe247116f495786fd544d3362605be2bbcd003af87e2abeba9` |
| `plugins/work_claims/test_installer_distribution.py` | `2027c73d564ad0242048cb62e6e855c830a2783799a75615ee2143a8fab5dcd5` |
| `tests/plugins/test_work_claims_cv_a01_dispatcher_scope.py` | `4ae863bc4d6b5012898ecea9f332a662c24349f00680e7fe64728fe487ee096a` |

No live profile is changed by the candidate commit itself. Promotion requires
independent security review, transactional distribution, plugin/gateway
readback, and a real dispatched-worker canary that proves the two reads succeed
and both execution/delegation bridges remain denied.

## Capability admission generation 7 — bridge authorization seam

### Failed canary and containment

The first real Daniel dispatch (`t_c24bacbf`) proved `tool_search` and
`tool_describe` worked, but also proved `tool_call` executed
`work_claim_status` instead of being blocked. Root cause: `model_tools.py`
handled all three progressive-disclosure bridges before request middleware and
`pre_tool_call`; only the recursively unwrapped underlying name traversed the
hook. Because work-claims deliberately exempts its own claim tools from normal
claim gating, the canary reached the read-only status handler. No delegation
occurred and `delegate_task` was absent. The abort condition fired immediately:
all eight profiles were transactionally returned to manifest-approved 1.5.0
(manifest `66060be0…`) and the host checkout was switched to rollback branch
`rollback/read-only-discovery-pre-9dd` at `63279301b`.

### Correction

`handle_function_call()` now applies request middleware and the pre-tool hook
to the bridge name before catalog handling or unwrap. A blocked bridge emits a
blocked post-tool event and returns without resolving or executing a target.
If `tool_call` is admitted in an ordinary session, its underlying tool still
traverses authorization again under the real name. The regression test
registers a real deferred handler, blocks only outer `tool_call`, and proves the
handler receives zero calls.

| artifact | sha256 |
|---|---|
| `model_tools.py` | `6eb2d4b661d4e2baba7f934295ac52fa3e9c21f4110a70aa451bb5ee24a795f2` |
| `tests/tools/test_tool_search.py` | `42ba14df11d9e59823e873fc9438a2abb1ba2496a5c68dc4d258f54470d0e3f2` |

This correction is not deployed. It requires a new independent security review
and a second real dispatched-worker canary with the same success/deny matrix.

## Capability admission generation 8 — bounded dispatcher delegation (2026-09-04)

The gen-7 correction admitted `tool_search` and `tool_describe` as read-only
capability-discovery bridges, but `delegate_task` remained in
`_DISPATCHER_DENIED_TOOLS`. Task t_0653bb66 (Daniel's kanban validation)
specifically requires `delegate_task` inside a dispatched worker for a
two-child dispatch + `max_concurrent_children=2` fail-closed test.

Prior candidate `4ef2b4db1` implemented bounded dispatcher delegation but was
built on base commit `63279301b` and was superseded by five subsequent commits
on `main` (including the active_pr guard v2 at `1b9fdeba9`). This generation
cherry-picks the three dispatcher delegation commits (`8f58f73ee`,
`609e81e2b`, `86515db7c`) onto `1b9fdeba9` (current main) with conflict
resolution limited to version/hash metadata.

### What the change admits

`_dispatcher_scope_decision` now handles `delegate_task` before the
denied-tools check, routing it to `_dispatcher_leaf_delegation_decision`.
The decision is bounded:

- only `action='spawn'` is admitted; control actions (list/steer/stop) are denied
- only the `tasks` batch form is accepted; single-goal delegation is denied
- tasks batch is capped at 2
- only `goal` and `context` fields per task; `output_schema` is excluded to
  avoid the structured-retry escape (an additional provider turn outside the
  child timeout)
- the authorization marker `_dispatcher_leaf_no_tools` must survive middleware
  intact; it is re-verified at `finalize_dispatcher_delegation_args` before
  dispatch
- one bounded batch per dispatched run (enforced by `_dispatcher_delegation_batch_consumed`)
- children receive an intentionally unknown sentinel toolset (`__dispatcher_no_tools__`)
  resolving to an empty tool surface
- children are synchronous (no background dispatch) and bounded to 3 iterations
- `_dispatcher_revalidate` polls the bound identity while the child runs,
  revoking authority if the task run expires

### Artifacts and hashes (candidate commit 04637d0f9)

| artifact | sha256 |
|---|---|
| `plugins/work_claims/core.py` | `35e4696ae42d7ca8098ca1b08cdff66e22f0f3deb5069c268089e319480a8b99` |
| `run_agent.py` | `d59db2751742bd7909c8ff5fa3b3de5278ff37a8958e7e17aa88a5f7f2311f2f` |
| `tools/delegate_tool.py` | `ae303be719a950ed5a205ea40186cc3f406e7b5c51a81492b18914d9a4f65ff4` |
| `tests/tools/test_dispatcher_safe_delegation.py` | `ecd2771e54f9fdb8491d52eb92998a54b602580f0da49b34cb6272beffb03fc1` |

### Pre-install test results (Oliver's integrated suite)

- 6/6 `test_dispatcher_safe_delegation.py` pass
- 8/8 `test_work_claims.py` pass
- 79/79 (1 skip) `test_installer_distribution.py` pass
- 15/15 (4 skip) combined delegation and gateway-restart-handoff tests pass

### Gate before install

This candidate requires a new independent Maya security review bound to the
exact commit immediately above (after the MANIFEST+PROVENANCE fixup). After
Maya approves, the bounded Daniel-only gateway restart + canary authorized by
Rook on 2026-09-04 09:21 applies to this candidate.

## Data-integrity correction generation 9 — orphaned claim targets

### Root cause and scope

SQLite foreign-key enforcement is disabled by default per connection.
`core._connect()` created `claim_targets.claim_id REFERENCES claims(claim_id)
ON DELETE CASCADE` but did not enable `PRAGMA foreign_keys`, so the declared
cascade was inert on plugin connections. An out-of-band connection using the
same default, or a partial/direct status transition, could therefore leave a
`claim_targets` row with a missing or non-active parent. The conflict query
correctly ignored that row, but `claim_targets.target` remained a primary key,
so the later insert failed with `UNIQUE constraint failed:
claim_targets.target`.

This slice is a minimal delta on accepted rc3 baseline
`b4048f0cf7adaa325e56a7e90d1aed1ea63bd14d`: every plugin connection enables
and verifies `PRAGMA foreign_keys=ON`; every `acquire()` reconciles target rows
inside its existing `BEGIN IMMEDIATE` transaction by deleting only rows whose
parent is absent or not `status='active'`; plugin version is `1.6.1`.
Reconciliation is deliberately on acquisition, not every connection, to avoid
adding a write to execution-lease read/renew/end hot paths.

### Regression evidence

Before the implementation, all three new defect tests failed against rc3:
missing-parent and released-parent reacquisitions returned the exact `UNIQUE
constraint failed: claim_targets.target` error, and `PRAGMA foreign_keys` read
back `0`. The unchanged active-claim control passed and continued to block a
competing acquisition.

After the fix, the tests prove:

- an out-of-band FK-off delete can leave a real orphan, after which acquisition
  from a second profile through the exact `shared_root()/work-claims.db` path
  self-heals and succeeds;
- a target whose parent is present but non-active self-heals and succeeds;
- every plugin connection reads `PRAGMA foreign_keys=1` and parent deletion
  cascades;
- a genuinely active parent still blocks a competing acquisition.

The live `/Users/rook/.hermes/work-claims.db` was inspected read-only before
candidate work. No WAL/SHM files were present, and the stable snapshot reported
zero missing/non-active-parent target rows and four active target rows. No live
plugin, profile, database row or gateway was mutated. Promotion, gateway
restart, and post-restart live readback remain an explicit post-review
activation gate; they are not claimed by this candidate.

### Final hashes

| artifact | sha256 |
|---|---|
| `plugins/work_claims/plugin.yaml` | `e6127feb837fe97621448ac8e5b68dfae585951088e6316a414c1c4c65da5fbb` |
| `plugins/work_claims/__init__.py` (unchanged) | `80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5` |
| `plugins/work_claims/core.py` | `d79e4b90ebc0884b95e8b617bc1cab565bcf7c92b98c3d376aca2af30f6f587f` |
| `plugins/work_claims/README.md` | `854e8f1e082dbe28556a4ff7cb7f7cf583db2c545d5812a1eb3a3f2e7bc2e729` |
| `plugins/work_claims/test_work_claims.py` | `f7a395f53ffd36a10bc974ea89b57a74108aef52b87e859aeaa3cedb8b85c377` |
| `plugins/work_claims/MANIFEST.json` | `c6c19634ddb187b1cd206eaaff790ca050eabcfc60c04b023102ca379ae10d02` |
| `plugins/work_claims/installer.py` | `9a6565f157eabac9cf03ba0c52fee73f07236d180706794ff41ae4a3d6d8bd6f` |
| `scripts/install_work_claims.py` | `10355fbcb6eac7fb3439399e43e9b7170d100140e365b1d9c53d4bb9614b39b8` |
| `~/.hermes/scripts/install_work_claims.py` (admitted migration source, read-only) | `dc20b4cdc08cf605d5183b2167c4da592c9ad5ad4ada3c3861826f101367f91e` |

No file under `~/.hermes/plugins`, `~/.hermes/profiles`, or
`~/.hermes/scripts` was changed. Independent Maya review is required before
any promotion.
