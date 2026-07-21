"""Phase 2 scaffolding tests (P2-T1..T6 data + pure helpers).

Pure unit tests — no Postgres, no Procrastinate, no filesystem walk (only tmp
files for the CACHEDIR.TAG signature check). Guards the inert scaffolding in
``filearr.presets`` and ``filearr.tasks.scoped_scan`` so the implementing tasks
inherit green data-integrity + pure-helper coverage.
"""

import pytest

from filearr.presets import (
    CACHEDIR_TAG_SIGNATURE,
    EXTENSION_GROUPS,
    PRESET_BUNDLES,
    build_exclusion_spec,
    get_preset,
    is_cachedir_tagged,
    validate_extension_group_names,
    validate_preset_names,
)
from filearr.tasks.scoped_scan import ScanPathRule, resolve_scan_path

# --- Preset data integrity -------------------------------------------------


def test_five_bundles_present():
    assert set(PRESET_BUNDLES) == {
        "system_files",
        "hidden_dotfiles",
        "caches_temp",
        "node_modules_build",
        "os_metadata",
    }


def test_all_patterns_nonempty_and_stripped():
    for name, bundle in PRESET_BUNDLES.items():
        assert bundle.label.strip(), name
        assert bundle.exclude, f"{name} has no patterns"
        for pat in bundle.exclude:
            assert pat and pat == pat.strip("\n"), f"{name}: bad pattern {pat!r}"


def test_bracket_expansion_present_for_thumbs_and_desktop():
    """R2: OS-junk name patterns ship pre-bracket-expanded (case-tolerant)."""
    all_patterns = [p for b in PRESET_BUNDLES.values() for p in b.exclude]
    assert "[Tt]humbs.db" in all_patterns
    assert "[Dd]esktop.ini" in all_patterns
    # the naive case-exact forms must NOT be shipped
    assert "Thumbs.db" not in all_patterns
    assert "desktop.ini" not in all_patterns


def test_hidden_dotfiles_default_on_others_off():
    assert PRESET_BUNDLES["hidden_dotfiles"].default_enabled is True
    assert PRESET_BUNDLES["hidden_dotfiles"].exclude == (".*",)
    for name, bundle in PRESET_BUNDLES.items():
        if name != "hidden_dotfiles":
            assert bundle.default_enabled is False, name


def test_hidden_dotfiles_has_upgrade_caveat():
    caveat = PRESET_BUNDLES["hidden_dotfiles"].caveat or ""
    assert "surface previously hidden" in caveat.lower()


def test_get_preset_and_validation():
    assert get_preset("system_files").label == "System files"
    with pytest.raises(KeyError):
        get_preset("nope")
    assert validate_preset_names(["system_files", "os_metadata"]) == []
    assert validate_preset_names(["system_files", "bogus"]) == ["bogus"]


# --- Extension groups ------------------------------------------------------


def test_extension_groups_typed_and_bare_ext():
    assert EXTENSION_GROUPS["raw_photos"].file_category == "image"
    assert "cr2" in EXTENSION_GROUPS["raw_photos"].extensions
    for name, grp in EXTENSION_GROUPS.items():
        assert grp.extensions, name
        for ext in grp.extensions:
            assert ext == ext.lower() and not ext.startswith("."), (name, ext)


def test_multiple_groups_per_mediatype_allowed():
    """R5: >1 group may target the same MediaType (union semantics)."""
    image_groups = [n for n, g in EXTENSION_GROUPS.items() if g.file_category == "image"]
    assert {"raw_photos", "jpeg_family"} <= set(image_groups)


def test_validate_extension_group_names():
    assert validate_extension_group_names(["office_docs"]) == []
    assert validate_extension_group_names(["office_docs", "x"]) == ["x"]


# --- build_exclusion_spec --------------------------------------------------


def test_node_modules_pruned_style_match():
    spec = build_exclusion_spec(["node_modules_build"])
    # directory-prune: the walk checks a dir entry WITH a trailing separator
    assert spec.match_file("node_modules/")
    assert spec.match_file("sub/deep/node_modules/")
    assert spec.match_file("dist/")
    # a normal media file is not excluded
    assert not spec.match_file("Movies/Arcane/s01e01.mkv")


def test_disabled_preset_not_applied():
    spec = build_exclusion_spec([])  # nothing enabled
    assert not spec.match_file("node_modules/")


def test_negation_reincludes_a_file():
    """include_globs re-admit a file a preset/exclude would drop (R1/roadmap)."""
    spec = build_exclusion_spec(
        enabled_presets=[],
        extra_excludes=["*.log"],
        includes=["important.log"],
    )
    assert spec.match_file("random.log")  # still excluded
    assert not spec.match_file("important.log")  # re-included via negation


def test_system_files_matches_case_variants():
    spec = build_exclusion_spec(["system_files"])
    assert spec.match_file("Thumbs.db")
    assert spec.match_file("thumbs.db")
    assert spec.match_file("a/b/Desktop.ini")
    assert spec.match_file("$RECYCLE.BIN/")


# --- CACHEDIR.TAG signature check (P2-T4) ----------------------------------


def test_cachedir_tag_valid_signature(tmp_path):
    (tmp_path / "CACHEDIR.TAG").write_bytes(
        CACHEDIR_TAG_SIGNATURE + b"\n# extra trailing content is fine\n"
    )
    assert is_cachedir_tagged(str(tmp_path)) is True


def test_cachedir_tag_wrong_signature_not_tagged(tmp_path):
    (tmp_path / "CACHEDIR.TAG").write_bytes(b"Signature: deadbeef not the real one\n")
    assert is_cachedir_tagged(str(tmp_path)) is False


def test_cachedir_tag_absent_not_tagged(tmp_path):
    assert is_cachedir_tagged(str(tmp_path)) is False


# --- resolve_scan_path longest-prefix (P2-T6) ------------------------------


def test_resolve_scan_path_longest_prefix_wins():
    rules = [
        ScanPathRule(rel_path="", scan_cron="0 3 * * *"),          # library root
        ScanPathRule(rel_path="Downloads", scan_cron="*/5 * * * *"),
        ScanPathRule(rel_path="Downloads/Incoming", scan_cron="* * * * *"),
    ]
    assert resolve_scan_path(rules, "Downloads/Incoming/a.mkv").rel_path == "Downloads/Incoming"
    assert resolve_scan_path(rules, "Downloads/other/b.mkv").rel_path == "Downloads"
    assert resolve_scan_path(rules, "Movies/c.mkv").rel_path == ""  # root override


def test_resolve_scan_path_no_match_returns_none():
    rules = [ScanPathRule(rel_path="Downloads")]
    assert resolve_scan_path(rules, "Movies/c.mkv") is None
    assert resolve_scan_path([], "anything") is None


def test_resolve_scan_path_segment_boundary_not_substring():
    """'Down' must not match 'Downloads' (segment boundary, not raw prefix)."""
    rules = [ScanPathRule(rel_path="Down")]
    assert resolve_scan_path(rules, "Downloads/x.mkv") is None
    assert resolve_scan_path(rules, "Down/x.mkv").rel_path == "Down"
