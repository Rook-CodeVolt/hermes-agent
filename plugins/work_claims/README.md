# Work Claims Plugin

> **This copy is a REVIEW CANDIDATE**, built and tested inside an isolated
> git worktree/branch of `hermes-agent`. It has not been promoted to
> `~/.hermes/plugins/work_claims/` or any profile. See `PROVENANCE.md` for
> baseline hashes and `RECOVERY.md` for the fix design and promotion plan.

This profile-local Hermes plugin prevents independent sessions from mutating the same shared resource.

## Behavior

- Atomic multi-target claims in `~/.hermes/work-claims.db`.
- A visible audit card for every claim on the `work-claims` Kanban board.
- Automatic isolated Git worktree creation when a primary checkout is claimed.
- Fail-closed `pre_tool_call` enforcement for material mutation tools.
- Bounded TTL, renewal, explicit release, and teardown release.
- Session finalize is turn-liveness aware: a live host execution-turn lease
  (`agent/execution_turn.py`, consumed via `on_execution_turn_begin/renew/
  end`) still open for a session defers that session's claim release instead
  of losing it to a stale orphan-reap timer. See `RECOVERY.md`.
- A dispatcher-spawned Kanban worker skips the claim-existence requirement
  only on the strength of the process-bound identity
  `agent/dispatcher_identity.py` issued it. No environment variable and no
  ContextVar is authority. Its scope is default-deny and confined to its
  assigned workspace — by descriptor-relative file operations for
  `write_file`/`patch`, and by an OS sandbox for `terminal`. See "CV-A01"
  below.
- Dispatcher scope admits `tool_search` and `tool_describe` as read-only
  catalog operations so a worker can discover and inspect its already-admitted
  tool schemas. `tool_call` remains default-deny, and `delegate_task` remains
  explicitly denied; catalog visibility does not grant execution authority or
  delegated-worker authority.

## Model tools

- `work_claim_acquire`
- `work_claim_status`
- `work_claim_renew`
- `work_claim_release`

## Hooks

- `pre_tool_call` — fail-closed mutation guard.
- `on_session_finalize` — runs one atomic `core.finalization_decision()` and
  releases this session's claim **only** when that decision's disposition is
  an explicit release (see "Finalize decisions" below).
- `on_execution_turn_begin` / `on_execution_turn_renew` / `on_execution_turn_end`
  — consume the host's own unconditional turn boundary (opened at turn entry,
  closed in `AIAgent.run_conversation`'s outer `finally` for every outcome:
  success, empty, tool-only, interrupted, failed, an escaping exception, and
  the durable-lease early returns) to track one renewable, holder-token-owned
  execution lease per live turn. Liveness is never inferred from a hook
  callback's own thread and never from raw PID inspection — see `RECOVERY.md`.

## Execution-lease holder identity

A lease's holder identity is the whole tuple
`(lease_id, session_id, turn_id, holder_token, pid, boot_id)`.

`on_execution_turn_begin` is a create-only compare-and-insert inside a single
`BEGIN IMMEDIATE` transaction: it inserts when no row exists, refreshes when
the **exact same** identity re-announces itself (a retried admission is
idempotent), and otherwise raises `LeaseIdentityConflict`, failing the
required hook closed. `on_execution_turn_renew` and `on_execution_turn_end`
match on the same full identity, so a replayed lease id, a forged token, or a
PID reused by a later process can neither refresh nor close a lease it does
not own. Error messages and audit records carry only a truncated SHA-256
fingerprint of a holder token, never the token itself.

## Finalize decisions

`core.finalization_decision(session_id, reason, durable_terminal)` is one
`BEGIN IMMEDIATE` transaction that snapshots every same-session lease **before
any prune**, snapshots the claim's own heartbeat/expiry, prunes expired
leases, sweeps expired claims, writes the structured evidence to
`finalize_audit`, and performs the resulting mutation with a claim CAS — all
indivisibly. It returns `disposition` = `preserve` or `release`, plus the same
evidence it recorded.

Two rules decide it:

