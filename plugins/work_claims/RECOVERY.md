# Runtime & recovery notes: premature-finalization fix

## Symptom

A work claim gets released ("Session finalized; claim released automatically")
while the session that holds it is still running — typically a one-shot or
detached session whose WebSocket handle disconnected (deliberately,
`detached_sessions=1`) and later hit a stale `ws_orphan_reap` timer from an
earlier, now-irrelevant WS epoch.

## Root cause

`on_session_finalize` fires from the reap path regardless of whether an
in-process turn is still executing for the durable `session_id`, because the
host's cross-backend preserve-guard is scoped to WS-connection leases, not to
same-process background threads. See `PROVENANCE.md` for the full incident.

## How the fix behaves

Liveness for "is a turn actually executing for this session" is bound
entirely to the host's own execution-turn lease (`agent/execution_turn.py`),
never to the identity of whichever thread ran a hook callback. The host opens
one lease per turn immediately after its authoritative start boundary,
rebinds it if the session id rotates mid-turn, and closes it exactly once as
the first guarded action of `AIAgent.run_conversation`'s outer `finally` —
covering every outcome: success, empty, tool-only, interrupted, failed, an
escaping exception, and both durable-lease early returns. `core.py` consumes
the three lifecycle hooks this publishes:

1. `on_execution_turn_begin` admits one `execution_leases` row **create-only
   per holder identity**. Inside a single `BEGIN IMMEDIATE` transaction it
   compares the stored row against the full incoming identity
   (`lease_id`, `session_id`, `turn_id`, `holder_token`, `pid`, `boot_id`)
   and then either inserts (no row yet), refreshes (the exact same holder
   re-announcing itself — a retried admission is idempotent), or raises
   `LeaseIdentityConflict`, failing the required hook closed. The earlier
   `INSERT OR REPLACE` keyed on `lease_id` alone let anything replaying a live
   lease id overwrite the holder identity and leave the real owner unable to
   renew or end its own turn.
2. `on_execution_turn_renew` refreshes `expires_at`, but only for that same
   full identity — a foreign/stale token, a replayed lease id from another
   process, or a PID reused after a restart matches no row and therefore
   changes nothing.
3. `on_execution_turn_end` deletes the row (again, full-identity-checked) and,
   in the *same* atomic transaction, resolves any finalize that was deferred
   while the turn ran — exactly once, whatever the turn's outcome, including
   `rebound` (a mid-turn session-id rotation: the old session id genuinely
   has no more live turns once that happens, so resolving it is correct, not
   a special case).
