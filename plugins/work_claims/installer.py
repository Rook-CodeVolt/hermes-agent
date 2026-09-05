"""Fail-closed, hash-provenanced distribution logic for the work-claims plugin.

This module is the canonical implementation. The production entrypoint,
``scripts/install_work_claims.py``, contains no distribution logic of its
own: it loads this file under a synthetic module name and delegates the
whole transaction to ``distribute()``. The pre-existing live installer at
``~/.hermes/scripts/install_work_claims.py`` -- pinned below as
``PRODUCTION_INSTALLER_SHA256`` and kept as the *admitted migration source*
-- hardcodes ``ROOT = Path("/Users/rook/.hermes")`` and a fixed profile
tuple, deletes each destination before validating anything, copies files
directly into the live destination (no staging, no atomicity), and has no
committed manifest or pinned provenance -- so a partially copied or
corrupted source can be distributed with no way to detect or roll it back,
and it cannot be exercised in a test without touching real profile
directories.

This module never defaults to a real Hermes home -- ``source``, ``root``,
and ``profiles`` are always passed in -- so it is fully exercisable against
a throwaway temp directory, and adds the safety properties the live
installer lacks:

1. Cryptographically pinned provenance. ``PRODUCTION_INSTALLER_SHA256`` pins
   the sha256 of the live installer script; ``verify_production_installer_provenance``
   proves (read-only) that it has not drifted since this candidate's review.
   ``APPROVED_MANIFEST_SHA256`` pins the sha256 of the committed
   ``MANIFEST.json`` in this directory; a manifest that does not match this
   hash is treated as untrusted and rejected before it is ever read for
   content.
2. Complete source validation before any destination is touched. Every file
   the approved manifest lists must exist, be a regular (non-symlink) file,
   and hash-match, or nothing is distributed to anyone.
3. Per-profile staging on the same filesystem as the destination (a sibling
   directory under the same parent), so the final swap is a single
   filesystem rename rather than a sequence of individual file copies.
4. Hash verification of the staged copy, an atomic directory swap (the
   previous destination -- if any -- is renamed aside, then the staged
   directory is renamed into place; the destination is therefore never
   observed as a partial mix of old and new files), and an exact readback
   verification of the swapped-in destination against the manifest.
5. Multi-profile transaction semantics: if any profile fails staging,
   verification, or swap, every profile already swapped in the same
   ``distribute()`` call is rolled back to its pre-call state before the
   error propagates -- all or nothing across the whole profile set.
6. Crash/interruption recovery. ``recover()`` resolves any state left by a
   prior run that did not finish (killed process, host crash mid-swap): a
   destination is always left either exactly as it was before the
   interrupted run, or exactly as the run intended to leave it -- never
   missing and never partially populated. ``distribute()`` calls it before
   starting a new transaction. The interrupted run's own transaction marker
   is the authoritative record of which profiles participated -- not the
   caller's argument -- and it is deleted only once every one of those
   participants has been resolved. A marker that cannot be parsed is fatal
   (``RecoveryError``) and is never deleted: it is the evidence.
7. Atomic, durable marker persistence. Because an unparseable marker is
   fatal, the marker's own name must never exist holding partial bytes: it
   is written and fsynced under a same-directory temp name and renamed into
   place, and its removal is flushed too. A crash inside the write leaves no
   marker at all -- which is the truth, since the marker precedes every
   destination the run would touch. See ``_write_txn_marker``.
8. Fail-closed at both ends of that persistence. The payload is written by a
   loop that advances until every byte is accepted (``_write_all``), because
   a short ``os.write`` is a legal result and a single call would publish
   truncated JSON under the marker's name -- the exact state that poisons
   every later run. The directory flush that makes the marker's name, and
   later its removal, durable is decided by a total policy over ``os.name``
   (``_DIRECTORY_FSYNC_POLICY``) rather than a boolean capability test:
   ``posix`` requires it and its failure propagates rather than being
   swallowed (``_fsync_dir``), ``nt`` is the one platform where the flush is
   documented as unavailable, and any other value raises
   ``UnsupportedPlatformError`` instead of inheriting the skip. Reporting
   success over a marker a crash could still un-publish or un-delete would
   defeat the whole mechanism, and an unrecognised platform must not be able
   to claim that success by default. Retiring the marker is
   ``distribute()``'s commit point and happens while every profile's
   pre-swap content still exists, so a failure there rolls the call back
   instead of committing it silently.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "MANIFEST.json"
_TXN_MARKER_FILENAME = ".work_claims_install_txn.json"
# Same-directory temp name the marker is written under before it is renamed
# onto _TXN_MARKER_FILENAME. Never authoritative: see _write_txn_marker.
_TXN_MARKER_TMP_PREFIX = ".work_claims_install_txn.tmp-"
_PREVIOUS_PREFIX = ".work_claims.previous-"
_STAGING_PREFIX = ".work_claims.staging-"

# sha256 of ~/.hermes/scripts/install_work_claims.py, captured 2026-09-02 from
# the untouched production file and unchanged since: this is the *admitted
# migration source* -- the exact bytes of the installer that the repository's
# scripts/install_work_claims.py replaces. The live file is never written by
# this candidate. See PROVENANCE.md.
PRODUCTION_INSTALLER_SHA256 = (
    "dc20b4cdc08cf605d5183b2167c4da592c9ad5ad4ada3c3861826f101367f91e"
)

# sha256 of this directory's committed MANIFEST.json. See PROVENANCE.md.
APPROVED_MANIFEST_SHA256 = (
    "34527d834ca834199132712a82a02357a4c06397576b0ba6627d483450b158ec"
)


class ProvenanceError(RuntimeError):
    """Source, manifest, or production-installer provenance failed to verify."""


class InstallTransactionError(RuntimeError):
    """A multi-profile distribution failed partway through and was rolled back."""


class RecoveryError(RuntimeError):
    """State recorded by an interrupted run could not be fully resolved.

    Raised without deleting anything: the transaction marker and every
    unresolved artifact are left exactly as found, because they are the only
    evidence of what the interrupted run was doing. A caller must inspect
    them before re-running ``distribute()``.
    """


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_production_installer_provenance(production_installer_path: Path) -> None:
    """Read-only proof that the pinned hash still matches the real production
    installer. Never writes to ``production_installer_path``."""
    if not production_installer_path.is_file() or production_installer_path.is_symlink():
        raise ProvenanceError(
            f"production installer not found: {production_installer_path}"
        )
    actual = sha256_file(production_installer_path)
    if actual != PRODUCTION_INSTALLER_SHA256:
        raise ProvenanceError(
            "production installer has drifted from the pinned provenance hash "
            f"({actual} != {PRODUCTION_INSTALLER_SHA256}); re-review before "
            "trusting this candidate's distribution logic"
        )


def load_manifest(source: Path) -> dict[str, Any]:
    """Load and provenance-check the manifest. Its own hash must match the
    pinned ``APPROVED_MANIFEST_SHA256`` before its contents are trusted."""
    manifest_path = source / MANIFEST_FILENAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ProvenanceError(f"missing or invalid manifest: {manifest_path}")
    actual = sha256_file(manifest_path)
    if actual != APPROVED_MANIFEST_SHA256:
        raise ProvenanceError(
            "MANIFEST.json does not match the approved, pinned hash "
            f"({actual} != {APPROVED_MANIFEST_SHA256}); refusing to trust an "
            "unapproved manifest"
        )
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ProvenanceError("manifest has no 'files' entry")
    for name, digest in files.items():
        if not isinstance(digest, str) or len(digest) != 64:
            raise ProvenanceError(f"manifest entry for {name!r} is not a sha256 hex digest")
    return manifest


def verify_source_complete(source: Path, manifest: dict[str, Any]) -> None:
    """Validate every manifest-listed source file before any destination is
    touched. Raises with every problem found, not just the first."""
    problems: list[str] = []
    for name, expected_digest in manifest["files"].items():
        path = source / name
        if not path.is_file() or path.is_symlink():
            problems.append(f"{name}: missing or not a regular file")
            continue
        actual = sha256_file(path)
        if actual != expected_digest:
            problems.append(f"{name}: sha256 mismatch (expected {expected_digest}, got {actual})")
    if problems:
        raise ProvenanceError("source validation failed:\n" + "\n".join(problems))


def _copy_set(manifest: dict[str, Any]) -> list[str]:
    return [*manifest["files"], MANIFEST_FILENAME]


def _verify_dir_matches_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    problems: list[str] = []
    for name, expected in manifest["files"].items():
        path = directory / name
        if not path.is_file() or path.is_symlink():
            problems.append(f"{name}: missing")
        elif sha256_file(path) != expected:
            problems.append(f"{name}: hash mismatch")
    manifest_copy = directory / MANIFEST_FILENAME
    if not manifest_copy.is_file() or manifest_copy.is_symlink():
        problems.append(f"{MANIFEST_FILENAME}: missing")
    elif sha256_file(manifest_copy) != APPROVED_MANIFEST_SHA256:
        problems.append(f"{MANIFEST_FILENAME}: hash mismatch")
    if any(path.is_symlink() for path in directory.rglob("*")):
        problems.append("symlink present")
    if problems:
        raise InstallTransactionError("; ".join(problems))


def _stage_profile(source: Path, manifest: dict[str, Any], staging_dir: Path) -> None:
    staging_dir.mkdir(mode=0o755)
    for name in _copy_set(manifest):
        shutil.copy2(source / name, staging_dir / name)
    _verify_dir_matches_manifest(staging_dir, manifest)


def _swap_in(destination: Path, staged: Path) -> Path | None:
    """Atomically replace ``destination`` with ``staged``. Returns the path
    holding the pre-swap content (for rollback), or ``None`` if there was
    none. Both renames are single filesystem operations, so ``destination``
    is never observed as a mix of old and new files -- only, transiently,
    absent."""
    previous = None
    moved_aside = False
    if destination.exists() or destination.is_symlink():
        previous = destination.parent / f"{_PREVIOUS_PREFIX}{uuid.uuid4().hex}"
        os.replace(destination, previous)
        moved_aside = True
    try:
        os.replace(staged, destination)
    except Exception:
        # Best-effort immediate self-heal: a failure between the two renames
        # (e.g. a transient filesystem error) must not leave the destination
        # missing for the rest of this process's lifetime -- restore it now,
        # in addition to recover() being the backstop for a true crash.
        if moved_aside:
            os.replace(previous, destination)
        raise
    return previous


def _remove_dir_or_link(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _rollback_swap(destination: Path, previous: Path | None) -> None:
    if destination.exists() or destination.is_symlink():
        _remove_dir_or_link(destination)
    if previous is not None and previous.exists():
        os.replace(previous, destination)


def _read_marker_participants(marker: Path) -> list[str]:
    """The profiles an interrupted run recorded as participants.

    The marker is the only authoritative record of who was in the aborted
    transaction -- the caller's profile list may be a subset, may be empty,
    or may belong to an entirely different run. Anything that cannot be
    parsed into a list of profile names is fatal: an unreadable marker means
    unknown participants, and silently treating it as "no transaction" would
    discard exactly the evidence needed to finish the recovery.
    """
    if marker.is_symlink():
        raise RecoveryError(
            f"transaction marker {marker} is a symlink, not a regular file; it "
            "is left in place as evidence -- inspect it before re-running "
            "distribute()"
        )
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            f"transaction marker {marker} could not be parsed ({exc}); it is "
            "left in place as evidence -- inspect it before re-running "
            "distribute()"
        ) from exc
    participants = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(participants, list) or not all(
        isinstance(entry, str) and entry for entry in participants
    ):
        raise RecoveryError(
            f"transaction marker {marker} does not record a list of profile "
            "names, so the participants of the interrupted run are unknown; it "
            "is left in place as evidence -- inspect it before re-running "
            "distribute()"
        )
    return list(dict.fromkeys(participants))


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _ordered_artifacts(plugins_dir: Path, prefix: str) -> list[Path]:
    """Artifacts sharing ``prefix``, oldest first.

    Two interrupted runs can each leave a ``.previous-*`` sibling, and the
    one holding the true pre-transaction content is the one written first.
    The token in the name is a random uuid4 and carries no ordering, so the
    filesystem timestamp is the ordering signal; the name breaks ties so the
    order is total (and therefore the choice deterministic) even when both
    land within one timestamp granule.
    """
    return sorted(plugins_dir.glob(f"{prefix}*"), key=lambda path: (_mtime_ns(path), path.name))


def _discard(path: Path) -> None:
    """Best-effort removal of a stale artifact. Failures are not raised here:
    the caller re-globs afterwards and reports whatever survived."""
    try:
        _remove_dir_or_link(path)
    except OSError:
        pass


def recover(root: Path, profiles: list[str]) -> list[str]:
    """Resolve state left by a distribute() run that did not finish.

    The transaction marker's own participant list is authoritative and is
    unioned with ``profiles`` (which may be empty, a subset, or a superset).
    Every participant ends this call in exactly one state: its
    pre-transaction content, or genuinely absent as it was before any run
    ever touched it -- never missing-when-it-should-exist, never partially
    populated. Safe to call with nothing to recover (a no-op cleanup pass).

    The marker is deleted only once every participant has been resolved. If
    the marker cannot be parsed, or any participant is left holding an
    artifact that could not be removed, ``RecoveryError`` is raised and
    nothing -- marker included -- is deleted.
    """
    marker = root / _TXN_MARKER_FILENAME
    marker_present = marker.is_symlink() or marker.exists()
    marker_participants = _read_marker_participants(marker) if marker_present else []

    recovery_set = list(dict.fromkeys([*marker_participants, *profiles]))
    resolved: list[str] = []
    unresolved: list[str] = []

    for name in recovery_set:
        plugins_dir = root / "profiles" / name / "plugins"
        if not plugins_dir.is_dir():
            # The participant has no profile directory at all, so the
            # interrupted run never reached it and it has nothing to restore.
            continue
        destination = plugins_dir / "work_claims"
        previous_candidates = _ordered_artifacts(plugins_dir, _PREVIOUS_PREFIX)

        destination_missing = not destination.exists() and not destination.is_symlink()
        if destination_missing and previous_candidates:
            # Crashed between "moved old destination aside" and "moved the
            # staged directory into place": the swap never completed, so
            # restore what was there before this run started.
            os.replace(previous_candidates[0], destination)
            previous_candidates = previous_candidates[1:]
            resolved.append(name)
        elif marker_present and not destination_missing and previous_candidates:
            # This profile's own swap completed, but the run crashed before
            # the whole multi-profile transaction committed. The
            # all-or-nothing guarantee means this profile rolls back too.
            _remove_dir_or_link(destination)
            os.replace(previous_candidates[0], destination)
            previous_candidates = previous_candidates[1:]
            resolved.append(name)

        for stale in previous_candidates:
            _discard(stale)
        for stale in _ordered_artifacts(plugins_dir, _STAGING_PREFIX):
            _discard(stale)

        survivors = [
            *plugins_dir.glob(f"{_PREVIOUS_PREFIX}*"),
            *plugins_dir.glob(f"{_STAGING_PREFIX}*"),
        ]
        if survivors:
            unresolved.append(f"{name}: {sorted(path.name for path in survivors)}")

    # A crash inside _write_txn_marker leaves a temp file and no marker. It
    # names no participants and authorises nothing -- the marker precedes every
    # destination a run would touch, so a kill in that window provably changed
    # none of them. Discarded here whatever the caller's profile list says,
    # because the cleanup does not depend on knowing who was in the run.
    if root.is_dir():
        for stale in sorted(root.glob(f"{_TXN_MARKER_TMP_PREFIX}*")):
            _discard(stale)
        stray = sorted(path.name for path in root.glob(f"{_TXN_MARKER_TMP_PREFIX}*"))
        if stray:
            unresolved.append(f"{root}: {stray}")

    if unresolved:
        raise RecoveryError(
            "recovery could not resolve every participant of the interrupted "
            f"run; {marker} and the surviving artifacts are left in place as "
            "evidence:\n" + "\n".join(unresolved)
        )
    if marker_present:
        try:
            _remove_txn_marker(marker)
        except OSError as exc:
            raise RecoveryError(
                f"every participant of the interrupted run was resolved, but "
                f"the removal of {marker} could not be persisted ({exc}); a "
                "crash now would replay this recovery against an already "
                "resolved tree -- resolve the marker before re-running "
                "distribute()"
            ) from exc
    return resolved


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte of ``payload`` to ``fd``, or raise.

    ``os.write`` may accept fewer bytes than it was handed -- a short write is
    a legal outcome on a regular file, not an error -- so one call is not a
    write. Writing under a temp name buys atomicity of *publication* and
    nothing else: whatever the temp file holds is exactly what ``os.replace``
    installs at the marker's own name. A truncated payload published there
    does not merely lose the participant record, it poisons the install:
    ``_read_marker_participants`` treats anything unparseable as fatal, and
    ``distribute()`` runs ``recover()`` first, so every later run refuses to
    start behind it.

    A call reporting no progress is the one short-write result a loop cannot
    advance on, so it fails the write rather than spinning on it forever.
    """
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(
                errno.EIO,
                f"write reported {written} bytes of progress with {len(view)} "
                "of the transaction marker still to write",
            )
        view = view[written:]


