"""Installer distribution tests -- run entirely against temp directories.

Verifies ``installer.distribute`` (the fail-closed, hash-provenanced mirror
of the live ``~/.hermes/scripts/install_work_claims.py``): pinned production-
installer and manifest provenance, complete source validation before any
destination is touched, per-profile staging on the destination's own
filesystem, hash verification, atomic swap with exact readback, all-or-
nothing multi-profile rollback, and recovery from an interrupted run. Never
touches any real Hermes home, and the real production installer is only ever
imported by file path with its module-level globals monkeypatched to a temp
directory -- it is never executed against a real profile.
"""
from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from . import installer

_CANDIDATE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _CANDIDATE_DIR.parents[1]
# The repository-owned replacement for the production entrypoint.
_ENTRYPOINT_PATH = _REPO_ROOT / "scripts" / "install_work_claims.py"
# The pre-existing live installer the replacement migrates from. Read-only.
_MIGRATION_SOURCE_PATH = Path("/Users/rook/.hermes/scripts/install_work_claims.py")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_migration_source_module():
    spec = importlib.util.spec_from_file_location(
        "_production_install_work_claims_readonly", _MIGRATION_SOURCE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_repo_entrypoint(module_name: str = "_repo_install_work_claims_under_test"):
    spec = importlib.util.spec_from_file_location(module_name, _ENTRYPOINT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Never let a test's loaded copy point at a real Hermes home.
    module.MIGRATION_SOURCE_INSTALLER = None
    return module


class _InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self._manifest = installer.load_manifest(_CANDIDATE_DIR)

    def tearDown(self):
        self.temp.cleanup()

    def _build_valid_source(self, dest: Path | None = None) -> Path:
        dest = dest if dest is not None else self.source
        dest.mkdir(parents=True, exist_ok=True)
        for name in [*self._manifest["files"], installer.MANIFEST_FILENAME]:
            shutil.copy2(_CANDIDATE_DIR / name, dest / name)
        return dest

    def _build_canonical_source(self, dest: Path | None = None) -> Path:
        """A valid source that also carries ``installer.py`` -- i.e. what the
        production entrypoint loads its implementation from."""
        dest = self._build_valid_source(dest)
        shutil.copy2(_CANDIDATE_DIR / "installer.py", dest / "installer.py")
        return dest

    def _destination(self, root: Path, profile: str) -> Path:
        return root / "profiles" / profile / "plugins" / "work_claims"


class ProvenancePinTest(_InstallerTestCase):
    def test_pinned_production_installer_hash_matches_the_admitted_migration_source(self):
        if not _MIGRATION_SOURCE_PATH.is_file():
            self.skipTest("migration-source installer not present in this environment")
        self.assertEqual(_sha256(_MIGRATION_SOURCE_PATH), installer.PRODUCTION_INSTALLER_SHA256)
        installer.verify_production_installer_provenance(_MIGRATION_SOURCE_PATH)

    def test_entrypoint_pins_the_canonical_installer_module_hash(self):
        entry = _load_repo_entrypoint()
        self.assertEqual(
            entry.CANONICAL_INSTALLER_SHA256,
            _sha256(_CANDIDATE_DIR / "installer.py"),
            "the entrypoint's pin must match the reviewed installer.py bytes",
        )

    def test_manifest_has_no_self_referential_commit_field(self):
        self.assertNotIn(
            "generated_from_candidate_commit",
            self._manifest,
            "MANIFEST must not record the commit that contains it; commit "
            "identity belongs in PROVENANCE.md, outside the distributed set",
        )

    def test_manifest_keys_are_exactly_the_allowed_identity_set(self):
        allowed = {
            "plugin_version",
            "production_installer_path",
            "production_installer_sha256",
            "files",
        }
        self.assertEqual(set(self._manifest), allowed)

    def test_provenance_attestation_lives_outside_the_distributed_manifest(self):
        provenance = _CANDIDATE_DIR / "PROVENANCE.md"
        self.assertTrue(provenance.is_file())
        self.assertNotIn("PROVENANCE.md", self._manifest["files"])
        text = provenance.read_text(encoding="utf-8")
        self.assertIn("e37cd649b6cab76a8fec5b863a82b4cd1e514d6b", text)
        self.assertIn("03602198995689c74943145820831f57dd77ee99", text)
        self.assertIn("07d3ac092addff9e2401e400a81431b76eac1549", text)

    def test_pinned_manifest_hash_matches_the_committed_manifest(self):
        self.assertEqual(
            _sha256(_CANDIDATE_DIR / installer.MANIFEST_FILENAME), installer.APPROVED_MANIFEST_SHA256
        )

    def test_manifest_covers_the_complete_plugin_source_set(self):
        self.assertEqual(
            set(self._manifest["files"]),
            {"plugin.yaml", "__init__.py", "core.py", "README.md", "test_work_claims.py"},
        )

    def test_distribute_rejects_a_drifted_production_installer_path(self):
        self._build_valid_source()
        with self.assertRaises(installer.ProvenanceError):
            installer.distribute(
                self.source,
                self.root / "dest",
                ["alpha"],
                production_installer_path=_CANDIDATE_DIR / "core.py",
            )
        self.assertFalse((self.root / "dest").exists())


class EntrypointEquivalenceTest(_InstallerTestCase):
    def test_repo_entrypoint_distributes_byte_identically_to_the_installer_module(self):
        entry = _load_repo_entrypoint()
        source = self._build_canonical_source()
        profiles = ["entry_alpha", "entry_bravo"]

        entry_root = self.root / "entrypoint"
        installed = entry.main(source=source, root=entry_root, profiles=profiles)
        self.assertEqual(set(installed), set(profiles))

        candidate_root = self.root / "candidate"
        installer.distribute(source, candidate_root, profiles)

        for name in profiles:
            entry_dest = self._destination(entry_root, name)
            cand_dest = self._destination(candidate_root, name)
            for filename in [*self._manifest["files"], installer.MANIFEST_FILENAME]:
                self.assertEqual(_sha256(entry_dest / filename), _sha256(cand_dest / filename))
            # The entrypoint distributes only the manifest set -- installer.py
            # itself is canonical-source-only and is never copied to a profile.
            self.assertFalse((entry_dest / "installer.py").exists())

    def test_repo_entrypoint_contains_no_distribution_logic_of_its_own(self):
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        for forbidden in ("shutil.copy", "shutil.rmtree", "os.replace", "mkdir("):
            self.assertNotIn(
                forbidden,
                text,
                f"the entrypoint must delegate, not implement ({forbidden!r} found)",
            )

    def test_repo_entrypoint_refuses_a_drifted_canonical_installer(self):
        entry = _load_repo_entrypoint()
        source = self._build_canonical_source()
        with open(source / "installer.py", "ab") as fh:
            fh.write(b"\n# tampered\n")

        with self.assertRaises(entry.EntrypointError):
            entry.main(source=source, root=self.root / "dest", profiles=["alpha"])
        self.assertFalse((self.root / "dest").exists())

    def test_repo_entrypoint_refuses_a_missing_canonical_installer(self):
        entry = _load_repo_entrypoint()
        source = self._build_valid_source()  # no installer.py
        with self.assertRaises(entry.EntrypointError):
            entry.main(source=source, root=self.root / "dest", profiles=["alpha"])
        self.assertFalse((self.root / "dest").exists())

    def test_repo_entrypoint_admits_only_a_known_installer_at_the_production_path(self):
        entry = _load_repo_entrypoint()
        source = self._build_canonical_source()
        impl = entry.load_installer(source)

        admitted = self.root / "admitted_install_work_claims.py"
        if _MIGRATION_SOURCE_PATH.is_file():
            shutil.copy2(_MIGRATION_SOURCE_PATH, admitted)
            self.assertEqual(
                entry.verify_migration_source(impl, admitted, self_path=_ENTRYPOINT_PATH),
                "admitted-migration-source",
            )

        already_migrated = self.root / "migrated_install_work_claims.py"
        shutil.copy2(_ENTRYPOINT_PATH, already_migrated)
        self.assertEqual(
            entry.verify_migration_source(impl, already_migrated, self_path=_ENTRYPOINT_PATH),
            "already-migrated",
        )

        self.assertEqual(
            entry.verify_migration_source(impl, self.root / "nothing.py", self_path=_ENTRYPOINT_PATH),
            "absent",
        )
        self.assertEqual(
            entry.verify_migration_source(impl, None, self_path=_ENTRYPOINT_PATH), "unset"
        )

        unknown = self.root / "unknown_install_work_claims.py"
        unknown.write_text("print('not a reviewed installer')\n", encoding="utf-8")
        with self.assertRaises(entry.EntrypointError):
            entry.verify_migration_source(impl, unknown, self_path=_ENTRYPOINT_PATH)


class ProductionRosterTest(_InstallerTestCase):
    """The default roster is a reviewed fact, not a hand-maintained list.

    A profile that enables ``work-claims`` but is absent from ``PROFILES``
    never receives the plugin, and nothing reports a problem: the run
    succeeds for every profile it was asked about, so a partial fleet looks
    exactly like a total one. That is how ``elias`` was missed. The roster is
    therefore pinned here, and the pin is read from the repository-owned
    entrypoint only -- these tests never consult a live profile directory,
    config or Hermes home.
    """

    # The intended production fleet. Adding or removing a profile is a
    # reviewed change: update this tuple in the same commit as PROFILES.
    EXPECTED_PRODUCTION_ROSTER = (
        "rook",
        "hannah",
        "clara",
        "daniel",
        "maya",
        "oliver",
        "sophie",
        "elias",
    )

    def test_default_roster_is_exactly_the_pinned_production_fleet(self):
        entry = _load_repo_entrypoint()
        self.assertEqual(tuple(entry.PROFILES), self.EXPECTED_PRODUCTION_ROSTER)

    def test_default_roster_includes_elias(self):
        entry = _load_repo_entrypoint()
        self.assertIn(
            "elias",
            tuple(entry.PROFILES),
            "elias enables work-claims and must be in the production roster; "
            "omitting it silently distributes to a partial fleet while "
            "reporting a successful run",
        )

    def test_default_roster_names_each_profile_once(self):
        entry = _load_repo_entrypoint()
        profiles = tuple(entry.PROFILES)
        self.assertEqual(
            sorted(profiles), sorted(set(profiles)), "a duplicated profile is a roster edit error"
        )

    def test_an_unparameterised_run_distributes_to_the_whole_pinned_roster(self):
        """The pin is only worth having if ``main()``'s default actually uses it."""
        entry = _load_repo_entrypoint()
        entry.SOURCE = self._build_canonical_source()
        entry.ROOT = self.root / "fleet"

        installed = entry.main()

        self.assertEqual(tuple(installed), self.EXPECTED_PRODUCTION_ROSTER)
        for name in self.EXPECTED_PRODUCTION_ROSTER:
            destination = self._destination(entry.ROOT, name)
            self.assertEqual(installed[name], destination)
            for filename in [*self._manifest["files"], installer.MANIFEST_FILENAME]:
                self.assertEqual(
                    _sha256(destination / filename),
                    _sha256(entry.SOURCE / filename),
                    f"{name} did not receive {filename}",
                )


class ModuleIdentityTest(_InstallerTestCase):
    def test_discovery_loaded_entrypoint_is_a_distinct_module_object(self):
        entry = _load_repo_entrypoint()
        self.assertIsNot(entry, installer)
        self.assertNotEqual(entry.__name__, installer.__name__)

    def test_loaded_impl_is_distinct_from_the_imported_installer_but_shares_pins(self):
        entry = _load_repo_entrypoint()
        source = self._build_canonical_source()
        impl = entry.load_installer(source)

        self.assertIsNot(impl, installer)
        self.assertEqual(impl.__name__, entry.IMPL_MODULE_NAME)
        self.assertNotEqual(impl.__name__, installer.__name__)
        self.assertEqual(impl.APPROVED_MANIFEST_SHA256, installer.APPROVED_MANIFEST_SHA256)
        self.assertEqual(impl.PRODUCTION_INSTALLER_SHA256, installer.PRODUCTION_INSTALLER_SHA256)

    def test_loading_the_impl_never_registers_it_in_sys_modules(self):
        entry = _load_repo_entrypoint()
        source = self._build_canonical_source()
        entry.load_installer(source)
        self.assertNotIn(entry.IMPL_MODULE_NAME, sys.modules)
        self.assertNotIn("_repo_install_work_claims_under_test", sys.modules)
        self.assertNotIn(str(_REPO_ROOT / "scripts"), sys.path)

    def test_impl_load_refuses_a_conflicting_pre_registered_module_name(self):
        entry = _load_repo_entrypoint()
        source = self._build_canonical_source()
        sys.modules[entry.IMPL_MODULE_NAME] = types.ModuleType(entry.IMPL_MODULE_NAME)
        self.addCleanup(sys.modules.pop, entry.IMPL_MODULE_NAME, None)
        with self.assertRaises(entry.EntrypointError):
            entry.load_installer(source)


class MigrationSourceEquivalenceTest(_InstallerTestCase):
    def test_distribution_matches_the_real_production_installer_entrypoint(self):
        if not _MIGRATION_SOURCE_PATH.is_file():
            self.skipTest("migration-source installer not present in this environment")
        installer.verify_production_installer_provenance(_MIGRATION_SOURCE_PATH)

        production = _load_migration_source_module()
        if not hasattr(production, "FILES"):
            self.skipTest(
                "production installer is already the transactional replacement; "
                "legacy FILES equivalence is not applicable"
            )
        prod_root = self.root / "production"
        prod_source = prod_root / "plugins" / "work_claims"
        prod_source.mkdir(parents=True)
        for filename in production.FILES:
            shutil.copy2(_CANDIDATE_DIR / filename, prod_source / filename)

        profiles = ["equivalence_alpha", "equivalence_bravo"]
        # Only this in-memory loaded copy's globals are reassigned -- the
        # real module at _PRODUCTION_INSTALLER_PATH is never imported under
        # its real name and never executed against a real Hermes home.
        production.ROOT = prod_root
        production.SOURCE = prod_source
        production.PROFILES = tuple(profiles)
        production.main()

        candidate_root = self.root / "candidate"
        self._build_valid_source()
        installer.distribute(self.source, candidate_root, profiles)

        for name in profiles:
            prod_dest = self._destination(prod_root, name)
            cand_dest = self._destination(candidate_root, name)
            for filename in production.FILES:
                self.assertEqual(
                    _sha256(prod_dest / filename),
                    _sha256(cand_dest / filename),
                    f"{filename} diverged between the production installer entrypoint and the candidate installer",
                )


class SourceValidationTest(_InstallerTestCase):
    def test_missing_source_file_blocks_all_profiles(self):
        self._build_valid_source()
        (self.source / "core.py").unlink()

        dest_root = self.root / "dest"
        with self.assertRaises(installer.ProvenanceError):
            installer.distribute(self.source, dest_root, ["alpha", "bravo"])
        self.assertFalse(dest_root.exists())

    def test_hash_mismatch_in_source_blocks_distribution(self):
        self._build_valid_source()
        with open(self.source / "README.md", "ab") as fh:
            fh.write(b"\ntampered\n")

        dest_root = self.root / "dest"
        with self.assertRaises(installer.ProvenanceError):
            installer.distribute(self.source, dest_root, ["alpha"])
        self.assertFalse(dest_root.exists())

    def test_missing_manifest_blocks_distribution(self):
        self._build_valid_source()
        (self.source / installer.MANIFEST_FILENAME).unlink()

        dest_root = self.root / "dest"
        with self.assertRaises(installer.ProvenanceError):
            installer.distribute(self.source, dest_root, ["alpha"])
        self.assertFalse(dest_root.exists())

    def test_tampered_manifest_is_rejected_even_if_internally_consistent(self):
        self._build_valid_source()
        forged = {
            "files": {
                name: hashlib.sha256((self.source / name).read_bytes()).hexdigest()
                for name in self._manifest["files"]
            }
        }
        forged["files"]["README.md"] = hashlib.sha256(b"forged content").hexdigest()
        (self.source / installer.MANIFEST_FILENAME).write_text(json.dumps(forged))

        dest_root = self.root / "dest"
        with self.assertRaises(installer.ProvenanceError):
            installer.distribute(self.source, dest_root, ["alpha"])
        self.assertFalse(dest_root.exists())

    def test_rejects_a_symlinked_source_file(self):
        self._build_valid_source()
        real_readme = self.root / "real_readme.md"
        shutil.copy2(self.source / "README.md", real_readme)
        (self.source / "README.md").unlink()
        (self.source / "README.md").symlink_to(real_readme)

        dest_root = self.root / "dest"
        with self.assertRaises(installer.ProvenanceError):
            installer.distribute(self.source, dest_root, ["alpha"])
        self.assertFalse(dest_root.exists())


class SuccessfulDistributionTest(_InstallerTestCase):
    def test_distributes_hash_identical_copies_to_every_profile(self):
        self._build_valid_source()
        profiles = ["alpha", "bravo", "charlie"]
        dest_root = self.root / "dest"
        installed = installer.distribute(self.source, dest_root, profiles)

        self.assertEqual(set(installed), set(profiles))
        for name in profiles:
            destination = installed[name]
            self.assertEqual(destination, self._destination(dest_root, name))
            for filename, expected in self._manifest["files"].items():
                self.assertEqual(_sha256(destination / filename), expected)
                self.assertEqual(_sha256(destination / filename), _sha256(self.source / filename))
            self.assertEqual(_sha256(destination / installer.MANIFEST_FILENAME), installer.APPROVED_MANIFEST_SHA256)
            self.assertFalse(any(p.is_symlink() for p in destination.rglob("*")))

    def test_distribution_is_deterministic_across_repeated_runs(self):
        self._build_valid_source()
        dest_root = self.root / "dest"
        installer.distribute(self.source, dest_root, ["alpha"])
        first = {
            name: _sha256(self._destination(dest_root, "alpha") / name) for name in self._manifest["files"]
        }
        installer.distribute(self.source, dest_root, ["alpha"])
        second = {
            name: _sha256(self._destination(dest_root, "alpha") / name) for name in self._manifest["files"]
        }
        self.assertEqual(first, second)
        # No leftover staging/backup/marker after two clean runs.
        plugins_dir = self._destination(dest_root, "alpha").parent
        self.assertEqual(list(plugins_dir.glob(".work_claims.previous-*")), [])
        self.assertEqual(list(plugins_dir.glob(".work_claims.staging-*")), [])
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())

    def test_replaces_a_pre_existing_symlinked_destination(self):
        self._build_valid_source()
        dest_root = self.root / "dest"
        destination_parent = self._destination(dest_root, "alpha").parent
        destination_parent.mkdir(parents=True)
        bait = self.root / "bait"
        bait.mkdir()
        (destination_parent / "work_claims").symlink_to(bait)

        installer.distribute(self.source, dest_root, ["alpha"])

        destination = self._destination(dest_root, "alpha")
        self.assertFalse(destination.is_symlink())
        for name, expected in self._manifest["files"].items():
            self.assertEqual(_sha256(destination / name), expected)


class RollbackAndReadbackTest(_InstallerTestCase):
    def test_distribute_rolls_back_all_profiles_when_one_fails(self):
        self._build_valid_source()
        dest_root = self.root / "dest"
        real_copy2 = shutil.copy2

        def flaky_copy2(src, dst, *a, **k):
            if "bravo" in str(dst) and Path(dst).name == "core.py":
                raise OSError("simulated staging failure for bravo")
            return real_copy2(src, dst, *a, **k)

        with mock.patch("plugins.work_claims.installer.shutil.copy2", side_effect=flaky_copy2):
            with self.assertRaises(installer.InstallTransactionError):
                installer.distribute(self.source, dest_root, ["alpha", "bravo"])

        # All-or-nothing: alpha must not have been left installed either,
        # even though its own staging/swap succeeded before bravo failed.
        self.assertFalse(self._destination(dest_root, "alpha").exists())
        self.assertFalse(self._destination(dest_root, "bravo").exists())
        for name in ("alpha", "bravo"):
            plugins_dir = self._destination(dest_root, name).parent
            if plugins_dir.exists():
                self.assertEqual(list(plugins_dir.glob(".work_claims.previous-*")), [])
                self.assertEqual(list(plugins_dir.glob(".work_claims.staging-*")), [])
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())

    def test_distribute_rolls_back_upgrade_to_prior_installed_version(self):
        self._build_valid_source()
        dest_root = self.root / "dest"
        installer.distribute(self.source, dest_root, ["alpha", "bravo"])
        baseline = {
            name: {
                filename: _sha256(self._destination(dest_root, name) / filename)
                for filename in self._manifest["files"]
            }
            for name in ("alpha", "bravo")
        }

        real_copy2 = shutil.copy2

        def flaky_copy2(src, dst, *a, **k):
            if "bravo" in str(dst) and Path(dst).name == "core.py":
                raise OSError("simulated staging failure for bravo on re-run")
            return real_copy2(src, dst, *a, **k)

        with mock.patch("plugins.work_claims.installer.shutil.copy2", side_effect=flaky_copy2):
            with self.assertRaises(installer.InstallTransactionError):
                installer.distribute(self.source, dest_root, ["alpha", "bravo"])

        # readback: both profiles must be exactly back at the pre-attempt version.
        for name in ("alpha", "bravo"):
            destination = self._destination(dest_root, name)
            self.assertTrue(destination.is_dir())
            for filename, digest in baseline[name].items():
                self.assertEqual(_sha256(destination / filename), digest)
            plugins_dir = destination.parent
            self.assertEqual(list(plugins_dir.glob(".work_claims.previous-*")), [])
            self.assertEqual(list(plugins_dir.glob(".work_claims.staging-*")), [])
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())

    def test_interrupted_swap_self_heals_destination_within_the_same_call(self):
        self._build_valid_source()
        dest_root = self.root / "dest"
        installer.distribute(self.source, dest_root, ["alpha"])
        destination = self._destination(dest_root, "alpha")
        baseline = {name: _sha256(destination / name) for name in self._manifest["files"]}

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated crash between the move-aside and move-in renames")
            return real_replace(src, dst, *a, **k)

        with mock.patch("plugins.work_claims.installer.os.replace", side_effect=flaky_replace):
            with self.assertRaises(installer.InstallTransactionError):
                installer.distribute(self.source, dest_root, ["alpha"])

        self.assertTrue(destination.is_dir(), "destination must never be left missing after an interrupted swap")
        for name, digest in baseline.items():
            self.assertEqual(_sha256(destination / name), digest)
        plugins_dir = destination.parent
        self.assertEqual(list(plugins_dir.glob(".work_claims.previous-*")), [])
        self.assertEqual(list(plugins_dir.glob(".work_claims.staging-*")), [])


