"""Phase 3 scaffolding tests (P3-T1/T6/T8/T11 pure helpers + data).

Pure unit tests — no Postgres, no Meilisearch, no engine subprocess (only tmp
files for the streaming-digest check). Guards the inert scaffolding in
``filearr.hashx`` / ``filearr.ocr`` / ``filearr.exif`` / ``filearr.embed`` so the
implementing tasks inherit green coverage of the load-bearing pure logic.
"""

import hashlib

import pytest

from filearr.embed import EmbedderConfig, embedder_fingerprint
from filearr.exif import GPS_KEYS, strip_gps
from filearr.hashx import HASH_ATTRIBUTES, compute_digests
from filearr.ocr import OcrPolicy, should_ocr

# --- hashx.compute_digests -------------------------------------------------


def test_hash_attributes_membership():
    # Reconciled with phase-9 disable_on_attributes; hash + string-shaped members.
    assert set(HASH_ATTRIBUTES) == {
        "quick_hash",
        "content_hash",
        "extension",
        "mtime",
        "sidecar_of",
    }


def test_compute_digests_matches_hashlib(tmp_path):
    data = b"the quick brown fox jumps over the lazy dog\n" * 4
    f = tmp_path / "sample.bin"
    f.write_bytes(data)
    got = compute_digests(str(f), algorithms=("md5", "sha256"))
    assert got["md5"] == hashlib.md5(data).hexdigest()
    assert got["sha256"] == hashlib.sha256(data).hexdigest()


def test_compute_digests_streaming_small_chunk_matches_wholefile(tmp_path):
    # A "big-ish" file read with a deliberately tiny chunk_size must still equal
    # a single whole-file hash — exercises the multi-iteration streaming loop.
    data = bytes(range(256)) * 5000  # ~1.28 MiB, spans many 7-byte chunks
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    got = compute_digests(str(f), algorithms=("sha256",), chunk_size=7)
    assert got["sha256"] == hashlib.sha256(data).hexdigest()


def test_compute_digests_empty_algorithms_no_read(tmp_path):
    # Does not even need to open the file when nothing is requested.
    assert compute_digests(str(tmp_path / "missing.bin"), algorithms=()) == {}


def test_compute_digests_default_algorithms(tmp_path):
    f = tmp_path / "d.bin"
    f.write_bytes(b"x")
    got = compute_digests(str(f))
    assert set(got) == {"md5", "sha256"}


def test_compute_digests_rejects_bad_chunk_size(tmp_path):
    f = tmp_path / "z.bin"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        compute_digests(str(f), chunk_size=0)


def test_compute_digests_unknown_algorithm(tmp_path):
    f = tmp_path / "z.bin"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        compute_digests(str(f), algorithms=("not-a-real-hash",))


# --- ocr.should_ocr policy matrix ------------------------------------------


def test_should_ocr_disabled_always_false():
    pol = OcrPolicy(enabled=False, min_text_chars=100)
    assert should_ocr({"text_len": 0}, pol) is False


def test_should_ocr_triggers_below_threshold():
    pol = OcrPolicy(enabled=True, min_text_chars=100)
    assert should_ocr({"text_len": 10}, pol) is True
    assert should_ocr({}, pol) is True  # no text at all -> OCR


def test_should_ocr_skips_when_text_layer_sufficient():
    pol = OcrPolicy(enabled=True, min_text_chars=100)
    assert should_ocr({"text_len": 100}, pol) is False  # boundary: >= skips
    assert should_ocr({"body_text": "a" * 250}, pol) is False


def test_should_ocr_body_text_len_used_when_no_text_len():
    pol = OcrPolicy(enabled=True, min_text_chars=100)
    assert should_ocr({"body_text": "short"}, pol) is True


def test_should_ocr_page_and_pixel_ceilings_skip():
    pol = OcrPolicy(enabled=True, min_text_chars=100, max_pages=10, max_pixels=1_000_000)
    assert should_ocr({"text_len": 0, "pages": 50}, pol) is False
    assert should_ocr({"text_len": 0, "pixels": 5_000_000}, pol) is False
    # within both ceilings -> still triggers
    assert should_ocr({"text_len": 0, "pages": 5, "pixels": 500_000}, pol) is True


