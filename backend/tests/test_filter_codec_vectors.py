"""Visual filter builder — the rows<->DSL codec must emit only grammar-valid DSL.

The builder page compiles structured condition rows into a querydsl string as the
single source of truth (``frontend/src/lib/filterBuilder.ts``). This test is the
guard that keeps every codec-emitted string HONEST against the normative reference
parser (``filearr.querydsl``), mirroring ``test_dsl_help_examples`` for the DSL
help chips:

  1. every ``CODEC_VECTORS`` entry in filterBuilder.ts parses without raising; and
  2. re-parsing its canonical ``str()`` yields an EQUAL AST (the codec output is a
     stable normal form — no lossy round-trip); and
  3. the set of vectors in filterBuilder.ts equals the set this test expects, so a
     codec vector added/removed without updating this fixture fails the build.

Runs on the pure parser alone (no app/pgserver), so it is part of every slice.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from filearr.querydsl import ParseError, parse

_FILTER_BUILDER_TS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "lib"
    / "filterBuilder.ts"
)

# The canonical codec outputs the builder is expected to emit (kept in lockstep
# with the CODEC_VECTORS array in filterBuilder.ts — this test enforces the match
# both ways).
EXPECTED_VECTORS: list[str] = [
    "invoice",
    '"annual report"',
    "-draft",
    "kind:video",
    "-kind:sample",
    "group:raw-photo",
    "-group:archive",
    "ext:pdf",
    "ext:mp4;mkv;avi",
    "-ext:tmp",
    "size:>1G",
    "size:<500K",
    "size:=0",
    "size:100M..4G",
    "modified:>7d",
    "modified:<=30d",
    "modified:>=2025-01-01",
    "created:2024-01-01..2024-12-31",
    "path:*/backups/*",
    'path:"*/Season 01/*"',
    "tag:archived",
    "-tag:draft",
    "hash:e3b0c442",
    "meta.height:>=1080",
    "meta.width:1920..3840",
    "cf.rating:>=4",
    "cf.shelf_location:A12",
    "kind:video meta.height:>=1080 -tag:archived",
]

_VECTOR_RE = re.compile(r"dsl:\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')")


def _extract_ts_vectors() -> list[str]:
    content = _FILTER_BUILDER_TS.read_text(encoding="utf-8")
    # Only scan the CODEC_VECTORS block so an unrelated `dsl:` elsewhere can't leak.
    start = content.index("CODEC_VECTORS")
    block = content[start:]
    out: list[str] = []
    for m in _VECTOR_RE.finditer(block):
        raw = m.group(1)
        out.append(raw[1:-1])  # strip the surrounding quote (no escapes used)
    return out


def test_filter_builder_ts_exists() -> None:
    assert _FILTER_BUILDER_TS.is_file(), f"missing {_FILTER_BUILDER_TS}"


@pytest.mark.parametrize("vector", EXPECTED_VECTORS)
def test_codec_vector_parses(vector: str) -> None:
    """Each codec-emitted DSL string is accepted by the reference parser."""
    try:
        parse(vector)
    except ParseError as exc:  # pragma: no cover - failure detail
        pytest.fail(f"codec vector {vector!r} failed to parse: {exc.code} — {exc.reason}")


@pytest.mark.parametrize("vector", EXPECTED_VECTORS)
def test_codec_vector_roundtrips_stable(vector: str) -> None:
    """The codec output is a stable normal form: parse -> str -> parse is equal."""
    ast = parse(vector)
    assert parse(str(ast)) == ast


def test_ts_vectors_match_expected_both_ways() -> None:
    """filterBuilder.ts CODEC_VECTORS must equal EXPECTED_VECTORS (no drift)."""
    ts = _extract_ts_vectors()
    assert set(ts) == set(EXPECTED_VECTORS), (
        f"filterBuilder.ts vectors {sorted(ts)} != expected "
        f"{sorted(EXPECTED_VECTORS)} — update EXPECTED_VECTORS or CODEC_VECTORS."
    )
    assert len(ts) == len(EXPECTED_VECTORS)