# The two verdicts the directory-flush policy can return. REQUIRED means the
# platform can flush a directory and a failure to do so is this installer's
# failure; UNAVAILABLE means the platform offers no such operation at all, so
# skipping it is the only behaviour available rather than a decision to skip.
DIRECTORY_FSYNC_REQUIRED = "required"
DIRECTORY_FSYNC_UNAVAILABLE = "unavailable"

# The policy is a *total* mapping over ``os.name``, deliberately not a boolean
# predicate. A boolean answers "not required" for every value it does not
# recognise, which silently routes an unknown platform onto the branch that
# skips the flush -- the one branch that lets an install report success over a
# marker nothing made durable. Only the two names below have a reviewed
# durability story; anything else is an unsupported platform, not a default.
_DIRECTORY_FSYNC_POLICY: dict[str, str] = {
    # A directory can be opened read-only and fsynced, so the rename that
    # publishes the marker -- and the unlink that retires it -- have a defined
    # way to be made durable, and a failure to do so is a real failure.
    "posix": DIRECTORY_FSYNC_REQUIRED,
    # Windows offers no equivalent: a directory cannot be opened for flushing
    # at all. This is the sole documented carve-out. It narrows what the marker
    # guarantees on Windows and changes nothing on macOS or Linux.
    "nt": DIRECTORY_FSYNC_UNAVAILABLE,
}


