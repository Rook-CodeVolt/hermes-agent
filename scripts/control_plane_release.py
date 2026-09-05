#!/usr/bin/env python3
"""Deterministic, fail-closed CodeVolt control-plane release tooling.

The builder binds exact Git commit/tree identity to content-addressed payload
bytes. The installer and scratch harness only accept that closed manifest and
never discover profiles or destinations from ambient state.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PRODUCTION_PROFILES: Tuple[str, ...] = (
    "clara", "daniel", "elias", "hannah", "maya", "oliver", "rook", "sophie"
)
REQUIRED_CLASSES = {
    "plugin", "helper", "launchd", "migration", "check", "fixture", "recovery_documentation"
}
SPEC_FIELDS = {
    "schema_version", "release_id", "policy_version", "release_channel",
    "production_profiles", "toolchain", "restart_scope", "migration",
    "rollback_release_id", "destinations",
}
DESTINATION_FIELDS = {
    "logical_name", "source", "profile", "destination_class",
    "relative_destination", "mode", "owner_class", "plugin_version",
    "dependency_order",
}
MANIFEST_FIELDS = {
    "schema_version", "release_id", "policy_version", "source_commit",
    "source_tree", "release_channel", "toolchain", "production_profiles",
    "destinations", "restart_scope", "migration", "rollback_release_id",
    "preimages",
}
MANIFEST_DESTINATION_FIELDS = {
    "logical_name", "profile", "destination_class", "relative_destination",
    "sha256", "byte_length", "mode", "owner_class", "plugin_version",
    "dependency_order", "payload_path",
}


class ContractError(RuntimeError):
    """The candidate is not the exact, closed release described by the contract."""

    def __init__(self, message: str, *, reason_code: str = "SCHEMA_INVALID") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class BuildResult:
    manifest_bytes: bytes
    archive_bytes: bytes
    manifest_sha256: str
    manifest_path: Path
    archive_path: Path


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ContractError(f"git identity unavailable: {exc.stderr.strip()}") from exc


def _safe_relative(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ContractError(f"{field} must be a normalized relative POSIX path")
    return path


def _validate_spec(spec: Mapping[str, Any]) -> None:
    if set(spec) != SPEC_FIELDS:
        raise ContractError("release specification fields are not the closed v1 set")
    if spec["schema_version"] != 1:
        raise ContractError("unsupported release specification schema")
    if spec["policy_version"] != "cv-control-plane-assurance-v2":
        raise ContractError("wrong policy version")
    if tuple(spec["production_profiles"]) != PRODUCTION_PROFILES:
        raise ContractError("production profile roster must be the exact declared fleet")
    if not isinstance(spec["destinations"], list) or not spec["destinations"]:
        raise ContractError("destinations must be a non-empty list")
    seen: set = set()
    classes = set()
    plugin_profiles = set()
    for item in spec["destinations"]:
        if not isinstance(item, dict) or set(item) != DESTINATION_FIELDS:
            raise ContractError("destination fields are not the closed v1 set")
        _safe_relative(item["source"], "source")
        _safe_relative(item["relative_destination"], "relative_destination")
        profile = item["profile"]
        if profile not in ("root", *PRODUCTION_PROFILES):
            raise ContractError(f"unknown profile destination: {profile}")
        key = (profile, item["relative_destination"])
        if key in seen:
            raise ContractError(f"duplicate destination: {profile}:{item['relative_destination']}")
        seen.add(key)
        classes.add(item["destination_class"])
        if item["destination_class"] == "plugin":
            plugin_profiles.add(profile)
        try:
            mode = int(item["mode"], 8)
        except (TypeError, ValueError) as exc:
            raise ContractError("mode must be a four-digit octal string") from exc
        if item["mode"] != f"{mode:04o}" or mode & 0o7000:
            raise ContractError("unsafe or non-canonical mode")
        if not isinstance(item["dependency_order"], int) or item["dependency_order"] < 0:
            raise ContractError("dependency_order must be a non-negative integer")
    if not REQUIRED_CLASSES.issubset(classes):
        raise ContractError(f"release unit is partial; missing classes: {sorted(REQUIRED_CLASSES - classes)}")
    if plugin_profiles != {"root", *PRODUCTION_PROFILES}:
        raise ContractError(
            "plugin distribution must cover root and every declared profile",
            reason_code="EXACT_TASK_OR_DISTRIBUTION_MISMATCH",
        )


def _tracked_head_bytes(repo: Path, relative: str) -> bytes:
    path = repo / relative
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"source is missing, non-regular, or a symlink: {relative}")
    try:
        head_bytes = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"HEAD:{relative}"], stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as exc:
        raise ContractError(f"source is not committed at HEAD: {relative}") from exc
    current = path.read_bytes()
    if current != head_bytes:
        raise ContractError(f"source differs from HEAD: {relative}")
    return current


def _archive(manifest_bytes: bytes, payloads: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        members = [("manifest.json", manifest_bytes)] + [
            (f"payload/{digest}", payloads[digest]) for digest in sorted(payloads)
        ]
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def build_release(repo: Path, spec: Mapping[str, Any], output_dir: Path) -> BuildResult:
    """Build the exact deterministic manifest and content-addressed archive."""
    repo = Path(repo).resolve()
    _validate_spec(spec)
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    payloads: Dict[str, bytes] = {}
    destinations: List[Dict[str, Any]] = []
    for raw in spec["destinations"]:
        content = _tracked_head_bytes(repo, raw["source"])
        digest = hashlib.sha256(content).hexdigest()
        payloads.setdefault(digest, content)
        destinations.append({
            "logical_name": raw["logical_name"],
            "profile": raw["profile"],
            "destination_class": raw["destination_class"],
            "relative_destination": raw["relative_destination"],
            "sha256": digest,
            "byte_length": len(content),
            "mode": raw["mode"],
            "owner_class": raw["owner_class"],
            "plugin_version": raw["plugin_version"],
            "dependency_order": raw["dependency_order"],
            "payload_path": f"payload/{digest}",
        })
    destinations.sort(key=lambda item: (
        item["dependency_order"], item["profile"], item["relative_destination"], item["logical_name"]
    ))
    manifest = {
        "schema_version": 1,
        "release_id": spec["release_id"],
        "policy_version": spec["policy_version"],
        "source_commit": commit,
        "source_tree": tree,
        "release_channel": spec["release_channel"],
        "toolchain": spec["toolchain"],
        "production_profiles": list(PRODUCTION_PROFILES),
        "destinations": destinations,
        "restart_scope": spec["restart_scope"],
        "migration": spec["migration"],
        "rollback_release_id": spec["rollback_release_id"],
        "preimages": "captured-at-install",
    }
    activation_sources = {
        item["source"] for item in spec["destinations"]
        if item["source"].endswith("ACTIVATION_MANIFEST.md")
    }
    if activation_sources:
        expected_activation_sources = {
            "operations/codevolt-control-plane/ACTIVATION_MANIFEST.md",
            "operations/codevolt-control-plane/continuity/ACTIVATION_MANIFEST.md",
        }
        if activation_sources != expected_activation_sources:
            raise ContractError("release must package both activation manifest copies")
        validate_activation_document_hashes(repo, manifest)
    manifest_bytes = _canonical(manifest)
    archive_bytes = _archive(manifest_bytes, payloads)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = output_dir / "manifest.json"
    archive_path = output_dir / "release.tar"
    manifest_path.write_bytes(manifest_bytes)
    archive_path.write_bytes(archive_bytes)
    return BuildResult(
        manifest_bytes=manifest_bytes,
        archive_bytes=archive_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_path=manifest_path,
        archive_path=archive_path,
    )


def load_manifest(path: Path) -> Dict[str, Any]:
    """Load and validate the closed canonical manifest representation."""
    raw = Path(path).read_bytes()
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ContractError("manifest fields are not the closed v1 set")
    if raw != _canonical(manifest):
        raise ContractError("manifest is not canonical JSON")
    if manifest["schema_version"] != 1 or manifest["policy_version"] != "cv-control-plane-assurance-v2":
        raise ContractError("manifest schema or policy is not admitted")
    if tuple(manifest["production_profiles"]) != PRODUCTION_PROFILES:
        raise ContractError("manifest profile roster is not the exact declared fleet")
    destinations = manifest.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        raise ContractError("manifest destinations must be non-empty")
    seen = set()
    plugin_profiles = set()
    classes = set()
    for item in destinations:
        if not isinstance(item, dict) or set(item) != MANIFEST_DESTINATION_FIELDS:
            raise ContractError("manifest destination fields are not the closed v1 set")
        profile = item["profile"]
        if profile not in ("root", *PRODUCTION_PROFILES):
            raise ContractError("manifest contains an unknown profile")
        relative = str(_safe_relative(item["relative_destination"], "relative_destination"))
        if (profile, relative) in seen:
            raise ContractError("manifest contains a duplicate destination")
        seen.add((profile, relative))
        classes.add(item["destination_class"])
        if item["destination_class"] == "plugin":
            plugin_profiles.add(profile)
        digest = item["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ContractError("manifest contains an invalid sha256")
        if item["payload_path"] != f"payload/{digest}":
            raise ContractError("manifest payload path is not content-addressed")
    if not REQUIRED_CLASSES.issubset(classes) or plugin_profiles != {"root", *PRODUCTION_PROFILES}:
        raise ContractError("manifest release unit or profile distribution is partial")
    return manifest


def _manifest_target(root: Path, item: Mapping[str, Any]) -> Path:
    base = root if item["profile"] == "root" else root / "profiles" / item["profile"]
    return base.joinpath(*PurePosixPath(item["relative_destination"]).parts)


def _ensure_safe_parent(root: Path, target: Path, created: List[Path]) -> None:
    relative = target.relative_to(root)
    cursor = root
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink() or not cursor.is_dir():
                raise ContractError(f"destination parent is not a real directory: {relative}")
        else:
            cursor.mkdir(mode=0o755)
            created.append(cursor)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ContractError(f"destination is not a regular file: {relative}")
    if target.exists() and target.stat().st_nlink != 1:
        raise ContractError(f"destination has multiple hard links: {relative}")


def _shape_digest(root: Path, manifest: Mapping[str, Any]) -> str:
    shape = []
    for item in manifest["destinations"]:
        target = _manifest_target(root, item)
        relative = target.relative_to(root).as_posix()
        if target.exists():
            content = target.read_bytes()
            shape.append({"path": relative, "exists": True, "sha256": hashlib.sha256(content).hexdigest(), "byte_length": len(content), "mode": f"{target.stat().st_mode & 0o777:04o}"})
        else:
            shape.append({"path": relative, "exists": False})
    return hashlib.sha256(_canonical(shape)).hexdigest()


def install_release(archive_path: Path, root: Path, *, fail_after: Optional[int] = None) -> Dict[str, Any]:
    """Install all destinations as one exact-preimage rollback transaction.

    ``fail_after`` is a scratch-only deterministic fault-injection boundary.
    This function never invokes an updater, service manager, launchd, or gateway.
    """
    archive_path = Path(archive_path)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ContractError("install root must be a real directory")
    root = root.resolve()
    try:
        archive = tarfile.open(archive_path, mode="r:")
    except (OSError, tarfile.TarError) as exc:
        raise ContractError("release archive is unreadable") from exc
    with archive:
        members = archive.getmembers()
        if any(not member.isfile() or member.name.startswith("/") or ".." in PurePosixPath(member.name).parts for member in members):
            raise ContractError("archive contains an unsafe member")
        names = [member.name for member in members]
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ContractError("archive member inventory is invalid")
        manifest_member = archive.extractfile("manifest.json")
        if manifest_member is None:
            raise ContractError("archive manifest is missing")
        manifest_bytes = manifest_member.read()
        with tempfile.TemporaryDirectory(dir=str(archive_path.parent)) as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            manifest = load_manifest(manifest_path)
        expected_names = {"manifest.json", *(item["payload_path"] for item in manifest["destinations"])}
        if set(names) != expected_names:
            raise ContractError("archive has missing or extra payload members")
        payloads: Dict[str, bytes] = {}
        for item in manifest["destinations"]:
            name = item["payload_path"]
            if name not in payloads:
                stream = archive.extractfile(name)
                if stream is None:
                    raise ContractError(f"payload is missing: {name}")
                content = stream.read()
                if len(content) != item["byte_length"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
                    raise ContractError(f"payload digest mismatch: {name}")
                payloads[name] = content

    preimage_digest = _shape_digest(root, manifest)
    preimages: Dict[Path, Optional[Tuple[bytes, int]]] = {}
    changed: List[Path] = []
    created_dirs: List[Path] = []
    changed_count = 0
    try:
        for index, item in enumerate(manifest["destinations"], start=1):
            target = _manifest_target(root, item)
            _ensure_safe_parent(root, target, created_dirs)
            desired_mode = int(item["mode"], 8)
            if (
                target.exists()
                and target.stat().st_mode & 0o777 == desired_mode
                and hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]
            ):
                continue
            if target not in preimages:
                preimages[target] = (target.read_bytes(), target.stat().st_mode & 0o777) if target.exists() else None
            content = payloads[item["payload_path"]]
            fd, stage_name = tempfile.mkstemp(prefix=".cv-release-", dir=str(target.parent))
            stage = Path(stage_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                stage.chmod(desired_mode)
                os.replace(stage, target)
            finally:
                if stage.exists():
                    stage.unlink()
            changed.append(target)
            changed_count += 1
            if fail_after is not None and index == fail_after:
                raise ContractError(f"injected failure after destination {index}")
    except BaseException:
        for target in reversed(changed):
            previous = preimages[target]
            if previous is None:
                if target.exists():
                    target.unlink()
            else:
                content, mode = previous
                target.write_bytes(content)
                target.chmod(mode)
        for directory in sorted(set(created_dirs), key=lambda value: len(value.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        if _shape_digest(root, manifest) != preimage_digest:
            raise ContractError("transaction rollback did not restore exact preimages")
        raise

    return {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "preimage_sha256": preimage_digest,
        "postimage_sha256": _shape_digest(root, manifest),
        "destinations_installed": len(manifest["destinations"]),
        "changed_destinations": changed_count,
        "verdict": "PASS",
    }


def run_installed_shape(
    manifest_path: Path,
    root: Path,
    *,
    python_executable: Path,
    cwd: Path,
    runtime_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify exact installed bytes and import each plugin in every profile scope."""
    manifest = load_manifest(manifest_path)
    root = Path(root).resolve()
    cwd = Path(cwd).resolve()
    runtime_commit: Optional[str] = None
    runtime_tree: Optional[str] = None
    if not cwd.is_dir() or cwd == root or root in cwd.parents:
        raise ContractError("scratch cwd must be an existing unrelated directory")
    for item in manifest["destinations"]:
        target = _manifest_target(root, item)
        if target.is_symlink() or not target.is_file():
            reason = (
                "INSTALLED_SHAPE_IMPORT_FAILED"
                if item["destination_class"] == "helper"
                else "MANIFEST_DESTINATION_MISSING"
            )
            raise ContractError(
                f"installed destination missing: {item['logical_name']}",
                reason_code=reason,
            )
        content = target.read_bytes()
        if len(content) != item["byte_length"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ContractError(
                f"installed destination digest mismatch: {item['logical_name']}",
                reason_code="RELEASE_UNIT_PARTIAL",
            )
        if target.stat().st_mode & 0o777 != int(item["mode"], 8):
            raise ContractError(f"installed destination mode mismatch: {item['logical_name']}")

    package_names = sorted({
        PurePosixPath(item["relative_destination"]).parts[1]
        for item in manifest["destinations"]
        if item["destination_class"] == "plugin"
        and len(PurePosixPath(item["relative_destination"]).parts) >= 2
        and PurePosixPath(item["relative_destination"]).parts[0] == "plugins"
    })
    if not package_names:
        raise ContractError("no importable plugin package is declared")
    runtime_modules = (
        ["hermes_state", "hermes_state_registry"]
        if any(
            item["profile"] == "root"
            and item["logical_name"] == "hermes-state-common"
            and item["relative_destination"] == "scripts/hermes_state_common.py"
            for item in manifest["destinations"]
        )
        else []
    )
    profiles = ["root", *manifest["production_profiles"]]
    with tempfile.TemporaryDirectory(prefix="cv-runtime-export-", dir=str(cwd)) as export_dir:
        immutable_runtime = ""
        if runtime_root is not None:
            runtime_root = Path(runtime_root).resolve()
            runtime_commit = _git(runtime_root, "rev-parse", "HEAD")
            runtime_tree = _git(runtime_root, "rev-parse", "HEAD^{tree}")
            if runtime_commit != manifest["source_commit"] or runtime_tree != manifest["source_tree"]:
                raise ContractError(
                    "runtime source identity does not match the release manifest",
                    reason_code="GATEWAY_RUNTIME_MISMATCH",
                )
            archive_bytes = subprocess.check_output(
                ["git", "-C", str(runtime_root), "archive", "--format=tar", runtime_commit],
                stderr=subprocess.PIPE,
            )
            export_root = Path(export_dir).resolve()
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as source_archive:
                members = source_archive.getmembers()
                if any(
                    member.issym() or member.islnk() or member.name.startswith("/")
                    or ".." in PurePosixPath(member.name).parts
                    for member in members
                ):
                    raise ContractError("runtime Git archive contains an unsafe member")
                source_archive.extractall(export_root)
            if _git(runtime_root, "rev-parse", f"{runtime_commit}^{{tree}}") != runtime_tree:
                raise ContractError("runtime Git object identity changed during export")
            immutable_runtime = str(export_root)
        for profile in profiles:
            home = root if profile == "root" else root / "profiles" / profile
            plugin_root = home / "plugins"
            code = (
                "import importlib,sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "sys.path.insert(0,sys.argv[2]) if sys.argv[2] else None;"
                "[importlib.import_module(name) for name in sys.argv[3:]]"
            )
            environment = {
                "HOME": str(home),
                "HERMES_HOME": str(home),
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "PYTHONNOUSERSITE": "1",
                "TMPDIR": str(cwd),
            }
            result = subprocess.run(
                [str(python_executable), "-I", "-c", code, str(plugin_root), immutable_runtime, *package_names],
                cwd=str(cwd), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=30,
            )
            if result.returncode != 0:
                raise ContractError(
                    f"installed-shape plugin import failed for profile {profile}",
                    reason_code="INSTALLED_SHAPE_IMPORT_FAILED",
                )
        if runtime_modules:
            code = (
                "import importlib,sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "sys.path.insert(0,sys.argv[2]);"
                "[importlib.import_module(name) for name in sys.argv[3:]]"
            )
            result = subprocess.run(
                [
                    str(python_executable), "-I", "-c", code,
                    immutable_runtime, str(root / "scripts"), *runtime_modules,
                ],
                cwd=str(cwd), env={
                    "HOME": str(root),
                    "HERMES_HOME": str(root),
                    "PATH": "/usr/bin:/bin",
                    "LC_ALL": "C",
                    "PYTHONNOUSERSITE": "1",
                    "TMPDIR": str(cwd),
                },
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise ContractError(
                    "installed-shape runtime import failed with the packaged state helper",
                    reason_code="INSTALLED_SHAPE_IMPORT_FAILED",
                )
    return {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "verdict": "PASS",
        "profiles_checked": profiles,
        "packages_checked": package_names,
        "runtime_modules_checked": runtime_modules,
        "cwd": "arbitrary",
        "environment": "minimal",
        "runtime_commit": runtime_commit,
        "runtime_tree": runtime_tree,
        "installed_shape_sha256": _shape_digest(root, manifest),
    }


def _hermes_pid_snapshot() -> Dict[str, str]:
    """Return pseudonymous PID+birth+command identities for live Hermes processes."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,lstart=,command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError("live PID inventory failed")
    identities: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "hermes" not in line.lower():
            continue
        digest = hashlib.sha256(line.strip().encode("utf-8")).hexdigest()
        identities[digest[:16]] = digest
    return identities


def run_supported_update(
    manifest_path: Path,
    install_root: Path,
    updater: Path,
    *,
    scratch: bool,
    pid_snapshot: Callable[[], Dict[str, str]] = _hermes_pid_snapshot,
) -> Dict[str, Any]:
    """Exercise the supported ``hermes update`` family against a synthetic clone.

    The remote branch must already resolve to the manifest's full commit. Scratch
    mode is mandatory here and exports an explicit service-effect suppression
    contract to the updater. Any failure or postimage mismatch resets the
    synthetic clone to its exact pre-update commit before returning an error.
    """
    if not scratch:
        raise ContractError("this harness admits scratch updater rehearsal only")
    manifest = load_manifest(manifest_path)
    install_root = Path(install_root).resolve()
    updater = Path(updater).resolve()
    if updater.name != "hermes" or not updater.is_file() or updater.is_symlink():
        raise ContractError("updater must be an exact real-file hermes entrypoint")
    if _git(install_root, "status", "--porcelain"):
        raise ContractError("synthetic runtime preimage must be clean")
    pre_commit = _git(install_root, "rev-parse", "HEAD")
    pre_tree = _git(install_root, "rev-parse", "HEAD^{tree}")
    remote = _git(
        install_root,
        "ls-remote",
        "--exit-code",
        "origin",
        f"refs/heads/{manifest['release_channel']}",
    ).split()
    if not remote or remote[0] != manifest["source_commit"]:
        raise ContractError("approved release channel moved or does not match the manifest")
    before_pids = pid_snapshot()
    command = [str(updater), "update", "--branch", manifest["release_channel"], "--yes", "--keep-stash"]
    environment = {
        "HOME": str(install_root.parent),
        "HERMES_HOME": str(install_root.parent / "synthetic-hermes-home"),
        "HERMES_AGENT_ROOT": str(install_root),
        "HERMES_RELEASE_SCRATCH": "1",
        "HERMES_SUPPRESS_SERVICE_EFFECTS": "1",
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }
    result = subprocess.run(
        command,
        cwd=str(install_root),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    try:
        if result.returncode != 0:
            reason = "SCRATCH_GLOBAL_EFFECT" if result.returncode == 92 else "SCHEMA_INVALID"
            raise ContractError("supported updater rehearsal failed", reason_code=reason)
        post_commit = _git(install_root, "rev-parse", "HEAD")
        post_tree = _git(install_root, "rev-parse", "HEAD^{tree}")
        if post_commit != manifest["source_commit"] or post_tree != manifest["source_tree"]:
            raise ContractError("supported updater did not land the manifest commit and tree")
        after_pids = pid_snapshot()
        if before_pids != after_pids:
            raise ContractError(
                "live PID birth identities changed during scratch updater proof",
                reason_code="LIVE_PROCESS_TOUCHED",
            )
    except BaseException:
        subprocess.run(
            ["git", "-C", str(install_root), "reset", "--hard", "-q", pre_commit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        raise
    return {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "verdict": "PASS",
        "command": ["hermes", *command[1:]],
        "pre_commit": pre_commit,
        "pre_tree": pre_tree,
        "post_commit": post_commit,
        "post_tree": post_tree,
        "global_effects_suppressed": True,
        "live_pid_birth_identities_unchanged": True,
        "live_pid_identity_count": len(after_pids),
    }


def run_restart_handoff(
    manifest_path: Path,
    runner: Path,
    *,
    scratch: bool,
    receipt_path: Path,
    pid_snapshot: Callable[[], Dict[str, str]] = _hermes_pid_snapshot,
) -> Dict[str, Any]:
    """Exercise only the manifest restart scope through a scratch effect adapter."""
    if not scratch:
        raise ContractError("this harness admits scratch restart handoff only")
    manifest = load_manifest(manifest_path)
    runner = Path(runner).resolve()
    if runner.name != "restart-handoff" or not runner.is_file() or runner.is_symlink():
        raise ContractError("restart runner must be the scratch restart-handoff adapter")
    scope = manifest["restart_scope"]
    if not isinstance(scope, list) or not scope or any(not isinstance(label, str) or not label for label in scope):
        raise ContractError("restart scope must be a non-empty list of labels")
    if len(scope) != len(set(scope)):
        raise ContractError("restart scope contains duplicates")
    before_pids = pid_snapshot()
    environment = {
        "HOME": str(Path(receipt_path).parent),
        "HERMES_RELEASE_SCRATCH": "1",
        "HERMES_SUPPRESS_SERVICE_EFFECTS": "1",
        "RESTART_RECEIPT": str(Path(receipt_path)),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }
    result = subprocess.run(
        [str(runner), *scope],
        cwd=str(Path(receipt_path).parent),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ContractError("scratch restart handoff failed")
    after_pids = pid_snapshot()
    if before_pids != after_pids:
        raise ContractError(
            "live PID birth identities changed during scratch restart handoff",
            reason_code="LIVE_PROCESS_TOUCHED",
        )
    return {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "verdict": "PASS",
        "restart_scope": list(scope),
        "global_effects_suppressed": True,
        "live_pid_birth_identities_unchanged": True,
        "live_pid_identity_count": len(after_pids),
    }


def _payload_projection(manifest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    fields = (
        "profile", "destination_class", "relative_destination", "sha256",
        "byte_length", "mode", "owner_class", "plugin_version", "dependency_order",
    )
    return [{field: item[field] for field in fields} for item in manifest["destinations"]]


def prove_forward_correction(
    prior_manifest_path: Path,
    current_manifest_path: Path,
    corrective_manifest_path: Path,
    installed_root: Path,
) -> Dict[str, Any]:
    """Prove a new release restores prior payload without identity rollback."""
    prior = load_manifest(prior_manifest_path)
    current = load_manifest(current_manifest_path)
    corrective = load_manifest(corrective_manifest_path)
    if corrective["rollback_release_id"] != current["release_id"]:
        raise ContractError("corrective release does not name the immediate current release")
    if corrective["release_id"] in {prior["release_id"], current["release_id"]}:
        raise ContractError("forward correction must have a new release identity")
    if corrective["source_commit"] in {prior["source_commit"], current["source_commit"]}:
        raise ContractError("forward correction must have a new source commit")
    if _payload_projection(corrective) != _payload_projection(prior):
        raise ContractError("forward correction does not restore the prior payload")
    if corrective["migration"].get("to_schema") != prior["migration"].get("to_schema"):
        raise ContractError("forward correction is not schema-compatible with the prior payload")
    for item in corrective["destinations"]:
        target = _manifest_target(Path(installed_root).resolve(), item)
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
            raise ContractError("installed forward correction does not match its manifest")
    return {
        "schema_version": 1,
        "verdict": "PASS",
        "reason": "FORWARD_CORRECTION_OK",
        "prior_release_id": prior["release_id"],
        "replaced_release_id": current["release_id"],
        "corrective_release_id": corrective["release_id"],
        "installed_shape_sha256": _shape_digest(Path(installed_root).resolve(), corrective),
    }


def _strict_json_file(path: Path) -> Any:
    def reject_duplicates(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"duplicate JSON key in {Path(path).name}")
            result[key] = value
        return result

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON document: {Path(path).name}") from exc


def _jcs(value: Any) -> bytes:
    """Canonicalize the contract's integer-only JSON subset as RFC 8785 bytes."""
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value).encode("ascii")
    if isinstance(value, float):
        raise ContractError("floating-point values are not admitted by the receipt contract")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, list):
        return b"[" + b",".join(_jcs(item) for item in value) + b"]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ContractError("JSON object keys must be strings")
        # RFC 8785 inherits ECMAScript's UTF-16 code-unit property ordering.
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return b"{" + b",".join(_jcs(key) + b":" + _jcs(value[key]) for key in keys) + b"}"
    raise ContractError("non-JSON value is not admitted")


