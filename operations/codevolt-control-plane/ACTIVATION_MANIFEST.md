# Multi-board liveness guard rc4 — activation and rollback

This repository-only candidate replaces rc3. It was built from the exact source commit and tree recorded in `release/manifest.json`; no live path, board, launchd job, profile configuration, credential, VPS, or website resource was changed while producing it.

## Bounded correction

- `scripts/codevolt_continuity_guard.py` imports board operations from `hermes_cli.kanban_db` and the supported connection context manager from `hermes_cli.kanban_db_connect`. Every connection still passes the selected board explicitly and all reads remain non-mutating.
- `scripts/hermes_state_common.py` is now a manifest-bound helper payload. It is installed in the same transaction as the guard so the script directory cannot shadow the canonical runtime with a stale helper missing `stat_db_file_identity`.
- The installed-shape gate places the packaged script helper ahead of an immutable export of the manifest-bound runtime and imports both `hermes_state` and `hermes_state_registry`. A legacy helper now fails with `INSTALLED_SHAPE_IMPORT_FAILED`.
- `config.yaml` and `Library/LaunchAgents/com.codevolt.continuity-guard.plist` are unchanged. The reviewed plist already supplies the mutually consistent pair `HERMES_PROFILE=rook` and `HERMES_HOME=/Users/rook/.hermes/profiles/rook`. The guard's child environment preserves that explicit `HERMES_HOME`; therefore the reported fallback warning cannot be caused by these candidate bytes. Treat it as stale output or evidence from a different spawner unless a fresh post-activation cycle reproduces it; do not expand this activation into a config/plist edit.

## Reviewed destination bytes

```text
0e74fc27b14c3101392ddb4aa1d099a016ef86997678f77b762499d068d8054b  scripts/codevolt_continuity_guard.py
9c5d88f09fffc88d31d8f1234cb513cef2836604cd8239f83cdcc2a0cb70e9c3  scripts/hermes_state_common.py
4fdcdff359e73a58a600e79fd588ae99f3f67f8b64f3c0845ee1963b97ded137  release-checks/test_codevolt_continuity_guard.py
a39ea459f692d77b261cfea67b20b6e15bc2888b42ea1f51d1f81d85b6cfcb23  scripts/tests/launchd_canary_worker.py
e0ac543dac9f45ad2dd10320c1e40fdd81408e2e0def8f18b84eeda954c04956  scripts/tests/run_launchd_canary.py
19953276caf699de0d22b4f214616cc48a6e4386da4944d0cc4e9e5d0750db42  Library/LaunchAgents/com.codevolt.continuity-guard.plist
```

The manifest is authoritative for every other payload hash, byte length, destination, mode, dependency order, source commit, and source tree. The deterministic archive contains only `manifest.json` and one content-addressed member per unique payload.

## Activation transaction — not performed here

1. Require Maya's literal PASS for the exact frozen HEAD/tree, source commit/tree, manifest SHA-256, archive SHA-256, and payload table. Confirm the durable release channel still resolves to the manifest's full `source_commit`. Stop on any mismatch.
2. Confirm CV-A01 `t_13b90c53` remains `done` with a literal PASS and the exact-task dispatch prerequisite remains present. The guard fails closed if either condition is absent.
3. Run the frozen isolated canary and require its two observations at least 60 seconds apart, successful bootout, and post-bootout absence proof. Do not use an older receipt as promotion evidence.
4. Before installation, capture exact bytes and modes for every manifest destination that exists, plus absence for destinations that do not. Store this preimage outside the target tree and verify every copied backup with both `shasum -a 256` and `cmp`. At minimum this includes the current guard, state helper, live test, and every other destination the installer reports as changed. Stop if any readback differs.
5. Invoke `scripts.control_plane_release.install_release()` on the frozen `release/release.tar` with install root `/Users/rook/.hermes`. This is one ordered transaction: every changed destination is staged beside its target and atomically replaced; any exception restores changed files in reverse order, including original modes, removes newly created files/directories, and verifies the exact preimage shape before returning failure. Never copy the guard/helper/test separately.
6. Read back every manifest destination. Require manifest SHA-256, byte length, mode, `shasum -a 256`, and `cmp` against the corresponding archive payload to match. Run the installed-shape gate against the same frozen runtime commit/tree.
7. Because the plist path and bytes are unchanged, reload `com.codevolt.continuity-guard` only if installation actually changed the guard or helper path/bytes. Do not reload any gateway merely because the manifest contains the unchanged fleet payload.
8. Run the live regression exactly from the installed tree:

   ```sh
   /Users/rook/.hermes/hermes-agent/venv/bin/python -m pytest -q /Users/rook/.hermes/release-checks/test_codevolt_continuity_guard.py
   ```

9. Observe at least one full interval (at least 60 seconds). Require a fresh schema-5 state with `reconciled=true`, `status` in `SUCCESS_DECISIONS`, no board read error, and no deprecated `kanban_db.connect_closing` warning. A fresh HERMES_HOME fallback warning is a separate spawner fault and blocks completion pending diagnosis; it does not authorize config/plist edits.

## Rollback transaction

1. Stop after the first failed gate; preserve the schema-5 state and logs as evidence. Do not delete state or mutate a board.
2. If the watchdog was reloaded, unload it before restoring code. Restore every changed destination from the verified preimage in exact reverse dependency/install order. Remove destinations whose recorded preimage was absent. Restore original modes.
3. Verify each restored file with both `cmp` and `shasum -a 256`, then verify the complete preimage shape. Any mismatch leaves rollback incomplete and the watchdog must remain unloaded.
4. Move the schema-5 state file aside without deleting it before restarting rc3, because the prior guard may not understand candidate state. Inspect and preserve any pending alerts first.
   The activation-step-5 preimage is schema 4; this candidate writes schema 5.
5. Reload the unchanged plist only after all preimage checks pass, then observe a full interval. No gateway restart, config change, plist edit, task mutation, or board repair is part of this rollback.

## Explicit non-actions

Candidate construction performs no live installation, dispatch, message send, launchd operation, gateway restart, configuration change, credential access, board mutation, VPS action, website action, or publication.