class UnsupportedPlatformError(RuntimeError):
    """The running platform has no reviewed directory-durability policy."""


def _directory_fsync_policy() -> str:
    """The directory-flush verdict for the running platform.

    Read at call time rather than frozen at import so every branch is directly
    exercisable. Raises rather than guessing: an unrecognised ``os.name`` is a
    platform whose durability semantics nobody has established, and the only
    correct resolution is a reviewed entry in ``_DIRECTORY_FSYNC_POLICY`` --
    not an install that quietly proceeds without the flush.
    """
    try:
        return _DIRECTORY_FSYNC_POLICY[os.name]
    except KeyError:
        raise UnsupportedPlatformError(
            f"no directory-durability policy for os.name {os.name!r}: the "
            f"transaction marker can only be made durable on 'posix', and "
            f"'nt' is the only platform where that flush is known to be "
            f"unavailable. Refusing to install rather than skip a flush whose "
            f"absence would let a crash un-publish or un-delete the marker"
        ) from None


def _fsync_dir(path: Path) -> None:
    """Persist a directory entry, so a crash cannot un-create or un-delete it.

    Renaming a durable file into place makes its *bytes* survive a power loss;
    the name that reaches them lives in the parent directory and needs its own
    flush. Where ``_directory_fsync_policy`` reports that flush REQUIRED, its
    failure is the caller's failure and propagates. Swallowing it -- whether
    the directory could not be opened or could not be flushed -- would let
    ``distribute()`` report success over a marker whose publication, or whose
    removal, a crash could still undo: precisely the state the marker exists to
    make impossible.

    The dispatch below is exhaustive on purpose. Falling through to either the
    flush or the skip on an unrecognised verdict would reintroduce exactly the
    implicit default the policy mapping exists to remove.
    """
    policy = _directory_fsync_policy()
    if policy == DIRECTORY_FSYNC_REQUIRED:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    if policy == DIRECTORY_FSYNC_UNAVAILABLE:
        return
    raise UnsupportedPlatformError(
        f"unhandled directory-durability verdict {policy!r} for os.name "
        f"{os.name!r}"
    )


