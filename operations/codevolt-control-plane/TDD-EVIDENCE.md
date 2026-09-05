# Control-plane release TDD evidence

Status: scratch candidate only. No live activation, profile write, service restart, credential access, website action, VPS action, or publication occurred.

Canonical construction baseline

- Commit: `f50e9babb7a036da4c184af9ac03ac57f740b736`
- Tree: `2fe2b2fb9a0bc3c143b9f1a0596f1e8f0deef98b`
- Branch in isolated worktree: `task/t_b10b3a5f`
- Baseline did not contain `scripts/control_plane_release.py` or its tests. Therefore each new release-harness regression is literal RED against the baseline by missing implementation, while the older authority/continuity regressions retain their own exact historical evidence in `plugins/work_claims/PROVENANCE.md`, `plugins/work_claims/CV_A01_TASK_BRIEF.md`, and `continuity/ACTIVATION_MANIFEST.md`.

Observed vertical RED then GREEN slices

The commands used the canonical repository venv Python and ran only in the isolated worktree.

1. Deterministic builder
   - RED: collection failed with `ModuleNotFoundError: No module named 'scripts.control_plane_release'`.
   - GREEN: `1 passed` for `test_double_build_is_byte_identical_and_binds_git_identity`.
2. Closed manifest
   - RED: `Failed: DID NOT RAISE ContractError`.
   - GREEN: focused test passed; then builder/manifest suite was `2 passed`.
3. Exact-preimage transaction recovery
   - RED: `NotImplementedError: transactional installer not implemented`.
   - GREEN: focused test passed.
4. Idempotent complete distribution
   - RED: `KeyError: 'changed_destinations'`.
   - GREEN: focused behavior passed and the focused file reached `4 passed`.
5. Installed shape from arbitrary cwd/minimal environment
   - RED: `NotImplementedError: installed-shape harness not implemented`.
   - GREEN: root plus all eight declared production profiles imported successfully.
6. Supported updater scratch rehearsal
   - RED: missing `run_supported_update`; the first execution was then correctly blocked by the repository live-system guard until the dedicated throwaway-repository bypass marker was added.
   - GREEN: the exact `hermes update --branch approved/test --yes --keep-stash` argv updated only a throwaway clone to the manifest commit/tree under explicit service-effect suppression; focused test passed.
7. Restart handoff
   - RED: missing `run_restart_handoff`.
   - GREEN: only the manifest label reached the scratch adapter, suppression variables were required, and PID identity snapshot equality passed.
8. Forward corrective release
   - RED: missing `prove_forward_correction`.
   - GREEN: release 3 had a new source/release identity, named release 2 as immediate rollback predecessor, and restored release 1 payload/schema; focused test passed.
9. Incident registry
   - RED: missing `validate_incident_registry`, then fail-closed `incident RED evidence does not resolve: DIST-MISSING-PROFILE` until this evidence file existed.
   - GREEN target: exact ordered coverage of every contract vector and resolution of each evidence/test path.
10. Missing profile, missing helper, and profile drift finite reasons
   - RED: three focused tests failed with `AttributeError: 'ContractError' object has no attribute 'reason_code'`.
   - GREEN: `3 passed`; reasons are respectively `EXACT_TASK_OR_DISTRIBUTION_MISMATCH`, `INSTALLED_SHAPE_IMPORT_FAILED`, and `RELEASE_UNIT_PARTIAL`.
11. Explicit runtime identity for installed-shape imports
   - RED: `TypeError: run_installed_shape() got an unexpected keyword argument 'runtime_root'`.
   - GREEN: the plugin imported a helper from an unrelated, explicit runtime checkout only after that checkout's commit/tree matched the manifest; focused test `1 passed`.

Independent-review remediation (task `t_9245f8db`)

12. Immutable installed-shape runtime
   - RED: Maya's adversarial review executed a tracked uncommitted helper while the harness returned PASS. New tracked-drift and untracked-module marker regressions first failed at collection because the immutable-export behavior did not exist.
   - GREEN: `run_installed_shape` now imports only from `git archive <manifest commit>` extracted under the unrelated scratch cwd. The tracked-drift test returns PASS without creating its marker; the untracked-only dependency fails `INSTALLED_SHAPE_IMPORT_FAILED` without creating its marker.
13. Executable typed gate contract
   - RED: focused collection failed because `build_gate_receipt`, `compute_evidence_sha256`, and `validate_gate_receipt` did not exist; the CLI regression then failed because `validate-gate` was not an admitted subcommand.
   - GREEN: the contract's input/output schemas, evidence registry, gate role/stage/type maps, prerequisite PASS chain, RFC 8785 canonicalization, domain prefix, uint64 framing, and SHA-256 fixture execute in code. Schema-valid synthetic G10→G20→G30 receipts pass; unknown fields, unsorted evidence, and a bad digest block. The positive contract fixture reproduces `44ba1ea86a73fbe917a9d60fd744ec658e8f349cd3becfd9139c805f23ccb955` exactly.
14. Exact incident selectors and five corrected mappings
   - RED: executing all registry selectors exposed a stale `ProductionRosterTest` class selector, while Maya identified five semantically false mappings.
   - GREEN: the stale class is corrected; `DIST-MISSING-PROFILE` removes an installed declared profile payload and asserts `MANIFEST_DESTINATION_MISSING`; AUTH wrong-board, scratch global-effect, changed PID identity, and wrong gateway runtime each use a minimized prohibited fixture asserting the exact contract BLOCK reason. Registry validation executes all selectors in one confined pytest run.
