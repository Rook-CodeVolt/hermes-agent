# Multi-board liveness guard rc4 remediation — activation and rollback

This repository-only candidate replaces the rejected rc4 freeze. It was built from the exact source commit and tree recorded in `release/manifest.json`; candidate construction did not mutate a live Hermes home, board, launchd job, profile configuration, credential, VPS, or website resource.

## Identity model

Two durable refs have different, non-interchangeable roles:

- `refs/heads/candidate/codevolt-control-plane-rc4-continuity-source` resolves exactly to `manifest.source_commit`. It is the only value admitted as `manifest.release_channel` and as the runtime update channel.
- `refs/heads/candidate/codevolt-control-plane-rc4-continuity-freeze` resolves to the later freeze commit that contains `release/manifest.json`, `release/release.tar`, and final receipts. It must not be passed to `hermes update`.

Before activation, the authorized operator must fetch or publish the source channel through the canonical runtime repository's `origin` without moving it and must prove that both local `git rev-parse` and `git ls-remote origin` return `manifest.source_commit`. A missing or moved remote channel is a stop condition, not permission to substitute the freeze ref or a commit with similar content.

## Scope correction

The archive is narrowed to exactly six continuity-owned destinations: guard, state helper, continuity regression, two isolated-canary helpers, and the continuity launchd plist. It contains no work-claims profile/root payload, installer, unrelated check, fixture, migration descriptor, or recovery-document copy. The source commit still preserves the exact independently reviewed work-claims 1.6.1 bytes (plugin `e6127feb…`, package entrypoint `80e4333a…`, core `d79e4b90…`, README `854e8f1e…`, and regression test `f7a395f5…`), so the runtime update cannot reintroduce the rejected 1.6.0 source.

`config.yaml` is not a destination. The reviewed plist already supplies `HERMES_PROFILE=rook` and `HERMES_HOME=/Users/rook/.hermes/profiles/rook`; no config or plist edit is authorized outside an exact manifest byte change.

## Reviewed continuity destination bytes

```text
0e74fc27b14c3101392ddb4aa1d099a016ef86997678f77b762499d068d8054b  scripts/codevolt_continuity_guard.py
9c5d88f09fffc88d31d8f1234cb513cef2836604cd8239f83cdcc2a0cb70e9c3  scripts/hermes_state_common.py
4fdcdff359e73a58a600e79fd588ae99f3f67f8b64f3c0845ee1963b97ded137  release-checks/test_codevolt_continuity_guard.py
a39ea459f692d77b261cfea67b20b6e15bc2888b42ea1f51d1f81d85b6cfcb23  scripts/tests/launchd_canary_worker.py
e0ac543dac9f45ad2dd10320c1e40fdd81408e2e0def8f18b84eeda954c04956  scripts/tests/run_launchd_canary.py
19953276caf699de0d22b4f214616cc48a6e4386da4944d0cc4e9e5d0750db42  Library/LaunchAgents/com.codevolt.continuity-guard.plist
```

The manifest is authoritative for all six destination hashes, byte lengths, modes, dependency order, source identity, and archive payload members.

## One ordered activation transaction — not performed here

The exact sequence is supported runtime update with its updater-owned fleet restart, payload transaction, continuity-guard activation, then health proof. Do not reorder it and do not copy individual files.

