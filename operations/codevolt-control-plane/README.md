# Manifest-bound CodeVolt control-plane release

Status: review candidate. Owner: Oliver. Governance/approval: Rook. Independent exact-byte reviewer: Maya.

Purpose

This directory and `scripts/control_plane_release.py` make Hermes runtime identity, the work-claims plugin, continuity helpers, launchd definition, state migration, checks, fixtures, and recovery documentation one deterministic release unit. It is deliberately an operational edge capability, not a new model tool or customer-facing feature.

Hard boundary

Candidate construction and proof are scratch-only. They do not authorize or perform live installation, `hermes update` against the live checkout, plugin enablement, service restart, launchd mutation, gateway/dashboard exposure, credential access, board mutation, website/VPS action, or publication. The updater and restart rehearsal APIs require `scratch=True`; their subprocesses receive `HERMES_RELEASE_SCRATCH=1` and `HERMES_SUPPRESS_SERVICE_EFFECTS=1`. Live activation remains a separate Rook-authorized task after independent exact-byte PASS.

Release shape

- Runtime: full Git commit and tree, activated only through `hermes update --branch <approved-channel> --yes --keep-stash` after remote ref equality.
- Plugin: exact root plus `clara`, `daniel`, `elias`, `hannah`, `maya`, `oliver`, `rook`, and `sophie` destinations. No ambient discovery or wildcards.
- Helpers/checks: work-claims installer, continuity guard, the manifest-bound canonical `hermes_state_common.py`, launchd canary helpers, tests, contract and incident registry.
- Launchd: the reviewed continuity-guard plist as payload; the proof uses only the separately labelled synthetic canary.
- Migration: closed schema-4-to-schema-5 descriptor.
- Recovery: exact-preimage restoration inside a failed transaction; accepted rollback is forward-only through a new release.

Build

From any working directory, using the frozen repository checkout:

    python /absolute/repo/scripts/control_plane_release.py build \
      --repo /absolute/repo \
      --spec /absolute/repo/operations/codevolt-control-plane/release-spec.json \
      --output /new/empty/output/path

The source tree must be committed and every source byte must equal `HEAD:<path>`. The output directory must not already exist. At freeze time the builder also parses both packaged activation-manifest copies and requires every documented SHA-256 row to equal the corresponding manifest payload digest. The command emits a closed JSON receipt with release ID, manifest SHA-256, archive SHA-256 and literal PASS. Build twice into two absent directories and compare both files byte-for-byte.

Typed gate receipt validation

    python /absolute/repo/scripts/control_plane_release.py validate-gate \
      --contract /absolute/repo/operations/codevolt-control-plane/assurance-contract.json \
      --input /absolute/path/gate-input.json \
      --output /absolute/path/gate-output.json \
      --prerequisite /absolute/path/prior-gate-envelope.json

The validator rejects duplicate JSON keys before schema validation; enforces every declared schema/custom constraint, including calendar-valid RFC3339 UTC timestamps, nested `x-sort-key`, sourced enums, finding-to-reason membership, input equality, and verdict-dependent emptiness; rejects PASS when typed evidence says BLOCK or records failures; and verifies the RFC 8785/JCS plus uint64 big-endian framed `CV-GATE-RECEIPT\0v2\0` evidence digest. Each `--prerequisite` file is a closed `{input,output,prerequisites}` envelope. The validator recursively validates every predecessor's full schema, evidence hash, gate/stage/role/release identity, verdict, and nested chain, then requires exact unique prerequisite cardinality and envelope hashes. Repeat `--prerequisite` only for the exact prerequisites declared by the selected gate.

Focused verification

    HERMES_KANBAN_HOME="$PWD/test-real-kanban" \
      <hermes-venv>/bin/python -m pytest -q \
      tests/operations/test_control_plane_release.py \
      operations/codevolt-control-plane/continuity/tests/test_codevolt_continuity_guard.py \
      plugins/work_claims/test_work_claims.py \
      plugins/work_claims/test_installer_distribution.py \
      tests/plugins/test_work_claims_lifecycle.py \
      tests/plugins/test_work_claims_cv_a01_dispatcher_scope.py \
      tests/plugins/test_work_claims_file_confinement.py \
      tests/agent/test_dispatcher_identity.py

