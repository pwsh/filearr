"""P2-T2 — pathspec migration correctness.

Locks the semantic change from the old two-list ``fnmatch`` AND (an ``exclude``
list plus an ``include`` *allowlist*) to a single ordered ``GitIgnoreSpec`` with
gitignore last-match-wins semantics:

  * gitignore negation re-includes a file a preset/exclude would drop;
  * the ``Icon[\\r]`` literal-carriage-return pattern matches ``Icon\\r``;
  * case sensitivity is documented + asserted (ruling R2): builtin OS-junk name
    patterns ship pre-bracket-expanded so they match common case variants, while
    user-supplied patterns remain gitignore case-sensitive (a documented gap);
  * every restated brief-§1.6 Windows/macOS/Linux pattern matches its target in
    a fixture tree;
  * a before/after guard captures an input where the OLD two-list fnmatch and the
    NEW gitignore semantics diverge (the include-allowlist -> negation change).
"""

from __future__ import annotations

import fnmatch

from filearr.presets import build_exclusion_spec

# --- gitignore negation re-include -----------------------------------------


def test_negation_reincludes_preset_excluded_file():
    # caches_temp excludes *.tmp; a per-library include re-admits one file.
    spec = build_exclusion_spec(["caches_temp"], includes=["important.tmp"])
    assert spec.match_file("scratch.tmp")          # still excluded
    assert not spec.match_file("important.tmp")     # re-included via negation


def test_negation_ordering_last_match_wins():
    spec = build_exclusion_spec([], extra_excludes=["*.iso"], includes=["keepme.iso"])
    assert spec.match_file("random.iso")
    assert not spec.match_file("keepme.iso")


# --- Icon[\r] literal carriage-return match --------------------------------


def test_icon_cr_literal_matches():
    spec = build_exclusion_spec(["os_metadata"])
    assert spec.match_file("Icon\r")             # the genuine Finder artifact
    assert spec.match_file("art/Icon\r")         # in any directory
    assert not spec.match_file("Icon")           # no trailing CR -> not a match
    assert not spec.match_file("IconX")


# --- case sensitivity (ruling R2) ------------------------------------------


def test_builtin_osjunk_patterns_case_tolerant():
    """R2: bracket-expanded builtins match common case variants."""
    spec = build_exclusion_spec(["system_files", "os_metadata"])
    for name in ("Thumbs.db", "thumbs.db", "Desktop.ini", "desktop.ini"):
        assert spec.match_file(name), name
        assert spec.match_file(f"a/b/{name}"), name


def test_user_patterns_remain_case_sensitive_documented_gap():
    """R2 documented gap: user-supplied patterns are gitignore case-SENSITIVE.

    pathspec's GitIgnoreSpec has no per-pattern case-insensitive flag (Syncthing's
    ``(?i)`` has no equivalent), so a user pattern only matches its exact case.
    Users who need case tolerance must bracket-expand themselves, exactly like the
    shipped builtins.
    """
    spec = build_exclusion_spec([], extra_excludes=["Secret.txt"])
    assert spec.match_file("Secret.txt")
    assert not spec.match_file("secret.txt")   # different case -> NOT excluded


# --- brief §1.6 pattern set against a fixture tree -------------------------

# Targets each shipped pattern transcribed from github/gitignore §1.6. Builtins
# are a curated subset; hidden_dotfiles is left OFF here so each SPECIFIC pattern
# is what fires (not the ``.*`` catch-all).
_S16_TARGETS_EXCLUDED = [
    # Windows.gitignore
    "Thumbs.db",
    "Thumbs.db:encryptable",
    "ehthumbs.db",
    "ehthumbs_vista.db",
    "Desktop.ini",
    "$RECYCLE.BIN/",
    "shortcut.lnk",
    # macOS.gitignore
    ".DS_Store",
    "Icon\r",
    "._hidden",
    ".Spotlight-V100/",
    ".Trashes/",
    ".fseventsd/",
    ".DocumentRevisions-V100/",
    ".TemporaryItems/",
    # Linux.gitignore
    "backup~",
    ".fuse_hidden0001",
    ".directory",
    ".Trash-1000/",
    ".nfs0001",
    "nohup.out",
]

_S16_KEEP = [
    "Movies/Arcane/s01e01.mkv",
    "Music/song.flac",
    "Docs/manual.pdf",
]


def test_s16_pattern_set_matches_targets():
    spec = build_exclusion_spec(["system_files", "os_metadata", "caches_temp"])
    for target in _S16_TARGETS_EXCLUDED:
        assert spec.match_file(target), f"§1.6 pattern failed to match {target!r}"
    for keep in _S16_KEEP:
        assert not spec.match_file(keep), f"real media wrongly excluded: {keep!r}"


# --- before/after divergence (two-list fnmatch vs gitignore) ---------------


def _old_kept(rel: str, include: list[str], exclude: list[str]) -> bool:
    """Replica of the PRE-P2-T1 walk file decision (two-list fnmatch AND)."""
    if exclude and any(fnmatch.fnmatch(rel, g) for g in exclude):
        return False
    if include and not any(fnmatch.fnmatch(rel, g) for g in include):
        return False
    return True


def test_include_semantics_diverge_old_allowlist_vs_new_negation():
    """The load-bearing semantic change: ``include_globs`` was an *allowlist*
    (only matching files kept); it is now a gitignore *negation* (re-include
    override). On a non-matching file the two disagree."""
    rel = "movie.mp4"
    include = ["*.mkv"]

    # OLD: include is an allowlist -> a .mp4 not matching *.mkv is DROPPED.
    assert _old_kept(rel, include, exclude=[]) is False

    # NEW: include is a negation -> with nothing excluding it, .mp4 is KEPT.
    spec = build_exclusion_spec([], includes=include)
    new_kept = not spec.match_file(rel)
    assert new_kept is True

    # The two engines diverge on exactly this input (the migration's semantics).
    assert _old_kept(rel, include, exclude=[]) != new_kept