- A **live same-session execution lease always preserves** the claim; a turn
  is running, and finalizing now is the proven premature-finalization bug.
- **Nothing else is proof the session ended.** A stale lease, another
  process's lease, and no lease row at all are equally inconclusive, so a
  claim is released only when the caller passes an explicit durable terminal
  signal. `core.is_durable_terminal_reason()` grants that only for a
  deliberate conversation boundary (`shutdown`, `session_boundary`,
  `new_session`, `session_reset`, `session_switch`, `tui_close`,
  `user_close`) and never for an automatic-cleanup stamp such as
  `ws_orphan_reap` (cross-checked against `hermes_state_common`'s own
  taxonomy) or an unrecognised/absent reason. Anything else preserves the
  claim and leaves it to its own TTL.

When a release is authorised but a turn is still live, the intent is recorded
in `deferred_finalizes` and resolved exactly once by that turn's own end.

## Validation

```bash
PYTHONPATH=<this directory's parent> <venv>/bin/python -m unittest -v \
  work_claims.test_work_claims \
  work_claims.test_installer_distribution
PYTHONPATH=<repo root> <venv>/bin/python -m pytest \
  tests/plugins/test_work_claims_lifecycle.py
```

(The live-deployment form of this command, run against the promoted plugin,
is `PYTHONPATH=~/.hermes/plugins ~/.hermes/hermes-agent/venv/bin/python -m
unittest -v work_claims.test_work_claims` plus `hermes plugins doctor
~/.hermes/plugins/work_claims --ci` — not run here, since this candidate is
never installed or activated.)

## Distribution (`scripts/install_work_claims.py`, `installer.py`, `MANIFEST.json`)

`installer.py` is the fail-closed, hash-provenanced distribution logic, and
`scripts/install_work_claims.py` (repository-owned) is the production
entrypoint that runs it. The entrypoint holds no distribution logic at all:
it verifies `installer.py` against its pinned sha256, executes it under the
synthetic module name `_work_claims_installer_impl` (never inserted into
`sys.modules`, never on `sys.path`, so it cannot shadow or be shadowed by an
imported `plugins.work_claims.installer`), and delegates to
`installer.distribute()`. `installer.py` never defaults to a real Hermes
home — `source`, `root`, and `profiles` are always passed in — so the whole
pipeline, entrypoint included, is exercisable against a throwaway temp
directory. Chain of trust:

0. `scripts/install_work_claims.py` pins `CANONICAL_INSTALLER_SHA256`, the
   sha256 of the reviewed `installer.py`, and checks it *before* executing
   it. The pin runs in that direction on purpose: a script cannot
   meaningfully attest to its own bytes, so the code that runs first vouches
   for the code it is about to run. It also classifies whatever occupies the
   production installer path (read-only): the admitted migration source, a
   copy of itself (already migrated), or absent — anything else aborts.
1. `installer.PRODUCTION_INSTALLER_SHA256` pins the sha256 of the
   pre-existing live installer this entrypoint replaces — the *admitted
   migration source* — so a test can prove it has not silently drifted since
   this candidate's safety review. That file is only ever read.
2. `installer.APPROVED_MANIFEST_SHA256` pins the sha256 of the committed
   `MANIFEST.json` in this directory, which in turn lists the sha256 of
   every file in the complete plugin source set (`plugin.yaml`,
   `__init__.py`, `core.py`, `README.md`, `test_work_claims.py`).
   `MANIFEST.json` is itself part of the distributed set, so every profile
   carries an auditable copy of exactly which manifest produced it.
3. `installer.distribute()` verifies the *entire* source set against the
   manifest before touching any destination, stages each profile's copy in a
   sibling directory on the same filesystem, re-verifies the staged hashes,
   swaps the staged directory into place with a single atomic rename (so a
   destination is never observed partially populated), reads the swapped-in
   destination back and re-verifies it, and rolls back every profile already
   swapped in the current run if any later profile fails. `installer.recover()`
   resolves any state left by a crash/interruption before a new run starts.
