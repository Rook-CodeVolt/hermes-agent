---
title: Preventive control-plane assurance contract
type: system
status: review-candidate
owner: Daniel
governance_owner: Rook
created: 2026-09-03
reviewed: 2026-09-03
review_after: 2026-10-03
machine_contract: Preventive Control-Plane Assurance Matrix.json
---

# Preventive Control-Plane Assurance Contract

## Decision

This versioned replacement is the review candidate for CodeVolt's closed local Hermes control-plane assurance contract. It governs candidate construction, review, activation, runtime verification, continuity classification, and regression capture. It does not grant production, website, VPS, Hostinger, credential, payment, publication, DNS, or tracking authority. The prior `cv-control-plane-assurance-v1` capsule remains immutable `BLOCK` evidence and is not authority for implementation or activation.

The machine-readable source for gates, SLOs, canaries, evidence envelopes, and the bounded implementation backlog is [[Preventive Control-Plane Assurance Matrix.json]]. Unknown fields, unknown enum values, absent evidence, stale evidence, and contradictory evidence fail closed.

## Authority and protected boundaries

- Tom King is the sole accountable owner. Rook owns orchestration and governance as the sole work-system manager, release approver, and escalation point; Rook does not own or author the contract content.
- Oliver owns bounded control-plane implementation, supported update execution, launchd/gateway handoff, exact-preimage transaction recovery, and runtime operations.
- Maya performs independent security/exact-byte review and a separate independent active-runtime review. An author or activator cannot review the same stage.
- Daniel owns this application-facing assurance contract and website-priority scheduling, not runtime activation.
- Specialists may mutate only the exact task, board, tenant, workspace, targets, and release paths in a current bounded commission.
- Login, credentials, profile access widening, plugin enablement, gateway/dashboard exposure, live activation, service restart, launchd mutation, board normalization, publication, deployment, Hostinger/VPS action, and rollback are protected actions. Only Rook may authorize them; Oliver executes admitted runtime actions; Maya verifies security-sensitive outcomes.
- A task status, environment variable, process existence, dashboard badge, reviewer comment, or prior PASS never grants authority by itself.

## Five non-interchangeable assurance states

1. **Source review** — review of repository logic, tests, policy, and commit/tree identity. It says the source is reviewable; it does not approve installed bytes.
2. **Scratch proof** — isolated execution of the final installed shape from arbitrary working directories and a minimal launchd-like environment, with global effects disabled and unchanged live PIDs. It says the candidate can work safely in isolation; it does not approve live installation.
3. **Exact-byte approval** — an independent reviewer verifies the frozen commit, tree, release manifest, every file digest/mode/destination, test receipts, rollback preimages, and scratch proof. Only a literal `PASS` with zero findings approves those exact bytes.
4. **Live activation** — an authorized operator installs only the approved manifest through one bounded transaction, uses the supported Hermes updater for core runtime changes, distributes plugins transactionally, and performs declared restarts. Activation is an action, not assurance.
5. **Active-state verification** — a different independent read-only review proves the running processes imported the approved bytes and that launchd, gateways, dashboard read-through, exact-task dispatch, hooks, watchdog cycles, and rollback evidence behave as contracted. Only this state closes a release.

Stages are ordered and non-collapsible. `done`, `REVIEW-CANDIDATE`, successful tests, source-equal installed files, or successful activation cannot substitute for the next stage.

## Release unit and update contract

A release is one deterministic manifest-bound unit containing all changed control-plane material:

- Hermes runtime commit and tree;
- plugin packages and helper modules;
- root and every explicitly listed production-profile plugin destination;
- launchd definitions;
- configuration migrations by schema version;
- checks, fixtures, test vectors, and recovery documentation.

The manifest is closed and canonical. It binds release ID, source commit/tree, policy version, platform/toolchain, exact destination inventory, SHA-256, byte length, mode, owner class, profile name, plugin version, dependency order, restart scope, state migration, preimage digest, and rollback release ID. Wildcards, ambient profile discovery during installation, mutable branch-only identity, unlisted helpers, and partial profile distribution are forbidden.

Core runtime activation uses `hermes update` against the Rook-approved protected release channel. Immediately before invocation, the operator proves that the channel resolves to the approved full commit; immediately after it, the operator proves the installed full commit/tree. Any movement or mismatch blocks and restores the transaction preimage. Direct ad-hoc `git pull`, `git checkout`, copied source, editable local patches, or venv mutation is not an admitted release path.

Rollback is forward-only at the release level: build and approve a new release whose declared payload restores the prior accepted bytes and schema compatibility, then activate it through the same gates. The activation transaction may automatically restore exact preimages on an in-transaction failure to preserve service, but that recovery is not a new accepted release and must be followed by reconciliation. Rehearsal must prove both exact-preimage transaction recovery and a forward corrective release without touching live state.

