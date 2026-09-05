"""Real-subprocess SIGKILL tests for the production distribution entrypoint.

Every test here spawns the *actual* repository-owned production entrypoint
(``scripts/install_work_claims.py``) in a real child process pointed at a
throwaway Hermes home, parks that process at one specific durable transition,
and kills it with ``SIGKILL`` -- no in-process exception injection, no
``try``/``finally`` cleanup, no interpreter shutdown hooks. The child is
gone the instant the signal lands, exactly like a power loss or an OOM kill,
so what remains on disk is genuine crash evidence.

The parked checkpoints cover every durable transition ``distribute()`` makes:

1. ``during_marker_persistence`` -- inside the marker write, after the temp
                            file is durable and before the rename publishes
                            it: the marker's final name never existed.
2. ``after_marker``      -- transaction marker written, nothing staged yet.
3. ``mid_staging``       -- staging directory half-populated, no swap yet.
4. ``between_renames``   -- destination renamed aside, staged directory not
                            yet renamed in: the one window where a
                            destination is legitimately absent.
5. ``after_first_swap``  -- one profile swapped, the rest untouched.
6. ``after_all_swaps``   -- every profile swapped, marker not yet removed.

After each kill the test asserts the on-disk state, then proves that
``recover()`` (or simply re-running the real entrypoint, which recovers
first) returns every profile to exactly its pre-transaction content and
removes the marker and every artifact.

A pre-existing destination is seeded with a ``PRE_EXISTING`` sentinel file
that is not part of the manifest set. Because the distributed bytes are
fixed by the manifest, that file is the only way to tell "rolled back to the
old directory" apart from "left holding the new one" -- it survives a
rollback and cannot survive a completed swap.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from . import installer

_CANDIDATE_DIR = Path(__file__).resolve().parent
_ENTRYPOINT_PATH = _CANDIDATE_DIR.parents[1] / "scripts" / "install_work_claims.py"

_PRE_EXISTING = "PRE_EXISTING"

# Written to a temp file and run as the child process. It loads the real
# entrypoint by file location, wraps one durable transition of the installer
# implementation the entrypoint loaded so the process parks there, and lets
# the parent deliver SIGKILL at that exact point.
_HARNESS_SOURCE = '''\
"""Test harness: park the real production entrypoint at one durable transition."""
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

entrypoint_path, source, root, profiles_json, sentinel, checkpoint = sys.argv[1:7]
source = Path(source)
root = Path(root)
profiles = json.loads(profiles_json)
sentinel = Path(sentinel)


def park():
    sentinel.write_text("reached", encoding="utf-8")
    while True:
        time.sleep(0.02)


spec = importlib.util.spec_from_file_location("_entrypoint_under_kill", entrypoint_path)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
entry.MIGRATION_SOURCE_INSTALLER = None

impl = entry.load_installer(source)
# main() resolves load_installer through module globals, so the process runs
# the same module object the checkpoint below is attached to.
entry.load_installer = lambda _source: impl

state = {"swaps": 0}

if checkpoint == "after_marker":
    original_write_marker = impl._write_txn_marker

    def write_marker_then_park(*args, **kwargs):
        original_write_marker(*args, **kwargs)
        park()

    impl._write_txn_marker = write_marker_then_park

elif checkpoint == "during_marker_persistence":
    # Inside _write_txn_marker: the temp file has been written and fsynced,
    # and the rename that publishes it under the marker's own name has not
    # happened. This is the window that makes marker persistence atomic --
    # park in it and the marker's final name was never created at all.
    class MarkerOsProxy:
        def __getattr__(self, name):
            return getattr(os, name)

        def replace(self, src, dst, *args, **kwargs):
            if Path(src).name.startswith(".work_claims_install_txn.tmp-"):
                park()
            return os.replace(src, dst, *args, **kwargs)

    impl.os = MarkerOsProxy()

elif checkpoint == "mid_staging":
    class ShutilProxy:
        def __getattr__(self, name):
            return getattr(shutil, name)

        def copy2(self, src, dst, *args, **kwargs):
            if Path(dst).name == "core.py":
                park()
            return shutil.copy2(src, dst, *args, **kwargs)

    impl.shutil = ShutilProxy()

elif checkpoint == "between_renames":
    class OsProxy:
        def __getattr__(self, name):
            return getattr(os, name)

        def replace(self, src, dst, *args, **kwargs):
            if Path(src).name.startswith(".work_claims.staging-"):
                park()
            return os.replace(src, dst, *args, **kwargs)

    impl.os = OsProxy()

elif checkpoint in ("after_first_swap", "after_all_swaps"):
    original_swap_in = impl._swap_in
    target = 1 if checkpoint == "after_first_swap" else len(profiles)

    def swap_then_park(destination, staged):
        previous = original_swap_in(destination, staged)
        state["swaps"] += 1
        if state["swaps"] == target:
            park()
        return previous

    impl._swap_in = swap_then_park

elif checkpoint != "none":
    raise SystemExit("unknown checkpoint: " + checkpoint)

entry.main(source=source, root=root, profiles=profiles)
'''


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _KillRecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.manifest = installer.load_manifest(_CANDIDATE_DIR)

        # A canonical source: the manifest-approved set plus installer.py,
        # which is what the entrypoint loads its implementation from.
        self.source = self.tmp / "canonical"
        self.source.mkdir()
        for name in [*self.manifest["files"], installer.MANIFEST_FILENAME, "installer.py"]:
            shutil.copy2(_CANDIDATE_DIR / name, self.source / name)

        self.harness = self.tmp / "kill_harness.py"
        self.harness.write_text(_HARNESS_SOURCE, encoding="utf-8")

        self.root = self.tmp / "home"
        self.spawn_count = 0

    def tearDown(self):
        self.temp.cleanup()

    # -- state helpers -------------------------------------------------
    def _destination(self, profile: str) -> Path:
        return self.root / "profiles" / profile / "plugins" / "work_claims"

    def _marker(self) -> Path:
        return self.root / installer._TXN_MARKER_FILENAME

    def _temp_markers(self) -> list[Path]:
        """Same-directory temp files the marker write renames from."""
        if not self.root.is_dir():
            return []
        return sorted(self.root.glob(f"{installer._TXN_MARKER_TMP_PREFIX}*"))

    def _seed_pre_existing(self, profiles: list[str]) -> None:
        for name in profiles:
            destination = self._destination(name)
            destination.mkdir(parents=True)
            (destination / _PRE_EXISTING).write_text(name, encoding="utf-8")

    def _assert_pre_transaction_state(self, profiles: list[str]) -> None:
        for name in profiles:
            destination = self._destination(name)
            self.assertTrue(destination.is_dir(), f"{name}: destination missing after recovery")
            self.assertEqual(
                (destination / _PRE_EXISTING).read_text(encoding="utf-8"),
                name,
                f"{name}: destination is not the pre-transaction directory",
            )

    def _assert_installed(self, profiles: list[str]) -> None:
        for name in profiles:
            destination = self._destination(name)
            for filename, digest in self.manifest["files"].items():
                self.assertEqual(_sha256(destination / filename), digest)
            self.assertEqual(
                _sha256(destination / installer.MANIFEST_FILENAME),
                installer.APPROVED_MANIFEST_SHA256,
            )
            self.assertFalse((destination / _PRE_EXISTING).exists())
            self.assertFalse((destination / "installer.py").exists())

    def _assert_no_artifacts(self, profiles: list[str]) -> None:
        for name in profiles:
            plugins_dir = self._destination(name).parent
            if not plugins_dir.is_dir():
                continue
            self.assertEqual(list(plugins_dir.glob(f"{installer._PREVIOUS_PREFIX}*")), [])
            self.assertEqual(list(plugins_dir.glob(f"{installer._STAGING_PREFIX}*")), [])

    def _artifacts(self, profile: str, prefix: str) -> list[Path]:
        plugins_dir = self._destination(profile).parent
        return sorted(plugins_dir.glob(f"{prefix}*")) if plugins_dir.is_dir() else []

    # -- subprocess helpers --------------------------------------------
    def _command(self, checkpoint: str, profiles: list[str], sentinel: Path) -> list[str]:
        return [
            sys.executable,
            str(self.harness),
            str(_ENTRYPOINT_PATH),
            str(self.source),
            str(self.root),
            json.dumps(profiles),
            str(sentinel),
            checkpoint,
        ]

    def _kill_at(self, checkpoint: str, profiles: list[str]) -> None:
        """Run the real entrypoint until it parks at ``checkpoint``, then
        SIGKILL it. Returns once the child is reaped."""
        self.spawn_count += 1
        sentinel = self.tmp / f"sentinel-{self.spawn_count}-{checkpoint}"
        proc = subprocess.Popen(
            self._command(checkpoint, profiles, sentinel),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 60
        try:
            while not sentinel.exists():
                if proc.poll() is not None:
                    _, err = proc.communicate()
                    self.fail(
                        f"harness exited (rc={proc.returncode}) before reaching "
                        f"{checkpoint}: {err.decode(errors='replace')}"
                    )
                if time.monotonic() > deadline:
                    self.fail(f"harness never reached checkpoint {checkpoint}")
                time.sleep(0.01)
            os.kill(proc.pid, signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=30)
        self.assertEqual(
            proc.returncode, -signal.SIGKILL, "child must die by signal, not by clean exit"
        )

    def _run_entrypoint_to_completion(self, profiles: list[str]) -> str:
        self.spawn_count += 1
        sentinel = self.tmp / f"sentinel-{self.spawn_count}-none"
        proc = subprocess.Popen(
            self._command("none", profiles, sentinel),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, err.decode(errors="replace"))
        return out.decode(errors="replace")


class ProductionEntrypointSubprocessTest(_KillRecoveryTestCase):
    def test_real_entrypoint_subprocess_installs_the_manifest_set(self):
        profiles = ["alpha", "bravo"]
        output = self._run_entrypoint_to_completion(profiles)

        self._assert_installed(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())
        for name in profiles:
            self.assertIn(f"installed {name}:", output)

    def test_real_entrypoint_subprocess_is_idempotent(self):
        profiles = ["alpha"]
        self._run_entrypoint_to_completion(profiles)
        first = {
            name: _sha256(self._destination("alpha") / name) for name in self.manifest["files"]
        }
        self._run_entrypoint_to_completion(profiles)
        second = {
            name: _sha256(self._destination("alpha") / name) for name in self.manifest["files"]
        }
        self.assertEqual(first, second)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())


class InstallerKillRecoveryTest(_KillRecoveryTestCase):
    def test_kill_during_marker_persistence_leaves_no_marker_to_recover(self):
        """The window inside ``_write_txn_marker`` itself.

        Persisting the marker is the first durable thing ``distribute()`` does,
        and the marker is the only record of who participated in a run that
        dies. Writing it byte-by-byte into its own final name would make this
        window fatal rather than recoverable: an empty or truncated file at the
        marker's path is exactly what ``_read_marker_participants`` refuses to
        guess about, so every later run would abort behind it. Publishing it by
        rename instead means a kill here leaves the marker's name untouched --
        which is the truth, since no destination has been touched yet.
        """
        profiles = ["alpha", "bravo"]
        self._seed_pre_existing(profiles)

        self._kill_at("during_marker_persistence", profiles)

        # No marker, so nothing claims a transaction was in flight.
        self.assertFalse(
            self._marker().exists(),
            "the marker's final name was created before it was complete",
        )
        # The crash evidence is a temp file, and it is complete: the bytes were
        # fsynced before the rename was attempted.
        strays = self._temp_markers()
        self.assertEqual(len(strays), 1, f"expected one temp marker, found {strays}")
        self.assertEqual(
            json.loads(strays[0].read_text(encoding="utf-8"))["profiles"], profiles
        )
        # Nothing was staged or swapped: the marker precedes all of it.
        self._assert_no_artifacts(profiles)
        self._assert_pre_transaction_state(profiles)

        # Recovery is deterministic without being told who participated -- the
        # hardest case, since no marker names them and the caller offers none.
        self.assertEqual(installer.recover(self.root, []), [])
        self.assertEqual(self._temp_markers(), [], "temp marker survived recovery")
        self.assertFalse(self._marker().exists())
        self._assert_no_artifacts(profiles)
        self._assert_pre_transaction_state(profiles)

        # And the next real run proceeds normally, leaving nothing behind.
        self._run_entrypoint_to_completion(profiles)
        self._assert_installed(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())
        self.assertEqual(self._temp_markers(), [])

    def test_kill_after_marker_before_staging(self):
        profiles = ["alpha", "bravo"]
        self._seed_pre_existing(profiles)

        self._kill_at("after_marker", profiles)

        self.assertTrue(self._marker().exists(), "marker must survive the kill as evidence")
        self.assertEqual(
            json.loads(self._marker().read_text(encoding="utf-8"))["profiles"], profiles
        )
        self._assert_no_artifacts(profiles)
        self._assert_pre_transaction_state(profiles)

        self.assertEqual(installer.recover(self.root, profiles), [])
        self.assertFalse(self._marker().exists())
        self._assert_no_artifacts(profiles)
        self._assert_pre_transaction_state(profiles)

        self._run_entrypoint_to_completion(profiles)
        self._assert_installed(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())

    def test_kill_mid_staging_leaves_destinations_untouched(self):
        profiles = ["alpha", "bravo"]
        self._seed_pre_existing(profiles)

        self._kill_at("mid_staging", profiles)

        self.assertTrue(self._marker().exists())
        staging = self._artifacts("alpha", installer._STAGING_PREFIX)
        self.assertEqual(len(staging), 1, "a half-populated staging directory must remain")
        self.assertFalse((staging[0] / "core.py").exists())
        self.assertEqual(self._artifacts("alpha", installer._PREVIOUS_PREFIX), [])
        self._assert_pre_transaction_state(profiles)

        installer.recover(self.root, profiles)

        self.assertFalse(self._marker().exists())
        self._assert_no_artifacts(profiles)
        self._assert_pre_transaction_state(profiles)

    def test_kill_between_the_two_swap_renames_leaves_a_recoverable_absent_destination(self):
        profiles = ["alpha"]
        self._seed_pre_existing(profiles)

        self._kill_at("between_renames", profiles)

        destination = self._destination("alpha")
        self.assertFalse(destination.exists(), "the kill landed in the transient window")
        previous = self._artifacts("alpha", installer._PREVIOUS_PREFIX)
        self.assertEqual(len(previous), 1)
        self.assertTrue((previous[0] / _PRE_EXISTING).is_file())
        self.assertTrue(self._marker().exists())

        self.assertEqual(installer.recover(self.root, profiles), ["alpha"])

        self._assert_pre_transaction_state(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())

    def test_rerunning_the_entrypoint_recovers_a_crash_between_the_swap_renames(self):
        profiles = ["alpha"]
        self._seed_pre_existing(profiles)
        self._kill_at("between_renames", profiles)
        self.assertFalse(self._destination("alpha").exists())

        # No explicit recover() call: distribute() must recover first.
        self._run_entrypoint_to_completion(profiles)

        self._assert_installed(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())

    def test_kill_after_first_swap_rolls_every_profile_back(self):
        profiles = ["alpha", "bravo"]
        self._seed_pre_existing(profiles)

        self._kill_at("after_first_swap", profiles)

        alpha = self._destination("alpha")
        self.assertTrue(alpha.is_dir())
        self.assertFalse(
            (alpha / _PRE_EXISTING).exists(), "alpha's swap completed before the kill"
        )
        self.assertEqual(len(self._artifacts("alpha", installer._PREVIOUS_PREFIX)), 1)
        self.assertTrue((self._destination("bravo") / _PRE_EXISTING).is_file())
        self.assertTrue(self._marker().exists())

        resolved = installer.recover(self.root, profiles)

        self.assertEqual(resolved, ["alpha"])
        self._assert_pre_transaction_state(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())

    def test_kill_after_all_swaps_before_marker_deletion_rolls_all_back(self):
        profiles = ["alpha", "bravo", "charlie"]
        self._seed_pre_existing(profiles)

        self._kill_at("after_all_swaps", profiles)

        self.assertTrue(self._marker().exists())
        for name in profiles:
            self.assertFalse((self._destination(name) / _PRE_EXISTING).exists())
            self.assertEqual(len(self._artifacts(name, installer._PREVIOUS_PREFIX)), 1)

        # The caller knows about no profiles at all: the marker written by the
        # killed process is the authoritative participant list.
        resolved = installer.recover(self.root, [])

        self.assertEqual(sorted(resolved), sorted(profiles))
        self._assert_pre_transaction_state(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())

    def test_recovery_after_a_kill_is_idempotent(self):
        profiles = ["alpha", "bravo"]
        self._seed_pre_existing(profiles)
        self._kill_at("after_first_swap", profiles)

        installer.recover(self.root, profiles)
        self.assertEqual(installer.recover(self.root, profiles), [])

        self._assert_pre_transaction_state(profiles)
        self._assert_no_artifacts(profiles)
        self.assertFalse(self._marker().exists())

    def test_marker_from_a_killed_run_survives_until_recovery_completes(self):
        profiles = ["alpha", "bravo"]
        self._seed_pre_existing(profiles)
        self._kill_at("after_all_swaps", profiles)

        marker = self._marker()
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(recorded["profiles"], profiles)
        self.assertEqual(recorded["status"], "in-progress")
        self.assertEqual(
            recorded["manifest_sha256"],
            _sha256(self.source / installer.MANIFEST_FILENAME),
        )

        # A recovery that cannot finish must leave the evidence in place.
        stuck = self._destination("alpha").parent / f"{installer._STAGING_PREFIX}stuck"
        stuck.mkdir()
        real_rmtree = shutil.rmtree

        def refuse_to_remove(path, *args, **kwargs):
            if Path(path).name.endswith("stuck"):
                raise OSError("cannot remove")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch(
            "plugins.work_claims.installer.shutil.rmtree", side_effect=refuse_to_remove
        ):
            with self.assertRaises(installer.RecoveryError):
                installer.recover(self.root, profiles)

        self.assertTrue(marker.exists())
        real_rmtree(stuck)
        installer.recover(self.root, profiles)
        self.assertFalse(marker.exists())
        self._assert_pre_transaction_state(profiles)


if __name__ == "__main__":
    unittest.main()