4. `installer.recover()` treats the interrupted run's own transaction marker
   as the authoritative record of which profiles took part — the caller's
   profile list is only unioned in, never a substitute — and deletes that
   marker only once every recorded participant is resolved. A marker that
   cannot be parsed raises `installer.RecoveryError` and is left untouched:
   it is the evidence of what the killed run was doing.
5. Because an unparseable marker is fatal, publishing one is fail-closed at
   both ends. The payload is written with a loop that advances until every
   byte is accepted — a short `os.write` is legal, and a single call would
   publish truncated JSON that poisons every later run — and a write
   reporting no progress fails rather than being retried. The directory
   flush that makes the marker's name (and later its removal) durable is
   decided by a *total* policy over `os.name`, not a boolean test: `posix`
   requires it and `distribute()` propagates any failure instead of
   reporting a success a crash could undo; `nt` is the one platform where
   that flush is documented as unavailable and is skipped; every other value
   raises `installer.UnsupportedPlatformError` rather than inheriting the
   skip by default. Retiring the marker is the commit point, so it happens
   while each profile's pre-swap content still exists and a failure there
   rolls the whole call back.

`MANIFEST.json` records file hashes only. Commit/tree identity is recorded
in `PROVENANCE.md`, outside the distributed set, because a manifest cannot
reliably name the commit that contains it. See `PROVENANCE.md` for the
pinned hash values and `RECOVERY.md` for the crash-recovery design.

The SQLite lock is authoritative for collision prevention. Kanban is the durable human-visible mirror. Do not delete the database to bypass a live claim.

## CV-A01: dispatcher scope (resolved)

`CV_A01_TASK_BRIEF.md` records the original `HERMES_KANBAN_TASK`
unrestricted-mutation bypass: any session with that env var set skipped
every downstream check in `mutation_allowed()`. The fix landed in four
stages: the second replaced the first stage's own foundations, the third
moved the file boundary out of the pre-tool check and into the write, and
the fourth closed the assumption the other three rested on — that the
worker had loaded this plugin at all.

### Stage 1 — a process-bound identity (`agent/dispatcher_identity.py`)

An environment variable cannot carry authority across a process boundary,
and neither can a ContextVar: both are inherited by, or default-true for,
processes the dispatcher never spawned. So authority is a one-time random
token the dispatcher mints *after* the child exists, stored hashed in the
authoritative Kanban database bound to exact task + run + workspace +
worker PID + kernel process-start time, and handed to that child over an
inherited pipe. The child CAS-consumes it once, before it reaches any
command, and receives a frozen `BoundIdentity`.

### Stage 2A — that identity is the only thing this plugin trusts

`core.pre_tool_decision()` resolves dispatcher scope from
`dispatcher_identity.get_bound()` and nothing else. `HERMES_KANBAN_TASK`
and `HERMES_KANBAN_WORKSPACE` are not read by the gate at all; a process
holding a complete and even *truthful* set of them, but no binding, is an
ordinary session and is denied like one.

1. **Revalidated at every decision, never trusted from bind time.**
   `dispatcher_identity.revalidate()` re-checks the token's expiry, this
   process's PID and kernel start time, and — against the database — that
   the task still exists, that the bound run is still its current run, and
   that its recorded workspace is unchanged. The workspace is then
   re-walked and re-matched by `(st_dev, st_ino)` to the directory the
   identity was bound to. Any mismatch denies; it never falls through to
   the claim path, because a revoked worker is not a session that might
   hold a claim.
2. **Suppressed wherever the execution is not the worker.** `delegate_task`
   children, in-process cron jobs, explicitly suppressed scopes, and any
   spawned subprocess (which inherits neither the in-memory binding nor a
   re-usable token) all resolve to no identity and fall through to ordinary
   claim enforcement.
3. **Default-deny, not a blocklist.** In dispatcher scope a tool proceeds
   only if the gate can confine it (`write_file`, `patch`, `terminal`) or
   can name it as leaving nothing outside the task (`_DISPATCHER_READ_ONLY_TOOLS`
   plus the `kanban_*` reporting family). `execute_code`, `skill_manage`,
   `memory`, the browser/computer/desktop families, `setup_mcp`, project
   tools, general `delegate_task`, process and cron management are denied outright,
   and so is any tool the gate does not recognise.