## Exact-task and process authority

A mutation is admitted only when the authority database confirms one current binding of board slug, tenant, task ID, run ID, assignee profile, canonical workspace, lease/claim identity, issued-at time, expiry, and one-use dispatch nonce. `HERMES_KANBAN_TASK`, `HERMES_HOME`, CWD, caller-supplied paths, and process environment are context only and never authority.

Required controls:

- atomic consume and replay ledger for dispatch nonces;
- transactional re-check immediately before the mutation;
- exact task dispatch with `--task-id` and maximum one spawn;
- denial after task finalisation, reassignment, expiry, claim loss, board mismatch, tenant mismatch, workspace mismatch, or run replacement;
- PID identity bound to PID, process start/birth token, executable, canonical environment, profile, task, and run; PID number alone is insufficient;
- case-folded and filesystem-identity confinement on case-insensitive hosts, covering root, profiles, plugins, boards, credentials, launchd, and every protected floor;
- no plugin may authorize its own overwrite or relocate its authority database through caller-controlled `HERMES_HOME`.

Replay, stale-run, PID-reuse, case-variant path, plugin self-overwrite, wrong-task, mirror-task, cross-session, and finalised-task canaries all have a zero-tolerance error budget.

## Claim, lease, heartbeat, detached-session, and finalisation contract

A Kanban worker's atomic task claim is its claim; it does not acquire a second work-claims plugin claim. Other material mutations require a global target claim before the first write.

A Kanban execution is healthy only when its current task/run binding, PID identity, claim lease, and heartbeat are all live and mutually consistent. A heartbeat cannot revive a dead or replaced PID. A PID cannot compensate for a stale lease. Finalisation atomically revokes mutation authority before or with the terminal task transition; a stale worker must fail its next mutation.

A detached or standalone execution counts only as collision/runway evidence when a durable lease binds a pseudonymous execution ID, PID identity, targets, expiry, heartbeat, and monotonically advancing progress sequence. The first observation is `warming`, the next fresh advance is `executing`, no advance becomes `stalled`, and expiry/dead PID becomes `stale`. It never creates or infers an accountable specialist owner. Detached sessions must survive terminal loss by a supported durable runner and must emit finalisation evidence; an open shell, tmux name, session record, or claim heartbeat alone is insufficient.

One writer per shared target is absolute. Collisions reduce safe runway; they never justify duplicate work.

## Continuity classification and scheduling

Every authoritative current record belongs to exactly one disjoint class and totals must reconcile:

- `executing_specialist` — current `running` or `review` work with one accountable specialist and complete healthy execution evidence;
- `ready` — dependency-satisfied, preflight-admitted, unprotected, non-colliding work;
- `dependency_gated` — `todo` work with at least one non-PASS parent/gate;
- `protected_stop` — current human/protected-action wait or literal `BLOCK`, `REJECT`, or `PIVOT_REQUIRED` verdict;
- `malformed` — contradictory, unowned, unknown-profile/skill, missing evidence, stale execution, invalid workspace, or unreconciled record;
- `historical` — `done` or `archived` evidence, including terminal BLOCK records.

Verdict wins over status. A `done` task with `BLOCK`, `REJECT`, or `PIVOT_REQUIRED` does not satisfy a PASS gate. Parent completion alone is insufficient for manual verdict gates. A `review` card counts as executing only with a current reviewer process; otherwise it is malformed/stale review.

A critical-path task is explicit machine data, never title inference. It must name an immediate successor. Only existing, well-formed children in `todo`, `ready`, `running`, or `review` satisfy the successor inventory. `done`, `archived`, `blocked`, missing, malformed, or unrelated children do not.

The target is two to three non-conflicting accountable specialist lanes when safe executable work exists. Count unique accountable specialists, not cards or PIDs; report worker-process count separately. One lane is allowed only when complete reconciled evidence shows every other lane dependency-gated, protected, malformed, or colliding. The guard may observe, classify, dispatch at most one already-ready exact task, and alert Rook. It may not create tasks, repair dependencies, invent owners, override verdicts, normalize boards, or run a second manager.

Website priority is explicit:

- while the CodeVolt website release is unresolved and safe website work exists, preserve at least one website lane and do not start preventive control-plane implementation that would contend for its people, repository, runtime, or review capacity;
- the manifest-bound assurance implementation begins only after the website-live gate is literal PASS with exact live readback/public checks/rollback evidence and the continuity guard has separate exact-byte and active-runtime PASS;
- a critical active authority or safety incident may pause all other work; Rook records the exception and recovery gate;
- commercial urgency never weakens authority, security, review, or one-writer controls.

## SLOs and error budgets