`HERMES_KANBAN_HOME` above is a test-only non-production deny-root override required when the repository itself is nested under the real `~/.hermes`; it prevents the repository test guard from mistaking confined pytest temp DBs for live DBs. Do not use it outside tests.

Installed-shape proof

1. Install the archive under a new synthetic root with `install_release`.
2. Run `run_installed_shape` from a different cwd with `runtime_root` set to the checkout holding the manifest commit. It verifies commit/tree identity, exports that exact Git object to a temporary immutable import root, verifies every installed digest, length and mode, then launches isolated Python imports for root plus every named profile under a minimal environment. When the state helper is declared, it also puts the packaged `scripts/hermes_state_common.py` ahead of the immutable runtime and imports `hermes_state` plus `hermes_state_registry`; a stale installed helper therefore blocks the release. Tracked drift and untracked modules in the checkout are never placed on `sys.path`.
3. Run the supported updater against a dedicated throwaway clone and a scratch `hermes` adapter. Pre- and post-commit/tree must equal the manifest contract; channel movement blocks.
4. Run only the manifest restart scope through a scratch adapter.
5. Capture pseudonymous live Hermes PID/birth/command digests before and after; exact equality is required.
6. Run the isolated launchd canary. It alone may bootstrap a separate non-root label, wait for one >=60-second interval advance, boot it out, and prove label absence.

Transaction and recovery

The installer validates the closed canonical manifest, exact archive member set, content hashes, lengths, profile roster, destination paths and modes before the first target write. Each changed file is staged in its destination directory and atomically replaced. Any exception restores all previous bytes and modes and removes targets that were absent. A repeated install skips byte-and-mode-identical files and preserves their mtimes.

On a failed live activation, the authorized operator would restore the exact runtime/plugin/launchd/state preimages and prior process state inside the same transaction, stop, and reconcile. That recovery does not become an accepted release. Long-term correction is a new manifest-bound release naming the immediate prior release and restoring compatible accepted payload bytes.

Evidence and handoff

- `assurance-contract.json`: closed machine contract snapshot.
- `incident-regressions.json`: exact ordered mapping of every vector to RED evidence and a permanent test node; validation executes the declared pytest selectors rather than substring-resolving their names.
- `TDD-EVIDENCE.md`: observed RED/GREEN construction log.
- `continuity/ACTIVATION_MANIFEST.md`: accepted continuity lineage and rollback preimage evidence.
- `release/manifest.json`, `release/release.tar`: frozen deterministic deliverables after commit.
- `receipts/`: closed scratch/test/hash receipts generated after freezing.

Dispatcher limitation and closure: macOS denied both `ps` and `launchctl bootstrap` inside the worker sandbox. `receipts/launchd-canary-dispatcher-BLOCK.json` preserves that fail-closed attempt and is retained as evidence. Rook then ran the two bounded scratch proofs outside the dispatcher sandbox without activation: `receipts/launchd-canary-rook-outside-sandbox-PASS.json` records RunAtLoad, the 60-second interval advance, successful bootout and proved absence; `receipts/pid-unchanged-and-restart-rook-outside-sandbox-PASS.json` records identical real ps-based Hermes PID/birth identities (38 before and after) around the supported synthetic updater rehearsal. The restart-handoff receipt still uses the permanent injected identity observer and does not claim a separate real-PID restart run. These post-freeze receipts close the construction gate but remain subject to Maya's independent exact-byte review before any activation decision.

Retirement

Do not delete historical regressions or rejected BLOCK evidence. A versioned replacement contract must prove a threat path absent before retiring a vector. A release candidate may be removed only after its review/activation status and replacement are recorded; live retirement requires a separately authorized transaction and exact recovery plan.
