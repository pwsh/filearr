"""P2-T1 — walk() rewrite over the single GitIgnoreSpec (preset engine swap).

Exercises the three tightened accept criteria directly against the synchronous
``scan.walk`` generator (no DB / Procrastinate needed — walk is pure):

  1. directory pruning is *provable*: a populated, deeply-nested ``node_modules/``
     is never DESCENDED into — asserted by counting ``os.scandir`` calls, not by
     the final item set;
  2. default-on ``hidden_dotfiles`` reproduces today's exact dotfile skip, and
     the ``-hidden_dotfiles`` disable sentinel surfaces them again;
  3. ruling R1 file-level ordering: a preset-excluded file that ``classify()``
     claims under an indexed parent is kept; the same name under a pruned dir is
     gone; a stray unparented excluded file is dropped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from filearr.presets import build_library_spec
from filearr.tasks.scan import walk


@dataclass
class FakeLibrary:
    """Minimal stand-in for the ORM row that build_library_spec reads."""

    enabled_presets: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)


def _mktree(root, files: list[str]) -> None:
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def _walk_rels(root, library) -> set[str]:
    spec = build_library_spec(library)
    return {rel for _p, rel, _s, _m in walk(str(root), spec)}


# --- Accept #1: pruning is provable via scandir-call counting ---------------


def test_node_modules_never_descended_scandir_count(tmp_path, monkeypatch):
    _mktree(
        tmp_path,
        [
            "project/src/index.js",
            "project/node_modules/pkg/deep/a.js",
            "project/node_modules/pkg/deep/b.js",
            "project/node_modules/pkg/deep/nested/c.js",
            "project/node_modules/.bin/tool",
            "Movies/film.mp4",
        ],
    )
    lib = FakeLibrary(enabled_presets=["node_modules_build"])

    scanned: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path):
        scanned.append(str(path))
        return real_scandir(path)

    # walk() calls os.scandir via the module object; patch it there.
    import filearr.tasks.scan as scan_mod

    monkeypatch.setattr(scan_mod.os, "scandir", counting_scandir)

    rels = _walk_rels(tmp_path, lib)

    # Relativise scanned paths against root (the pytest tmp dir name itself can
    # contain the substring "node_modules" from this test's function name).
    root = str(tmp_path)
    scanned_rel = [os.path.relpath(s, root) for s in scanned]
    assert not any("node_modules" in s for s in scanned_rel), scanned_rel
    # Exactly the non-pruned directories were scanned: root, project, src, Movies.
    assert len(scanned) == 4, scanned_rel
    # And nothing under node_modules leaked into the results.
    assert "project/src/index.js" in rels
    assert "Movies/film.mp4" in rels
    assert not any("node_modules" in r for r in rels)


# --- Accept #2: hidden_dotfiles default-on == today's dotfile skip ----------


def test_default_presets_reproduce_dotfile_skip(tmp_path):
    _mktree(
        tmp_path,
        [
            "keep.mp4",
            "sub/keep2.mkv",
            ".hidden_file",
            ".config/settings.ini",  # dot-DIR: pruned, contents never surface
            "sub/.dotfile",
        ],
    )
    # Empty enabled_presets => column default => hidden_dotfiles resolves active.
    rels = _walk_rels(tmp_path, FakeLibrary())
    assert rels == {"keep.mp4", "sub/keep2.mkv"}


def test_disable_hidden_dotfiles_sentinel_surfaces_dotfiles(tmp_path):
    _mktree(tmp_path, ["keep.mp4", ".hidden_file", "sub/.dotfile"])
    # '-hidden_dotfiles' negative sentinel opts out of the default-on preset.
    rels = _walk_rels(tmp_path, FakeLibrary(enabled_presets=["-hidden_dotfiles"]))
    assert rels == {"keep.mp4", ".hidden_file", "sub/.dotfile"}


# --- Accept #3: ruling R1 file-level ordering (sidecar wins over preset) -----


def test_r1_sidecar_kept_stray_dropped_pruned_gone(tmp_path, monkeypatch):
    _mktree(
        tmp_path,
        [
            "Movies/Film/Film.mp4",          # indexed primary
            "Movies/Film/._Film.nfo",        # excluded (dotfile/._*) but sidecar -> KEEP
            "Movies/Film/._orphan.txt",      # excluded, classify() claims nothing -> DROP
            "project/node_modules/._Film.nfo",  # sidecar-claimed but under PRUNED dir -> GONE
        ],
    )
    lib = FakeLibrary(enabled_presets=["os_metadata", "node_modules_build"])

    # Prove node_modules was never descended (directory pruning wins, R1).
    scanned: list[str] = []
    import filearr.tasks.scan as scan_mod

    real = os.scandir

    def counting(path):
        scanned.append(str(path))
        return real(path)

    monkeypatch.setattr(scan_mod.os, "scandir", counting)

    rels = _walk_rels(tmp_path, lib)

    root = str(tmp_path)
    scanned_rel = [os.path.relpath(s, root) for s in scanned]
    assert "Movies/Film/Film.mp4" in rels
    assert "Movies/Film/._Film.nfo" in rels          # R1: sidecar kept despite ._*
    assert "Movies/Film/._orphan.txt" not in rels    # excluded, not a sidecar
    assert not any("node_modules" in r for r in rels)
    assert not any("node_modules" in s for s in scanned_rel)  # never descended


def test_r1_sidecar_keep_requires_indexed_parent(tmp_path):
    """The kept sidecar's parent dir must itself be non-pruned (indexed)."""
    _mktree(
        tmp_path,
        [
            "caches/.cache/._art.nfo",  # parent '.cache' is a dot-dir -> pruned
            "caches/real.mp4",
        ],
    )
    # hidden_dotfiles (default) prunes the .cache dir; the ._art.nfo inside is
    # unreachable even though classify() would claim it.
    rels = _walk_rels(tmp_path, FakeLibrary())
    assert rels == {"caches/real.mp4"}


# --- effective-default reconciliation helper (P2-T1) ------------------------


def test_resolve_effective_presets_default_on():
    from filearr.presets import resolve_effective_presets

    # empty column => defaults apply => only hidden_dotfiles (the sole default-on)
    assert resolve_effective_presets([]) == ["hidden_dotfiles"]
    assert resolve_effective_presets(None) == ["hidden_dotfiles"]


def test_resolve_effective_presets_union_canonical_order():
    from filearr.presets import PRESET_BUNDLES, resolve_effective_presets

    got = resolve_effective_presets(["node_modules_build", "system_files"])
    # union of default-on + positives, returned in canonical PRESET_BUNDLES order
    assert got == [n for n in PRESET_BUNDLES
                   if n in {"hidden_dotfiles", "node_modules_build", "system_files"}]


def test_resolve_effective_presets_negative_sentinel_disables_default():
    from filearr.presets import resolve_effective_presets

    assert resolve_effective_presets(["-hidden_dotfiles"]) == []
    # a positive + a disable of the default coexist
    assert resolve_effective_presets(["-hidden_dotfiles", "os_metadata"]) == ["os_metadata"]


def test_resolve_effective_presets_ignores_unknown_names():
    from filearr.presets import resolve_effective_presets

    # unknown names (positive or negative) are ignored — validation is the API's job
    assert resolve_effective_presets(["bogus", "-alsobogus"]) == ["hidden_dotfiles"]