- **Authority integrity:** 100% of protected mutations have an exact current binding; budget 0 unauthorized, replayed, wrong-task, wrong-board, wrong-workspace, or post-finalisation mutations.
- **Release integrity:** 100% of installed files match one approved manifest across root and every listed production profile; budget 0 missing, extra, drifted, or partially installed files.
- **Review ordering:** 100% of activations follow independent exact-byte PASS; budget 0 pre-review activations or reviewer/author identity conflicts.
- **Continuity observation:** at least 99.9% of scheduled watchdog observations complete and persist a reconciled result within 10 seconds; count-based monthly budget 0.1%. Two consecutive misses or any 120-second evidence gap is a critical alert.
- **Critical detection:** authority, zero-owner, stale-review, duplicate-writer, or dispatch-integrity faults are detected and alertable within 120 seconds; budget 0 undetected fault intervals beyond 120 seconds.
- **Execution freshness:** at least 99% of records presented as executing have mutually consistent evidence no older than 90 seconds; count-based monthly budget 1%. Any stale record is excluded immediately rather than consuming budget as healthy.
- **Activation recovery:** every failed activation restores exact preimages and declared prior service state within 5 minutes; budget 0 unreconciled partial activations.
- **Alert privacy:** 100% of external alerts conform to the allowlist; budget 0 secret, credential, session capability, prompt, absolute path, raw target, raw exception, or customer data disclosures.

A zero-budget breach blocks further activation. A non-zero-budget SLO breach creates a bounded corrective task and freezes discretionary control-plane release work until Rook accepts recovery evidence.

## Alert projection

External alerts may contain only schema version, severity, stable reason code, board slug, task ID, profile name, release ID, aggregate counts, claim pseudonym, and UTC observation time rounded to seconds. Claim pseudonyms are keyed local digests and cannot be reversed from the alert.

External alerts must not contain secret values, credential-like strings, prompts, message content, session IDs, dispatch nonces, raw claim IDs, PIDs, absolute/home/workspace paths, raw targets, environment values, stack traces, exception text, customer data, or attachment content. Sensitive diagnostics stay in owner-only local evidence with bounded retention. Rendering is allowlist projection, never blacklist replacement.

## launchd, gateway, and dashboard handoff

Scratch proof uses a separately labelled non-root launchd canary with synthetic roots. It must prove bootstrap, RunAtLoad, one interval advance of at least 60 seconds, bootout, label absence, global-effect suppression, arbitrary CWD/minimal environment behavior, helper imports, and unchanged live PIDs.

Live handoff is one manifest-defined transaction:

1. freeze source, manifest, receipts, destination inventory, restart scope, and preimages;
2. obtain exact-byte PASS;
3. Rook authorizes the exact release ID and protected actions;
4. Oliver performs supported core update and transactional root/profile plugin installation;
5. restart only listed launchd jobs/gateways through supported lifecycle commands;
6. prove exact installed bytes and process birth identities;
7. observe two watchdog intervals and exact hook convergence;
8. verify each production profile's gateway from its own profile scope;
9. verify the dashboard is only a read-through view of authoritative state, uses the intended profile/board, and shows no contradictory health;
10. Maya independently performs active-state verification and returns literal PASS or BLOCK.

A successful command is not a successful handoff. Active-state PASS requires exact imports from the manifest, current PIDs/birth tokens, expected gateway ownership, exact board confinement, two advancing cycles, no duplicate manager/writer, no unintended restart, and no unresolved finding. The dashboard never becomes an authority source.

## Closed receipt contract

The matrix defines the complete receipt vocabulary. Every gate input evidence object and every produced-evidence object has exactly one registered type, all required fields for that type, and no additional fields. Nested objects are closed by the same rule. Unknown evidence types, fields, enums, reason codes, or gate/reason combinations are rejected. Findings are closed objects rather than free-form maps.

Gate `BLOCK` reason codes come from one finite registry and must also be permitted by the exact gate's reason-code map. `PASS` has an empty reason-code list and no findings. Test-vector outcome labels use the same finite registry but cannot appear in gate output unless the gate map admits them.

Receipt evidence hashes use the matrix's `receipt_hash_contract`: RFC 8785 JSON Canonicalization Scheme bytes for the complete gate input and a closed produced-evidence envelope, each independently canonicalized; evidence arrays are first sorted by `(type, evidence_id)` and duplicate keys are rejected. The two byte strings are length-framed with unsigned 64-bit big-endian lengths after the exact ASCII/NUL domain prefix `CV-GATE-RECEIPT\0v2\0`, then SHA-256 hashed. No concatenated JSON, delimiter-only framing, platform-default encoding, alternate Unicode normalization, reordered object semantics, unsorted evidence, duplicate evidence IDs, omitted empty arrays, or hashing of rendered/log text is admitted. The matrix freezes one positive vector with exact canonical lengths and digest plus prohibited vectors.

