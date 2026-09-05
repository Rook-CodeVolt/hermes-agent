"""Workspace confinement enforced in the *actual* file operation.

A pre-tool-call path check can only ever describe the filesystem as it was
at check time.  Between that check and the write, an attacker with any
foothold in the workspace can swap a directory for a symlink and redirect
the write outside -- the check/use race the CV-A01 review reproduced.

So confinement lives here, in the operation itself:

* every path component is opened **descriptor-relative** from a verified
  workspace directory descriptor under the strongest no-follow flag the
  kernel offers, so a symlink anywhere in the path fails the open rather
  than redirecting it;
* the content lands in a temp file created inside the **verified parent**
  and is moved into place with ``renameat``, so no partially-written file
  is ever observable and the rename cannot cross out of that parent;
* a target with more than one link is refused, so a hardlink planted
  inside the workspace cannot be used to mutate a file outside it;
* a name that resolves only because the filesystem case-folds is refused,
  so ``NOTES.TXT`` cannot silently replace ``notes.txt`` on APFS;
* a read-modify-write can pin its write to the exact ``(st_dev, st_ino)``
  it read (``expect_key``), so a target swapped underneath it is refused
  rather than overwritten with content derived from a stranger;
* nothing is cached: the walk and the descriptor chain are rebuilt for
  every operation, so a swap between two operations is caught by the next.

If the platform cannot provide descriptor-relative operations the module
fails closed -- it never silently degrades to a following ``open()``.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

# Darwin's O_NOFOLLOW_ANY refuses to follow a symlink at *any* component,
# not just the last -- strictly stronger than O_NOFOLLOW, which only guards
# the final one.  The two are mutually exclusive: passing both is EINVAL, so
# ``_NOFOLLOW`` picks whichever this kernel actually offers.  On Linux the
# weaker flag is sufficient because every intermediate component is opened
# individually by the descriptor chain below.
O_NOFOLLOW_ANY = 0x20000000 if sys.platform == "darwin" else 0
_NOFOLLOW = O_NOFOLLOW_ANY or os.O_NOFOLLOW

_TEMP_PREFIX = ".hermes-confined-"


class ConfinementError(RuntimeError):
    """A file operation would have escaped its confined workspace."""


class ConfinementUnavailable(ConfinementError):
    """The platform cannot enforce confinement, so nothing may be written."""


# --------------------------------------------------------------------------- #
# Path verification (shared with the work-claims mutation gate)
# --------------------------------------------------------------------------- #

def exact_case_entry(parent: Path, name: str) -> str | None:
    """Return ``name`` iff a byte-for-byte identical entry exists.

    Never trusts a case-insensitive match -- APFS is case-insensitive but
    case-preserving by default, so a lexically different name can resolve
    to the same inode.
    """
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name == name:
                    return entry.name
    except OSError:
        return None
    return None


def case_insensitive_alias(parent: Path, name: str) -> bool:
    try:
        with os.scandir(parent) as entries:
            return any(
                entry.name != name and entry.name.lower() == name.lower()
                for entry in entries
            )
    except OSError:
        return False


def canonical_walk(
    path: Path, *, require_dir: bool
) -> tuple[bool, tuple[int, int] | None, str | None]:
    """Verify ``path`` component-by-component from the filesystem root.

    Every ancestor must be an exact case match for its on-disk entry and
    must not be a symlink.  Re-derived on every call (never cached) so a
    swap between two calls is caught by the next verification rather than
    trusted from a stale result.

    Returns ``(ok, (st_dev, st_ino), reason)``.
    """
    if not path.is_absolute():
        return False, None, f"path is not absolute: {path}"
    current = Path(path.anchor)
    for part in path.relative_to(path.anchor).parts:
        real_name = exact_case_entry(current, part)
        if real_name is None:
            if case_insensitive_alias(current, part):
                return False, None, f"case-variant path alias rejected: {current / part}"
            return False, None, f"ancestor does not exist: {current / part}"
        current = current / part
        try:
            st = current.lstat()
        except OSError:
            return False, None, f"path vanished during verification: {current}"
        if stat.S_ISLNK(st.st_mode):
            return False, None, f"symlink rejected in path: {current}"
    try:
        st = current.lstat()
    except OSError:
        return False, None, f"path vanished during verification: {current}"
    if require_dir and not stat.S_ISDIR(st.st_mode):
        return False, None, f"not a directory: {current}"
    return True, (st.st_dev, st.st_ino), None


def confined_by_ancestor_walk(
    candidate: str, workspace: Path, workspace_key: tuple[int, int]
) -> tuple[bool, str | None]:
    """Confine ``candidate`` to ``workspace`` via a fresh ancestor walk.

    Deliberately not ``Path.resolve()``, which silently normalizes both
    symlinks and case aliases.  ``workspace`` must already have passed
    :func:`canonical_walk`; ``workspace_key`` is its verified
    ``(st_dev, st_ino)``, re-checked at every descended component so a
    bind-mount aliasing a subdirectory onto a different device is caught.
    """
    if not isinstance(candidate, str) or not candidate.strip():
        return False, "no path was given"
    target = Path(candidate).expanduser()
    if not target.is_absolute():
        target = workspace / target
    try:
        rel_parts = target.relative_to(workspace).parts
    except ValueError:
        return False, f"path escapes the confined workspace: {candidate}"
    if any(part == ".." for part in rel_parts):
        return False, f"path escapes the confined workspace: {candidate}"
    current = workspace
    for index, part in enumerate(rel_parts):
        is_last = index == len(rel_parts) - 1
        real_name = exact_case_entry(current, part)
        if real_name is None:
            if case_insensitive_alias(current, part):
                return False, f"case-variant path alias rejected: {current / part}"
            if is_last:
                # A not-yet-existing final component (new file/dir) is fine
                # as long as every ancestor above it was verified.
                return True, None
            return False, f"ancestor does not exist: {current / part}"
        current = current / part
        try:
            st = current.lstat()
        except OSError:
            return False, f"path vanished during verification: {current}"
        if stat.S_ISLNK(st.st_mode):
            return False, f"symlink rejected in path: {current}"
        if st.st_dev != workspace_key[0]:
            return False, f"mount/bind boundary crossed at: {current}"
        if is_last and stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            return False, f"hard-linked regular file rejected: {current}"
    return True, None


# --------------------------------------------------------------------------- #
# Platform capability
# --------------------------------------------------------------------------- #

def containment_supported() -> bool:
    """True only when every descriptor-relative primitive we need exists."""
    if os.name != "posix":
        return False
    required: Iterable[Callable[..., Any]] = (
        os.open, os.stat, os.mkdir, os.unlink, os.rename,
    )
    return all(fn in os.supports_dir_fd for fn in required)


def require_containment() -> None:
    if not containment_supported():
        raise ConfinementUnavailable(
            "descriptor-relative file confinement is unavailable on this "
            "platform; refusing to perform a confined write"
        )


# --------------------------------------------------------------------------- #
# Descriptor-relative primitives
# --------------------------------------------------------------------------- #

def _relative_parts(workspace: Path, target: str) -> tuple[str, ...]:
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        parts = candidate.relative_to(workspace).parts
    except ValueError:
        raise ConfinementError(
            f"path escapes the confined workspace {workspace}: {target}"
        ) from None
    if not parts:
        raise ConfinementError(f"refusing to operate on the workspace root: {target}")
    if any(part in ("..", ".") for part in parts):
        raise ConfinementError(
            f"path escapes the confined workspace {workspace}: {target}"
        )
    return parts


class _DirChain:
    """A verified chain of directory descriptors, closed as one unit."""

    def __init__(self, workspace: Path) -> None:
        self._fds: list[int] = []
        ok, _, reason = canonical_walk(workspace, require_dir=True)
        if not ok:
            raise ConfinementError(f"workspace failed verification: {reason}")
        try:
            root = os.open(str(workspace), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ConfinementError(f"cannot open workspace {workspace}: {exc}") from exc
        self._fds.append(root)
        self._root_key = _fd_key(root)

    @property
    def fd(self) -> int:
        return self._fds[-1]

    def descend(self, name: str, *, create: bool = False) -> None:
        """Open ``name`` under the current descriptor, refusing symlinks."""
        flags = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW
        try:
            child = os.open(name, flags, dir_fd=self.fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ConfinementError(
                    f"symlink rejected in confined path component {name!r}"
                ) from exc
            if exc.errno == errno.ENOENT and create:
                child = self._mkdir_and_open(name)
            elif exc.errno == errno.ENOTDIR:
                raise ConfinementError(
                    f"confined path component {name!r} is not a directory"
                ) from exc
            else:
                raise ConfinementError(
                    f"cannot open confined path component {name!r}: {exc}"
                ) from exc
        self._fds.append(child)
        if _fd_key(child)[0] != self._root_key[0]:
            raise ConfinementError(
                f"mount/bind boundary crossed at confined component {name!r}"
            )

    def _mkdir_and_open(self, name: str) -> int:
        try:
            os.mkdir(name, 0o755, dir_fd=self.fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ConfinementError(
                f"cannot create confined directory {name!r}: {exc}"
            ) from exc
        try:
            return os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW,
                dir_fd=self.fd,
            )
        except OSError as exc:
            raise ConfinementError(
                f"cannot open confined directory {name!r}: {exc}"
            ) from exc

    def close(self) -> None:
        while self._fds:
            try:
                os.close(self._fds.pop())
            except OSError:
                pass

    def __enter__(self) -> "_DirChain":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _fd_key(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return st.st_dev, st.st_ino


def _open_parent(workspace: Path, parts: tuple[str, ...], *, create_dirs: bool) -> _DirChain:
    chain = _DirChain(workspace)
    try:
        for part in parts[:-1]:
            chain.descend(part, create=create_dirs)
        return chain
    except Exception:
        chain.close()
        raise


def _inspect_target(parent_fd: int, name: str) -> os.stat_result | None:
    """lstat the final component without following it."""
    try:
        return os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfinementError(f"cannot inspect confined target {name!r}: {exc}") from exc


def _reject_unsafe_target(st: os.stat_result | None, name: str) -> None:
    if st is None:
        return
    if stat.S_ISLNK(st.st_mode):
        raise ConfinementError(f"symlink rejected as confined write target: {name!r}")
    if not stat.S_ISREG(st.st_mode):
        raise ConfinementError(f"confined write target is not a regular file: {name!r}")
    if st.st_nlink > 1:
        raise ConfinementError(
            f"hard-linked file rejected as confined write target: {name!r} "
            f"(st_nlink={st.st_nlink})"
        )


def _reject_case_variant(parent_fd: int, name: str, st: os.stat_result | None) -> None:
    """Refuse a name that only resolves because the filesystem case-folds.

    ``lstat`` finding an entry proves nothing about its spelling on APFS,
    which is case-insensitive but case-preserving: ``NOTES.TXT`` resolves to
    ``notes.txt`` and a rename would then replace a file the caller never
    named.  ``os.listdir`` on the parent *descriptor* gives the on-disk
    spellings without leaving the verified chain.
    """
    if st is None:
        return
    try:
        entries = os.listdir(parent_fd)
    except OSError as exc:
        raise ConfinementError(
            f"cannot verify the exact name of confined target {name!r}: {exc}"
        ) from exc
    if name not in entries:
        raise ConfinementError(
            f"case-variant path alias rejected as confined target: {name!r}"
        )


def _require_expected_target(
    st: os.stat_result | None, expect_key: tuple[int, int] | None, name: str
) -> None:
    """Bind the write to the exact file the caller read its preimage from.

    ``patch`` computes its replacement from content it read a moment ago.
    If the target is swapped in between, applying that replacement writes a
    file derived from a preimage that was never in it -- so the write is
    refused rather than applied to a stranger.
    """
    if expect_key is None:
        return
    if st is None:
        raise ConfinementError(
            f"confined write target {name!r} is no longer the file the patch "
            "was matched against: it has been removed"
        )
    if (st.st_dev, st.st_ino) != expect_key:
        raise ConfinementError(
            f"confined write target {name!r} is no longer the file the patch "
            "was matched against: it was replaced during the operation"
        )


@dataclass(frozen=True)
class ConfinedWrite:
    path: str
    bytes_written: int
    dirs_created: bool
    created: bool


def confined_write_bytes(
    workspace: Path,
    target: str,
    data: bytes,
    *,
    create_dirs: bool = True,
    expect_key: tuple[int, int] | None = None,
) -> ConfinedWrite:
    """Atomically write ``data`` to ``target``, confined to ``workspace``.

    The write is temp-file + ``renameat`` **inside the verified parent**:
    readers never see a partial file, and the rename has no path to
    traverse, so it cannot be redirected by a concurrent swap.

    ``expect_key`` pins the write to one specific file: the ``(st_dev,
    st_ino)`` the caller derived its content from.  A target that has since
    been removed or replaced is refused rather than overwritten.
    """
    require_containment()
    parts = _relative_parts(workspace, target)
    name = parts[-1]
    absolute = str(workspace.joinpath(*parts))

    with _open_parent(workspace, parts, create_dirs=create_dirs) as chain:
        parent_fd = chain.fd
        existing = _inspect_target(parent_fd, name)
        _reject_unsafe_target(existing, name)
        _reject_case_variant(parent_fd, name, existing)
        _require_expected_target(existing, expect_key, name)
        mode = stat.S_IMODE(existing.st_mode) if existing is not None else 0o644
        tmp_name = f"{_TEMP_PREFIX}{os.getpid()}-{os.urandom(6).hex()}"
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            written = 0
            view = memoryview(data)
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fchmod(fd, mode)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            _unlink_quietly(parent_fd, tmp_name)
            raise
        os.close(fd)
        try:
            # Re-check immediately before the swap: a target that became a
            # symlink, gained a link, or was replaced by a different file
            # since the inspection above must not be replaced under a name
            # the caller believes is a plain file it already inspected.
            current = _inspect_target(parent_fd, name)
            _reject_unsafe_target(current, name)
            _reject_case_variant(parent_fd, name, current)
            _require_expected_target(current, expect_key, name)
            os.rename(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            _unlink_quietly(parent_fd, tmp_name)
            raise
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    return ConfinedWrite(
        path=absolute,
        bytes_written=len(data),
        dirs_created=create_dirs and len(parts) > 1,
        created=existing is None,
    )


def _unlink_quietly(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass


def confined_write_text(
    workspace: Path, target: str, content: str, *, encoding: str = "utf-8"
) -> ConfinedWrite:
    return confined_write_bytes(workspace, target, content.encode(encoding))


def confined_read_bytes(workspace: Path, target: str, *, max_bytes: int = 64 << 20) -> bytes:
    """Read ``target`` through the same no-follow descriptor chain."""
    require_containment()
    parts = _relative_parts(workspace, target)
    name = parts[-1]
    with _open_parent(workspace, parts, create_dirs=False) as chain:
        parent_fd = chain.fd
        try:
            fd = os.open(
                name,
                os.O_RDONLY | _NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ConfinementError(
                    f"symlink rejected as confined read target: {name!r}"
                ) from exc
            raise ConfinementError(f"cannot read confined target {name!r}: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ConfinementError(
                    f"confined read target is not a regular file: {name!r}"
                )
            if st.st_size > max_bytes:
                raise ConfinementError(f"confined read target too large: {name!r}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)


def confined_read_text(workspace: Path, target: str, *, encoding: str = "utf-8") -> str:
    return confined_read_bytes(workspace, target).decode(encoding, "replace")


def confined_target_key(workspace: Path, target: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of ``target``, read through the verified chain.

    ``None`` when the final component does not exist.  Paired with
    ``confined_write_bytes(expect_key=...)`` this is how a read-modify-write
    binds its write to the file it actually read.
    """
    require_containment()
    parts = _relative_parts(workspace, target)
    name = parts[-1]
    with _open_parent(workspace, parts, create_dirs=False) as chain:
        st = _inspect_target(chain.fd, name)
        if st is None:
            return None
        _reject_case_variant(chain.fd, name, st)
        return st.st_dev, st.st_ino