15. Activation hash derivation
   - RED: the permanent regression raised `ROLLBACK_DOCUMENTATION_STALE` for the old `f4b018c1...` test row (and the independently reported canary/plist rows were likewise stale).
   - GREEN: both byte-identical activation documents now list the manifest payload identities `0309b9c3...`, `e0ac543d...`, and `19953276...`; the non-payload config row was removed. Build-time validation derives and checks every remaining row against the manifest destination payloads before archive creation.
16. Recursive prerequisite envelopes and strict custom-schema execution
   - RED: independent adversarial execution accepted a three-field fake predecessor, duplicate claimed predecessor gates, PASS with BLOCK test evidence, a calendar-invalid RFC3339-shaped timestamp, finding reasons outside `reason_codes`, and unsorted nested `evidence_ids`.
   - GREEN: prerequisite hashes now bind closed recursive `{input,output,prerequisites}` envelopes; every predecessor is fully revalidated against gate/stage/role/release/evidence/hash/verdict contracts; exact unique cardinality is required; contradictory PASS evidence blocks. All used custom schema constraints execute, and permanent prohibited regressions cover each formerly accepted shape.
17. Four incident mappings execute real control entrypoints
   - RED: AUTH-WRONG-BOARD, SCRATCH-NO-EFFECTS, SCRATCH-PIDS, and GATEWAY-PROFILE selected a disconnected dictionary comparator.
   - GREEN: the comparator was removed. Selectors now drive the dispatcher identity handshake against a distinct Kanban DB, the supported updater with its explicit scratch-effect refusal signal, supported-update PID before/after enforcement, and installed-shape runtime identity verification; each asserts the finite production reason.

rc4 compatibility correction (task `t_fc863dc9`)

18. Supported kanban connection API after facade-pointer removal
   - RED: `SupportedEvidenceTests.test_board_fetch_uses_supported_connection_module_after_facade_shim_removal` failed because the guard had no `_kanban_modules` seam and still resolved `connect_closing` through the deprecated `hermes_cli.kanban_db` facade.
   - GREEN: the guard imports operations from `hermes_cli.kanban_db`, imports `connect_closing` from `hermes_cli.kanban_db_connect`, and preserves the explicit selected-board argument; focused test passed.
19. Installed runtime with a stale script-directory state helper
   - RED: `test_installed_shape_rejects_legacy_state_helper_missing_runtime_symbol` returned without raising even though both immutable runtime modules imported `stat_db_file_identity` from a packaged legacy helper that lacked it.
   - GREEN: a declared `hermes-state-common` payload triggers isolated imports of `hermes_state` and `hermes_state_registry` with the packaged script helper first on `sys.path`; the legacy fixture now raises `INSTALLED_SHAPE_IMPORT_FAILED`. The rc4 spec installs the exact canonical helper in the same transaction as the guard.
20. Current-runtime control-plane dependencies
   - RED: the accepted rc3 checks could not collect on current main because dispatcher identity, confinement, and execution-turn helpers were absent and tests still used removed `hermes_cli.kanban_db` compatibility pointers.
   - GREEN: the accepted helpers were ported to the decomposed current APIs (`kanban_db_connect`, `kanban_db_dispatch`, and `kanban_db_identity`), wired into worker spawn, CLI admission, plugin dispatch, turn lifetime, and file mutations, and validated without in-tree compatibility-pointer use.

Final interacting candidate suite

- Command scope: release harness, 118 continuity regressions, dispatcher identity, execution-turn lifetime, workspace and file confinement, terminal containment, lifecycle, CV-A01 dispatcher scope, and work-claims core.
- Frozen-source interacting result: `406 passed, 0 failed` across 9 files in 17.5 seconds.
- Impacted current-runtime result: `167 passed, 2 Windows-only skipped` across file operations, durable-turn admission, and plugin dispatch tests.
- Static checks: Ruff passed all changed Python; mypy passed `scripts/control_plane_release.py`; the compatibility-pointer scan and `git diff --check` passed.
- Deterministic release: two builds were byte-identical, then a scratch install changed all 78 destinations and immutable-runtime installed-shape verification passed for root plus eight profiles; exact digests are recorded in the build and freeze receipts.
- The real synthetic launchd canary attempted from the dispatcher sandbox failed closed because `launchctl bootstrap` returned 5 and the label stayed absent (`print` returned 113). The exact BLOCK receipt is retained. macOS also denied `ps`, so this worker did not claim either missing proof from inside the sandbox.
- Post-freeze gate closure by Rook outside the dispatcher sandbox: `launchd-canary-rook-outside-sandbox-PASS.json` records bootstrap rc 0, RunAtLoad, a distinct 60-second interval observation, bootout rc 0 and proved absence. `pid-unchanged-and-restart-rook-outside-sandbox-PASS.json` records a real ps-based `_hermes_pid_snapshot()` around the supported synthetic updater rehearsal with identical 38-entry PID/birth identities before and after. No live activation or live-path mutation was performed. The restart-handoff proof remains the injected-observer regression and is not represented as a separate real-PID restart run.

Historical RED evidence

- CV-A01, exact-task, replay/PID reuse, confinement, claim/lease/finalisation: `plugins/work_claims/PROVENANCE.md` and `plugins/work_claims/CV_A01_TASK_BRIEF.md` retain the observed baseline hashes, RED output, correction generations, and accepted identity.
- Detached-session, continuity classification, exact dispatch, alert redaction, successor status, launchd canary: `continuity/ACTIVATION_MANIFEST.md` retains rejected candidate identities, findings, corrected bytes, tests, rollback preimage, and independent PASS lineage.
- `incident-regressions.json` is the closed one-to-one index from every matrix vector to those evidence files and one permanent candidate test node.

This file records observed outputs, not synthetic test receipts. Final machine receipts are generated only after the candidate commit/tree and release manifest are frozen.
