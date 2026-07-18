"""P2-T4 — CACHEDIR.TAG signature-verified pruning, wired into the walk.

The pure signature check is covered by test_presets_scaffold; this module proves
the *walk integration*: a directory carrying a valid ``CACHEDIR.TAG`` is pruned
entirely (never descended — asserted by counting ``os.scandir`` calls, not just
final items), while a directory whose ``CACHEDIR.TAG`` has the wrong/missing
signature is scanned normally (the false-positive guard).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from filearr.presets import CACHEDIR_TAG_SIGNATURE, build_library_spec
from filearr.tasks.scan import walk


@dataclass
class FakeLibrary:
    enabled_presets: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=list)


def _walk_with_scandir_count(root, library, monkeypatch):
    import filearr.tasks.scan as scan_mod

    scanned: list[str] = []
    real = os.scandir

    def counting(path):
        scanned.append(os.path.relpath(str(path), str(root)))
        return real(path)

    monkeypatch.setattr(scan_mod.os, "scandir", counting)
    spec = build_library_spec(library)
    rels = {rel for _p, rel, _s, _m in walk(str(root), spec)}
    return rels, scanned


def test_valid_cachedir_tag_pruned_wrong_sig_not(tmp_path, monkeypatch):
    # Valid cache dir: signature-tagged, must be pruned wholesale.
    cache = tmp_path / "cachedir"
    (cache / "payload").mkdir(parents=True)
    (cache / "CACHEDIR.TAG").write_bytes(CACHEDIR_TAG_SIGNATURE + b"\n# regenerable\n")
    (cache / "payload" / "big.bin").write_bytes(b"cached bytes")

    # Wrong-signature dir: a file named CACHEDIR.TAG but NOT the bford signature —
    # must be scanned normally (false-positive guard).
    notcache = tmp_path / "notcache"
    notcache.mkdir()
    (notcache / "CACHEDIR.TAG").write_bytes(b"Signature: not-the-real-one\n")
    (notcache / "data.mp4").write_bytes(b"real media")

    (tmp_path / "keep.mp4").write_bytes(b"real media")

    rels, scanned = _walk_with_scandir_count(tmp_path, FakeLibrary(), monkeypatch)

    # Valid cache dir pruned: never descended, nothing under it indexed.
    assert not any(s == "cachedir" or s.startswith("cachedir" + os.sep) for s in scanned)
    assert not any(r.startswith("cachedir/") for r in rels)

    # Wrong-sig dir scanned normally.
    assert "notcache/data.mp4" in rels
    assert "keep.mp4" in rels


def test_missing_cachedir_tag_not_pruned(tmp_path, monkeypatch):
    d = tmp_path / "regular"
    d.mkdir()
    (d / "a.mp4").write_bytes(b"x")
    rels, scanned = _walk_with_scandir_count(tmp_path, FakeLibrary(), monkeypatch)
    assert "regular/a.mp4" in rels
    assert "regular" in scanned  # descended (no tag at all)