1. Require Maya's literal PASS for the exact freeze commit/tree, source commit/tree, manifest SHA-256, archive SHA-256, and all payload hashes. Require CV-A01 `t_13b90c53` and the exact-task dispatch prerequisite to remain accepted. Run the frozen isolated launchd canary before any mutation and require two successful observations at least 60 seconds apart, successful bootout, and post-bootout absence proof. Drain one-shot workers for every affected profile.
2. Set the fixed transaction paths and identities:

   ```sh
   ROOT=/Users/rook/.hermes
   RUNTIME=/Users/rook/.hermes/hermes-agent
   SOURCE_CHANNEL=candidate/codevolt-control-plane-rc4-continuity-source
   SOURCE_REF=refs/heads/candidate/codevolt-control-plane-rc4-continuity-source
   FREEZE_REF=refs/heads/candidate/codevolt-control-plane-rc4-continuity-freeze
   TX=/Users/rook/.hermes/release-preimages/cv-control-plane-2026-09-05-rc4-remediation1
   MANIFEST="$TX/candidate/manifest.json"
   ARCHIVE="$TX/candidate/release.tar"
   REVIEW_RECEIPT="$TX/candidate/security-review-receipt.json"
   ```

   `TX` must not exist. Create it mode 0700 and export the two frozen files with these literal commands:

   ```sh
   test ! -e "$TX"
   install -d -m 0700 "$TX/candidate" "$TX/payload-preimage" "$TX/state-evidence"
   git -C "$RUNTIME" show "$FREEZE_REF:operations/codevolt-control-plane/release/manifest.json" > "$MANIFEST"
   git -C "$RUNTIME" show "$FREEZE_REF:operations/codevolt-control-plane/release/release.tar" > "$ARCHIVE"
   shasum -a 256 "$MANIFEST" "$ARCHIVE"
   ```

   Copy Maya's exact attached JSON receipt to `$REVIEW_RECEIPT` without editing it. Require its verdict to be `PASS` and its `freeze_commit`, `freeze_tree`, `source_commit`, `source_tree`, `manifest_sha256`, and `archive_sha256` values to identify this candidate. Compare both printed digests with that receipt before any live change.
3. Capture complete preimages while the continuity guard is unloaded and before runtime or payload mutation:

   - Require `git -C "$RUNTIME" status --porcelain=v1 --untracked-files=all` to be empty. Record runtime HEAD/tree, source/freeze ref values, `git stash list`, remote URL, and `git ls-remote origin "$SOURCE_REF"`.
   - Archive `$RUNTIME/venv` with modes and symlinks, record the archive SHA-256, and record hashes/modes for every tracked runtime path. This is the dependency/runtime preimage; do not include or restore `.git/worktrees`.
   - For every manifest destination, record exact relative path, existence/absence, bytes, mode, length, and SHA-256 in `$TX/payload-preimage`; verify each existing backup with both `shasum -a 256` and `cmp`.
   - Record which of `default, clara, daniel, elias, hannah, maya, nova, oliver, rook, sophie` gateways were running and their PID/birth identities. Record whether `com.codevolt.continuity-guard` was loaded/running.
   - Copy the continuity schema-state file and relevant logs into `$TX/state-evidence`, with hashes. These evidence copies are immutable; candidate state produced later is preserved separately on rollback.
4. Prove the channel, then perform the supported runtime update exactly:

   ```sh
   EXPECTED_FREEZE_COMMIT=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["freeze_commit"])' "$REVIEW_RECEIPT")
   EXPECTED_FREEZE_TREE=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["freeze_tree"])' "$REVIEW_RECEIPT")
   test "$(git -C "$RUNTIME" rev-parse --verify "$FREEZE_REF^{commit}")" = "$EXPECTED_FREEZE_COMMIT"
   test "$(git -C "$RUNTIME" rev-parse --verify "$FREEZE_REF^{tree}")" = "$EXPECTED_FREEZE_TREE"
   test "$(git -C "$RUNTIME" rev-parse --verify "$SOURCE_REF^{commit}")" = "$(python "$RUNTIME/scripts/control_plane_release.py" --help >/dev/null 2>&1; python -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_commit"])' "$MANIFEST")"
   test "$(git -C "$RUNTIME" ls-remote --exit-code origin "$SOURCE_REF" | cut -f1)" = "$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_commit"])' "$MANIFEST")"
   "$RUNTIME/venv/bin/hermes" update --branch "$SOURCE_CHANNEL" --yes --keep-stash
   ```

   Immediately require runtime HEAD/tree to equal the manifest source commit/tree and require the checkout to be clean. Any update failure begins rollback; payload installation must not run.
