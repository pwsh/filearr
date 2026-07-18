"""P11-T2 — targeted unit checks for the ``meta.``/``cf.`` grammar extension.

The canonical, cross-language cases live in ``shared/querydsl-vectors.json`` (run
by ``test_querydsl_scaffold``). These are Python-only pins for the boundaries the
vector file does not enumerate exhaustively (length cap, dotted nesting, the
bare-``meta``/``cf`` free-text fallthrough)."""

from __future__ import annotations

import pytest

from filearr import querydsl as q


def test_meta_text_equality_default():
    f = q.parse("meta.resolution:1080p").filters[0]
    assert f.key == "meta.resolution"
    assert f.value == q.MetaValue("=", "1080p")


def test_meta_numeric_comparator_and_range():
    assert q.parse("meta.bitrate:>1000000").filters[0].value == q.MetaValue(">", "1000000")
    assert q.parse("meta.height:480..720").filters[0].value == q.MetaValue(
        "range", "480", "720"
    )


def test_cf_prefix_and_negation():
    assert q.parse("cf.rating:5").filters[0] == q.Filter(
        "cf.rating", q.MetaValue("=", "5")
    )
    assert q.parse("-cf.status:archived").filters[0].negated is True


def test_dotted_subkey_is_kept_verbatim():
    assert q.parse("meta.audio.channels:2").filters[0].key == "meta.audio.channels"


def test_bare_meta_or_cf_without_dot_is_free_text():
    assert q.parse("meta:2").terms[0] == q.Term("meta:2")
    assert q.parse("cf:x").terms[0] == q.Term("cf:x")


@pytest.mark.parametrize(
    "bad,code",
    [
        ("meta.Resolution:x", "bad_meta_key"),   # uppercase rejected
        ("cf.ra-ting:x", "bad_cf_key"),           # out-of-charset
        ("meta.:1", "bad_meta_key"),              # empty subkey
        ("meta.a..b:1", "bad_meta_key"),          # double dot
        ("meta.a.:1", "bad_meta_key"),            # trailing dot
        ("~meta.x:1", "fuzzy_on_filter"),         # fuzzy on a filter
        ("meta.x:>", "empty_value"),              # comparator, no value
        ("meta.x:>1..2", "bad_range"),            # range + comparator
    ],
)
def test_dynamic_key_errors(bad, code):
    with pytest.raises(q.ParseError) as ei:
        q.parse(bad)
    assert ei.value.code == code


def test_length_cap_rejected():
    long_key = "a" * (q.MAX_DYNAMIC_KEY_LEN + 1)
    with pytest.raises(q.ParseError) as ei:
        q.parse(f"meta.{long_key}:1")
    assert ei.value.code == "bad_meta_key"


def test_roundtrip_dynamic_filters():
    for s in ["meta.resolution:1080p", "meta.bitrate:>1000000", "cf.rating:5",
              "meta.height:480..720", "-cf.status:archived", "meta.audio.channels:2"]:
        ast = q.parse(s)
        assert q.parse(str(ast)) == ast, s
