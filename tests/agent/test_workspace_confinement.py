"""Coverage for the confinement primitives that ship with Stage 1.

Stage 1 (process-bound worker identity) uses only the path *verification*
helpers here -- ``canonical_walk`` pins the workspace an identity binds to.
The descriptor-relative write primitives in the same module are not yet
wired into any tool; that is Stage 4.  They are covered here anyway so
nothing in the commit ships untested, and so a regression in the primitives
is caught before the integration lands on top of them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent import workspace_confinement as wc


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def outside(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    return target


# --------------------------------------------------------------------------- #
# Verification helpers (the part Stage 1 depends on)
# --------------------------------------------------------------------------- #

def test_canonical_walk_accepts_a_real_directory(workspace):
    ok, key, reason = wc.canonical_walk(workspace, require_dir=True)
    assert ok and reason is None
    st = workspace.stat()
    assert key == (st.st_dev, st.st_ino)


def test_canonical_walk_rejects_a_symlinked_ancestor(workspace, outside, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(outside)
    ok, key, reason = wc.canonical_walk(link / "sub", require_dir=True)
    assert not ok and key is None
    assert "symlink" in reason or "does not exist" in reason


def test_canonical_walk_rejects_relative_and_missing_paths(workspace):
    ok, _, reason = wc.canonical_walk(Path("relative/path"), require_dir=True)
    assert not ok and "not absolute" in reason
    ok, _, reason = wc.canonical_walk(workspace / "nope", require_dir=True)
    assert not ok and "does not exist" in reason


def test_canonical_walk_rejects_a_file_when_a_directory_is_required(workspace):
    target = workspace / "file.txt"
    target.write_text("x")
    ok, _, reason = wc.canonical_walk(target, require_dir=True)
    assert not ok and "not a directory" in reason


def test_ancestor_walk_confines_to_the_workspace(workspace, outside):
    _, key, _ = wc.canonical_walk(workspace, require_dir=True)
    assert wc.confined_by_ancestor_walk("new.txt", workspace, key) == (True, None)
    ok, reason = wc.confined_by_ancestor_walk(str(outside / "x"), workspace, key)
    assert not ok and "escapes the confined workspace" in reason
    ok, reason = wc.confined_by_ancestor_walk("../escape.txt", workspace, key)
    assert not ok and "escapes the confined workspace" in reason


def test_ancestor_walk_rejects_symlink_and_hardlink_targets(workspace, outside):
    _, key, _ = wc.canonical_walk(workspace, require_dir=True)

    (workspace / "link.txt").symlink_to(outside / "target.txt")
    ok, reason = wc.confined_by_ancestor_walk("link.txt", workspace, key)
    assert not ok and "symlink" in reason

    original = workspace / "orig.txt"
    original.write_text("data")
    os.link(original, workspace / "alias.txt")
    ok, reason = wc.confined_by_ancestor_walk("alias.txt", workspace, key)
    assert not ok and "hard-linked" in reason


# --------------------------------------------------------------------------- #
# Descriptor-relative write primitives (staged for Stage 4, not yet wired)
# --------------------------------------------------------------------------- #

def test_containment_is_available_on_this_platform():
    assert wc.containment_supported() is True
    wc.require_containment()  # must not raise


def test_confined_write_and_read_round_trip(workspace):
    result = wc.confined_write_text(workspace, "nested/dir/file.txt", "hello\n")
    written = workspace / "nested" / "dir" / "file.txt"
    assert written.read_text() == "hello\n"
    assert result.path == str(written)
    assert result.created is True
    assert wc.confined_read_text(workspace, "nested/dir/file.txt") == "hello\n"


def test_confined_write_leaves_no_temp_files_behind(workspace):
    wc.confined_write_text(workspace, "file.txt", "content")
    assert [p.name for p in workspace.iterdir()] == ["file.txt"]


def test_confined_write_refuses_a_symlinked_target(workspace, outside):
    victim = outside / "victim.txt"
    victim.write_text("original")
    (workspace / "link.txt").symlink_to(victim)
    with pytest.raises(wc.ConfinementError, match="symlink"):
        wc.confined_write_text(workspace, "link.txt", "pwned")
    assert victim.read_text() == "original"


def test_confined_write_refuses_a_symlinked_ancestor(workspace, outside):
    (workspace / "sub").symlink_to(outside)
    with pytest.raises(wc.ConfinementError):
        wc.confined_write_text(workspace, "sub/escape.txt", "pwned")
    assert not (outside / "escape.txt").exists()


def test_confined_write_refuses_a_hardlinked_target(workspace, outside):
    victim = outside / "victim.txt"
    victim.write_text("original")
    os.link(victim, workspace / "alias.txt")
    with pytest.raises(wc.ConfinementError, match="hard-linked"):
        wc.confined_write_text(workspace, "alias.txt", "pwned")
    assert victim.read_text() == "original"


def test_confined_write_refuses_paths_outside_the_workspace(workspace, outside):
    for target in (str(outside / "x.txt"), "../x.txt", "sub/../../x.txt"):
        with pytest.raises(wc.ConfinementError, match="escapes the confined workspace"):
            wc.confined_write_text(workspace, target, "pwned")
    assert not (outside / "x.txt").exists()


def test_confined_write_refuses_the_workspace_root(workspace):
    with pytest.raises(wc.ConfinementError, match="workspace root"):
        wc.confined_write_text(workspace, str(workspace), "pwned")


def test_confined_write_preserves_the_existing_file_mode(workspace):
    target = workspace / "script.sh"
    target.write_text("old")
    target.chmod(0o750)
    wc.confined_write_text(workspace, "script.sh", "new")
    assert target.read_text() == "new"
    assert oct(target.stat().st_mode)[-3:] == "750"


def test_confined_read_refuses_a_symlink(workspace, outside):
    secret = outside / "secret.txt"
    secret.write_text("secret")
    (workspace / "peek.txt").symlink_to(secret)
    with pytest.raises(wc.ConfinementError, match="symlink"):
        wc.confined_read_text(workspace, "peek.txt")


def test_require_containment_fails_closed_when_unsupported(monkeypatch):
    monkeypatch.setattr(wc, "containment_supported", lambda: False)
    with pytest.raises(wc.ConfinementUnavailable):
        wc.require_containment()
