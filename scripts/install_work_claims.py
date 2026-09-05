#!/usr/bin/env python3
"""Production entrypoint: distribute the reviewed work-claims plugin.

This file is the repository-owned replacement for the live
``~/.hermes/scripts/install_work_claims.py``. It holds no distribution logic
of its own -- no copying, no removal, no renaming. It loads the canonical,
reviewed implementation at ``<canonical source>/installer.py`` and hands the
entire transaction to ``installer.distribute()``, which validates the whole
source set against the pinned manifest before touching any destination,
stages each profile, swaps it in atomically, reads it back, rolls every
profile back if any one fails, and recovers a previously interrupted run
before starting a new one.

Why a synthetic module name rather than an import:

* ``installer.py`` is loaded with ``importlib.util.spec_from_file_location``
  under the name ``_work_claims_installer_impl``, is deliberately never
  inserted into ``sys.modules``, and its directory is never added to
  ``sys.path``. Nothing can import it under that name, and it can neither
  shadow nor be shadowed by a ``plugins.work_claims.installer`` a host
  process or test has already imported: the two are distinct module objects
  under distinct names.
* The dependency runs one way only -- ``installer.py`` never imports this
  file, and this file imports nothing but the standard library and PyYAML
  (for ``discover_enabled_profiles``) -- so there is no self-import
  ambiguity in either direction.

Fail-closed provenance, pinned in the direction that is worth checking:

* ``CANONICAL_INSTALLER_SHA256`` pins the sha256 of the reviewed
  ``installer.py`` and is verified *before* that file is executed. The check
  deliberately runs this way round: a script cannot meaningfully attest to
  its own bytes (a tampered copy would simply drop the check), so the code
  that runs first vouches for the code it is about to run.
* ``MIGRATION_SOURCE_INSTALLER`` is the pre-existing production installer
  this file replaces; its reviewed sha256 is pinned inside ``installer.py``
  as ``PRODUCTION_INSTALLER_SHA256``. Whatever sits at that path must be
  either exactly that admitted migration source, or a byte-identical copy of
  this replacement (the migration already applied), or absent. Anything else
  is an unknown installer at the production path and aborts the run before
  any profile is touched. That path is only ever read, never written.

Default-install roster completeness (``profiles=None``):

``discover_enabled_profiles(root)`` scans every ``<root>/profiles/*/config.yaml``
that exists and returns the names of profiles whose config enables
``work-claims`` (present in ``plugins.enabled``) and does not explicitly
disable it (absent from ``plugins.disabled``). Enablement is decided by
config semantics, never by profile name.

When ``main`` is called with ``profiles=None`` (the production CLI path) it
unions the hard-coded ``PROFILES`` baseline with the live discovery result,
so a newly enabled profile is automatically included on the next run without
requiring a roster edit. The baseline is preserved in full -- its ordering
and membership are unchanged -- and any newly discovered name is appended
after it. A profile that is both in ``PROFILES`` and discovered is included
exactly once.

Fail-closed on discovery problems: an unreadable ``config.yaml``, a file
that is not a regular file, a YAML parse error, or a ``plugins`` block that
is not a mapping or whose ``enabled``/``disabled`` values are not lists all
raise ``ConfigDiscoveryError`` before any profile name is returned and
therefore before any distribution mutation is attempted.

Explicit ``profiles=`` remains exact: when the caller supplies a list,
``discover_enabled_profiles`` is never called. This supports staged drtest
canaries and targeted single-profile installs without scanning the fleet.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import yaml

ROOT = Path("/Users/rook/.hermes")
SOURCE = ROOT / "plugins" / "work_claims"
# The reviewed production fleet baseline: every profile known to enable
# ``work-claims`` as of the last reviewed roster commit.  A profile absent
# here but present in the filesystem is picked up by ``discover_enabled_profiles``
# at run time, so the roster self-heals without a code edit.  The baseline
# is pinned by ``ProductionRosterTest`` in ``test_installer_distribution.py``;
# change both in the same commit.
PROFILES = ("rook", "hannah", "clara", "daniel", "maya", "oliver", "sophie", "elias")
MIGRATION_SOURCE_INSTALLER: Path | None = ROOT / "scripts" / "install_work_claims.py"

IMPL_MODULE_NAME = "_work_claims_installer_impl"

# sha256 of the reviewed plugins/work_claims/installer.py. See PROVENANCE.md.
CANONICAL_INSTALLER_SHA256 = (
    "9a6565f157eabac9cf03ba0c52fee73f07236d180706794ff41ae4a3d6d8bd6f"
)

_PLUGIN_NAME = "work-claims"


class EntrypointError(RuntimeError):
    """The canonical installer could not be loaded and trusted, so nothing ran."""


class ConfigDiscoveryError(RuntimeError):
    """A profile config.yaml could not be read, parsed, or interpreted.

    Raised before any distribution mutation: an unreadable or malformed
    config is treated as unknown intent rather than as ``disabled``, so the
    run fails closed instead of silently omitting the profile.
    """


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_enabled_profiles(root: Path) -> list[str]:
    """Return the names of profiles under ``<root>/profiles`` that enable work-claims.

    Scans every ``<root>/profiles/<name>/config.yaml`` that is present,
    parses it with PyYAML (``safe_load``), and includes the profile name when:

    * ``plugins.enabled`` is a list that contains ``"work-claims"``, AND
    * ``plugins.disabled`` is absent, ``None``, or a list that does NOT
      contain ``"work-claims"``.

    Profiles with no ``config.yaml`` are skipped (they have no stated intent
    about which plugins they want, so they are simply not a target).

    Fails closed: any of the following raise ``ConfigDiscoveryError`` before
    any name is returned:

    * ``config.yaml`` exists but is not a regular, non-symlink file.
    * ``config.yaml`` cannot be read (``OSError``).
    * ``config.yaml`` cannot be parsed as YAML, or parses to something other
      than a mapping.
    * ``plugins`` is present but is not a mapping.
    * ``plugins.enabled`` is present but is not a list.
    * ``plugins.disabled`` is present but is not a list.

    A ``plugins`` key that is absent or ``None`` means ``work-claims`` is not
    configured for that profile (skipped, not an error). An ``enabled`` or
    ``disabled`` key whose value is ``None`` is treated as an empty list.

    Results are returned in lexical order of profile name so that repeated
    calls over the same directory produce a stable, deterministic sequence.
    """
    profiles_dir = root / "profiles"
    if not profiles_dir.is_dir():
        return []

    discovered: list[str] = []
    for entry in sorted(profiles_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        cfg_path = entry / "config.yaml"
        if not cfg_path.exists() and not cfg_path.is_symlink():
            continue  # no config at all -- not a target
        if cfg_path.is_symlink() or not cfg_path.is_file():
            raise ConfigDiscoveryError(
                f"config.yaml for profile {entry.name!r} is not a regular file: {cfg_path}"
            )
        try:
            raw = cfg_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigDiscoveryError(
                f"cannot read config.yaml for profile {entry.name!r}: {exc}"
            ) from exc
        try:
            config = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigDiscoveryError(
                f"malformed config.yaml for profile {entry.name!r}: {exc}"
            ) from exc
        if config is None:
            continue  # empty file -- no plugins configured
        if not isinstance(config, dict):
            raise ConfigDiscoveryError(
                f"config.yaml for profile {entry.name!r} is not a YAML mapping"
            )
        plugins_section = config.get("plugins")
        if plugins_section is None:
            continue  # key absent -- work-claims not configured
        if not isinstance(plugins_section, dict):
            raise ConfigDiscoveryError(
                f"config.yaml for profile {entry.name!r}: 'plugins' is not a mapping"
            )
        enabled_raw = plugins_section.get("enabled")
        if enabled_raw is None:
            enabled: list = []
        elif not isinstance(enabled_raw, list):
            raise ConfigDiscoveryError(
                f"config.yaml for profile {entry.name!r}: 'plugins.enabled' is not a list"
            )
        else:
            enabled = enabled_raw

        disabled_raw = plugins_section.get("disabled")
        if disabled_raw is None:
            disabled: list = []
        elif not isinstance(disabled_raw, list):
            raise ConfigDiscoveryError(
                f"config.yaml for profile {entry.name!r}: 'plugins.disabled' is not a list"
            )
        else:
            disabled = disabled_raw

        if _PLUGIN_NAME in enabled and _PLUGIN_NAME not in disabled:
            discovered.append(entry.name)

    return discovered


def load_installer(source: Path) -> ModuleType:
    """Execute the pinned canonical ``installer.py`` as an isolated module."""
    impl_path = Path(source) / "installer.py"
    if not impl_path.is_file() or impl_path.is_symlink():
        raise EntrypointError(f"canonical installer module is missing: {impl_path}")
    actual = _sha256(impl_path)
    if actual != CANONICAL_INSTALLER_SHA256:
        raise EntrypointError(
            f"canonical installer module {impl_path} does not match the pinned "
            f"reviewed hash ({actual} != {CANONICAL_INSTALLER_SHA256}); "
            "re-review before distributing it"
        )
    if IMPL_MODULE_NAME in sys.modules:
        raise EntrypointError(
            f"module name {IMPL_MODULE_NAME!r} is already registered; refusing "
            "to load the canonical installer under an ambiguous identity"
        )
    spec = importlib.util.spec_from_file_location(IMPL_MODULE_NAME, impl_path)
    if spec is None or spec.loader is None:
        raise EntrypointError(f"cannot build an import spec for {impl_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # never registered in sys.modules
    return module


def verify_migration_source(
    impl: ModuleType, migration_source: Path | None, *, self_path: Path
) -> str:
    """Classify whatever occupies the production installer path. Read-only."""
    if migration_source is None:
        return "unset"
    path = Path(migration_source)
    if not path.exists() and not path.is_symlink():
        return "absent"
    if path.is_symlink() or not path.is_file():
        raise EntrypointError(f"production installer path is not a regular file: {path}")
    digest = _sha256(path)
    if digest == impl.PRODUCTION_INSTALLER_SHA256:
        return "admitted-migration-source"
    if self_path.is_file() and digest == _sha256(self_path):
        return "already-migrated"
    raise EntrypointError(
        f"unknown installer at the production path {path} (sha256 {digest}): it "
        f"is neither the admitted migration source "
        f"({impl.PRODUCTION_INSTALLER_SHA256}) nor a copy of this replacement"
    )


def main(
    *,
    source: Path | None = None,
    root: Path | None = None,
    profiles: list[str] | None = None,
) -> dict[str, Path]:
    source = Path(source) if source is not None else SOURCE
    root = Path(root) if root is not None else ROOT

    if profiles is not None:
        # Explicit list: exact -- no filesystem scanning.  Supports staged
        # drtest canaries and single-profile targeted installs.
        profiles = list(profiles)
    else:
        # Default production path: union the reviewed baseline with any
        # newly discovered profiles.  discover_enabled_profiles fails closed
        # on unreadable or malformed configs before any distribution starts.
        discovered = discover_enabled_profiles(root)
        baseline: list[str] = list(PROFILES)
        merged: list[str] = list(baseline)
        for name in discovered:
            if name not in merged:
                merged.append(name)
        profiles = merged

    impl = load_installer(source)
    verify_migration_source(
        impl, MIGRATION_SOURCE_INSTALLER, self_path=Path(__file__).resolve()
    )
    installed = impl.distribute(source=source, root=root, profiles=profiles)
    for name, destination in installed.items():
        print(f"installed {name}: {destination}")
    return installed


if __name__ == "__main__":
    main()
