"""Phase 4 scaffolding tests (P4-T1/T2/T3/T4 pure helpers + real profile data).

Pure unit tests — no Postgres, no Meilisearch, no engine. Guards the inert
scaffolding in ``filearr.profiles`` / ``filearr.custom_fields`` so the
implementing tasks inherit green coverage of the load-bearing pure logic:
profile coverage of the live extractor vocabularies, the shared pydantic
validator, custom-field name sanitisation, and the R5 namespace helpers.
"""

import pytest

from filearr.custom_fields import (
    CustomFieldDef,
    cf_meili_attribute,
    normalize_field_name,
    validate_custom_values,
)
from filearr.models import MediaType
from filearr.profiles import (
    METADATA_PROFILES,
    FieldSpec,
    build_validator,
    family_of,
    get_profile,
    is_namespaced,
    validate_metadata,
)

# --- profile coverage -------------------------------------------------------


def test_every_media_type_has_a_profile():
    # Every MediaType member must resolve to a profile (empty allowed for `other`).
    for mt in MediaType:
        assert mt in METADATA_PROFILES, f"no profile for {mt}"
        assert isinstance(get_profile(mt), list)


def test_models_import_is_side_effect_free():
    # profiles imports MediaType from models; confirm the enum is the real one.
    assert MediaType.video.value == "video"
    assert {mt.value for mt in MediaType} >= {
        "video", "audio", "audiobook", "sample", "image",
        "model3d", "document", "spreadsheet", "other",
    }


def _field_names(mt: MediaType) -> set[str]:
    return {f.name for f in get_profile(mt)}


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (MediaType.video, {"duration", "video_codec", "audio_tracks", "resolution", "hdr"}),
        (MediaType.audio, {"artist", "album", "genre", "duration", "bitrate"}),
        (MediaType.audiobook, {"chapters", "chapter_count", "artist"}),
        (MediaType.image, {"width", "height", "camera", "taken_at"}),
        (MediaType.model3d, {"triangles", "watertight", "bbox", "mesh_count"}),
        (MediaType.document, {"pages", "author", "encrypted"}),
        (MediaType.spreadsheet, {"sheets", "sheet_count"}),
    ],
)
def test_profile_covers_documented_extractor_keys(media_type, expected):
    # Spot-check that the profile declares the keys the extractor actually emits.
    assert expected <= _field_names(media_type), (
        f"{media_type} profile missing {expected - _field_names(media_type)}"
    )


def test_sample_reuses_audio_vocabulary():
    assert _field_names(MediaType.sample) == _field_names(MediaType.audio)


def test_other_profile_is_empty():
    assert get_profile(MediaType.other) == []


def test_all_field_specs_have_valid_data_type():
    # __post_init__ guards this, but assert across the whole real dataset too.
    for mt, specs in METADATA_PROFILES.items():
        for spec in specs:
            assert spec.data_type in {
                "string", "integer", "float", "boolean", "datetime", "string_list"
            }, f"{mt}.{spec.name} has bad data_type {spec.data_type}"


def test_field_spec_rejects_unknown_data_type():
    with pytest.raises(ValueError):
        FieldSpec("x", "colour", "X")


# --- validate_metadata ------------------------------------------------------


def test_validate_metadata_wrong_type_flagged():
    # video_codec is a string; an int must produce a structured per-field error.
    errs = validate_metadata(MediaType.video, {"video_codec": 123})
    assert errs, "expected a validation error for int-into-string field"
    assert any(e.field == "video_codec" for e in errs)
    # a numeric-typed field with a non-numeric string is also caught.
    errs2 = validate_metadata(MediaType.document, {"pages": "not-a-number"})
    assert any(e.field == "pages" for e in errs2)


def test_validate_metadata_accepts_real_shaped_output():
    # A realistic ffprobe-shaped payload (incl. the list[dict] track arrays) is valid.
    payload = {
        "title": "Blade Runner",
        "year": 1982,
        "duration": 117.0,
        "video_codec": "hevc",
        "resolution": "3840x2160",
        "hdr": True,
        "audio_tracks": [{"codec": "eac3", "channels": 6, "language": "eng"}],
        "subtitle_tracks": [{"codec": "subrip", "language": "eng", "forced": False}],
    }
    assert validate_metadata(MediaType.video, payload) == []