### Bounded dispatcher capability discovery and delegation pilot

Version 1.6.0 admits two previously blocked read-only catalog operations,
`tool_search` and `tool_describe`. They can describe only the worker's already
admitted schema; `tool_call` remains default-deny and every resolved tool is
still checked independently by this hook.

`delegate_task` remains denied except for one narrow host-enforced form: a
foreground batch of one or two leaf tasks containing only `goal`, `context`,
and optional `output_schema`. The hook injects a private runtime marker. The
Hermes host consumes it by forcing synchronous execution, capping each child
at three iterations, resolving an explicit empty child tool surface, and
aborting before execution if any tool survives. Control actions, legacy
single-goal calls, background execution, role/tool overrides, nested
delegation, and batches larger than two fail closed before a child runs.
4. **`write_file`/`patch` are confined by a fresh ancestor walk** performed
   on every call (never cached, so a TOCTOU swap between calls is
   re-caught) that rejects target symlinks, symlinked intermediate
   directories, case-variant lexical aliases (APFS is
   case-insensitive-but-preserving — a different-case path can share an
   inode with the real one), existing regular files with `st_nlink > 1`,
   and any component whose device differs from the workspace's own. Only a
   not-yet-existing *final* component is allowed; everything above it must
   already exist and verify. Stage 2B repeats this walk inside the write
   itself — see below.
5. **`terminal` is confined by the OS**, because a `workdir` check says
   nothing about where a command writes. The call must name an exact,
   existing, in-workspace `workdir` and must not be `background` (a
   detached process outlives the check that authorised it); the command is
   then rewritten by `agent/command_containment.py` to run under
   `/usr/bin/sandbox-exec` with a profile generated for that exact
   workspace: reads unrestricted, writes permitted only inside the
   workspace subtree. The rewrite travels to the host as a `pre_tool_call`
   `modify` directive, which `hermes_cli/plugins.py` merges into the args
   the tool actually receives. Absolute paths, `../` traversal, `>>`
   redirects, `tee`, `cp` and any process the command spawns are all denied
   at the kernel's VFS layer, which is why child processes cannot escape.
   `hermes config`/`hermes profile` mutations are reachable only through
   the terminal and land outside the workspace, so they are denied there
   too — by the sandbox, not by parsing a command line.
6. **Fail closed.** A missing or non-executable `sandbox-exec`, a
   non-canonical workspace, or a workspace path that cannot be expressed
   safely inside a profile denies the terminal call rather than running it
   unconfined. `mutation_allowed()`'s tuple form, which cannot carry a
   rewrite, reports any decision that requires one as denied.

Ordinary claims are unaffected: `path_within()` and the claim-workspace
scoping it backs are unchanged, and a claim-holding session keeps every
tool dispatcher scope denies. See
`tests/plugins/test_work_claims_cv_a01_dispatcher_scope.py` for the
authorization matrix and
`tests/plugins/test_work_claims_terminal_containment.py` for the escape
matrix, which executes each vector as a real subprocess and asserts
against the real filesystem.

### Stage 2B — the file boundary moved into the operation

A pre-tool check describes the filesystem as it was when the hook ran. The
write happens afterwards, and the shipped writer did something else
entirely: `ShellFileOperations._atomic_write` deliberately `readlink -f`s a
symlinked target and writes to whatever it resolves to. Anything with a
foothold in the workspace could therefore pass Stage 2A's walk and then
swap a component before the bytes landed.

So under a bound identity the real implementations in
`tools/file_operations.py` — `write_file`, `patch_replace` (and through it
`patch_v4a`), plus the `_python_delete` and `move_file` that V4A `Delete
File:`/`Move File:` reach — resolve a `ConfinedScope` from
`agent/confined_file_ops.py` and perform the operation through the
descriptor-relative primitives in `agent/workspace_confinement.py`:

- every component is opened **from the workspace's own descriptor** under
  the strongest no-follow flag the kernel offers (`O_NOFOLLOW_ANY` on
  Darwin), so a symlink at any component fails the open rather than
  redirecting it;
- the content lands in a temp file created **inside the verified parent**,
  is `fsync`ed, and is moved into place with `renameat` between two
  descriptors — a rename with no path left to traverse;
- the target is re-inspected immediately **before** the swap, so a file
  that became a symlink, gained a link, or was replaced between the check
  and the write is refused rather than overwritten;
- `patch` pins its write to the exact `(st_dev, st_ino)` its preimage came
  from, so a target swapped underneath the match is refused instead of
  receiving content derived from a file it is not;
- the identity is **revalidated in the operation**, not trusted from the
  hook that authorized it: a run that advanced or a workspace that moved
  stops the write itself.

Two things are refused even *inside* the workspace. A worker whose
workspace is the checkout it runs from may not rewrite the control plane —
the gate module it is authorized by, the plugin directory that module
lives in, the installer that distributes it, the `HERMES_HOME`
`plugins/`/`scripts/` trees and config, or the Kanban database identity is
recorded in — because the next process would load the rewrite. And the
write creates no directory the verification never walked: the operation is
exactly as permissive as the gate, never more.

Everything fails closed. A platform without descriptor-relative operations
refuses the write rather than degrading to a following `open()`, and a
revoked identity or an unverifiable workspace refuses it too. Processes
with no binding — ordinary sessions, `delegate_task` children, in-process
cron, spawned subprocesses — are not confined here at all and are
unchanged: they have no assigned workspace to confine to, and remain
bounded by the claim gate and, for shell commands, the OS sandbox.

`tests/plugins/test_work_claims_file_confinement.py` drives every case
through the real `write_file`/`patch` tools against the real filesystem.

### Stage 2C — the gate has to be loaded to gate anything

Every boundary above is enforced by hooks this plugin registers, so all of
them rest on one unstated assumption: that the worker loaded the plugin.
`HERMES_SAFE_MODE=1` makes `PluginManager.discover_and_load` return before
it scans anything, and the dispatcher built each worker's environment from
its own — so a dispatcher started in safe mode handed every worker that
variable, and each one ran its task with no claim gate, no file
confinement and no terminal sandbox. Nothing said so. The containment
failed *open*, which is the one direction it may not fail.

Both halves are closed, in different places, because either alone is a
single point of failure:

- `hermes_cli.kanban_db._scrub_worker_env` removes `HERMES_SAFE_MODE` from
  the environment the dispatcher hands its worker. Safe mode itself is
  untouched — it remains a deliberate escape hatch, fully honoured by the
  process the user actually started. What is severed is its *inheritance*
  by a detached, dispatcher-owned worker the user never launched.
- `agent/execution_turn.py` will not let a bound worker run a turn without
  the security consumer. `begin()` normally returns `None` when no plugin
  consumes the lease hooks — a cost gate, so an uninterested host pays
  nothing per turn — but for a process holding a `BoundIdentity` that same
  early return is the fail-open, because "no plugin consumes the hook"
  describes exactly the worker that has no containment. A bound worker
  therefore takes the required-hook path unconditionally and aborts
  through the existing `RequiredHookError` if nothing is registered. That
  backstop covers every *other* way discovery can come up empty: a
  disabled plugin, a failed scan, a partial install.

`get_bound()` is already `None` for `delegate_task` children, in-process
cron and explicitly suppressed scopes, so they inherit neither the
worker's authority nor its admission requirement; ordinary sessions still
skip the lease entirely.

`tests/plugins/test_work_claims_terminal_containment.py` proves the chain
end to end in a real second process: starting from a dispatcher that
really is in safe mode, the child discovers plugins for itself, binds its
own identity, opens a real execution-turn lease, and has an
outside-the-workspace write denied by the sandbox. The same probe run with
the scrub undone shows the fail-closed half — discovery skipped, no gate,
and the turn refused rather than run unconfined.