## Incident-to-regression rule

Every material incident, rejected review probe, or production escape becomes a named deterministic test vector before the correction is implemented:

1. record a minimized non-secret fixture and stable incident/control ID;
2. run it against the prior accepted baseline and capture literal RED for the expected reason;
3. implement the smallest correction;
4. capture focused GREEN and full-suite GREEN;
5. bind fixture, test, receipts, commit/tree, and release manifest;
6. have the independent reviewer rerun the probe against exact frozen bytes;
7. retain rejected artifacts immutably and label them `BLOCK`; never overwrite or promote them.

A regression may be retired only by a new accepted contract version proving that the threat path no longer exists; deletion for convenience is forbidden.

## Evidence reconciliation — 2026-09-03 09:15 BST

### CV-A01 authority ledger

CV-A01 began as an environment-only bypass in `work_claims` that trusted `HERMES_KANBAN_TASK`. Four staged generations were correctly blocked: wrong mirror-task binding plus seven missing profile copies; caller-controlled `HERMES_HOME` authority relocation and incomplete protected floors; plugin self-overwrite; and case-variant APFS path bypass (CV-A01-R4). These are permanent RED/GREEN vectors, not discarded attempts.

CV-A01 itself is accepted live only for reviewed commit `3fac3d9a2d64eee47aa425b480fe4721bd4487d8`, tree `db2ee8bbdfa95ba9bcca5057eea9375c0e6f1652`, transactional work-claims version `1.5.0`, root plus eight production-profile copies, supported updater activation, exact-task dispatch, gateway/process restart, and separate active-runtime PASS. This acceptance does not approve later continuity-guard releases.

### Continuity-guard evidence

The initial multi-board candidate `0c12c6648a343c946713150bd2de9a6a82a3562f` was activated before independent review and is rejected. Independent review `t_3fbf61b1` found seven discrepancies: ordering, non-unique lane counting, incomplete standalone evidence, absent immediate successors, sensitive alerts, stale migration state, and incomplete isolated launchd proof.

Correction `beb414cd4fd8007fd655f66a421bf6d1e64f6b36` preserved and passed 109 upstream tests, the five prior reviewer probes, and an isolated launchd receipt, and its unique-specialist, detached lease, alert-redaction, stale-state-pruning, and launchd controls are accepted as reusable control evidence. The artifact as a whole remains `BLOCK`, because review `t_79906740` proved that historical children could satisfy the staged-successor rule and that rollback documentation was stale.

At this snapshot, task `t_5dc0acb5` is correcting those two findings; `t_a6886f6a` must independently return literal PASS before `t_8fd7780f` may activate anything. The guard remains unapproved for live activation. Accepted controls inside a rejected candidate are evidence for the next review, not authority to promote that candidate.

The 2026-08-29 root-cause note is historical. Its governor wake and single-board claims were superseded by the 2026-09-01 simplified control model and this contract: the guard does not create work or wake a reasoning governor, and it covers only the manifest-declared managed-board set.

## Bounded implementation backlog

The exact backlog, dependencies, owners, and acceptance criteria are closed in the machine matrix. In order:

1. Contract/matrix validator and canonical receipt schema.
2. Incident fixture pack and mandatory RED baselines.
3. Deterministic release/manifest builder for core, helpers, plugins, launchd, migrations, checks, and docs.
4. Transactional root/profile installer plus installed-shape scratch harness.
5. Exact-task/process/claim/lease/finalisation and confinement controls.
6. Supported-updater, exact-preimage recovery, and forward corrective-release rehearsal.
7. launchd/gateway/dashboard/continuity canaries and alert projection.
8. Independent exact-byte review.
9. Rook-authorized live activation only after website and continuity manual gates pass.
10. Separate independent active-runtime review and SLO baseline.

No backlog item may choose a new authority source, profile set, successor status set, updater path, rollback model, reviewer model, board boundary, scheduling exception, or alert field. Changing one requires a versioned replacement contract approved by Rook.

## Sources

Retrieved 2026-09-03:

- Hermes Agent: Updating & Uninstalling — https://hermes-agent.nousresearch.com/docs/getting-started/updating
- Hermes Agent: Plugins — https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
- Hermes Agent: Kanban — https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban
- Hermes Agent: Running Many Gateways at Once — https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways
- Hermes Agent: Web Dashboard — https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard
- [[Cross-Session Work Coordination]]
- [[2026-09-01 CodeVolt Simplified Work Control Model]]
- CV-A01 task `t_13b90c53`
- continuity reviews `t_3fbf61b1` and `t_79906740`

## Related

- [[20 Areas/Platform Operations]]
- [[CodeVolt Managed Delivery Service]]
- [[Repository Documentation and Handover Standard]]
- [[80 Workforce/Registry|Permanent Workforce Registry]]