# --- exif.strip_gps --------------------------------------------------------


def test_strip_gps_removes_flat_keys_keeps_others():
    meta = {
        "GPSLatitude": 51.5,
        "GPSLongitude": -0.12,
        "gpsAltitude": 10,   # case-insensitive
        "Make": "Canon",
        "Model": "R5",
        "ISO": 400,
    }
    out = strip_gps(meta)
    assert out == {"Make": "Canon", "Model": "R5", "ISO": 400}
    assert meta.get("GPSLatitude") == 51.5  # original untouched (non-mutating)


def test_strip_gps_removes_nested_and_listed_keys():
    meta = {
        "exif": {
            "GPSPosition": "51.5 -0.12",
            "GPSLatitudeRef": "N",
            "FNumber": 2.8,
        },
        "tracks": [
            {"com.apple.quicktime.location.ISO6709": "+51-000/", "codec": "hevc"},
            {"GPSCoordinates": "x", "duration": 12},
        ],
        "keep": "yes",
    }
    out = strip_gps(meta)
    assert out["exif"] == {"FNumber": 2.8}
    assert out["tracks"] == [{"codec": "hevc"}, {"duration": 12}]
    assert out["keep"] == "yes"


def test_strip_gps_covers_every_declared_gps_key():
    meta = {k: "v" for k in GPS_KEYS}
    meta["Keep"] = "v"
    out = strip_gps(meta)
    assert out == {"Keep": "v"}


# --- embed.embedder_fingerprint --------------------------------------------


def test_fingerprint_stable_for_equal_config():
    a = EmbedderConfig(model_id="nomic-embed-text-v1.5", dim=768)
    b = EmbedderConfig(model_id="nomic-embed-text-v1.5", dim=768)
    assert embedder_fingerprint(a) == embedder_fingerprint(b)


def test_fingerprint_changes_on_any_field():
    base = EmbedderConfig(model_id="nomic-embed-text-v1.5", dim=768)
    fp = embedder_fingerprint(base)
    assert embedder_fingerprint(EmbedderConfig("bge-m3", 768)) != fp          # model
    assert embedder_fingerprint(EmbedderConfig("nomic-embed-text-v1.5", 512)) != fp  # dim
    assert (
        embedder_fingerprint(EmbedderConfig("nomic-embed-text-v1.5", 768, quantized=True))
        != fp
    )  # quantization
    assert (
        embedder_fingerprint(EmbedderConfig("nomic-embed-text-v1.5", 768, version="2"))
        != fp
    )  # version


def test_fingerprint_is_hex_sha256():
    fp = embedder_fingerprint(EmbedderConfig("m", 8))
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


# --- api.search facet-unavailable fallback (live 500 regression 2026-07-18) --


def test_is_facet_unavailable_detects_meili_facet_error():
    """A newly-added filterable attribute (e.g. file_group) is not facetable until
    Meili finishes re-indexing; the search endpoint must recognize that specific
    error and degrade (drop facet counts) instead of 500-ing all search."""
    from filearr.api.search import _is_facet_unavailable

    class _Err:
        def __init__(self, code: str, msg: str) -> None:
            self.code = code
            self._msg = msg

        def __str__(self) -> str:
            return self._msg

    # By stable Meili error code.
    assert _is_facet_unavailable(_Err("invalid_search_facets", "whatever"))
    # By message, case-insensitively (code absent/renamed across versions).
    assert _is_facet_unavailable(
        _Err("", "Attribute `file_group` is not filterable. Available: media_type")
    )
    # An unrelated API error must NOT be swallowed as a facet degrade.
    assert not _is_facet_unavailable(_Err("index_not_found", "Index `x` not found."))
    assert not _is_facet_unavailable(_Err("", "some other failure"))