def _resolve_pointer(root: Mapping[str, Any], pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise ContractError("only local schema references are admitted")
    value: Any = root
    for token in pointer[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or token not in value:
            raise ContractError(f"schema reference does not resolve: {pointer}")
        value = value[token]
    return value


def _schema_validate(
    value: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str = "$",
    *,
    instance_root: Optional[Any] = None,
    input_root: Optional[Mapping[str, Any]] = None,
) -> None:
    """Fail-closed validator for every JSON-Schema keyword used by this contract."""
    if instance_root is None:
        instance_root = value
    if "$ref" in schema:
        _schema_validate(
            value, _resolve_pointer(root, schema["$ref"]), root, path,
            instance_root=instance_root, input_root=input_root,
        )
        return
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                _schema_validate(
                    value, candidate, root, path,
                    instance_root=instance_root, input_root=input_root,
                )
            except ContractError:
                continue
            matches += 1
        if matches != 1:
            raise ContractError(f"gate schema oneOf mismatch at {path}")
    expected_type = schema.get("type")
    type_checks = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    type_ok = type_checks.get(expected_type, True) if isinstance(expected_type, str) else True
    if not type_ok:
        raise ContractError(f"gate schema type mismatch at {path}")
    if "const" in schema and value != schema["const"]:
        raise ContractError(f"gate schema const mismatch at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"gate schema enum mismatch at {path}")
    enum_source = schema.get("x-enum-source")
    if enum_source is not None and value not in _resolve_pointer(root, enum_source):
        raise ContractError(f"gate schema sourced enum mismatch at {path}")
    member_field = schema.get("x-member-of-output")
    if member_field is not None:
        if (
            not isinstance(instance_root, dict)
            or not isinstance(instance_root.get(member_field), list)
            or value not in instance_root[member_field]
        ):
            raise ContractError(f"gate schema output membership mismatch at {path}")
    equals_input = schema.get("x-equals-input")
    if equals_input is not None and (
        input_root is None or equals_input not in input_root or value != input_root[equals_input]
    ):
        raise ContractError(f"gate schema input equality mismatch at {path}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ContractError(f"gate schema required fields missing at {path}: {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ContractError(f"gate schema unknown fields at {path}")
        for key, item in value.items():
            if key in properties:
                _schema_validate(
                    item, properties[key], root, f"{path}.{key}",
                    instance_root=instance_root, input_root=input_root,
                )
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise ContractError(f"gate schema array length mismatch at {path}")
        if schema.get("uniqueItems") and len({_jcs(item) for item in value}) != len(value):
            raise ContractError(f"gate schema duplicate array item at {path}")
        if "items" in schema:
            for index, item in enumerate(value):
                _schema_validate(
                    item, schema["items"], root, f"{path}[{index}]",
                    instance_root=instance_root, input_root=input_root,
                )
        if "x-sort-key" in schema:
            _require_sorted(value, schema["x-sort-key"], path)
        empty_iff = schema.get("x-empty-iff-verdict")
        if empty_iff is not None:
            if not isinstance(instance_root, dict) or instance_root.get("verdict") not in {"PASS", "BLOCK"}:
                raise ContractError(f"gate schema verdict relation unavailable at {path}")
            if (instance_root["verdict"] == empty_iff) != (len(value) == 0):
                raise ContractError(f"gate schema verdict emptiness mismatch at {path}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise ContractError(f"gate schema string length mismatch at {path}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ContractError(f"gate schema pattern mismatch at {path}")
        if schema.get("format") == "date-time":
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value) is None:
                raise ContractError(f"gate schema timestamp mismatch at {path}")
            try:
                parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00")
            except ValueError as exc:
                raise ContractError(f"gate schema timestamp mismatch at {path}") from exc
            if parsed.utcoffset() != datetime.timedelta(0):
                raise ContractError(f"gate schema timestamp is not UTC at {path}")
    if isinstance(value, int) and not isinstance(value, bool) and value < schema.get("minimum", value):
        raise ContractError(f"gate schema minimum mismatch at {path}")
    for child in schema.get("allOf", []):
        condition = child.get("if")
        applies = True
        if condition is not None:
            try:
                _schema_validate(
                    value, condition, root, path,
                    instance_root=instance_root, input_root=input_root,
                )
            except ContractError:
                applies = False
        if applies and "then" in child:
            _schema_validate(
                value, child["then"], root, path,
                instance_root=instance_root, input_root=input_root,
            )


def _require_sorted(items: Sequence[Any], sort_key: Any, field: str) -> None:
    if sort_key == "lexicographic":
        desired = sorted(items)
    else:
        fields = tuple(sort_key)
        desired = sorted(items, key=lambda item: tuple(item[name] for name in fields))
        identities = [tuple(item[name] for name in fields) for item in items]
        if len(identities) != len(set(identities)):
            raise ContractError(f"duplicate typed evidence identity in {field}")
    if list(items) != desired:
        raise ContractError(f"{field} is not in canonical contract order")


def compute_evidence_sha256(
    contract: Mapping[str, Any], gate_input: Mapping[str, Any], produced_evidence: Sequence[Mapping[str, Any]]
) -> str:
    hash_contract = contract["receipt_hash_contract"]
    if hash_contract.get("contract_version") != 2 or hash_contract.get("algorithm") != "SHA-256":
        raise ContractError("receipt hash contract identity is not admitted")
    prefix = bytes.fromhex(hash_contract["domain_prefix_hex"])
    if prefix != b"CV-GATE-RECEIPT\0v2\0":
        raise ContractError("receipt hash domain prefix is not admitted")
    envelope: Dict[str, Any] = {"schema_version": 1, "evidence": list(produced_evidence)}
    _schema_validate(envelope, hash_contract["produced_evidence_envelope"], contract)
    _require_sorted(envelope["evidence"], ("type", "evidence_id"), "produced evidence")
    input_bytes = _jcs(dict(gate_input))
    envelope_bytes = _jcs(envelope)
    framed = prefix + struct.pack(">Q", len(input_bytes)) + input_bytes + struct.pack(">Q", len(envelope_bytes)) + envelope_bytes
    return hashlib.sha256(framed).hexdigest()


def compute_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(_jcs(dict(receipt))).hexdigest()


def _evidence_contradicts_pass(items: Sequence[Mapping[str, Any]]) -> bool:
    """Return true when typed evidence itself denies a literal PASS."""
    for item in items:
        if item.get("verdict") == "BLOCK":
            return True
        if item.get("type") == "test_receipt" and item.get("failed", 0) != 0:
            return True
    return False


def _validate_gate_pair(
    contract: Mapping[str, Any],
    gate_input: Mapping[str, Any],
    gate_output: Mapping[str, Any],
    prerequisite_envelopes: Sequence[Mapping[str, Any]],
    *,
    depth: int,
) -> None:
    if depth > len(contract["gates"]):
        raise ContractError("gate prerequisite chain exceeds the admitted gate graph")
    _schema_validate(gate_input, contract["gate_input_schema"], contract)
    _schema_validate(
        gate_output, contract["gate_output_schema"], contract,
        input_root=gate_input,
    )
    _require_sorted(gate_input["prerequisite_receipts"], "lexicographic", "prerequisite receipts")
    _require_sorted(gate_input["evidence"], ("type", "evidence_id"), "input evidence")
    _require_sorted(gate_output["reason_codes"], "lexicographic", "reason codes")
    _require_sorted(gate_output["produced_evidence"], ("type", "evidence_id"), "produced evidence")
    gate_id = gate_input["gate_id"]
    gates = {gate["id"]: gate for gate in contract["gates"]}
    gate = gates.get(gate_id)
    if gate is None or gate_input["stage"] != gate["stage"]:
        raise ContractError("gate stage does not match the admitted gate")
    if gate_input["actor_role"] != gate["owner_role"]:
        raise ContractError("gate actor role does not match the admitted owner")
    for field in ("gate_id", "release_id", "stage", "actor"):
        if gate_output[field] != gate_input[field]:
            raise ContractError(f"gate output {field} does not match input")
    input_types = {item["type"] for item in gate_input["evidence"]}
    type_map = contract["gate_evidence_type_map"][gate_id]
    if not set(type_map["input_required_types"]).issubset(input_types):
        raise ContractError("required typed gate evidence is missing", reason_code="EVIDENCE_MISSING")
    if not {item["type"] for item in gate_output["produced_evidence"]}.issubset(type_map["produced_allowed_types"]):
        raise ContractError("gate produced an inadmissible evidence type")
    admitted_codes = set(contract["gate_reason_code_map"][gate_id])
    if not set(gate_output["reason_codes"]).issubset(admitted_codes):
        raise ContractError("gate output contains an inadmissible reason code")
    expected_hash = compute_evidence_sha256(contract, gate_input, gate_output["produced_evidence"])
    if gate_output["evidence_sha256"] != expected_hash:
        raise ContractError("gate receipt evidence hash mismatch")
    if gate_output["verdict"] == "PASS" and _evidence_contradicts_pass(
        [*gate_input["evidence"], *gate_output["produced_evidence"]]
    ):
        raise ContractError(
            "literal PASS is contradicted by admitted evidence",
            reason_code="EVIDENCE_CONTRADICTORY",
        )

    expected_prerequisites = [
        item.split(":", 1)[0] for item in gate["prerequisites"] if item.startswith("G")
    ]
    if len(prerequisite_envelopes) != len(expected_prerequisites):
        raise ContractError("gate prerequisite receipt cardinality is not exact", reason_code="PREREQUISITE_NOT_PASS")
    supplied_by_gate: Dict[str, Mapping[str, Any]] = {}
    for envelope in prerequisite_envelopes:
        if not isinstance(envelope, dict) or set(envelope) != {"input", "output", "prerequisites"}:
            raise ContractError("gate prerequisite is not a closed receipt envelope", reason_code="PREREQUISITE_NOT_PASS")
        prior_input = envelope["input"]
        prior_output = envelope["output"]
        nested = envelope["prerequisites"]
        if not isinstance(prior_input, dict) or not isinstance(prior_output, dict) or not isinstance(nested, list):
            raise ContractError("gate prerequisite envelope types are invalid", reason_code="PREREQUISITE_NOT_PASS")
        _validate_gate_pair(contract, prior_input, prior_output, nested, depth=depth + 1)
        prior_id = prior_output["gate_id"]
        if prior_id in supplied_by_gate:
            raise ContractError("gate prerequisite receipt is duplicated", reason_code="PREREQUISITE_NOT_PASS")
        supplied_by_gate[prior_id] = envelope
        for identity_field in ("release_id", "source_commit", "source_tree", "policy_version", "manifest_sha256"):
            if prior_input[identity_field] != gate_input[identity_field]:
                raise ContractError("gate prerequisite release identity differs", reason_code="PREREQUISITE_NOT_PASS")
    if list(supplied_by_gate) != expected_prerequisites:
        raise ContractError("gate prerequisite receipt sequence is not exact", reason_code="PREREQUISITE_NOT_PASS")
    supplied_hashes = sorted(compute_receipt_sha256(item) for item in prerequisite_envelopes)
    if supplied_hashes != gate_input["prerequisite_receipts"]:
        raise ContractError("gate prerequisite envelope hashes do not match", reason_code="PREREQUISITE_NOT_PASS")
    for prerequisite_id in expected_prerequisites:
        receipt = supplied_by_gate[prerequisite_id]["output"]
        if receipt["verdict"] != "PASS":
            raise ContractError("gate prerequisite is not literal PASS", reason_code="PREREQUISITE_NOT_PASS")


def validate_gate_receipt(
    contract_path: Path,
    gate_input: Mapping[str, Any],
    gate_output: Mapping[str, Any],
    *,
    prerequisite_receipts: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    contract = _strict_json_file(contract_path)
    _validate_gate_pair(
        contract, gate_input, gate_output, prerequisite_receipts, depth=0
    )
    return dict(gate_output)


def build_gate_receipt(
    contract_path: Path,
    gate_input: Mapping[str, Any],
    *,
    verdict: str,
    produced_evidence: Sequence[Mapping[str, Any]],
    observed_at_utc: str,
    reason_codes: Sequence[str] = (),
    findings: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    contract = _strict_json_file(contract_path)
    _schema_validate(gate_input, contract["gate_input_schema"], contract)
    _require_sorted(gate_input["prerequisite_receipts"], "lexicographic", "prerequisite receipts")
    _require_sorted(gate_input["evidence"], ("type", "evidence_id"), "input evidence")
    _require_sorted(produced_evidence, ("type", "evidence_id"), "produced evidence")
    output = {
        "schema_version": 1,
        "gate_id": gate_input["gate_id"],
        "release_id": gate_input["release_id"],
        "stage": gate_input["stage"],
        "verdict": verdict,
        "reason_codes": list(reason_codes),
        "findings": list(findings),
        "produced_evidence": list(produced_evidence),
        "evidence_sha256": compute_evidence_sha256(contract, gate_input, produced_evidence),
        "actor": gate_input["actor"],
        "observed_at_utc": observed_at_utc,
    }
    _schema_validate(output, contract["gate_output_schema"], contract, input_root=gate_input)
    return output


def validate_activation_document_hashes(repo: Path, manifest_source: Any) -> Dict[str, Any]:
    repo = Path(repo).resolve()
    manifest = load_manifest(Path(manifest_source)) if isinstance(manifest_source, (str, Path)) else manifest_source
    paths = [
        repo / "operations/codevolt-control-plane/ACTIVATION_MANIFEST.md",
        repo / "operations/codevolt-control-plane/continuity/ACTIVATION_MANIFEST.md",
    ]
    documents = [path.read_bytes() for path in paths]
    if documents[0] != documents[1]:
        raise ContractError("packaged activation manifests are not byte-identical")
    table = re.findall(rb"^([0-9a-f]{64})  ([^\r\n]+)$", documents[0], flags=re.MULTILINE)
    if not table:
        raise ContractError("activation manifest hash table is missing")
    destination_hashes = {
        item["relative_destination"]: item["sha256"]
        for item in manifest["destinations"] if item["profile"] == "root"
    }
    checked = []
    for raw_digest, raw_relative in table:
        digest = raw_digest.decode("ascii")
        relative = raw_relative.decode("utf-8")
        _safe_relative(relative, "activation hash path")
        actual = destination_hashes.get(relative)
        if digest != actual:
            raise ContractError(f"activation manifest hash is stale: {relative}", reason_code="ROLLBACK_DOCUMENTATION_STALE")
        checked.append(relative)
    return {"schema_version": 1, "verdict": "PASS", "paths_checked": checked}


def validate_incident_registry(contract_path: Path, registry_path: Path, repo: Path) -> Dict[str, Any]:
    """Prove every contract vector has one resolvable RED-to-GREEN regression."""
    contract = _strict_json_file(contract_path)
    registry = _strict_json_file(registry_path)
    if set(registry) != {"schema_version", "policy_version", "vectors"}:
        raise ContractError("incident registry fields are not the closed v1 set")
    if registry["schema_version"] != 1 or registry["policy_version"] != contract["contract_id"]:
        raise ContractError("incident registry policy identity mismatch")
    contract_vectors = contract.get("test_vectors")
    vectors = registry.get("vectors")
    if not isinstance(contract_vectors, list) or not isinstance(vectors, list):
        raise ContractError("incident vectors must be lists")
    expected_ids = [item["id"] for item in contract_vectors]
    actual_ids = [item.get("id") for item in vectors if isinstance(item, dict)]
    if actual_ids != expected_ids or len(actual_ids) != len(set(actual_ids)):
        raise ContractError("incident registry does not exactly cover contract vectors in order")
    repo = Path(repo).resolve()
    by_id = {item["id"]: item for item in contract_vectors}
    for item in vectors:
        if set(item) != {"id", "fixture", "expected", "reason", "baseline", "candidate_test"}:
            raise ContractError(f"incident registry entry fields are not closed: {item.get('id')}")
        contract_item = by_id[item["id"]]
        for field in ("fixture", "expected", "reason"):
            if item[field] != contract_item[field]:
                raise ContractError(f"incident registry differs from contract: {item['id']}:{field}")
        baseline = item["baseline"]
        if not isinstance(baseline, dict) or set(baseline) != {"verdict", "evidence_ref"} or baseline["verdict"] != "RED":
            raise ContractError(f"incident has no closed RED baseline: {item['id']}")
        evidence = repo.joinpath(*_safe_relative(baseline["evidence_ref"], "evidence_ref").parts)
        if not evidence.is_file() or evidence.is_symlink():
            raise ContractError(f"incident RED evidence does not resolve: {item['id']}")
        node = item["candidate_test"]
        if not isinstance(node, str) or "::" not in node:
            raise ContractError(f"incident test node is invalid: {item['id']}")
        relative_file, test_selector = node.split("::", 1)
        test_file = repo.joinpath(*_safe_relative(relative_file, "candidate_test").parts)
        if not test_file.is_file() or test_file.is_symlink():
            raise ContractError(f"incident test file does not resolve: {item['id']}")
    selectors = [item["candidate_test"] for item in vectors]
    selector_temp_parent = repo / ".release-test-tmp"
    selector_temp_parent.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="cv-incident-pytest-", dir=str(selector_temp_parent)) as base_temp:
            environment = dict(os.environ)
            environment["TMPDIR"] = base_temp
            environment["HERMES_KANBAN_HOME"] = str(Path(base_temp) / "guarded-real-root")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--basetemp", base_temp, *selectors],
                cwd=str(repo), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=300,
            )
    finally:
        selector_temp_parent.rmdir()
    if result.returncode != 0:
        summary = result.stdout[-2000:].replace(str(repo), "<repo>")
        raise ContractError(f"incident test selectors failed:\n{summary}")
    return {
        "schema_version": 1,
        "policy_version": registry["policy_version"],
        "verdict": "PASS",
        "vector_ids": expected_ids,
        "baseline_red_count": len(vectors),
        "permanent_test_count": len(vectors),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build and verify manifest-bound control-plane releases")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build a deterministic release")
    build_parser.add_argument("--repo", type=Path, required=True)
    build_parser.add_argument("--spec", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    gate_parser = subparsers.add_parser("validate-gate", help="strictly validate a typed gate receipt")
    gate_parser.add_argument("--contract", type=Path, required=True)
    gate_parser.add_argument("--input", type=Path, required=True)
    gate_parser.add_argument("--output", type=Path, required=True)
    gate_parser.add_argument("--prerequisite", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            spec = _strict_json_file(args.spec)
            result = build_release(args.repo, spec, args.output)
            receipt = {
                "schema_version": 1,
                "release_id": spec["release_id"],
                "manifest_sha256": result.manifest_sha256,
                "archive_sha256": hashlib.sha256(result.archive_bytes).hexdigest(),
                "verdict": "PASS",
            }
            print(_canonical(receipt).decode("utf-8"), end="")
            return 0
        if args.command == "validate-gate":
            gate_input = _strict_json_file(args.input)
            gate_output = _strict_json_file(args.output)
            prerequisites = [_strict_json_file(path) for path in args.prerequisite]
            receipt = validate_gate_receipt(
                args.contract, gate_input, gate_output, prerequisite_receipts=prerequisites
            )
            summary = {
                "schema_version": 1,
                "gate_id": receipt["gate_id"],
                "release_id": receipt["release_id"],
                "verdict": receipt["verdict"],
            }
            print(_canonical(summary).decode("utf-8"), end="")
            return 0
    except ContractError as exc:
        print(json.dumps({"verdict": "BLOCK", "reason_code": exc.reason_code}), file=__import__("sys").stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