def test_validate_metadata_passes_unregistered_keys():
    # Unknown keys (and the _extract_error sentinel) pass through untouched.
    payload = {"video_codec": "h264", "some_future_key": "whatever", "_extract_error": "boom"}
    assert validate_metadata(MediaType.video, payload) == []


def test_validate_metadata_empty_profile_accepts_anything():
    assert validate_metadata(MediaType.other, {"anything": [1, 2, 3], "x": "y"}) == []


def test_build_validator_extra_allow_roundtrip():
    model = build_validator(get_profile(MediaType.image))
    obj = model.model_validate({"width": 800, "height": 600, "extra_thing": 1})
    assert obj.width == 800


# --- R5 namespace helpers ---------------------------------------------------


@pytest.mark.parametrize(
    ("key", "namespaced", "family"),
    [
        ("exif.camera_model", True, "exif"),
        ("archive.member_count", True, "archive"),
        ("camera", False, None),          # grandfathered flat key
        ("video_codec", False, None),
        (".leading", False, None),        # no head
        ("trailing.", False, None),       # no tail
    ],
)
def test_namespace_helpers(key, namespaced, family):
    assert is_namespaced(key) is namespaced
    assert family_of(key) == family


# --- custom-field name sanitisation ----------------------------------------


def test_normalize_field_name_valid():
    assert normalize_field_name("shelf_location") == "shelf_location"
    assert normalize_field_name("rating2") == "rating2"


def test_normalize_field_name_uppercase_normalised():
    assert normalize_field_name("Rating") == "rating"
    assert normalize_field_name("  Shelf_Location  ") == "shelf_location"


@pytest.mark.parametrize(
    "bad",
    [
        "my field",     # space
        "field-name",   # hyphen
        "field.name",   # dot
        "2cool",        # leading digit
        "",             # empty
        "   ",          # blank
    ],
)
def test_normalize_field_name_invalid_rejected(bad):
    with pytest.raises(ValueError):
        normalize_field_name(bad)


@pytest.mark.parametrize("reserved", ["genre", "mtime", "title", "user_metadata", "resolution"])
def test_normalize_field_name_reserved_core_attr_rejected(reserved):
    with pytest.raises(ValueError):
        normalize_field_name(reserved)


@pytest.mark.parametrize("bad_prefix", ["cf_rating", "_secret", "cf_genre"])
def test_normalize_field_name_reserved_prefix_rejected(bad_prefix):
    with pytest.raises(ValueError):
        normalize_field_name(bad_prefix)


def test_cf_meili_attribute():
    assert cf_meili_attribute("rating") == "cf_rating"
    assert cf_meili_attribute("shelf_location") == "cf_shelf_location"


# --- custom-field value validation -----------------------------------------


def test_validate_custom_values_wrong_type():
    defs = [CustomFieldDef(name="rating", label="Rating", data_type="integer")]
    errs = validate_custom_values(defs, {"rating": "high"})
    assert any(e.field == "rating" for e in errs)


def test_validate_custom_values_ok_and_passthrough():
    defs = [CustomFieldDef(name="rating", label="Rating", data_type="integer")]
    # valid registered value + an unregistered ad-hoc key -> no errors.
    assert validate_custom_values(defs, {"rating": 5, "adhoc_note": "keep me"}) == []


def test_validate_custom_values_required_not_enforced_v1():
    # R3: a required custom field that is omitted is NOT rejected in v1.
    defs = [CustomFieldDef(name="rating", label="Rating", data_type="integer", required=True)]
    assert validate_custom_values(defs, {}) == []


def test_validate_custom_values_no_defs_is_noop():
    assert validate_custom_values([], {"anything": 123}) == []


def test_custom_field_def_type_mapping():
    # Paperless-shaped types collapse onto the FieldSpec type set for validation.
    for cf_type, fs_type in [
        ("string", "string"), ("integer", "integer"), ("float", "float"),
        ("boolean", "boolean"), ("date", "datetime"), ("url", "string"),
        ("select", "string"),
    ]:
        spec = CustomFieldDef(name="f", label="F", data_type=cf_type).to_field_spec()
        assert spec.data_type == fs_type
