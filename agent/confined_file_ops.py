"""Dispatcher confinement for the *actual* file-mutating operations.

Stage 2A confined ``write_file``/``patch`` with a pre-tool ancestor walk.
That check describes the filesystem as it was when the hook ran, and the
shipped writer then goes and does something else entirely: ``_atomic_write``
explicitly ``readlink -f``s a symlinked target and writes to whatever it
resolves to.  Between the two, anything with a foothold in the workspace can
swap a component and redirect the write -- the check/use race the CV-A01
review reproduced.

So this module puts the boundary in the operation.  A bound
:class:`~agent.dispatcher_identity.BoundIdentity` resolves to a
:class:`ConfinedScope`, and every mutation the file tools perform under one
goes through the descriptor-relative primitives in
:mod:`agent.workspace_confinement`: each component opened no-follow from a
freshly re-verified workspace descriptor, symlinks and hardlinked targets
refused, and the content swapped in by ``renameat`` inside the verified
parent.

Three things are re-derived on *every* operation rather than trusted from
the hook that authorized it:

* the identity itself (``revalidate``) -- expiry, this process's PID and
  kernel start time, and the task's current run and recorded workspace;
* the workspace directory, re-matched by ``(st_dev, st_ino)`` against the
  directory the identity was bound to;
* the target path, re-walked from that workspace.

And one thing is denied even *inside* the workspace: the control plane.  A
worker whose workspace is the checkout it runs from could otherwise rewrite
the gate that authorizes it, the plugin the gate lives in, the installer
that distributes it, or the database identity itself is stored in -- and the
next process would load the rewrite.  See :func:`control_plane_guard`.

Nothing here degrades: a platform that cannot provide the descriptor-relative
primitives makes the write fail, never proceed unconfined.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from agent.workspace_confinement import (
    ConfinedWrite,
    ConfinementError,
    canonical_walk,
    confined_by_ancestor_walk,
    confined_delete,
    confined_move,
    confined_target_key,
    confined_write_bytes,
    require_containment,
)

__all__ = [
    "ConfinedScope",
    "ConfinementDenied",
    "ConfinementError",
    "active_scope",
    "control_plane_guard",
]


class ConfinementDenied(ConfinementError):
    """A confined operation is refused: it would leave the worker's mandate.

    A subclass of :class:`ConfinementError` so a caller that catches the
    base class -- which is every call site in ``tools/file_operations.py``
    -- treats a policy denial and a primitive failure identically.  Both
    mean the same thing to the tool: nothing was written.
    """


# --------------------------------------------------------------------------- #
# The control plane
# --------------------------------------------------------------------------- #

#: Modules whose on-disk file *is* the authorization decision.  Resolved
#: through ``sys.modules`` rather than by guessing at paths, so what is
#: protected is exactly what this process actually loaded -- including the
#: case that matters, a worker running out of the very checkout it was given
#: as a workspace.
_CONTROL_PLANE_MODULES = (
    __name__,
    "agent.workspace_confinement",
    "agent.dispatcher_identity",
    "agent.command_containment",
    "agent.delegation_context",
    "agent.file_safety",
    "hermes_cli.kanban_db",
    "hermes_cli.plugins",
    "plugins.work_claims",
    "plugins.work_claims.core",
    "plugins.work_claims.installer",
)

#: Directories under a Hermes home that decide what runs at all: the plugin
#: set and the installer that distributes it.  Deliberately not the whole
#: home -- Kanban workspaces live under ``~/.hermes/worktrees/``, so a
#: blanket home rule would deny a worker its own workspace.
_CONTROL_PLANE_HOME_TREES = ("plugins", "scripts")

#: Files under a Hermes home with the same standing: the config decides
#: which plugins are enabled, so rewriting it disables the gate.
_CONTROL_PLANE_HOME_FILES = ("config.yaml", "config.yml")


def _normalized(path: Path | str) -> str:
    """Comparison form: normalized, and case-folded on case-folding hosts.

    ``normcase`` is a no-op on Linux and lowercases on Windows; APFS is
    case-insensitive but case-*preserving*, so the exact-case walks in
    :mod:`agent.workspace_confinement` are what actually reject an alias.
    This is the coarse containment check that sits behind them.
    """
    return os.path.normcase(os.path.normpath(str(path)))


def _hermes_homes() -> list[Path]:
    """The active home and the profile-independent root, both if distinct."""
    homes: list[Path] = []
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home

        candidates = [get_hermes_home(), get_default_hermes_root()]
    except Exception:
        candidates = [Path(os.environ.get("HERMES_HOME", "~/.hermes"))]
    for candidate in candidates:
        try:
            resolved = Path(candidate).expanduser()
        except Exception:
            continue
        if resolved not in homes:
            homes.append(resolved)
    return homes


def _module_file(name: str) -> Path | None:
    module = sys.modules.get(name)
    if module is None:
        return None
    origin = getattr(module, "__file__", None)
    if not origin:
        return None
    try:
        return Path(origin)
    except Exception:
        return None


def _lstat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


@dataclass(frozen=True)
class ControlPlaneGuard:
    """The paths a confined worker may not write, whatever its workspace."""

    files: frozenset[str]
    trees: tuple[str, ...]
    file_keys: frozenset[tuple[int, int]]

    def violation(self, absolute: Path) -> str | None:
        candidate = _normalized(absolute)
        if candidate in self.files:
            return f"{absolute} is a control-plane file"
        for tree in self.trees:
            if candidate == tree or candidate.startswith(tree + os.sep):
                return f"{absolute} is inside the control-plane directory {tree}"
        # An existing target may *be* a protected file reached under another
        # name (a bind alias, or a hardlink the nlink check somehow allowed):
        # identity, not spelling, is what matters.
        key = _lstat_key(absolute)
        if key is not None and key in self.file_keys:
            return f"{absolute} is the same file as a control-plane file"
        return None


def control_plane_guard(*, db_path: str | None = None) -> ControlPlaneGuard:
    """Build the deny set for this process, from what it actually loaded.

    Rebuilt per operation rather than cached: a module imported later (the
    installer, say) must be protected from the moment it exists, and a home
    that moved must be re-read rather than remembered.
    """
    files: set[str] = set()
    trees: set[str] = set()

    for name in _CONTROL_PLANE_MODULES:
        origin = _module_file(name)
        if origin is None:
            continue
        files.add(_normalized(origin))
        module = sys.modules.get(name)
        # A package protects its whole directory: the plugin ships a
        # manifest, an installer and a plugin.yaml alongside its code, and
        # each of them is as load-bearing as ``core.py``.
        if getattr(module, "__path__", None):
            trees.add(_normalized(origin.parent))

    # The repository-side entrypoints, whether or not they have been
    # imported: this module lives at ``<root>/agent/confined_file_ops.py``.
    own = _module_file(__name__)
    if own is not None:
        root = own.resolve().parent.parent
        files.add(_normalized(root / "scripts" / "install_work_claims.py"))
        trees.add(_normalized(root / "plugins" / "work_claims"))

    for home in _hermes_homes():
        for tree in _CONTROL_PLANE_HOME_TREES:
            trees.add(_normalized(home / tree))
        for name in _CONTROL_PLANE_HOME_FILES:
            files.add(_normalized(home / name))

    # The Kanban database is where identity itself is recorded; a worker
    # able to rewrite it can mint its own successor's authority.
    if db_path:
        files.add(_normalized(db_path))

    keys = {key for key in (_lstat_key(Path(f)) for f in files) if key is not None}
    return ControlPlaneGuard(
        files=frozenset(files), trees=tuple(sorted(trees)), file_keys=frozenset(keys)
    )


# --------------------------------------------------------------------------- #
# The scope
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ConfinedScope:
    """A verified workspace a single file operation must stay inside.

    Produced fresh by :func:`active_scope` for each operation and never
    stored: holding one across calls would reintroduce exactly the stale
    authorization this stage exists to remove.
    """

    workspace: Path
    workspace_key: tuple[int, int]
    task_id: str
    run_id: int
    db_path: str

    # -- verification ------------------------------------------------- #

    def absolute(self, target: str) -> Path:
        candidate = Path(target).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate

    def verify(self, target: str, *, verb: str = "write") -> Path:
        """Re-walk ``target`` from the workspace, or refuse it.

        Not a substitute for the descriptor-relative open that follows --
        that is the enforcement.  This runs first so a target that is
        already outside the boundary is refused before the operation reads
        anything, and so the caller gets a reason naming the component.
        """
        ok, reason = confined_by_ancestor_walk(
            target, self.workspace, self.workspace_key
        )
        if not ok:
            raise ConfinementDenied(
                f"{verb} must stay inside the confined dispatcher workspace "
                f"{self.workspace}: {reason}"
            )
        absolute = self.absolute(target)
        violation = control_plane_guard(db_path=self.db_path).violation(absolute)
        if violation is not None:
            raise ConfinementDenied(
                f"{violation}: a confined worker may not rewrite the gate, "
                "plugin, installer, configuration or database that authorizes "
                "it, even inside its own workspace"
            )
        return absolute

    # -- operations ---------------------------------------------------- #

    def preimage_key(self, target: str) -> tuple[int, int] | None:
        """``(st_dev, st_ino)`` of ``target`` now, or None if it is absent."""
        return confined_target_key(self.workspace, target)

    def write_bytes(
        self, target: str, data: bytes, *, expect_preimage: tuple[int, int] | None = None
    ) -> ConfinedWrite:
        # ``create_dirs=False`` keeps this exactly as permissive as
        # :meth:`verify`, which (like the Stage 2A gate it shares its walk
        # with) allows a not-yet-existing *final* component and nothing
        # above it.  A write must never create a directory the verification
        # never saw: the two layers agree on the same boundary or the
        # weaker one is the real one.
        return confined_write_bytes(
            self.workspace, target, data, create_dirs=False,
            expect_key=expect_preimage,
        )

    def delete(self, target: str, *, recursive: bool = False) -> None:
        if self.preimage_key(target) is None:
            # Deleting what is not there is the no-op the shell path has
            # always reported as success.
            return
        if recursive:
            raise ConfinementDenied(
                "recursive deletion cannot be confined descriptor-relative in "
                f"{self.workspace}: delete files individually"
            )
        confined_delete(self.workspace, target)

    def move(self, source: str, destination: str) -> None:
        confined_move(self.workspace, source, destination)


def active_scope() -> ConfinedScope | None:
    """The confinement in force for this operation, or ``None``.

    ``None`` means this process holds no dispatcher identity -- an ordinary
    session, a delegated child, an in-process cron job, or any spawned
    subprocess.  Those are bounded by the claim gate and (for shell
    commands) by the OS sandbox; they are deliberately *not* confined here,
    because confinement without an assigned workspace has no boundary to
    enforce.

    Raises :class:`ConfinementError` when an identity exists but the
    operation must not proceed: a revoked identity, a workspace that no
    longer verifies, or a platform without the primitives.  Every caller
    turns that into a refusal -- never a fallback to the unconfined write.
    """
    try:
        from agent import dispatcher_identity
    except ImportError:  # pragma: no cover - agent package is always present
        return None

    identity = dispatcher_identity.get_bound()
    if identity is None:
        return None

    reason = dispatcher_identity.revalidate(identity)
    if reason is not None:
        raise ConfinementDenied(
            f"dispatcher worker identity is no longer valid: {reason}"
        )

    ok, key, reason = canonical_walk(identity.workspace, require_dir=True)
    if not ok or key is None:
        raise ConfinementDenied(
            f"dispatcher worker workspace failed verification: {reason}"
        )
    if key != identity.workspace_key:
        raise ConfinementDenied(
            "dispatcher worker workspace no longer matches the directory the "
            f"identity was bound to: {identity.workspace}"
        )

    # Fail closed before the caller has read or written anything.
    require_containment()

    return ConfinedScope(
        workspace=identity.workspace,
        workspace_key=key,
        task_id=identity.task_id,
        run_id=identity.run_id,
        db_path=identity.db_path,
    )
