"""Phase 7 scaffolding tests — the local query DSL reference parser.

Two independent layers of coverage:

1. **Vector-driven (the R6 contract).** Every case in
   ``shared/querydsl-vectors.json`` — the single, language-neutral source of
   truth the future Go port must also satisfy — is asserted against the Python
   reference :func:`filearr.querydsl.parse`. This guards regressions here and
   pins the exact bytes the Go parser is graded on.
2. **Hand-written unit assertions.** A handful of ASTs/errors written out
   longhand (not generated from the parser) so the vectors themselves can't
   drift the semantics silently — if someone regenerates the vectors from a
   broken parser, these still fail.

Plus a round-trip property check (``parse(str(ast)) == ast``) and a smoke check
of the inert wire-contract models in :mod:`filearr.localapi_contracts`.
"""

import json
from pathlib import Path

import pytest

from filearr import localapi_contracts as contracts
from filearr import querydsl as q

VECTORS_PATH = Path(__file__).resolve().parents[2] / "shared" / "querydsl-vectors.json"


def _load_vectors():
    doc = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    return doc["vectors"]


_VECTORS = _load_vectors()
_OK = [v for v in _VECTORS if "ast" in v]
_ERR = [v for v in _VECTORS if "error" in v]


def test_vector_file_present_and_sized():
    # R6: at least 40 canonical cases; both success and error paths represented.
    assert len(_VECTORS) >= 40
    assert len(_OK) >= 20
    assert len(_ERR) >= 10
    names = [v["name"] for v in _VECTORS]
    assert len(names) == len(set(names)), "vector names must be unique"


@pytest.mark.parametrize("vec", _OK, ids=[v["name"] for v in _OK])
def test_valid_vectors_parse_to_expected_ast(vec):
    ast = q.parse(vec["input"])
    assert ast.to_dict() == vec["ast"], vec["name"]


@pytest.mark.parametrize("vec", _ERR, ids=[v["name"] for v in _ERR])
def test_error_vectors_raise_expected_parse_error(vec):
    with pytest.raises(q.ParseError) as ei:
        q.parse(vec["input"])
    # The contract is (code, position); reason is informational.
    assert ei.value.code == vec["error"]["code"], vec["name"]
    assert ei.value.position == vec["error"]["position"], vec["name"]


@pytest.mark.parametrize("vec", _OK, ids=[v["name"] for v in _OK])
def test_roundtrip_str_reparses_to_equal_ast(vec):
    ast = q.parse(vec["input"])
    assert q.parse(str(ast)) == ast, vec["name"]


# --- Independent, hand-written semantic pins (not generated) ----------------


def test_empty_query_is_empty():
    ast = q.parse("")
    assert ast == q.Query()
    assert ast.terms == () and ast.filters == () and ast.fuzzy is False


def test_binary_size_suffixes():
    assert q.parse("size:>1G").filters[0].value == q.SizeValue(">", 1073741824)
    assert q.parse("size:>=500M").filters[0].value == q.SizeValue(">=", 500 * 1024**2)
    assert q.parse("size:2T").filters[0].value == q.SizeValue("=", 2 * 1024**4)
    assert q.parse("size:1M..10M").filters[0].value == q.SizeValue(
        "range", 1024**2, 10 * 1024**2
    )


def test_relative_durations_normalise_to_seconds():
    assert q.parse("modified:<7d").filters[0].value == q.DurationValue("<", 604800)
    assert q.parse("created:>=30m").filters[0].value == q.DurationValue(">=", 1800)
    assert q.parse("created:<90s").filters[0].value == q.DurationValue("<", 90)


def test_iso_date_and_range():
    assert q.parse("modified:>2026-01-01").filters[0].value == q.DateValue(
        ">", "2026-01-01"
    )
    assert q.parse("modified:2026-01-01..2026-02-01").filters[0].value == q.DateValue(
        "range", "2026-01-01", "2026-02-01"
    )


def test_ext_list_lowered_and_dot_stripped():
    assert q.parse("ext:.PDF;doc").filters[0].value == q.ListValue(("pdf", "doc"))


def test_negation_and_fuzzy_markers():
    assert q.parse("-kind:video").filters[0] == q.Filter(
        "kind", q.StringValue("video"), negated=True
    )
    assert q.parse("~report").terms[0] == q.Term("report", fuzzy=True)
    assert q.parse("~report").fuzzy is True
    assert q.parse("-~word").terms[0] == q.Term("word", negated=True, fuzzy=True)


def test_unknown_key_is_free_text_not_error():
    ast = q.parse("foo:bar")
    assert ast.filters == ()
    assert ast.terms == (q.Term("foo:bar"),)


def test_quoted_phrase_never_a_filter():
    ast = q.parse('"kind:video"')
    assert ast.filters == ()
    assert ast.terms == (q.Term("kind:video"),)


def test_fuzzy_on_filter_is_rejected():
    with pytest.raises(q.ParseError) as ei:
        q.parse("~kind:video")
    assert ei.value.code == "fuzzy_on_filter"
    assert ei.value.position == 0


def test_hash_must_be_hex():
    assert q.parse("hash:AbCd12").filters[0].value == q.StringValue("abcd12")
    with pytest.raises(q.ParseError) as ei:
        q.parse("hash:xyz")
    assert ei.value.code == "bad_hash"


def test_unterminated_quote_position():
    with pytest.raises(q.ParseError) as ei:
        q.parse('"unterminated')
    assert ei.value.code == "unterminated_quote"
    assert ei.value.position == 0


def test_only_parse_error_escapes_for_malformed_input():
    for bad in ["size:1.5G", "modified:2026-13-01", "size:1X", "ext:", "size:1M.."]:
        with pytest.raises(q.ParseError):
            q.parse(bad)


# --- Inert wire-contract smoke checks ---------------------------------------


def test_contracts_scope_required_and_snake_case():
    resp = contracts.QueryResponse(
        rows=[
            contracts.ResultRow(
                id="0193",
                rel_path="a/b.mkv",
                filename="b.mkv",
                size=10,
                mtime="2026-07-07T00:00:00Z",
            )
        ],
        total=1,
        truncated=False,
        fuzzy=False,
        scope=contracts.ScopeInfo(active=True, predicates=["media/**"]),
        elapsed_ms=3,
    )
    dumped = resp.model_dump()
    assert dumped["scope"]["active"] is True  # R3 affordance flag present
    assert "rel_path" in dumped["rows"][0]  # snake_case wire key


def test_contracts_reject_unknown_keys():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        contracts.HealthResponse(status="ok", index_ready=True, item_count=0, bogus=1)


def test_health_is_read_only_by_default():
    h = contracts.HealthResponse(status="ok", index_ready=True, item_count=5)
    assert h.read_only is True
