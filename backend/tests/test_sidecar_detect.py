"""Sidecar classification (pure, lexical)."""

from filearr.sidecar import classify


def test_episode_nfo_links_to_stem():
    info = classify("Arcane/Season 01/Arcane.S01E01.nfo")
    assert info is not None
    assert info.kind == "nfo"
    assert info.parent_stem == "Arcane.S01E01"
    assert info.directory == "Arcane/Season 01"


def test_stem_thumb_jpg():
    info = classify("Movies/Dune (2021)/Dune (2021)-thumb.jpg")
    assert info is not None
    assert info.kind == "artwork"
    assert info.parent_stem == "Dune (2021)"


def test_directory_poster_is_dir_artwork():
    info = classify("Movies/Dune (2021)/poster.jpg")
    assert info is not None
    assert info.kind == "artwork"
    assert info.parent_stem is None  # directory-level → primary item


def test_folder_jpg_and_cover_and_fanart():
    for name in ("folder.jpg", "cover.png", "fanart.jpg", "banner.jpeg"):
        info = classify(f"Music/Album/{name}")
        assert info is not None and info.kind == "artwork" and info.parent_stem is None


def test_movie_nfo_is_directory_level():
    info = classify("Movies/Dune (2021)/movie.nfo")
    assert info is not None and info.kind == "nfo" and info.parent_stem is None


def test_tvshow_nfo_is_directory_level():
    info = classify("Shows/Arcane/tvshow.nfo")
    assert info is not None and info.kind == "nfo" and info.parent_stem is None


def test_jriver_sidecar():
    info = classify("Library/track01_JRSidecar.xml")
    assert info is not None
    assert info.kind == "jriver"
    assert info.parent_stem == "track01"


def test_jriver_sidecar_case_insensitive():
    info = classify("Library/Track_jrsidecar.xml")
    assert info is not None and info.kind == "jriver"


def test_regular_media_is_not_sidecar():
    assert classify("Movies/Dune (2021)/Dune (2021).mkv") is None
    assert classify("Music/song.flac") is None
    assert classify("Photos/vacation.jpg") is None  # plain image, no convention


def test_season_poster():
    info = classify("Shows/Arcane/season01-poster.jpg")
    assert info is not None and info.kind == "artwork"


# --------------------------------------------------------------------------- #
# OPS-T4: .xmp (Adobe metadata) + .thm (camera thumbnail) same-stem sidecars   #
# --------------------------------------------------------------------------- #
def test_xmp_sidecar_same_stem():
    info = classify("Photos/2024/IMG_1234.xmp")
    assert info is not None
    assert info.kind == "xmp"
    assert info.parent_stem == "IMG_1234"  # → IMG_1234.<raw> sibling
    assert info.directory == "Photos/2024"


def test_thm_sidecar_same_stem():
    info = classify("Videos/MVI_5678.thm")
    assert info is not None
    assert info.kind == "artwork"  # camera thumbnail image
    assert info.parent_stem == "MVI_5678"


def test_xmp_case_insensitive_ext():
    info = classify("Photos/DSC01.XMP")
    assert info is not None and info.kind == "xmp" and info.parent_stem == "DSC01"


def test_bare_xmp_dotfile_is_not_sidecar():
    # A file literally named ".xmp"/".thm" has no stem (splitext yields ext="") —
    # no parent stem, so it is NOT a sidecar (falls through to media_type=other).
    assert classify("Photos/.xmp") is None
    assert classify("Videos/.thm") is None