def _write_txn_marker(root: Path, manifest_sha256: str, profiles: list[str]) -> Path:
    """Publish the transaction marker atomically and durably.

    The marker is the only record of who participated in a run that dies
    without finishing, and ``_read_marker_participants`` deliberately treats
    anything it cannot parse as fatal evidence rather than as "no
    transaction". How the marker reaches the disk is therefore part of the
    recovery contract, not an implementation detail: writing the bytes into
    the marker's own name would leave a window in which that name exists
    holding zero or partial bytes, and a ``SIGKILL`` inside it would turn a
    recoverable interruption into a permanent ``RecoveryError`` on every
    subsequent run -- the installer would refuse to start behind evidence of
    a transaction that never began.

    So the payload is written and fsynced into a same-directory temp file
    (same directory so the rename is a single filesystem operation), and only
    a complete, durable file is renamed onto the marker's name. A crash before
    that rename leaves the marker's name untouched, which is the truth: the
    marker precedes every destination this call would touch. The root
    directory is fsynced afterwards, because bytes that are durable inside a
    file no directory entry yet names are not durable at all.

    The temp file carries no authority and is never read back. ``recover()``
    discards any that a crash left behind.
    """
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _TXN_MARKER_FILENAME
    payload = json.dumps(
        {"status": "in-progress", "manifest_sha256": manifest_sha256, "profiles": profiles}
    ).encode("utf-8")

    temp = root / f"{_TXN_MARKER_TMP_PREFIX}{uuid.uuid4().hex}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            # Complete payload first, then flush it: flushing a partially
            # written file only makes a truncated marker durably wrong.
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, marker)
    except BaseException:
        # Including KeyboardInterrupt: a half-written temp file left in the
        # Hermes root is litter that later runs would have to reason about.
        _discard(temp)
        raise
    # Deliberately outside that handler: the rename has already happened, so
    # there is no temp file left to discard, and a failure here must leave the
    # published marker in place as the evidence recover() reads.
    _fsync_dir(root)
    return marker