def confined_move(workspace: Path, source: str, destination: str) -> None:
    """``renameat`` between two verified parents, both inside ``workspace``.

    Both ends are opened through their own descriptor chains, so neither the
    source nor the destination can be redirected by a symlinked component,
    and the rename itself has no path left to traverse.
    """
    require_containment()
    src_parts = _relative_parts(workspace, source)
    dst_parts = _relative_parts(workspace, destination)
    src_name, dst_name = src_parts[-1], dst_parts[-1]

    with _open_parent(workspace, src_parts, create_dirs=False) as src_chain:
        src_st = _inspect_target(src_chain.fd, src_name)
        if src_st is None:
            raise ConfinementError(f"confined move source does not exist: {src_name!r}")
        if stat.S_ISLNK(src_st.st_mode):
            raise ConfinementError(f"symlink rejected as confined move source: {src_name!r}")
        if not (stat.S_ISREG(src_st.st_mode) or stat.S_ISDIR(src_st.st_mode)):
            raise ConfinementError(
                f"confined move source is not a regular file or directory: {src_name!r}"
            )
        if stat.S_ISREG(src_st.st_mode) and src_st.st_nlink > 1:
            raise ConfinementError(
                f"hard-linked file rejected as confined move source: {src_name!r} "
                f"(st_nlink={src_st.st_nlink})"
            )
        _reject_case_variant(src_chain.fd, src_name, src_st)
        with _open_parent(workspace, dst_parts, create_dirs=False) as dst_chain:
            existing = _inspect_target(dst_chain.fd, dst_name)
            _reject_unsafe_target(existing, dst_name)
            _reject_case_variant(dst_chain.fd, dst_name, existing)
            try:
                os.rename(
                    src_name, dst_name,
                    src_dir_fd=src_chain.fd, dst_dir_fd=dst_chain.fd,
                )
            except OSError as exc:
                raise ConfinementError(
                    f"cannot move confined {src_name!r} to {dst_name!r}: {exc}"
                ) from exc


def confined_delete(workspace: Path, target: str) -> None:
    require_containment()
    parts = _relative_parts(workspace, target)
    name = parts[-1]
    with _open_parent(workspace, parts, create_dirs=False) as chain:
        st = _inspect_target(chain.fd, name)
        if st is None:
            raise ConfinementError(f"confined delete target does not exist: {name!r}")
        if stat.S_ISLNK(st.st_mode):
            raise ConfinementError(f"symlink rejected as confined delete target: {name!r}")
        _reject_case_variant(chain.fd, name, st)
        try:
            os.unlink(name, dir_fd=chain.fd)
        except OSError as exc:
            raise ConfinementError(
                f"cannot delete confined target {name!r}: {exc}"
            ) from exc
