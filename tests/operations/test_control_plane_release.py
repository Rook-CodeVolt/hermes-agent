from __future__ import annotations

import hashlib
import json
import subprocess
import copy
from pathlib import Path

import pytest

from scripts.control_plane_release import (
    ContractError,
    build_release,
    build_gate_receipt,
    compute_evidence_sha256,
    compute_receipt_sha256,
    install_release,
    load_manifest,
    prove_forward_correction,
    run_installed_shape,
    run_restart_handoff,
    run_supported_update,
    validate_activation_document_hashes,
    validate_gate_receipt,
    validate_incident_registry,
)


PROFILES = ["clara", "daniel", "elias", "hannah", "maya", "oliver", "rook", "sophie"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    files = {
        "plugins/demo/__init__.py": "from .helper import HELPER\nVALUE = HELPER\n",
        "plugins/demo/helper.py": "HELPER = 'ok'\n",
        "helpers/helper.py": "HELPER = 'ok'\n",
        "launchd/demo.plist": "<?xml version=\"1.0\"?><plist version=\"1.0\"><dict/></plist>\n",
        "migrations/001.json": '{"from":0,"to":1}\n',
        "checks/check.py": "import demo; assert demo.VALUE == 'ok'\n",
        "fixtures/vector.json": '{"id":"fixture"}\n',
        "docs/recovery.md": "restore exact preimages\n",
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo


def _spec(repo: Path) -> dict:
    destinations = []
    for profile in ["root", *PROFILES]:
        destinations.append(
            {
                "logical_name": f"demo-plugin-{profile}",
                "source": "plugins/demo/__init__.py",
                "profile": profile,
                "destination_class": "plugin",
                "relative_destination": "plugins/demo/__init__.py",
                "mode": "0644",
                "owner_class": "profile_owner" if profile != "root" else "root_owner",
                "plugin_version": "1.0.0",
                "dependency_order": 20,
            }
        )
        destinations.append(
            {
                "logical_name": f"demo-helper-{profile}",
                "source": "plugins/demo/helper.py",
                "profile": profile,
                "destination_class": "helper",
                "relative_destination": "plugins/demo/helper.py",
                "mode": "0644",
                "owner_class": "profile_owner" if profile != "root" else "root_owner",
                "plugin_version": "1.0.0",
                "dependency_order": 10,
            }
        )
    destinations.extend(
        [
            {"logical_name": "helper", "source": "helpers/helper.py", "profile": "root", "destination_class": "helper", "relative_destination": "scripts/helper.py", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 10},
            {"logical_name": "launchd", "source": "launchd/demo.plist", "profile": "root", "destination_class": "launchd", "relative_destination": "Library/LaunchAgents/demo.plist", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 50},
            {"logical_name": "migration", "source": "migrations/001.json", "profile": "root", "destination_class": "migration", "relative_destination": "migrations/001.json", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 30},
            {"logical_name": "check", "source": "checks/check.py", "profile": "root", "destination_class": "check", "relative_destination": "checks/check.py", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 60},
            {"logical_name": "fixture", "source": "fixtures/vector.json", "profile": "root", "destination_class": "fixture", "relative_destination": "fixtures/vector.json", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 40},
            {"logical_name": "recovery", "source": "docs/recovery.md", "profile": "root", "destination_class": "recovery_documentation", "relative_destination": "docs/recovery.md", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 70},
        ]
    )
    return {
        "schema_version": 1,
        "release_id": "cv-test-1",
        "policy_version": "cv-control-plane-assurance-v2",
        "release_channel": "approved/test",
        "production_profiles": PROFILES,
        "toolchain": {"python": "3.9+", "builder": "control-plane-release-v1"},
        "restart_scope": ["com.codevolt.demo"],
        "migration": {"from_schema": 0, "to_schema": 1, "entrypoint": "migrations/001.json"},
        "rollback_release_id": "cv-test-0",
        "destinations": destinations,
    }


def test_double_build_is_byte_identical_and_binds_git_identity(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    spec = _spec(repo)

    first = build_release(repo, spec, tmp_path / "build-a")
    second = build_release(repo, spec, tmp_path / "build-b")

    assert first.manifest_bytes == second.manifest_bytes
    assert first.archive_bytes == second.archive_bytes
    manifest = json.loads(first.manifest_bytes)
    assert manifest["source_commit"] == _git(repo, "rev-parse", "HEAD")
    assert manifest["source_tree"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert first.manifest_sha256 == hashlib.sha256(first.manifest_bytes).hexdigest()


def test_narrow_continuity_policy_admits_only_the_six_owned_destinations(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    spec = _spec(repo)
    spec["policy_version"] = "cv-continuity-guard-v1"
    spec["production_profiles"] = []
    spec["destinations"] = [
        {"logical_name": "continuity-guard", "source": "helpers/helper.py", "profile": "root", "destination_class": "helper", "relative_destination": "scripts/codevolt_continuity_guard.py", "mode": "0755", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 10},
        {"logical_name": "hermes-state-common", "source": "helpers/helper.py", "profile": "root", "destination_class": "helper", "relative_destination": "scripts/hermes_state_common.py", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 10},
        {"logical_name": "continuity-launchd-canary", "source": "helpers/helper.py", "profile": "root", "destination_class": "helper", "relative_destination": "scripts/tests/run_launchd_canary.py", "mode": "0755", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 10},
        {"logical_name": "continuity-launchd-worker", "source": "helpers/helper.py", "profile": "root", "destination_class": "helper", "relative_destination": "scripts/tests/launchd_canary_worker.py", "mode": "0755", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 10},
        {"logical_name": "continuity-launchd", "source": "launchd/demo.plist", "profile": "root", "destination_class": "launchd", "relative_destination": "Library/LaunchAgents/com.codevolt.continuity-guard.plist", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 50},
        {"logical_name": "continuity-test", "source": "checks/check.py", "profile": "root", "destination_class": "check", "relative_destination": "release-checks/test_codevolt_continuity_guard.py", "mode": "0644", "owner_class": "root_owner", "plugin_version": None, "dependency_order": 60},
    ]

    build = build_release(repo, spec, tmp_path / "narrow-build")
    manifest = json.loads(build.manifest_bytes)
    assert manifest["production_profiles"] == []
    assert len(manifest["destinations"]) == 6

    broadened = copy.deepcopy(spec)
    broadened["destinations"].append(_spec(repo)["destinations"][0])
    with pytest.raises(ContractError, match="only the exact owned destination set"):
        build_release(repo, broadened, tmp_path / "must-not-build")


def test_manifest_loader_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":1,"unknown":true}\n', encoding="utf-8")

    with pytest.raises(ContractError, match="closed"):
        load_manifest(path)


def test_manifest_loader_rejects_special_permission_bits(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    manifest = json.loads(build.manifest_bytes)
    manifest["destinations"][0]["mode"] = "4755"
    path = tmp_path / "unsafe-manifest.json"
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(ContractError, match="unsafe or non-canonical mode"):
        load_manifest(path)


def _target(root: Path, item: dict) -> Path:
    base = root if item["profile"] == "root" else root / "profiles" / item["profile"]
    return base / item["relative_destination"]


def _shape(root: Path, manifest: dict) -> dict:
    result = {}
    for item in manifest["destinations"]:
        path = _target(root, item)
        relative = path.relative_to(root).as_posix()
        result[relative] = None if not path.exists() else (path.read_bytes(), path.stat().st_mode & 0o777)
    return result


def test_mid_transaction_failure_restores_exact_preimages(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    root.mkdir()
    manifest = json.loads(build.manifest_bytes)
    for item in manifest["destinations"][:3]:
        path = _target(root, item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preimage-" + item["logical_name"].encode())
        path.chmod(0o600)
    before = _shape(root, manifest)

    with pytest.raises(ContractError, match="injected failure"):
        install_release(build.archive_path, root, fail_after=5)

    assert _shape(root, manifest) == before


def test_successful_install_is_complete_and_second_run_is_idempotent(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"

    first = install_release(build.archive_path, root)
    manifest = json.loads(build.manifest_bytes)
    mtimes = {_target(root, item): _target(root, item).stat().st_mtime_ns for item in manifest["destinations"]}
    second = install_release(build.archive_path, root)

    assert first["changed_destinations"] == len(manifest["destinations"])
    assert second["changed_destinations"] == 0
    assert first["postimage_sha256"] == second["postimage_sha256"]
    assert {_target(root, item): _target(root, item).stat().st_mtime_ns for item in manifest["destinations"]} == mtimes
    for item in manifest["destinations"]:
        path = _target(root, item)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        assert path.stat().st_mode & 0o777 == int(item["mode"], 8)


def test_installer_cli_executes_the_transaction_and_emits_receipt(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    build = build_release(source, _spec(source), tmp_path / "build")
    root = tmp_path / "installed"
    repo = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [
            __import__("sys").executable,
            str(repo / "scripts/control_plane_release.py"),
            "install",
            "--archive",
            str(build.archive_path),
            "--root",
            str(root),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["verdict"] == "PASS"
    assert receipt["destinations_installed"] == len(_spec(source)["destinations"])
    assert receipt["changed_destinations"] == len(_spec(source)["destinations"])


def test_installed_shape_imports_every_profile_from_arbitrary_cwd_and_minimal_env(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    arbitrary_cwd = tmp_path / "unrelated-cwd"
    arbitrary_cwd.mkdir()

    receipt = run_installed_shape(
        build.manifest_path,
        root,
        python_executable=Path(__import__("sys").executable),
        cwd=arbitrary_cwd,
    )

    assert receipt["verdict"] == "PASS"
    assert receipt["profiles_checked"] == ["root", *PROFILES]
    assert receipt["cwd"] == "arbitrary"
    assert receipt["environment"] == "minimal"


@pytest.mark.live_system_guard_bypass
def test_supported_updater_lands_approved_commit_with_service_effects_suppressed(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "approved/test"], check=True)
    (repo / "docs/recovery.md").write_text("target release\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "target"], check=True)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    install = tmp_path / "runtime"
    subprocess.run(["git", "clone", "-q", str(repo), str(install)], check=True)
    subprocess.run(["git", "-C", str(install), "reset", "--hard", "-q", "HEAD^"], check=True)
    updater = tmp_path / "hermes"
    updater.write_text(
        "#!/bin/sh\n"
        "test \"$1\" = update && test \"$2\" = --branch && "
        "test \"$4\" = --yes && test \"$5\" = --keep-stash || exit 91\n"
        "test \"$HERMES_RELEASE_SCRATCH\" = 1 && test \"$HERMES_SUPPRESS_SERVICE_EFFECTS\" = 1 || exit 92\n"
        "git -C \"$HERMES_AGENT_ROOT\" fetch -q origin \"$3\" && "
        "git -C \"$HERMES_AGENT_ROOT\" reset --hard -q FETCH_HEAD\n",
        encoding="utf-8",
    )
    updater.chmod(0o755)

    receipt = run_supported_update(
        build.manifest_path,
        install,
        updater,
        scratch=True,
        pid_snapshot=lambda: {"live-hermes": "unchanged-birth-token"},
    )

    assert receipt["verdict"] == "PASS"
    assert receipt["command"] == ["hermes", "update", "--branch", "approved/test", "--yes", "--keep-stash"]
    assert receipt["post_commit"] == json.loads(build.manifest_bytes)["source_commit"]
    assert receipt["post_tree"] == json.loads(build.manifest_bytes)["source_tree"]
    assert receipt["global_effects_suppressed"] is True
    assert receipt["live_pid_birth_identities_unchanged"] is True


def test_restart_handoff_is_manifest_scoped_and_scratch_suppressed(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    calls = tmp_path / "calls"
    runner = tmp_path / "restart-handoff"
    runner.write_text(
        "#!/bin/sh\n"
        "test \"$HERMES_RELEASE_SCRATCH\" = 1 && "
        "test \"$HERMES_SUPPRESS_SERVICE_EFFECTS\" = 1 || exit 93\n"
        "printf '%s\\n' \"$@\" > \"$RESTART_RECEIPT\"\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)

    receipt = run_restart_handoff(
        build.manifest_path,
        runner,
        scratch=True,
        receipt_path=calls,
        pid_snapshot=lambda: {"live-hermes": "unchanged-birth-token"},
    )

    assert calls.read_text(encoding="utf-8").splitlines() == ["com.codevolt.demo"]
    assert receipt["restart_scope"] == ["com.codevolt.demo"]
    assert receipt["global_effects_suppressed"] is True
    assert receipt["live_pid_birth_identities_unchanged"] is True


def test_forward_corrective_release_restores_prior_payload_under_new_identity(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    spec1 = _spec(repo)
    spec1["release_id"] = "cv-release-1"
    spec1["rollback_release_id"] = "cv-release-0"
    release1 = build_release(repo, spec1, tmp_path / "build-1")
    root = tmp_path / "installed"
    install_release(release1.archive_path, root)

    (repo / "plugins/demo/__init__.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "changed"], check=True)
    spec2 = _spec(repo)
    spec2["release_id"] = "cv-release-2"
    spec2["rollback_release_id"] = "cv-release-1"
    release2 = build_release(repo, spec2, tmp_path / "build-2")
    install_release(release2.archive_path, root)

    subprocess.run(["git", "-C", str(repo), "checkout", "HEAD^", "--", "plugins/demo/__init__.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "forward correction"], check=True)
    spec3 = _spec(repo)
    spec3["release_id"] = "cv-release-3"
    spec3["rollback_release_id"] = "cv-release-2"
    release3 = build_release(repo, spec3, tmp_path / "build-3")
    install_release(release3.archive_path, root)

    receipt = prove_forward_correction(
        release1.manifest_path,
        release2.manifest_path,
        release3.manifest_path,
        root,
    )

    assert receipt["verdict"] == "PASS"
    assert receipt["reason"] == "FORWARD_CORRECTION_OK"
    assert receipt["corrective_release_id"] == "cv-release-3"


def test_incident_registry_exactly_covers_contract_and_resolves_permanent_tests() -> None:
    repo = Path(__file__).resolve().parents[2]
    receipt = validate_incident_registry(
        repo / "operations/codevolt-control-plane/assurance-contract.json",
        repo / "operations/codevolt-control-plane/incident-regressions.json",
        repo,
    )

    contract = json.loads((repo / "operations/codevolt-control-plane/assurance-contract.json").read_text())
    assert receipt["verdict"] == "PASS"
    assert receipt["vector_ids"] == [item["id"] for item in contract["test_vectors"]]
    assert receipt["baseline_red_count"] == len(contract["test_vectors"])
    assert receipt["permanent_test_count"] == len(contract["test_vectors"])


def test_missing_profile_destination_has_finite_reason_code(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    manifest = json.loads(build.manifest_bytes)
    plugin = next(item for item in manifest["destinations"] if item["profile"] == "maya" and item["destination_class"] == "plugin")
    _target(root, plugin).unlink()
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    with pytest.raises(ContractError) as failure:
        run_installed_shape(build.manifest_path, root, python_executable=Path(__import__("sys").executable), cwd=cwd)

    assert failure.value.reason_code == "MANIFEST_DESTINATION_MISSING"


def test_missing_helper_in_installed_profile_has_finite_reason_code(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    manifest = json.loads(build.manifest_bytes)
    helper = next(item for item in manifest["destinations"] if item["profile"] == "maya" and item["destination_class"] == "helper" and item["relative_destination"].startswith("plugins/"))
    _target(root, helper).unlink()
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    with pytest.raises(ContractError) as failure:
        run_installed_shape(build.manifest_path, root, python_executable=Path(__import__("sys").executable), cwd=cwd)

    assert failure.value.reason_code == "INSTALLED_SHAPE_IMPORT_FAILED"


def test_one_profile_byte_drift_has_finite_reason_code(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    manifest = json.loads(build.manifest_bytes)
    plugin = next(item for item in manifest["destinations"] if item["profile"] == "oliver" and item["destination_class"] == "plugin")
    _target(root, plugin).write_bytes(b"drifted\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    with pytest.raises(ContractError) as failure:
        run_installed_shape(build.manifest_path, root, python_executable=Path(__import__("sys").executable), cwd=cwd)

    assert failure.value.reason_code == "RELEASE_UNIT_PARTIAL"


def test_build_cli_runs_from_arbitrary_cwd_and_emits_closed_receipt(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(repo), sort_keys=True) + "\n", encoding="utf-8")
    output = tmp_path / "build"
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    script = Path(__file__).resolve().parents[2] / "scripts/control_plane_release.py"

    result = subprocess.run(
        [__import__("sys").executable, str(script), "build", "--repo", str(repo), "--spec", str(spec_path), "--output", str(output)],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert set(receipt) == {"archive_sha256", "manifest_sha256", "release_id", "schema_version", "verdict"}
    assert receipt["verdict"] == "PASS"
    assert (output / "manifest.json").is_file()
    assert (output / "release.tar").is_file()


def test_installed_shape_uses_explicit_manifest_runtime_not_cwd(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    (repo / "runtime_helper.py").write_text("VALUE = 'runtime-ok'\n", encoding="utf-8")
    (repo / "plugins/demo/__init__.py").write_text("import runtime_helper\nVALUE = runtime_helper.VALUE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "runtime dependency"], check=True)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    receipt = run_installed_shape(
        build.manifest_path,
        root,
        python_executable=Path(__import__("sys").executable),
        cwd=cwd,
        runtime_root=repo,
    )

    assert receipt["runtime_commit"] == json.loads(build.manifest_bytes)["source_commit"]
    assert receipt["runtime_tree"] == json.loads(build.manifest_bytes)["source_tree"]


def test_installed_shape_rejects_legacy_state_helper_missing_runtime_symbol(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    (repo / "hermes_state_common.py").write_text("LEGACY = True\n", encoding="utf-8")
    (repo / "hermes_state.py").write_text(
        "from hermes_state_common import stat_db_file_identity\n", encoding="utf-8"
    )
    (repo / "hermes_state_registry.py").write_text(
        "from hermes_state_common import stat_db_file_identity\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "legacy installed helper"], check=True)
    spec = _spec(repo)
    helper = next(
        item for item in spec["destinations"]
        if item["profile"] == "root" and item["relative_destination"] == "scripts/helper.py"
    )
    helper.update(
        logical_name="hermes-state-common",
        source="hermes_state_common.py",
        relative_destination="scripts/hermes_state_common.py",
    )
    build = build_release(repo, spec, tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    with pytest.raises(ContractError) as failure:
        run_installed_shape(
            build.manifest_path,
            root,
            python_executable=Path(__import__("sys").executable),
            cwd=cwd,
            runtime_root=repo,
        )

    assert failure.value.reason_code == "INSTALLED_SHAPE_IMPORT_FAILED"
    assert "runtime import" in str(failure.value)


def test_installed_shape_executes_committed_runtime_not_tracked_drift(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    marker = tmp_path / "uncommitted-executed"
    (repo / "runtime_helper.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
    (repo / "plugins/demo/__init__.py").write_text("import runtime_helper\nVALUE = runtime_helper.VALUE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "runtime dependency"], check=True)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    (repo / "runtime_helper.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nVALUE='drift'\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    receipt = run_installed_shape(build.manifest_path, root, python_executable=Path(__import__("sys").executable), cwd=cwd, runtime_root=repo)

    assert receipt["verdict"] == "PASS"
    assert not marker.exists()


def test_installed_shape_never_executes_untracked_runtime_module(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    marker = tmp_path / "untracked-executed"
    (repo / "plugins/demo/__init__.py").write_text("import surprise_runtime\nVALUE = surprise_runtime.VALUE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "declare unavailable runtime dependency"], check=True)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    (repo / "surprise_runtime.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nVALUE='untracked'\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    with pytest.raises(ContractError) as failure:
        run_installed_shape(build.manifest_path, root, python_executable=Path(__import__("sys").executable), cwd=cwd, runtime_root=repo)

    assert failure.value.reason_code == "INSTALLED_SHAPE_IMPORT_FAILED"
    assert not marker.exists()


def test_contract_positive_fixture_uses_strict_jcs_and_framing() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract = json.loads((repo / "operations/codevolt-control-plane/assurance-contract.json").read_text())
    fixture = contract["receipt_hash_contract"]["positive_fixture"]

    digest = compute_evidence_sha256(contract, fixture["gate_input"], fixture["produced_evidence_envelope"]["evidence"])

    assert digest == fixture["expected_evidence_sha256"]


def _typed_gate_input(gate_id: str, role: str, prerequisites: list[str]) -> dict:
    stages = {"G10_SOURCE_REVIEW": "source_review", "G20_SCRATCH_PROOF": "scratch_proof", "G30_EXACT_BYTE_APPROVAL": "exact_byte_approval"}
    return {
        "schema_version": 1, "gate_id": gate_id, "release_id": "cv-test-release",
        "stage": stages[gate_id],
        "source_commit": "2" * 40, "source_tree": "3" * 40,
        "policy_version": "cv-control-plane-assurance-v2", "manifest_sha256": "1" * 64,
        "actor": "oliver" if role == "operator" else "maya", "actor_role": role,
        "prerequisite_receipts": prerequisites,
        "evidence": [
            {"type": "artifact_digest", "evidence_id": "manifest", "subject": "release-manifest", "sha256": "4" * 64, "byte_length": 10, "media_type": "application/json", "produced_at_utc": "2026-09-05T08:00:00Z"},
            {"type": "test_receipt", "evidence_id": "suite", "suite": "control-plane", "verdict": "PASS", "passed": 1, "failed": 0, "receipt_sha256": "5" * 64, "produced_at_utc": "2026-09-05T08:00:00Z"},
        ],
        "observed_at_utc": "2026-09-05T08:00:00Z",
    }


def test_schema_valid_typed_g20_and_g30_receipts_enforce_prerequisite() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract_path = repo / "operations/codevolt-control-plane/assurance-contract.json"
    fixtures = json.loads((repo / "operations/codevolt-control-plane/gate-receipt-fixtures.json").read_text())
    assert fixtures["fixture_only"] is True
    (g10, g20, g30) = fixtures["gates"]

    assert validate_gate_receipt(contract_path, g10["input"], g10["output"], prerequisite_receipts=g10["prerequisites"])["verdict"] == "PASS"
    assert validate_gate_receipt(contract_path, g20["input"], g20["output"], prerequisite_receipts=g20["prerequisites"])["verdict"] == "PASS"
    assert validate_gate_receipt(contract_path, g30["input"], g30["output"], prerequisite_receipts=g30["prerequisites"])["verdict"] == "PASS"


def test_gate_rejects_forged_duplicate_and_contradictory_prerequisite_envelopes() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract_path = repo / "operations/codevolt-control-plane/assurance-contract.json"
    contract = json.loads(contract_path.read_text())
    fixtures = json.loads((repo / "operations/codevolt-control-plane/gate-receipt-fixtures.json").read_text())
    g10, g20, g30 = fixtures["gates"]

    forged = {"gate_id": "G10_SOURCE_REVIEW", "release_id": "cv-contract-fixture", "verdict": "PASS"}
    forged_input = copy.deepcopy(g20["input"])
    forged_input["prerequisite_receipts"] = [compute_receipt_sha256(forged)]
    forged_output = copy.deepcopy(g20["output"])
    forged_output["evidence_sha256"] = compute_evidence_sha256(contract, forged_input, forged_output["produced_evidence"])
    with pytest.raises(ContractError, match="closed receipt envelope"):
        validate_gate_receipt(contract_path, forged_input, forged_output, prerequisite_receipts=[forged])

    second_g20 = copy.deepcopy(g20)
    second_g20["output"]["observed_at_utc"] = "2026-09-05T08:02:01Z"
    duplicate_input = copy.deepcopy(g30["input"])
    duplicate_input["prerequisite_receipts"] = sorted(
        compute_receipt_sha256(item)
        for item in [g20, second_g20]
    )
    duplicate_output = copy.deepcopy(g30["output"])
    duplicate_output["evidence_sha256"] = compute_evidence_sha256(contract, duplicate_input, duplicate_output["produced_evidence"])
    with pytest.raises(ContractError, match="cardinality"):
        validate_gate_receipt(contract_path, duplicate_input, duplicate_output, prerequisite_receipts=[g20, second_g20])

    contradictory_g10 = copy.deepcopy(g10)
    test_evidence = next(item for item in contradictory_g10["input"]["evidence"] if item["type"] == "test_receipt")
    test_evidence["verdict"] = "BLOCK"
    test_evidence["failed"] = 1
    contradictory_g10["output"]["evidence_sha256"] = compute_evidence_sha256(
        contract, contradictory_g10["input"], contradictory_g10["output"]["produced_evidence"]
    )
    contradictory_input = copy.deepcopy(g20["input"])
    contradictory_input["prerequisite_receipts"] = [
        compute_receipt_sha256(contradictory_g10)
    ]
    contradictory_output = copy.deepcopy(g20["output"])
    contradictory_output["evidence_sha256"] = compute_evidence_sha256(
        contract, contradictory_input, contradictory_output["produced_evidence"]
    )
    with pytest.raises(ContractError) as failure:
        validate_gate_receipt(
            contract_path, contradictory_input, contradictory_output,
            prerequisite_receipts=[contradictory_g10],
        )
    assert failure.value.reason_code == "EVIDENCE_CONTRADICTORY"


def test_gate_schema_rejects_calendar_invalid_timestamp_membership_and_nested_order() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract_path = repo / "operations/codevolt-control-plane/assurance-contract.json"
    valid_input = _typed_gate_input("G10_SOURCE_REVIEW", "exact_byte_reviewer", [])
    valid_input["observed_at_utc"] = "2026-99-99T99:99:99Z"
    with pytest.raises(ContractError, match="timestamp"):
        build_gate_receipt(
            contract_path, valid_input, verdict="PASS", produced_evidence=[],
            observed_at_utc="2026-09-05T08:01:00Z",
        )

    valid_input = _typed_gate_input("G10_SOURCE_REVIEW", "exact_byte_reviewer", [])
    with pytest.raises(ContractError, match="membership"):
        build_gate_receipt(
            contract_path, valid_input, verdict="BLOCK", produced_evidence=[],
            observed_at_utc="2026-09-05T08:01:00Z",
            reason_codes=["SOURCE_REVIEW_FAILED"],
            findings=[{"reason_code": "EVIDENCE_MISSING", "summary": "bad", "evidence_ids": ["a"]}],
        )
    with pytest.raises(ContractError, match="canonical contract order"):
        build_gate_receipt(
            contract_path, valid_input, verdict="BLOCK", produced_evidence=[],
            observed_at_utc="2026-09-05T08:01:00Z",
            reason_codes=["SOURCE_REVIEW_FAILED"],
            findings=[{"reason_code": "SOURCE_REVIEW_FAILED", "summary": "bad", "evidence_ids": ["z", "a"]}],
        )


def test_gate_rejects_unknown_fields_unsorted_evidence_and_bad_hash() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract_path = repo / "operations/codevolt-control-plane/assurance-contract.json"
    gate_input = _typed_gate_input("G20_SCRATCH_PROOF", "operator", [])
    gate_input["unknown"] = True
    with pytest.raises(ContractError, match="schema"):
        build_gate_receipt(contract_path, gate_input, verdict="PASS", produced_evidence=[], observed_at_utc="2026-09-05T08:01:00Z")

    unsorted_input = _typed_gate_input("G10_SOURCE_REVIEW", "exact_byte_reviewer", [])
    unsorted_input["evidence"].reverse()
    with pytest.raises(ContractError, match="canonical contract order"):
        build_gate_receipt(contract_path, unsorted_input, verdict="PASS", produced_evidence=[], observed_at_utc="2026-09-05T08:01:00Z")

    valid_input = _typed_gate_input("G10_SOURCE_REVIEW", "exact_byte_reviewer", [])
    produced = [{"type": "test_receipt", "evidence_id": "result", "suite": "control-plane", "verdict": "PASS", "passed": 1, "failed": 0, "receipt_sha256": "6" * 64, "produced_at_utc": "2026-09-05T08:01:00Z"}]
    output = build_gate_receipt(contract_path, valid_input, verdict="PASS", produced_evidence=produced, observed_at_utc="2026-09-05T08:01:00Z")
    output["evidence_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="hash mismatch"):
        validate_gate_receipt(contract_path, valid_input, output, prerequisite_receipts=[])


def test_gate_receipt_validator_cli_executes_closed_contract(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    contract_path = repo / "operations/codevolt-control-plane/assurance-contract.json"
    gate_input = _typed_gate_input("G10_SOURCE_REVIEW", "exact_byte_reviewer", [])
    produced = [{"type": "test_receipt", "evidence_id": "result", "suite": "control-plane", "verdict": "PASS", "passed": 1, "failed": 0, "receipt_sha256": "6" * 64, "produced_at_utc": "2026-09-05T08:01:00Z"}]
    gate_output = build_gate_receipt(contract_path, gate_input, verdict="PASS", produced_evidence=produced, observed_at_utc="2026-09-05T08:01:00Z")
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps(gate_input, sort_keys=True, separators=(",", ":")) + "\n")
    output_path.write_text(json.dumps(gate_output, sort_keys=True, separators=(",", ":")) + "\n")

    result = subprocess.run(
        [__import__("sys").executable, str(repo / "scripts/control_plane_release.py"), "validate-gate", "--contract", str(contract_path), "--input", str(input_path), "--output", str(output_path)],
        cwd=tmp_path, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"gate_id": "G10_SOURCE_REVIEW", "release_id": "cv-test-release", "schema_version": 1, "verdict": "PASS"}


def _update_fixture(tmp_path: Path, script_body: str) -> tuple[Path, Path, Path]:
    repo = _source_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "approved/test"], check=True)
    (repo / "docs/recovery.md").write_text("target release\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "target"], check=True)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    install = tmp_path / "runtime"
    subprocess.run(["git", "clone", "-q", str(repo), str(install)], check=True)
    subprocess.run(["git", "-C", str(install), "reset", "--hard", "-q", "HEAD^"], check=True)
    updater = tmp_path / "hermes"
    updater.write_text(script_body, encoding="utf-8")
    updater.chmod(0o755)
    return build.manifest_path, install, updater


@pytest.mark.live_system_guard_bypass
def test_scratch_global_effect_attempt_blocks_through_supported_updater(tmp_path: Path) -> None:
    manifest_path, install, updater = _update_fixture(
        tmp_path,
        "#!/bin/sh\n"
        "test \"$1\" = update && test \"$HERMES_RELEASE_SCRATCH\" = 1 || exit 91\n"
        "# Exit 92 is the updater adapter's explicit attempted-global-effect signal.\n"
        "exit 92\n",
    )
    with pytest.raises(ContractError) as failure:
        run_supported_update(
            manifest_path, install, updater, scratch=True,
            pid_snapshot=lambda: {"live": "unchanged"},
        )
    assert failure.value.reason_code == "SCRATCH_GLOBAL_EFFECT"


@pytest.mark.live_system_guard_bypass
def test_changed_live_pid_identity_blocks_through_supported_updater(tmp_path: Path) -> None:
    manifest_path, install, updater = _update_fixture(
        tmp_path,
        "#!/bin/sh\n"
        "git -C \"$HERMES_AGENT_ROOT\" fetch -q origin \"$3\" && "
        "git -C \"$HERMES_AGENT_ROOT\" reset --hard -q FETCH_HEAD\n",
    )
    snapshots = iter(({"live": "birth-1"}, {"live": "birth-2"}))
    with pytest.raises(ContractError) as failure:
        run_supported_update(
            manifest_path, install, updater, scratch=True,
            pid_snapshot=lambda: next(snapshots),
        )
    assert failure.value.reason_code == "LIVE_PROCESS_TOUCHED"


def test_wrong_gateway_runtime_blocks_through_installed_shape(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    build = build_release(repo, _spec(repo), tmp_path / "build")
    root = tmp_path / "installed"
    install_release(build.archive_path, root)
    (repo / "docs/recovery.md").write_text("new runtime identity\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "runtime moved"], check=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    with pytest.raises(ContractError) as failure:
        run_installed_shape(
            build.manifest_path, root,
            python_executable=Path(__import__("sys").executable), cwd=cwd,
            runtime_root=repo,
        )
    assert failure.value.reason_code == "GATEWAY_RUNTIME_MISMATCH"


def test_activation_hash_tables_match_manifest_payload_bytes() -> None:
    repo = Path(__file__).resolve().parents[2]
    receipt = validate_activation_document_hashes(repo, repo / "operations/codevolt-control-plane/release/manifest.json")
    assert receipt["verdict"] == "PASS"


def test_rc4_candidate_preserves_accepted_work_claims_1_6_1() -> None:
    repo = Path(__file__).resolve().parents[2]
    spec = json.loads((repo / "operations/codevolt-control-plane/release-spec.json").read_text())
    manifest = load_manifest(repo / "operations/codevolt-control-plane/release/manifest.json")
    accepted_hashes = {
        "plugins/work_claims/plugin.yaml": "e6127feb837fe97621448ac8e5b68dfae585951088e6316a414c1c4c65da5fbb",
        "plugins/work_claims/__init__.py": "80e4333a17562145e02f542edaa0ceabf83b7e3ec55e2cb18fbd4e1d127c0bc5",
        "plugins/work_claims/core.py": "d79e4b90ebc0884b95e8b617bc1cab565bcf7c92b98c3d376aca2af30f6f587f",
        "plugins/work_claims/README.md": "854e8f1e082dbe28556a4ff7cb7f7cf583db2c545d5812a1eb3a3f2e7bc2e729",
        "plugins/work_claims/test_work_claims.py": "f7a395f53ffd36a10bc974ea89b57a74108aef52b87e859aeaa3cedb8b85c377",
    }
    for relative, expected in accepted_hashes.items():
        assert hashlib.sha256((repo / relative).read_bytes()).hexdigest() == expected

    expected_destinations = {
        "scripts/codevolt_continuity_guard.py",
        "scripts/hermes_state_common.py",
        "scripts/tests/run_launchd_canary.py",
        "scripts/tests/launchd_canary_worker.py",
        "Library/LaunchAgents/com.codevolt.continuity-guard.plist",
        "release-checks/test_codevolt_continuity_guard.py",
    }
    assert spec["policy_version"] == "cv-continuity-guard-v1"
    assert spec["production_profiles"] == []
    assert {item["relative_destination"] for item in spec["destinations"]} == expected_destinations
    assert not [item for item in spec["destinations"] if item["destination_class"] == "plugin"]
    assert spec["release_channel"] == "candidate/codevolt-control-plane-rc4-continuity-source"
    assert manifest["release_id"] == spec["release_id"]
    assert manifest["release_channel"] == spec["release_channel"]
    assert manifest["policy_version"] == "cv-continuity-guard-v1"
    assert manifest["production_profiles"] == []
    assert {item["relative_destination"] for item in manifest["destinations"]} == expected_destinations
    assert not [item for item in manifest["destinations"] if item["destination_class"] == "plugin"]