5. Install the frozen six-destination payload with the literal transactional command:

   ```sh
   "$RUNTIME/venv/bin/python" "$RUNTIME/scripts/control_plane_release.py" install \
     --archive "$ARCHIVE" \
     --root "$ROOT" > "$TX/install-receipt.json"
   ```

   The installer stages beside each target, atomically replaces only byte/mode differences, and restores all changed destinations in reverse order on any exception. Require `verdict=PASS`, `destinations_installed=6`, and a changed count consistent with the preimage comparison.
6. Read back every manifest destination and require exact path, existence, mode, byte length, SHA-256, and `cmp` against the archive payload. Run the installed-shape gate against the manifest source commit/tree. Stop on any mismatch.
7. The supported updater in step 4 owns the Hermes fleet restart for an actual runtime code change; do not issue a second gateway restart after payload installation. Require its plan/readback to cover every gateway recorded running in step 3, including Nova, while preserving stopped profiles as stopped. If the runtime commit/tree was already exact and the updater performed no code/path change, require gateway PID/birth identities to remain unchanged. Reload `com.codevolt.continuity-guard` only if it was loaded before and installation changed the guard/helper/plist path, bytes, or mode; otherwise preserve its prior loaded/running state without gratuitous reload.
8. Run the live regression:

   ```sh
   "$RUNTIME/venv/bin/python" -m pytest -q "$ROOT/release-checks/test_codevolt_continuity_guard.py"
   ```

9. Observe at least one complete interval of at least 60 seconds. Require fresh schema-5 state with `reconciled=true`, `status` in `SUCCESS_DECISIONS`, no board-read error, no deprecated `kanban_db.connect_closing` warning, exact post-activation process state, and no unplanned destination or profile change. A fresh HERMES_HOME fallback warning blocks completion and does not authorize config/plist edits.

## Exact reverse rollback contract

Rollback begins at the first failed gate and runs in strict reverse activation order.

1. Preserve the failed candidate schema-state and logs under `$TX/state-evidence/failed-candidate`; never delete them and never mutate a board.
2. Unload the continuity guard if activation loaded/reloaded it. Stop only gateway profiles restarted by the supported updater, in reverse of the updater's recorded order.
3. Restore every changed manifest destination from `$TX/payload-preimage` in exact reverse dependency/install order. Remove only destinations whose recorded preimage was absent. Restore original modes. Verify every restored file using both `cmp` and `shasum -a 256`, then require the complete payload preimage digest.
4. Restore the runtime checkout to the recorded preimage commit with `git reset --hard <recorded-pre-runtime-commit>`; restore the archived preimage venv by atomic directory replacement; verify runtime commit/tree, tracked-path inventory, venv archive digest, stash list, remote URL, and clean status. Do not restore `.git/worktrees` or delete update evidence/snapshots.
5. Move the candidate schema-5 state aside into evidence, then restore the exact pre-activation state bytes/mode when the preimage existed, or restore absence when it did not. Preserve pending alerts and logs.
   The activation-step-5 preimage is schema 4; this candidate writes schema 5.
6. Restart only gateways recorded running before activation, in the original fixed profile order. Restore the continuity guard's prior loaded/running state only after all runtime, payload, and state preimages verify.
7. Observe a full interval of at least 60 seconds and require exact preimage runtime/payload/process state, healthy prior-version behavior, preserved board/database state, and no transaction residue. Any mismatch leaves rollback incomplete and affected services stopped for Rook/Oliver escalation.

## Explicit non-actions

Candidate construction performs no live installation, updater invocation, dispatch, message send, launchd operation, gateway restart, configuration change, credential access, board mutation, VPS action, website action, or publication. Activation remains separately authorized operator work after independent PASS.