class CrashRecoveryTest(_InstallerTestCase):
    def _swapped_away_state(self, dest_root: Path, profile: str) -> Path:
        """Simulate a crash between the two swap renames: destination is
        missing, a `.previous-*` sibling holds the pre-crash content."""
        self._build_valid_source()
        installer.distribute(self.source, dest_root, [profile])
        destination = self._destination(dest_root, profile)
        previous = destination.parent / f"{installer._PREVIOUS_PREFIX}deadbeef"
        os.replace(destination, previous)
        return previous

    def test_recover_restores_destination_missing_a_completed_swap(self):
        dest_root = self.root / "dest"
        previous = self._swapped_away_state(dest_root, "alpha")
        destination = self._destination(dest_root, "alpha")
        self.assertFalse(destination.exists())

        resolved = installer.recover(dest_root, ["alpha"])

        self.assertEqual(resolved, ["alpha"])
        self.assertTrue(destination.is_dir())
        for name, expected in self._manifest["files"].items():
            self.assertEqual(_sha256(destination / name), expected)
        self.assertFalse(previous.exists())

    def test_recover_rolls_back_a_completed_profile_when_transaction_marker_present(self):
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha", "bravo"])

        alpha_dest = self._destination(dest_root, "alpha")
        alpha_previous = alpha_dest.parent / f"{installer._PREVIOUS_PREFIX}deadbeef"
        shutil.copytree(alpha_dest, alpha_previous)  # pre-crash-run content
        # alpha's swap "completed" during the crashed run; bravo was never reached.
        bravo_dest = self._destination(dest_root, "bravo")
        bravo_before = {name: _sha256(bravo_dest / name) for name in self._manifest["files"]}

        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(json.dumps({"status": "in-progress", "profiles": ["alpha", "bravo", "charlie"]}))

        resolved = installer.recover(dest_root, ["alpha", "bravo", "charlie"])

        self.assertIn("alpha", resolved)
        self.assertNotIn("bravo", resolved)
        self.assertFalse(alpha_previous.exists())
        self.assertFalse(marker.exists())
        for name, expected in self._manifest["files"].items():
            self.assertEqual(_sha256(alpha_dest / name), expected)
        for name, digest in bravo_before.items():
            self.assertEqual(_sha256(bravo_dest / name), digest)

    def test_recover_is_a_noop_cleanup_pass_with_nothing_to_recover(self):
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha"])
        destination = self._destination(dest_root, "alpha")
        before = {name: _sha256(destination / name) for name in self._manifest["files"]}

        resolved = installer.recover(dest_root, ["alpha"])

        self.assertEqual(resolved, [])
        for name, digest in before.items():
            self.assertEqual(_sha256(destination / name), digest)

    def test_recover_uses_marker_participants_when_the_caller_passes_none(self):
        dest_root = self.root / "dest"
        previous = self._swapped_away_state(dest_root, "alpha")
        destination = self._destination(dest_root, "alpha")
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(
            json.dumps({"status": "in-progress", "profiles": ["alpha"]}), encoding="utf-8"
        )

        # The caller knows about nothing; the marker is authoritative.
        resolved = installer.recover(dest_root, [])

        self.assertEqual(resolved, ["alpha"])
        self.assertTrue(destination.is_dir())
        for name, expected in self._manifest["files"].items():
            self.assertEqual(_sha256(destination / name), expected)
        self.assertFalse(previous.exists())
        self.assertFalse(marker.exists())

    def test_recover_resolves_marker_participants_the_caller_omitted(self):
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha", "bravo"])
        bravo_dest = self._destination(dest_root, "bravo")
        bravo_previous = bravo_dest.parent / f"{installer._PREVIOUS_PREFIX}deadbeef"
        os.replace(bravo_dest, bravo_previous)
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(
            json.dumps({"status": "in-progress", "profiles": ["alpha", "bravo"]}),
            encoding="utf-8",
        )

        # Caller supplies only alpha; bravo must still be recovered.
        resolved = installer.recover(dest_root, ["alpha"])

        self.assertIn("bravo", resolved)
        self.assertTrue(bravo_dest.is_dir())
        self.assertFalse(bravo_previous.exists())
        self.assertFalse(marker.exists())

    def test_recover_raises_and_keeps_a_truncated_marker(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text('{"profiles": [', encoding="utf-8")

        with self.assertRaises(installer.RecoveryError):
            installer.recover(dest_root, ["alpha"])

        self.assertTrue(marker.exists(), "malformed marker is evidence and must survive")
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"profiles": [')

    def test_recover_raises_and_keeps_a_marker_without_a_profiles_key(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(json.dumps({"status": "in-progress"}), encoding="utf-8")

        with self.assertRaises(installer.RecoveryError):
            installer.recover(dest_root, ["alpha"])
        self.assertTrue(marker.exists())

    def test_recover_raises_on_a_marker_with_non_string_participants(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(json.dumps({"profiles": ["alpha", 7]}), encoding="utf-8")

        with self.assertRaises(installer.RecoveryError):
            installer.recover(dest_root, ["alpha"])
        self.assertTrue(marker.exists())

    def test_recover_raises_on_an_empty_marker_file(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text("", encoding="utf-8")

        with self.assertRaises(installer.RecoveryError):
            installer.recover(dest_root, ["alpha"])
        self.assertTrue(marker.exists())

    def test_distribute_refuses_to_start_behind_a_malformed_marker(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text("}{ not json", encoding="utf-8")
        self._build_valid_source()

        with self.assertRaises(installer.RecoveryError):
            installer.distribute(self.source, dest_root, ["alpha"])

        self.assertTrue(marker.exists())
        self.assertFalse(self._destination(dest_root, "alpha").exists())

    def test_recover_resolves_duplicate_previous_siblings_deterministically(self):
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha"])
        destination = self._destination(dest_root, "alpha")
        plugins_dir = destination.parent

        # Two interrupted runs, neither cleaned up: the older snapshot holds
        # the true pre-transaction content.
        older = plugins_dir / f"{installer._PREVIOUS_PREFIX}0000older"
        newer = plugins_dir / f"{installer._PREVIOUS_PREFIX}ffffnewer"
        shutil.copytree(destination, older)
        (older / "MARK").write_text("older", encoding="utf-8")
        shutil.copytree(destination, newer)
        (newer / "MARK").write_text("newer", encoding="utf-8")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        shutil.rmtree(destination)
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(json.dumps({"profiles": ["alpha"]}), encoding="utf-8")

        resolved = installer.recover(dest_root, ["alpha"])

        self.assertEqual(resolved, ["alpha"])
        self.assertTrue(destination.is_dir())
        self.assertEqual((destination / "MARK").read_text(encoding="utf-8"), "older")
        self.assertEqual(list(plugins_dir.glob(f"{installer._PREVIOUS_PREFIX}*")), [])
        self.assertFalse(marker.exists())

    def test_recover_keeps_the_marker_when_an_artifact_cannot_be_removed(self):
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha"])
        plugins_dir = self._destination(dest_root, "alpha").parent
        stale = plugins_dir / f"{installer._STAGING_PREFIX}stuck"
        stale.mkdir()
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(json.dumps({"profiles": ["alpha"]}), encoding="utf-8")

        with mock.patch(
            "plugins.work_claims.installer.shutil.rmtree", side_effect=OSError("cannot remove")
        ):
            with self.assertRaises(installer.RecoveryError):
                installer.recover(dest_root, ["alpha"])

        self.assertTrue(marker.exists(), "unresolved participants must keep the marker")
        self.assertTrue(stale.is_dir())

    def test_recover_ignores_marker_participants_with_no_profile_directory(self):
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha"])
        marker = dest_root / installer._TXN_MARKER_FILENAME
        marker.write_text(
            json.dumps({"profiles": ["alpha", "never_created"]}), encoding="utf-8"
        )

        self.assertEqual(installer.recover(dest_root, []), [])
        self.assertFalse(marker.exists())

    def test_distribute_recovers_a_prior_interrupted_run_before_starting(self):
        dest_root = self.root / "dest"
        previous = self._swapped_away_state(dest_root, "alpha")
        destination = self._destination(dest_root, "alpha")

        installer.distribute(self.source, dest_root, ["alpha"])

        self.assertTrue(destination.is_dir())
        for name, expected in self._manifest["files"].items():
            self.assertEqual(_sha256(destination / name), expected)
        self.assertFalse(previous.exists())


class MarkerPersistenceTest(_InstallerTestCase):
    """The transaction marker must be written atomically and durably.

    The marker is the *only* record of an interrupted run's participants, and
    ``_read_marker_participants`` deliberately treats anything it cannot parse
    as fatal evidence rather than as "no transaction". That makes how the
    marker reaches the disk part of the recovery contract: a marker built by
    writing bytes into its own final name has a window in which its final path
    exists holding zero (or partial) bytes, and a crash inside that window
    turns a recoverable interruption into a permanent ``RecoveryError`` on
    every subsequent run.

    So the payload is written and fsynced into a same-directory temp file, and
    only a complete, durable file is renamed onto the marker's name -- a single
    filesystem operation. The root directory is fsynced afterwards, because
    bytes that are durable inside a file the directory entry does not yet name
    are not durable at all.
    """

    class _OsRecorder:
        """Real ``os``, with the two calls this contract rests on recorded."""

        def __init__(self):
            self.fsynced = []
            self.replaces = []
            self.observed_at_replace = []

        def __getattr__(self, name):
            return getattr(os, name)

        def fsync(self, fd):
            info = os.fstat(fd)
            self.fsynced.append((info.st_dev, info.st_ino))
            return os.fsync(fd)

        def replace(self, src, dst, *args, **kwargs):
            src_path, dst_path = Path(src), Path(dst)
            if dst_path.name == installer._TXN_MARKER_FILENAME:
                info = src_path.stat()
                self.observed_at_replace.append(
                    {
                        "final_marker_exists": dst_path.exists(),
                        "source_key": (info.st_dev, info.st_ino),
                        "source_payload": src_path.read_text(encoding="utf-8"),
                        "source_is_sibling": src_path.parent == dst_path.parent,
                    }
                )
            self.replaces.append((src_path, dst_path))
            return os.replace(src, dst, *args, **kwargs)

    def _distribute_recording_os(self, dest_root: Path, profiles: list[str]):
        recorder = self._OsRecorder()
        self._build_valid_source()
        with mock.patch.object(installer, "os", recorder):
            installer.distribute(self.source, dest_root, profiles)
        return recorder

    def test_the_final_marker_name_only_ever_appears_via_an_atomic_rename(self):
        dest_root = self.root / "dest"

        recorder = self._distribute_recording_os(dest_root, ["alpha"])

        self.assertEqual(
            len(recorder.observed_at_replace), 1, "marker was not published by a rename"
        )
        observed = recorder.observed_at_replace[0]
        # Nothing ever occupied the marker's own name before the rename, so no
        # crash can leave a partial file there.
        self.assertFalse(observed["final_marker_exists"])
        # The rename is a single filesystem operation only if both sides share
        # a directory (and therefore a filesystem).
        self.assertTrue(observed["source_is_sibling"])
        # What was renamed in was already complete and parseable.
        payload = json.loads(observed["source_payload"])
        self.assertEqual(payload["profiles"], ["alpha"])
        self.assertEqual(payload["status"], "in-progress")

    def test_the_marker_and_its_directory_entry_are_both_fsynced(self):
        dest_root = self.root / "dest"

        recorder = self._distribute_recording_os(dest_root, ["alpha"])

        marker_key = recorder.observed_at_replace[0]["source_key"]
        self.assertIn(marker_key, recorder.fsynced, "marker bytes were never fsynced")
        root_stat = dest_root.stat()
        self.assertIn(
            (root_stat.st_dev, root_stat.st_ino),
            recorder.fsynced,
            "the directory entry naming the marker was never fsynced",
        )

    def test_the_temp_marker_is_not_mistaken_for_a_transaction(self):
        """A crash during persistence leaves the temp file and no marker.

        ``recover()`` must read that as "no transaction started" -- which is
        the truth, because the marker is written before any destination is
        touched -- and must not leave the temp file behind either.
        """
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha"])
        destination = self._destination(dest_root, "alpha")
        before = {name: _sha256(destination / name) for name in self._manifest["files"]}

        stray = dest_root / f"{installer._TXN_MARKER_TMP_PREFIX}deadbeef"
        stray.write_text(json.dumps({"status": "in-progress", "profiles": ["alpha"]}))

        self.assertEqual(installer.recover(dest_root, ["alpha"]), [])

        self.assertFalse(stray.exists(), "a stray temp marker survived recovery")
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())
        for name, digest in before.items():
            self.assertEqual(_sha256(destination / name), digest)

    def test_recover_clears_a_stray_temp_marker_with_no_participants_named(self):
        """The caller's profile list is not what makes the cleanup happen."""
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        stray = dest_root / f"{installer._TXN_MARKER_TMP_PREFIX}cafebabe"
        stray.write_text("half-writ")

        self.assertEqual(installer.recover(dest_root, []), [])

        self.assertFalse(stray.exists())

    def test_a_truncated_temp_marker_is_still_only_a_temp_marker(self):
        """Its contents are irrelevant: it never held authority to begin with.

        The half-written payload that would be fatal under the marker's own
        name is merely discarded under the temp name -- which is the entire
        point of writing it there first.
        """
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        (dest_root / f"{installer._TXN_MARKER_TMP_PREFIX}0001").write_text('{"prof')

        self.assertEqual(installer.recover(dest_root, ["alpha"]), [])

        self.assertEqual(
            list(dest_root.glob(f"{installer._TXN_MARKER_TMP_PREFIX}*")), []
        )

    def test_distribute_starts_cleanly_behind_a_stray_temp_marker(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        (dest_root / f"{installer._TXN_MARKER_TMP_PREFIX}0002").write_text("")
        self._build_valid_source()

        installer.distribute(self.source, dest_root, ["alpha"])

        destination = self._destination(dest_root, "alpha")
        for name, expected in self._manifest["files"].items():
            self.assertEqual(_sha256(destination / name), expected)
        self.assertEqual(
            list(dest_root.glob(f"{installer._TXN_MARKER_TMP_PREFIX}*")), []
        )
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())

    def test_marker_removal_is_persisted_too(self):
        """Cleanup a crash can undo is not cleanup.

        An unlink whose directory entry never reaches the disk leaves the next
        process recovering behind a marker for a transaction that committed.
        """
        dest_root = self.root / "dest"

        recorder = self._distribute_recording_os(dest_root, ["alpha"])

        root_stat = dest_root.stat()
        root_key = (root_stat.st_dev, root_stat.st_ino)
        self.assertGreaterEqual(
            recorder.fsynced.count(root_key),
            2,
            "the root directory was fsynced for the marker's creation but not "
            "for its removal",
        )
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())

    def test_a_failed_marker_write_leaves_no_temp_file_behind(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        self._build_valid_source()

        with mock.patch.object(installer.os, "fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        self.assertEqual(
            list(dest_root.glob(f"{installer._TXN_MARKER_TMP_PREFIX}*")), []
        )
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())


class MarkerCompleteWriteTest(_InstallerTestCase):
    """Every byte of the marker payload must reach the temp file.

    ``os.write`` is permitted to write fewer bytes than it was handed -- a
    short write is a legal outcome, not an error -- so a single call is not a
    write. Writing the marker under a temp name buys atomicity of *publication*
    and nothing else: whatever that temp file holds is what ``os.replace``
    installs at the marker's own name. A truncated payload published there does
    not merely lose the participant record, it poisons the installation:
    ``_read_marker_participants`` treats anything unparseable as fatal
    evidence, and ``distribute()`` runs ``recover()`` first, so every later run
    refuses to start behind it.
    """

    class _ChunkedWriteOs:
        """Real ``os``, with ``write`` capped to a legal short write.

        ``fail_after`` makes the write fail *inside* the payload, after some of
        it has already been accepted -- the case a single ``os.write`` call can
        never produce and therefore never has to survive.
        """

        def __init__(self, chunk: int, fail_after: int | None = None):
            self.chunk = chunk
            self.fail_after = fail_after
            self.writes: list[int] = []
            self.bytes_written_at_fsync: list[int] = []

        def __getattr__(self, attr):
            return getattr(os, attr)

        def write(self, fd, data):
            if self.fail_after is not None and len(self.writes) >= self.fail_after:
                raise OSError(errno.ENOSPC, "no space left on device")
            written = os.write(fd, memoryview(data)[: self.chunk])
            self.writes.append(written)
            return written

        def fsync(self, fd):
            self.bytes_written_at_fsync.append(sum(self.writes))
            return os.fsync(fd)

    def test_a_legal_short_write_still_publishes_a_complete_marker(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        chunked = self._ChunkedWriteOs(chunk=7)

        with mock.patch.object(installer, "os", chunked):
            marker = installer._write_txn_marker(dest_root, "0" * 64, ["alpha", "beta"])

        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(payload["profiles"], ["alpha", "beta"])
        self.assertEqual(payload["manifest_sha256"], "0" * 64)
        self.assertEqual(payload["status"], "in-progress")
        # The published marker is what recovery actually reads back.
        self.assertEqual(installer._read_marker_participants(marker), ["alpha", "beta"])
        # Negative control: the cap really did split the payload across calls,
        # so a single os.write would have published 7 bytes of JSON here.
        self.assertGreater(len(chunked.writes), 1)
        self.assertEqual(max(chunked.writes), 7)
        self.assertEqual(sum(chunked.writes), len(marker.read_bytes()))

    def test_the_payload_is_complete_before_the_marker_bytes_are_fsynced(self):
        """Durability is only worth having once there is a whole payload.

        Flushing a partially written file makes a truncated marker *durably*
        wrong, which is worse than losing it.
        """
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        chunked = self._ChunkedWriteOs(chunk=3)

        with mock.patch.object(installer, "os", chunked):
            marker = installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        self.assertTrue(chunked.bytes_written_at_fsync, "the marker was never fsynced")
        # Negative control: with the payload split across several calls, a
        # flush issued after the first one would record fewer bytes than the
        # whole payload -- and the marker would parse as nothing at all.
        self.assertGreater(len(chunked.writes), 1)
        self.assertEqual(chunked.bytes_written_at_fsync[0], sum(chunked.writes))
        self.assertEqual(chunked.bytes_written_at_fsync[0], len(marker.read_bytes()))
        self.assertEqual(installer._read_marker_participants(marker), ["alpha"])

    def test_a_short_write_does_not_poison_every_later_run(self):
        """The permanent-``RecoveryError`` failure mode, end to end.

        A run that published a truncated marker and then died must still leave
        a tree the next run can recover and install into.
        """
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        self._build_valid_source()

        with mock.patch.object(installer, "os", self._ChunkedWriteOs(chunk=5)):
            installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        installer.distribute(self.source, dest_root, ["alpha"])

        destination = self._destination(dest_root, "alpha")
        for name, expected in self._manifest["files"].items():
            self.assertEqual(_sha256(destination / name), expected)
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())

    def test_a_write_reporting_no_progress_fails_closed(self):
        """Zero bytes written is a failure, not a truncation and not a retry.

        It is the one short-write result a loop cannot make progress on, so it
        must terminate the write rather than publish what was written so far
        (the pre-fix behaviour) or spin forever (the naive loop).
        """

        class _ZeroWriteOs:
            def __getattr__(self, attr):
                return getattr(os, attr)

            def write(self, fd, data):
                return 0

        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)

        with mock.patch.object(installer, "os", _ZeroWriteOs()):
            with self.assertRaises(OSError):
                installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())
        self.assertEqual(
            list(dest_root.glob(f"{installer._TXN_MARKER_TMP_PREFIX}*")), []
        )

    def test_an_error_partway_through_the_payload_publishes_nothing(self):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        chunked = self._ChunkedWriteOs(chunk=9, fail_after=1)

        with mock.patch.object(installer, "os", chunked):
            with self.assertRaises(OSError):
                installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        # Negative control: the first chunk was accepted, so this is a failure
        # *inside* the payload -- reachable only because the write loops.
        self.assertEqual(chunked.writes, [9])
        self.assertFalse((dest_root / installer._TXN_MARKER_FILENAME).exists())
        self.assertEqual(
            list(dest_root.glob(f"{installer._TXN_MARKER_TMP_PREFIX}*")), []
        )


class DirectoryDurabilityTest(_InstallerTestCase):
    """A required directory flush that fails is the caller's failure.

    ``os.replace`` makes the marker's *bytes* durable; the directory entry that
    names them is a separate write that needs its own flush, and so does the
    unlink that retires it. On POSIX that flush is both available and required,
    so swallowing its failure lets ``distribute()`` report success over a
    marker whose publication -- or whose removal -- a crash could still undo.
    That is exactly the state the marker exists to make impossible.
    """

    class _DirDurabilityOs:
        """Real ``os``, failing the directory half of the durability contract.

        ``mode`` picks which half: ``"open"`` refuses to open the directory at
        all (the network-mount case the pre-fix code cited as a reason to skip),
        ``"fsync"`` opens it and refuses to flush. ``skip`` lets that many
        directory operations through first, so a specific one -- the marker's
        publication or its removal -- can be singled out.
        """

        def __init__(self, mode: str, *, skip: int = 0, name: str = os.name):
            self.mode = mode
            self.skip = skip
            self.name = name
            self.dir_opens = 0
            self.dir_fsyncs = 0

        def __getattr__(self, attr):
            return getattr(os, attr)

        def open(self, path, flags, *args, **kwargs):
            if Path(path).is_dir():
                self.dir_opens += 1
                if self.mode == "open" and self.dir_opens > self.skip:
                    raise OSError(errno.EPERM, "cannot open directory for flush")
            return os.open(path, flags, *args, **kwargs)

        def fsync(self, fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                self.dir_fsyncs += 1
                if self.mode == "fsync" and self.dir_fsyncs > self.skip:
                    raise OSError(errno.EIO, "directory flush failed")
            return os.fsync(fd)

    def _assert_publish_failure_aborts_the_install(self, mode: str):
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        self._build_valid_source()
        failing = self._DirDurabilityOs(mode)

        with mock.patch.object(installer, "os", failing):
            with self.assertRaises(OSError):
                installer.distribute(self.source, dest_root, ["alpha"])

        # It failed at the marker, which precedes every destination.
        self.assertFalse(self._destination(dest_root, "alpha").exists())
        # And the marker it did publish is still usable recovery evidence.
        marker = dest_root / installer._TXN_MARKER_FILENAME
        self.assertTrue(marker.is_file())
        self.assertEqual(installer._read_marker_participants(marker), ["alpha"])
        self.assertEqual(installer.recover(dest_root, ["alpha"]), [])
        self.assertFalse(marker.exists())
        return failing

    def test_a_root_directory_open_failure_aborts_the_install(self):
        failing = self._assert_publish_failure_aborts_the_install("open")
        self.assertEqual(failing.dir_opens, 1)

    def test_a_root_directory_fsync_failure_aborts_the_install(self):
        failing = self._assert_publish_failure_aborts_the_install("fsync")
        self.assertEqual(failing.dir_fsyncs, 1)

    def test_a_marker_removal_that_cannot_be_persisted_rolls_the_call_back(self):
        """Retiring the marker is this transaction's commit point.

        An unlink whose directory entry never reaches the disk leaves a marker
        for a transaction that actually committed, and the next ``recover()``
        would roll every profile back off a good install. Rather than report a
        success a crash cannot be trusted to preserve, the call rolls itself
        back -- which it can only do while each profile's pre-swap content
        still exists, so the removal happens before that cleanup.
        """
        dest_root = self.root / "dest"
        self._build_valid_source()
        installer.distribute(self.source, dest_root, ["alpha"])
        destination = self._destination(dest_root, "alpha")
        (destination / "SENTINEL").write_text("pre-existing")

        # skip=1: the publication flush succeeds; the removal flush fails.
        failing = self._DirDurabilityOs("fsync", skip=1)
        with mock.patch.object(installer, "os", failing):
            with self.assertRaises(installer.InstallTransactionError):
                installer.distribute(self.source, dest_root, ["alpha"])

        self.assertEqual(failing.dir_fsyncs, 2)
        self.assertEqual((destination / "SENTINEL").read_text(), "pre-existing")
        # Nothing is left for a later run to trip over.
        self.assertEqual(installer.recover(dest_root, ["alpha"]), [])
        self.assertEqual((destination / "SENTINEL").read_text(), "pre-existing")
        self.assertEqual(
            sorted(
                path.name
                for path in destination.parent.glob(f"{installer._PREVIOUS_PREFIX}*")
            ),
            [],
        )

    def test_a_recovery_that_cannot_retire_the_marker_raises(self):
        """``recover()`` resolved every participant but could not record it."""
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        with mock.patch.object(installer, "os", self._DirDurabilityOs("fsync")):
            with self.assertRaises(installer.RecoveryError):
                installer.recover(dest_root, ["alpha"])


class DirectoryFsyncPolicyTest(_InstallerTestCase):
    """The directory-flush policy is total over ``os.name``, not a boolean.

    Three successive corrections to this one durability decision each found
    the same shape of bug: a boolean predicate answers "not required" for
    every input it does not recognise, so an unknown platform is silently
    handed the *safe-looking* branch -- the flush is skipped and the install
    reports success over a marker nothing has made durable. The replacement
    names all three outcomes explicitly. ``posix`` requires the flush and
    fails closed on error; ``nt`` is the single documented carve-out, where
    the flush is *unavailable* rather than failed; every other value is an
    unsupported platform and raises before any caller can read the silence as
    permission.
    """

    class _PlatformOs:
        """Real ``os`` under a substituted ``os.name``, counting directory ops.

        Nothing here fails -- these tests are about which branch the policy
        selects, so any directory operation that happens is recorded rather
        than broken, and a branch that should not run is proved not to have.
        """

        def __init__(self, name: str):
            self.name = name
            self.dir_opens = 0
            self.dir_fsyncs = 0

        def __getattr__(self, attr):
            return getattr(os, attr)

        def open(self, path, flags, *args, **kwargs):
            if Path(path).is_dir():
                self.dir_opens += 1
            return os.open(path, flags, *args, **kwargs)

        def fsync(self, fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                self.dir_fsyncs += 1
            return os.fsync(fd)

    # Platform names Python has actually shipped or documented for os.name and
    # which this installer has no durability story for, plus the degenerate
    # empty value an embedding could hand us.
    _UNSUPPORTED_NAMES = ("java", "riscos", "ce", "os2", "posix2", "")

    def test_posix_requires_the_flush(self):
        with mock.patch.object(installer, "os", self._PlatformOs("posix")):
            self.assertEqual(
                installer._directory_fsync_policy(),
                installer.DIRECTORY_FSYNC_REQUIRED,
            )

    def test_nt_is_the_sole_unavailable_carve_out(self):
        with mock.patch.object(installer, "os", self._PlatformOs("nt")):
            self.assertEqual(
                installer._directory_fsync_policy(),
                installer.DIRECTORY_FSYNC_UNAVAILABLE,
            )

    def test_every_other_platform_name_is_an_explicit_error(self):
        for name in self._UNSUPPORTED_NAMES:
            with self.subTest(os_name=name):
                with mock.patch.object(installer, "os", self._PlatformOs(name)):
                    with self.assertRaises(installer.UnsupportedPlatformError) as caught:
                        installer._directory_fsync_policy()
                # The error has to say which platform and which two are known,
                # because the only fix is a reviewed decision about that name.
                message = str(caught.exception)
                self.assertIn(repr(name), message)
                self.assertIn("posix", message)
                self.assertIn("nt", message)

    def test_the_policy_admits_no_verdict_outside_the_two_named_ones(self):
        self.assertEqual(
            set(installer._DIRECTORY_FSYNC_POLICY),
            {"posix", "nt"},
        )
        self.assertEqual(
            set(installer._DIRECTORY_FSYNC_POLICY.values()),
            {installer.DIRECTORY_FSYNC_REQUIRED, installer.DIRECTORY_FSYNC_UNAVAILABLE},
        )

    def test_posix_actually_opens_and_flushes_the_directory(self):
        """The ``required`` verdict is a real flush, not a label."""
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        platform = self._PlatformOs("posix")

        with mock.patch.object(installer, "os", platform):
            marker = installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        self.assertTrue(marker.is_file())
        self.assertEqual(platform.dir_opens, 1)
        self.assertEqual(platform.dir_fsyncs, 1)

    def test_the_nt_carve_out_skips_the_flush_and_still_publishes(self):
        """Windows cannot open a directory to flush it at all, so there the
        flush is *unavailable* rather than failed and the marker is published
        with whatever durability the platform offers."""
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        platform = self._PlatformOs("nt")

        with mock.patch.object(installer, "os", platform):
            marker = installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        self.assertTrue(marker.is_file())
        self.assertEqual(installer._read_marker_participants(marker), ["alpha"])
        self.assertEqual(platform.dir_opens, 0)
        self.assertEqual(platform.dir_fsyncs, 0)

    def test_the_carve_out_is_keyed_on_nt_and_nothing_else(self):
        """Under a POSIX name the identical directory-open failure aborts."""
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)

        with mock.patch.object(
            installer,
            "os",
            DirectoryDurabilityTest._DirDurabilityOs("open", name="nt"),
        ):
            installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        with mock.patch.object(
            installer,
            "os",
            DirectoryDurabilityTest._DirDurabilityOs("open", name="posix"),
        ):
            with self.assertRaises(OSError):
                installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

    def test_an_unknown_platform_stops_the_marker_write(self):
        """The negative control for the whole component.

        This is the case the boolean predicate got wrong, and it is wrong
        *silently*: ``os.name == "posix"`` is false under ``java``, so the
        flush was skipped and ``_write_txn_marker`` returned a marker no
        directory entry had been made durable -- an install that looks
        successful and is not. Restoring that predicate makes this test fail,
        because the call below would return the marker instead of raising.
        """
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        platform = self._PlatformOs("java")

        with mock.patch.object(installer, "os", platform):
            with self.assertRaises(installer.UnsupportedPlatformError):
                installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        # It raised instead of quietly taking the skip branch.
        self.assertEqual(platform.dir_opens, 0)
        self.assertEqual(platform.dir_fsyncs, 0)

    def test_an_unknown_platform_stops_the_marker_removal(self):
        """The removal side is the same decision and fails the same way.

        A skipped removal flush leaves a marker for a transaction that
        committed, which the next ``recover()`` reads as an aborted run and
        rolls a good install back off.
        """
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        marker = installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        with mock.patch.object(installer, "os", self._PlatformOs("java")):
            with self.assertRaises(installer.UnsupportedPlatformError):
                installer._remove_txn_marker(marker)

    def test_an_unknown_platform_stops_a_distribution(self):
        """End to end: no destination is created behind an unknown platform."""
        dest_root = self.root / "dest"
        self._build_valid_source()

        with mock.patch.object(installer, "os", self._PlatformOs("java")):
            with self.assertRaises(installer.UnsupportedPlatformError):
                installer.distribute(self.source, dest_root, ["alpha"])

        self.assertFalse(self._destination(dest_root, "alpha").exists())

    def test_an_unknown_platform_stops_a_recovery(self):
        """``recover()`` cannot durably retire a marker it cannot flush."""
        dest_root = self.root / "dest"
        dest_root.mkdir(parents=True)
        installer._write_txn_marker(dest_root, "0" * 64, ["alpha"])

        with mock.patch.object(installer, "os", self._PlatformOs("java")):
            with self.assertRaises(installer.UnsupportedPlatformError):
                installer.recover(dest_root, ["alpha"])


class DefaultInstallDiscoveryTest(_InstallerTestCase):
    """The default (profiles=None) install discovers and includes every enabled profile.

    These tests exercise ``discover_enabled_profiles`` and the ``main(profiles=None)``
    integration path, proving that:

    1. A newly enabled future profile not in PROFILES is automatically included
       on the next default production install (no code edit required).
    2. A profile with work-claims in ``plugins.disabled`` is excluded even when
       it is also in ``plugins.enabled``.
    3. An unreadable or malformed config.yaml fails before any distribution
       mutation (fail-closed, not silent omission).
    4. An explicit ``profiles=`` list remains exact -- discovery is not called.
    5. The current 8-profile fleet is pinned: all eight are included on a
       default run over a filesystem that reflects the live inventory.

    All tests run entirely against temp directories; no real Hermes home or
    live profile config is read.
    """

    def _make_profile(self, profiles_dir: Path, name: str, *, enabled: bool = True,
                      disabled: bool = False, plugins_section: object = "ABSENT") -> Path:
        """Create a minimal per-profile directory with an appropriate config.yaml."""
        pdir = profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        if plugins_section == "ABSENT":
            if enabled and not disabled:
                cfg = {"plugins": {"enabled": ["work-claims"]}}
            elif enabled and disabled:
                cfg = {"plugins": {"enabled": ["work-claims"], "disabled": ["work-claims"]}}
            else:
                cfg = {"plugins": {"enabled": []}}
        else:
            cfg = plugins_section  # caller supplies raw structure
        import yaml as _yaml
        (pdir / "config.yaml").write_text(
            _yaml.dump(cfg) if cfg is not None else "", encoding="utf-8"
        )
        return pdir

    def _load_entrypoint(self):
        return _load_repo_entrypoint()

    def test_newly_enabled_future_profile_is_included_in_default_install(self):
        """A profile not in PROFILES but with work-claims enabled is auto-included.

        This is the root cause being fixed: the hardcoded PROFILES list could
        silently omit any profile enabled after the last roster edit. With
        filesystem discovery unioned in, the next default run picks it up
        without any code change.
        """
        entry = self._load_entrypoint()
        source = self._build_canonical_source()
        fleet_root = self.root / "fleet"
        profiles_dir = fleet_root / "profiles"

        # Create a profile that does NOT appear in the hardcoded PROFILES tuple.
        future_profile = "future_enabled_profile"
        self.assertNotIn(future_profile, entry.PROFILES,
                         "test requires a name absent from the baseline PROFILES tuple")
        self._make_profile(profiles_dir, future_profile, enabled=True)

        installed = entry.main(source=source, root=fleet_root)

        self.assertIn(future_profile, installed,
                      "a newly enabled profile not in PROFILES must be auto-discovered "
                      "and distributed to on the default install path")
        dest = fleet_root / "profiles" / future_profile / "plugins" / "work_claims"
        self.assertTrue(dest.is_dir(),
                        "the auto-discovered profile must have received the plugin files")

    def test_disabled_profile_is_excluded_by_config_semantics(self):
        """A profile in plugins.disabled is not distributed to, regardless of plugins.enabled."""
        entry = self._load_entrypoint()
        source = self._build_canonical_source()
        fleet_root = self.root / "fleet"
        profiles_dir = fleet_root / "profiles"

        # A profile that enables AND disables the plugin -- net result: disabled.
        self._make_profile(profiles_dir, "explicitly_disabled", enabled=True, disabled=True)
        # A profile that never enables the plugin at all.
        self._make_profile(profiles_dir, "never_enabled", enabled=False, disabled=False)

        installed = entry.main(source=source, root=fleet_root)

        self.assertNotIn("explicitly_disabled", installed,
                         "a profile with work-claims in plugins.disabled must be excluded")
        self.assertNotIn("never_enabled", installed,
                         "a profile that does not enable work-claims must be excluded")

    def test_malformed_config_fails_before_any_distribution_mutation(self):
        """An unreadable or malformed config.yaml raises before any profile is touched.

        Fail-closed: the intent of the profile cannot be determined, so the
        run aborts rather than silently omitting it.
        """
        entry = self._load_entrypoint()
        source = self._build_canonical_source()
        fleet_root = self.root / "fleet"
        profiles_dir = fleet_root / "profiles"

        # One good enabled profile.
        self._make_profile(profiles_dir, "good_profile", enabled=True)
        # A profile with a YAML parse error.
        bad_dir = profiles_dir / "malformed_profile"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "config.yaml").write_text(
            "plugins: {enabled: [work-claims\nmalformed yaml here: {{{",
            encoding="utf-8",
        )

        with self.assertRaises(entry.ConfigDiscoveryError,
                               msg="malformed config.yaml must raise ConfigDiscoveryError "
                                   "before any distribution mutation"):
            entry.main(source=source, root=fleet_root)

        # No mutation: the good profile must not have been touched.
        good_dest = fleet_root / "profiles" / "good_profile" / "plugins" / "work_claims"
        self.assertFalse(good_dest.exists(),
                         "no profile must have been distributed to when discovery fails")

    def test_explicit_profiles_list_is_exact_and_skips_discovery(self):
        """When profiles= is supplied, it is used verbatim -- discovery is not invoked.

        This supports staged drtest canaries and single-profile targeted installs
        without scanning unrelated profiles.
        """
        entry = self._load_entrypoint()
        source = self._build_canonical_source()
        fleet_root = self.root / "fleet"
        profiles_dir = fleet_root / "profiles"

        # An enabled profile in the filesystem that is NOT in the explicit list.
        self._make_profile(profiles_dir, "filesystem_extra", enabled=True)
        # A non-existent profile that IS in the explicit list.
        explicit_only = "explicit_only"

        installed = entry.main(source=source, root=fleet_root, profiles=[explicit_only])

        self.assertEqual(list(installed.keys()), [explicit_only],
                         "explicit profiles= must be used verbatim, with no extra "
                         "profiles discovered from the filesystem")
        self.assertNotIn("filesystem_extra", installed,
                         "a profile enabled in the filesystem but absent from profiles= "
                         "must not be installed when profiles= is explicit")

    def test_current_8_profile_fleet_is_pinned_and_all_installed_by_default(self):
        """All eight currently-enabled profiles are installed on a default run.

        This test pins the live fleet: clara, daniel, elias, hannah, maya,
        oliver, rook, sophie.  It builds a minimal filesystem with exactly those
        eight profiles enabled, runs main() with no profiles= argument, and
        asserts every one was installed and nothing extra was added.
        """
        entry = self._load_entrypoint()
        source = self._build_canonical_source()
        fleet_root = self.root / "fleet"
        profiles_dir = fleet_root / "profiles"

        # The live fleet from the confirmed read-only inventory.
        live_fleet = frozenset(
            ("clara", "daniel", "elias", "hannah", "maya", "oliver", "rook", "sophie")
        )
        for name in live_fleet:
            self._make_profile(profiles_dir, name, enabled=True)

        installed = entry.main(source=source, root=fleet_root)

        self.assertEqual(
            frozenset(installed.keys()), live_fleet,
            "all eight currently-enabled profiles must be installed by the default "
            "run and no others (no drtest-rook or other disabled/unconfigured profile)"
        )
        for name in live_fleet:
            dest = fleet_root / "profiles" / name / "plugins" / "work_claims"
            self.assertTrue(dest.is_dir(),
                            f"profile {name!r} must have received the plugin files")


if __name__ == "__main__":
    unittest.main()