4. `core.finalization_decision(session_id, reason, durable_terminal)` (driven
   by `on_session_finalize`) is the single, atomic preserve-or-release
   decision. In one `BEGIN IMMEDIATE` transaction it snapshots every
   same-session lease **before any prune**, snapshots the claim's own
   heartbeat/expiry, prunes expired leases, sweeps expired claims, writes an
   unconditional `finalize_audit` row — `reason`, `durable_terminal`, the
   claim identity if any, the observed lease id set, the full structured
   `evidence` (each lease row with its age, remaining TTL, live/stale and
   foreign flags; the pruned ids; the claim's heartbeat and expiry) and a
   fresh `decision_id` — and only then performs the resulting mutation with a
   claim CAS. It decides by two rules:
   - a **live same-session lease always preserves** the claim; and
   - **nothing else is proof the session ended** — a stale lease, another
     process's lease, and no lease row at all are equally inconclusive, so a
     claim is released only on an explicit durable terminal signal
     (`core.is_durable_terminal_reason`: a deliberate boundary such as
     `shutdown`/`session_boundary`/`session_reset`, never an automatic
     cleanup stamp such as `ws_orphan_reap`, and never an unrecognised or
     absent reason). Otherwise the claim is preserved and left to its TTL.
   When a release *is* authorised but a turn is still live, it is
   **deferred**, not skipped: a `deferred_finalizes` row keyed by `claim_id`
   (not `session_id` — a session that replaces its claim can never have the
   old claim's deferred row resolve against the new one) and a
   `finalize_deferred` claim event are written in the same transaction as the
   audit row and the liveness observation, so no other writer can interleave
   between "observed live" and "decided to defer." Holder tokens never enter
   the evidence: only a truncated SHA-256 fingerprint is recorded.
5. Every execution-turn lease self-heals: `on_execution_turn_end` (and any
   finalize attempt) prunes rows whose renewable `expires_at` has already
   passed, so a turn that crashed (SIGKILL, power loss — the same cases the
   host's own `finally` cannot run for) cannot permanently block
   finalization. Correctness never depends on OS PID/thread inspection — two
   backend processes sharing this database, or a PID reused across restarts,
   are both handled correctly by holder-identity ownership and renewable
   expiry alone; `pid`/`boot_id` are enforced as part of that identity and
   additionally recorded as `foreign` evidence, never used to guess liveness.
6. Explicit release (`core.release`), TTL expiry (`core._expire_stale`), and
   claim replacement (`core.acquire` purging a session's stale rows once no
   claim is active) each retire any `deferred_finalizes` row tied to the
   claim they touch, so a deferred decision can never resolve against a claim
   other than the one it was written for.
7. None of the above depends on the claim's own TTL. The pre-existing TTL
   sweep (`_expire_stale`, default 240 min) is untouched and remains the
   final backstop for any claim, exactly as before this fix, and now also
   ignores turn liveness by design (a live lease defers *finalize*, it never
   blocks TTL expiry).

## Diagnosing a live incident (once promoted)

```sql
-- Every finalize decision, most recent first (always present, even with no
-- active claim):
SELECT decision_id, claim_id, reason, durable_terminal, disposition, outcome,
       observed_leases, occurred_at
FROM finalize_audit
ORDER BY occurred_at DESC LIMIT 50;

-- The full structured evidence one decision was taken on (every same-session
-- lease as it stood *before* the prune, with its freshness, plus the claim's
-- heartbeat/expiry):
SELECT evidence FROM finalize_audit WHERE decision_id = ?;

-- Claim-scoped events for one claim:
SELECT event, occurred_at, detail
FROM claim_events
WHERE claim_id = ?
ORDER BY event_id;
```

Reading the outcomes:

| `outcome` | meaning |
|---|---|
| `released` | an authorised terminal finalize released the claim |
| `deferred` | authorised, but a turn was live; resolves at that turn's end |
| `preserved_live_lease` | a turn was live and no release was authorised |
| `preserved_no_durable_terminal` | no live turn, but the reason was not proof the session ended — the claim stays until its TTL or an explicit release |
| `no_claim` | nothing to decide; recorded anyway |

A `preserved_no_durable_terminal` row is the expected, correct outcome for a
stale reaper tick — it is the fix for the original incident, not a fault. The
claim's own TTL (`claims.expires_at`, visible in `evidence.claim`) remains the
backstop.

An `outcome='deferred'` audit row with no matching later
`deferred_resolved_released` row for the same `claim_id` means the deferred
release has not yet resolved — check whether `execution_leases` still holds
a row for that `session_id` and whether its `expires_at` is in the future;
`evidence.leases` records what that decision actually saw.

## A worker that cannot load the gate now refuses to run

A dispatcher-spawned worker is only contained because it loaded this plugin.
`HERMES_SAFE_MODE=1` skips plugin discovery entirely, and the dispatcher
built each worker's environment from its own — so a dispatcher in safe mode
produced workers with no claim gate, no file confinement and no terminal
sandbox, silently. `hermes_cli.kanban_db._scrub_worker_env` now removes
`HERMES_SAFE_MODE` from the worker environment (safe mode is unchanged for
the process the user started; only its inheritance across the spawn boundary
is severed), and `agent/execution_turn.py` refuses to open a turn for a
process holding a `BoundIdentity` when nothing consumes
`on_execution_turn_begin`.

What that looks like in a live incident: the worker exits early and its log
(`hermes kanban log <task>`) ends in a `RequiredHookError` naming
`on_execution_turn_begin` with `has no registered callbacks`. That is the
backstop working — the worker declined to run the task unconfined. It means
the plugin did not load in that process, so check, in order:

1. whether the profile's `config.yaml` still lists `work-claims` under
   `plugins.enabled`;
2. whether `<profile>/plugins/work_claims/` is present and complete (an
   interrupted distribution leaves a `.work_claims_install_txn.json` marker
   — see the recovery section above);
3. `hermes plugins doctor` in that profile for a load error.

The dispatcher records the run as crashed and retries, so a transient cause
self-heals; a persistent one keeps failing closed rather than quietly
producing unconfined work.

## Guarded promotion plan (not executed by this candidate)

This candidate makes **no** change to `~/.hermes/plugins/work_claims/` or any
profile copy. Promotion is a separate, explicitly authorized step:

1. Review this diff and the full test suite (`tests/plugins/
   test_work_claims_lifecycle.py`, `test_work_claims.py`,
   `test_installer_distribution.py` — all green under the project's
   `venv/bin/python3.11`).
2. Stage to a single low-traffic profile first (e.g. a disposable/test
   profile, not `oliver`/`rook`), by running the *real*
   `~/.hermes/scripts/install_work_claims.py`-equivalent copy for that one
   profile only, from `~/.hermes/plugins/work_claims/` updated to this
   candidate's `core.py`/`__init__.py`/`plugin.yaml`.
3. Smoke-test live: acquire a claim, start a one-shot turn, disconnect the
   WS mid-turn (detach), and confirm the claim survives an orphan-reap tick
   and is released within one turn-cycle of completion (mirrors this
   candidate's `test_stale_orphan_reap_defers_then_resolves_exactly_once_via_real_plugin_manager`
   in `tests/plugins/test_work_claims_lifecycle.py`).
4. Watch `finalize_audit` for `deferred` / `deferred_resolved_released`
   volume for at least one full day on that profile before broadening.
5. Run `~/.hermes/scripts/install_work_claims.py` (updated to source from
   this reviewed candidate) to distribute to the remaining profiles.

## Rollback

Nothing live was changed by building this candidate — there is nothing to
roll back on the live system. To abandon the candidate itself: delete this
branch/worktree, or `git revert` the fix commit(s) on this branch. If step 2
of the promotion plan above has already happened on a live profile, rollback
there is: restore that profile's `plugins/work_claims/{core.py,__init__.py,plugin.yaml}`
from the pre-promotion baseline hashes recorded in `PROVENANCE.md`, or
re-run `install_work_claims.py` from the untouched
`~/.hermes/plugins/work_claims/` canonical source.

## Distribution crash/interruption recovery (finding 7 correction)

`installer.distribute()` never mutates a destination directory in place --
it stages the complete manifest-approved file set in a sibling directory on
the same filesystem, then performs exactly two filesystem renames per
profile: the pre-existing destination (if any) is renamed aside to
`.work_claims.previous-<token>`, then the staged directory is renamed into
the now-vacant destination path. Each rename is a single atomic filesystem
operation, so the destination is never observed holding a mix of old and new
files -- only, for the instant between the two renames, transiently absent.

- A failure between the two renames within the same process (e.g. a
  transient OS error) is caught by `_swap_in` itself, which immediately
  renames `.previous-*` back before re-raising -- self-healing without
  waiting for a later `recover()` call.
- A true crash (SIGKILL, power loss) that lands in that same window is
  resolved by `installer.recover()`, which every `distribute()` call runs
  before starting a new transaction: a profile found with a missing
  destination and a `.previous-*` sibling has that sibling renamed back.
- A crash that lands after one profile's swap has fully completed but
  before the whole multi-profile call has committed is distinguished from a
  normal at-rest state by a transaction marker
  (`<root>/.work_claims_install_txn.json`), written before the first
  profile is touched and removed only once every profile in the call has
  either succeeded or been rolled back. If `recover()` finds that marker, it
  treats *every* profile in it as part of an aborted transaction and rolls
  each one back to its `.previous-*` state (or removes it entirely if it had
  none), preserving all-or-nothing semantics across a crash, not just across
  an in-process exception.
- Stale `.previous-*`/`.staging-*` siblings left over from a prior
  successful run whose final cleanup didn't finish are removed by
  `recover()` as a no-op cleanup pass (no marker present means nothing to
  roll back, just tidy up).

### How the marker itself reaches the disk (finding 2 correction)

The marker is the only record of who participated in a run that dies, and
`_read_marker_participants` deliberately treats anything it cannot parse as
fatal evidence rather than as "no transaction". That makes *how* the marker
is persisted part of the recovery contract, not an implementation detail.
Writing the bytes into the marker's own name left a window in which that
name existed holding zero or partial bytes; a `SIGKILL` inside it turned a
recoverable interruption into a permanent `RecoveryError` on every later
run, because `distribute()` refuses to start behind an unparseable marker.
The write was not durable either, so a marker whose bytes never reached the
platter could be lost while the artifacts it described survived.

`_write_txn_marker` now:

1. writes the payload into a same-directory temp file
   (`.work_claims_install_txn.tmp-<token>`) and `fsync`s it, so the bytes
   are durable before anything names them;
2. `os.replace`s that file onto `.work_claims_install_txn.json` — a single
   filesystem operation, and the only way the marker's final name is ever
   created;
3. `fsync`s the root directory, because bytes that are durable inside a
   file no directory entry yet names are not durable at all.

A crash before step 2 therefore leaves the marker's name untouched, which
is the truth: the marker precedes every destination the run would touch, so
a kill in that window provably changed none of them. The temp file carries
no authority and is never read back — `recover()` discards any it finds,
whatever their contents and regardless of the profile list it was called
with, and reports them as unresolved if they cannot be removed.

Marker *removal* is persisted the same way (`_remove_txn_marker` unlinks
and then `fsync`s the parent). Cleanup a crash can undo is not cleanup: an
unlink whose directory entry never reached the disk would leave the next
process recovering behind a marker for a transaction that actually
committed, rolling every profile back off a good install.

### Both ends of that persistence fail closed (correction generation 2)

Steps 1 and 3 above each had a way to succeed without having done their job.

**The payload was written with one `os.write` call.** `os.write` is allowed
to accept fewer bytes than it was handed; a short write is a legal result on
a regular file, not an error. The temp-file-then-rename dance buys atomicity
of *publication* and nothing else — whatever the temp file holds is exactly
what `os.replace` installs at the marker's name — so a short write published
a truncated, unparseable marker, which is precisely the permanent
`RecoveryError` the temp file was introduced to prevent. `_write_all` now
advances a `memoryview` until every byte is accepted, and a call reporting
zero bytes of progress raises `OSError` rather than being retried forever.
The `fsync` follows the complete payload, because flushing a partial file
only makes a truncated marker *durably* wrong.

**The directory flush swallowed both its failure modes.** `_fsync_dir`
returned silently if the directory could not be opened and passed on an
`fsync` error, so `distribute()` could report success over a marker whose
publication — or whose removal — a crash could still undo. Where the flush
is a real platform capability the error now propagates:

- The flush is required on POSIX, where a directory can be opened read-only
  and `fsync`ed. macOS and Linux are unchanged in what they attempt and
  strictly stricter in what they accept.
- On Windows a directory cannot be opened for flushing at all, so the flush
  is *unavailable* rather than failed and is skipped. This narrows what the
  marker guarantees there and changes nothing on the platforms this plugin
  runs on. There is no "the flush failed, carry on" path on any platform.
- A publish-side failure leaves the marker in place: it has already been
  renamed on, so it is exactly the evidence `recover()` reads, and the run
  aborts before any destination is touched.
- A removal-side failure is handled where it happens. `recover()` raises
  `RecoveryError` (every participant was resolved, but the resolution could
  not be recorded). `distribute()` treats retiring the marker as its commit
  point and therefore does it *before* discarding each profile's
  `.previous-*` content, so a failure there can still roll the whole call
  back — which it does, raising `InstallTransactionError` rather than
  returning a success a later `recover()` would undo.

### The platform decision is a total policy, not a boolean (pivot)

Three successive corrections landed on the same durability decision, and the
third one found that the *shape* of the decision was the problem rather than
any particular condition inside it. `_directory_fsync_required()` returned
`os.name == "posix"`. That is false for `nt`, which is correct, and also
false for every other value `os.name` could ever hold — and "false" here
means *skip the flush and report success*. An unknown platform was silently
handed the one branch that lets an install claim to have made a marker
durable without having done so. A boolean cannot express "I do not know";
it only has a safe-looking answer and a strict one, and the safe-looking
answer is the unsafe one here.

The predicate is replaced by a total mapping over `os.name`, consulted at
call time:

| `os.name` | verdict | behaviour |
| --- | --- | --- |
| `posix` | `DIRECTORY_FSYNC_REQUIRED` | Open the directory read-only, `fsync` it, propagate any failure. |
| `nt` | `DIRECTORY_FSYNC_UNAVAILABLE` | Skip: Windows cannot open a directory for flushing at all. The sole documented carve-out. |
| anything else | — | `installer.UnsupportedPlatformError`, naming the platform and the two known ones. |

`_fsync_dir` dispatches exhaustively on that verdict — a value matching
neither constant raises rather than falling through to the flush or the
skip — so there is no path by which an unrecognised platform is interpreted
as safe. Adding a platform is therefore a reviewed edit to
`_DIRECTORY_FSYNC_POLICY` with a durability story attached, not something a
new `os.name` acquires by default. Because `_fsync_dir` sits under both
`_write_txn_marker` and `_remove_txn_marker`, an unsupported platform stops
`distribute()` before any destination is created and stops `recover()`
before it retires a marker it cannot durably retire.

### Who counts as a participant (finding 3 correction)

The marker is the authoritative record of the aborted run's participants,
because it is the only thing written by the process that crashed. The
profile list passed to `recover()` belongs to whoever is calling *now* and
may be empty, a subset, or about an entirely different run, so it is only
unioned in -- never used as a substitute.

- A marker that cannot be parsed into a list of profile names -- truncated,
  empty, not JSON, not an object, missing `profiles`, non-string entries, or
  a symlink -- raises `installer.RecoveryError`. Nothing is deleted: the
  marker and every artifact are exactly as the crash left them, because they
  are the only evidence of what the killed run was doing. `distribute()`
  runs `recover()` first, so it refuses to start behind such a marker rather
  than distributing over unknown state.
- The marker is unlinked only after every recorded participant has been
  resolved. A participant that still holds an artifact which could not be
  removed keeps the marker alive and raises `RecoveryError`. (A participant
  with no profile directory at all is resolved trivially: the interrupted
  run never reached it.)
- More than one interrupted run can leave more than one `.previous-*` (or
  `.staging-*`) sibling. They are ordered oldest-first by `st_mtime_ns`,
  with the filename as a total-order tiebreak, and the oldest is the one
  restored -- it holds the true pre-transaction content. The uuid4 token in
  the name is random and carries no ordering, so sorting by name alone would
  pick an arbitrary snapshot. The remaining siblings are discarded.

### How these states are tested

`plugins/work_claims/test_installer_kill_recovery.py` spawns the real
production entrypoint (`scripts/install_work_claims.py`) in a child process
against a temp Hermes home, parks it at one durable transition, and sends it
a real `SIGKILL` -- `during_marker_persistence`, `after_marker`,
`mid_staging`, `between_renames`, `after_first_swap`, `after_all_swaps` --
then asserts the crash evidence and
drives recovery, including `recover(root, [])` rolling back every profile
the killed process recorded. Because the distributed bytes are fixed by the
manifest, each destination is seeded with a non-manifest `PRE_EXISTING`
sentinel so a rollback is distinguishable from a completed swap.

`plugins/work_claims/test_installer_distribution.py::CrashRecoveryTest` and
`::RollbackAndReadbackTest` complement that by manufacturing states directly
(missing-destination-with-previous, marker-present mid-transaction,
self-heal via a monkeypatched `os.replace` failure, malformed markers,
duplicate `.previous-*` siblings) and by `::MarkerPersistenceTest`, which
records the real `os.replace`/`os.fsync` calls a distribution makes and
asserts the marker's final name never existed before the rename, that both
the file and the directory entry were flushed, and that a failed write
leaves no temp file behind.

`::MarkerCompleteWriteTest` drives `_write_txn_marker` through an `os` whose
`write` is capped to a legal short write, and asserts the published marker is
complete, parses, and reads back the right participants — with the cap itself
asserted, so a single-call write would have published a fragment. It also
covers a zero-progress write (must raise, publish nothing, leave no temp
file), a failure *inside* the payload after a chunk was accepted, that the
`fsync` sees the whole payload, and, end to end, that a run which published a
short-written marker and died does not stop the next run from recovering and
installing.

`::DirectoryDurabilityTest` fails the directory half deterministically —
refusing the open, or refusing the flush — and asserts that the install
raises rather than reporting success, that no destination was touched, that
the published marker is still parseable evidence a subsequent `recover()`
resolves, that a failure retiring the marker rolls the call back to the
pre-swap destination, and that `recover()` raises `RecoveryError` when it
cannot record its own completion. Two further tests pin the platform
carve-out: the flush is required under a POSIX `os.name`, and the identical
open failure that is skipped under `nt` raises under `posix`.
