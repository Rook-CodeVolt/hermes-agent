"""OS-enforced write confinement for a dispatcher worker's shell commands.

A pre-tool check can describe a terminal call's ``workdir``.  It cannot
describe what the command *does*: an absolute path, a ``../`` traversal, a
shell redirect, ``tee``, ``cp`` or any process the command spawns can all
write wherever the worker's uid can write, and no amount of string
inspection catches that -- the CV-A01 review's escapes were all of this
shape.

So the boundary is moved into the kernel.  The command is re-written to run
under ``sandbox-exec`` with a profile generated for that worker's exact
workspace: **reads are permitted, writes are permitted only inside the
workspace subtree**.  The Apple sandbox is enforced at the VFS layer and is
inherited by every descendant, so a grandchild spawned three levels down is
confined identically.

Everything here fails closed.  No sandbox binary, a workspace path that
cannot be expressed safely inside a profile, or a profile that cannot be
written all raise rather than returning an unconfined command.

Proven on Darwin 25.6.0 arm64 by the CV-A01 containment spike (9/9): reads
allowed; in-workspace write allowed; absolute, ``../``, ``>>``, ``tee``,
``cp`` and spawned-child writes outside the workspace all denied at the
kernel; and a missing binary never executing anything at all.
"""

from __future__ import annotations

import os
import shlex
import stat
import sys
import time
from pathlib import Path

# Apple's sandbox wrapper.  Referenced by absolute path on purpose: resolving
# it through ``PATH`` would let anything that can prepend to ``PATH`` supply a
# no-op "sandbox".
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# The shell the contained command runs under.  Also absolute for the same
# reason.
CONTAINED_SHELL = "/bin/sh"

_PROFILE_SUFFIX = ".sb"
_PROFILE_TTL_SECONDS = 6 * 3600

# A profile is an s-expression built by string interpolation, so a workspace
# path containing any of these could close the string early and rewrite the
# policy.  Such a path is refused rather than escaped: no legitimate Kanban
# workspace contains one.
_FORBIDDEN_PATH_CHARS = ('"', "\\", "\n", "\r", "\x00")


class ContainmentUnavailable(RuntimeError):
    """The OS cannot confine this command, so it must not be run."""


class ProfileGenerationError(RuntimeError):
    """A sandbox profile could not be generated for this workspace."""


def containment_supported() -> bool:
    """True only when a real, non-symlinked sandbox binary is executable."""
    try:
        st = os.lstat(SANDBOX_EXEC)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    return os.access(SANDBOX_EXEC, os.X_OK)


def require_containment() -> None:
    if not containment_supported():
        raise ContainmentUnavailable(
            f"OS command containment is unavailable ({SANDBOX_EXEC} is missing "
            "or not executable); refusing to run an unconfined command"
        )


def generate_profile(workspace: Path) -> str:
    """Build the sandbox profile confining writes to ``workspace``.

    ``workspace`` must be an absolute, existing directory whose own path is
    already canonical: the kernel matches the profile against *real* paths,
    so a symlinked workspace would confine a different subtree than the one
    the caller verified.
    """
    if not isinstance(workspace, Path):
        raise ProfileGenerationError(f"workspace is not a path: {workspace!r}")
    raw = str(workspace)
    if not workspace.is_absolute():
        raise ProfileGenerationError(f"workspace is not an absolute path: {raw}")
    for char in _FORBIDDEN_PATH_CHARS:
        if char in raw:
            raise ProfileGenerationError(
                "workspace path cannot be expressed safely in a sandbox "
                f"profile (contains {char!r}): {raw}"
            )
    real = os.path.realpath(raw)
    if real != raw:
        raise ProfileGenerationError(
            "workspace path is not canonical, so the sandbox would confine a "
            f"different subtree: {raw} resolves to {real}"
        )
    if not os.path.isdir(real):
        raise ProfileGenerationError(f"workspace is not an existing directory: {raw}")

    subtree = real.rstrip("/") or "/"
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process-exec*)\n"
        "(allow process-fork)\n"
        "(allow signal)\n"
        "(allow sysctl*)\n"
        "(allow mach*)\n"
        "(allow ipc*)\n"
        ";; Reads are unrestricted: containment is a write boundary, and a\n"
        ";; worker that cannot read its own toolchain cannot do its task.\n"
        "(allow file-read*)\n"
        ";; Writes: the assigned workspace subtree, and nothing else.\n"
        f'(allow file-write* (subpath "{subtree}"))\n'
        ";; Character devices only -- /dev/null, /dev/tty and friends. Not\n"
        ";; file-write* : that would permit creating nodes under /dev.\n"
        '(allow file-write-data (subpath "/dev"))\n'
        '(allow file-ioctl (subpath "/dev"))\n'
    )


def profile_root() -> Path:
    """Where generated profiles live: private, and outside every workspace.

    Outside matters -- a profile stored inside the confined subtree would be
    writable by the very command it confines, so a first command could
    rewrite the policy applied to the second.
    """
    home = os.environ.get("HERMES_HOME", "").strip() or "~/.hermes"
    root = Path(home).expanduser() / "work-claims" / "sandbox-profiles"
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _prune_stale_profiles(root: Path) -> None:
    cutoff = time.time() - _PROFILE_TTL_SECONDS
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(_PROFILE_SUFFIX):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue


def materialize_profile(workspace: Path) -> Path:
    """Write a fresh profile for ``workspace`` and return its path.

    A new file per call: the profile is never reused, so a stale one can
    never outlive the workspace it was generated for.
    """
    profile = generate_profile(workspace)
    root = profile_root()
    _prune_stale_profiles(root)
    name = f"worker-{os.getpid()}-{os.urandom(8).hex()}{_PROFILE_SUFFIX}"
    path = root / name
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, profile.encode("utf-8"))
    except OSError as exc:
        raise ProfileGenerationError(f"could not write sandbox profile: {exc}") from exc
    finally:
        os.close(fd)
    return path


def contain_command(command: str, workspace: Path) -> str:
    """Return ``command`` rewritten to run confined to ``workspace``.

    Always wraps, even when the command already names ``sandbox-exec``
    itself: a model-supplied wrapper is not evidence of confinement, and
    nesting Apple sandboxes intersects their policies rather than replacing
    the outer one.
    """
    if not isinstance(command, str) or not command.strip():
        raise ContainmentUnavailable("no command was given to contain")
    require_containment()
    profile = materialize_profile(workspace)
    return " ".join((
        SANDBOX_EXEC,
        "-f", shlex.quote(str(profile)),
        CONTAINED_SHELL, "-c", shlex.quote(command),
    ))


def describe() -> dict[str, object]:
    """Diagnostic view of the containment primitive on this host."""
    return {
        "platform": sys.platform,
        "sandbox_exec": SANDBOX_EXEC,
        "available": containment_supported(),
    }