def _remove_txn_marker(marker: Path) -> None:
    """Remove the marker and persist the removal.

    Cleanup a crash can undo is not cleanup: an unlink whose directory entry
    never reaches the disk leaves the next process recovering behind a marker
    for a transaction that actually committed, rolling every profile back off
    a good install.
    """
    if not marker.is_symlink() and not marker.exists():
        return
    marker.unlink()
    _fsync_dir(marker.parent)


def distribute(
    source: Path,
    root: Path,
    profiles: list[str],
    *,
    production_installer_path: Path | None = None,
) -> dict[str, Path]:
    """Fail-closed distribution of the manifest-approved plugin source set.

    Validates the entire source set against the committed, hash-pinned
    ``MANIFEST.json`` before any destination is touched, stages each
    profile's copy on the same filesystem as its destination, verifies the
    staged hashes, swaps each profile in with a single atomic rename, reads
    each destination back and re-verifies it, and rolls back every profile
    already swapped in this call if any later profile fails. Recovers from a
    prior interrupted run before starting.

    If ``production_installer_path`` is given, its sha256 is checked against
    the pinned ``PRODUCTION_INSTALLER_SHA256`` first (read-only) -- proving
    this call's provenance traces to an unmodified production installer.
    """
    if production_installer_path is not None:
        verify_production_installer_provenance(production_installer_path)

    manifest = load_manifest(source)
    verify_source_complete(source, manifest)
    profiles = list(profiles)

    recover(root, profiles)

    marker = _write_txn_marker(root, sha256_file(source / MANIFEST_FILENAME), profiles)

    installed: dict[str, Path] = {}
    swapped: list[tuple[Path, Path | None]] = []
    try:
        for name in profiles:
            destination = root / "profiles" / name / "plugins" / "work_claims"
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.parent / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
            try:
                _stage_profile(source, manifest, staging)
                previous = _swap_in(destination, staging)
                _verify_dir_matches_manifest(destination, manifest)  # exact readback
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            swapped.append((destination, previous))
            installed[name] = destination
    except Exception as exc:
        for destination, previous in reversed(swapped):
            _rollback_swap(destination, previous)
        try:
            _remove_txn_marker(marker)
        except OSError as marker_exc:
            raise InstallTransactionError(
                f"distribution failed and was rolled back: {exc}; the "
                f"transaction marker {marker} could not be durably retired "
                f"({marker_exc}) -- recover() will resolve it"
            ) from exc
        raise InstallTransactionError(f"distribution failed and was rolled back: {exc}") from exc

    # Retiring the marker is this call's commit point, so it happens while
    # every profile's pre-swap content is still available: a removal that
    # cannot be made durable rolls the whole call back rather than reporting a
    # success a crash could re-read as an aborted transaction and undo.
    try:
        _remove_txn_marker(marker)
    except OSError as exc:
        for destination, previous in reversed(swapped):
            _rollback_swap(destination, previous)
        raise InstallTransactionError(
            f"every profile swapped in, but retiring the transaction marker "
            f"{marker} could not be persisted ({exc}); the call was rolled back"
        ) from exc

    for _, previous in swapped:
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
    return installed
