"""FIX-12 (Item B) — the front-end "Query syntax" help must stay truthful.

The shared help module ``frontend/src/lib/dslHelp.ts`` documents the filter DSL
with clickable example chips. This test is the guard that keeps every documented
example HONEST against the normative reference parser (``filearr.querydsl``):

  1. every fixture example parses without raising ``ParseError``; and
  2. every fixture example appears VERBATIM in dslHelp.ts (so the docs and this
     parse-verified fixture can never drift); and
  3. dslHelp.ts contains no UNDOCUMENTED-by-this-fixture example (the count of
     ``{ q:`` example entries equals the fixture size), so a new chip added to the
     help without a matching parse-verified fixture entry fails the build.

The fixture is the single list the reviewer reads to see exactly what syntax the
UI advertises.
"""

from __future__ import annotations

import pathlib

import pytest

from filearr.querydsl import ParseError, parse

# Every example string shown as a chip in frontend/src/lib/dslHelp.ts. Keep this
# in sync with that file — this test enforces the correspondence both ways.
DOC_EXAMPLES: list[str] = [
    # Free text
    "invoice",
    '"annual report"',
    '"quarterly report"',
    # Fuzzy (search only)
    "~documentaru",
    # Negation
    "-draft",
    "-kind:sample",
    "!ext:tmp",
    # kind:
    "kind:video",
    "kind:audio",
    # ext:
    "ext:pdf",
    "ext:mp4;mkv;avi",
    # size:
    "size:>1G",
    "size:<500K",
    "size:100M..4G",
    # modified: / created:
    "modified:>7d",
    "modified:<30d",
    "modified:>=2025-01-01",
    "created:2024-01-01..2024-12-31",
    # path:
    "path:*/backups/*",
    'path:"*/Season 01/*"',
    # tag:
    "tag:archived",
    "-tag:draft",
    # hash:
    "hash:e3b0c442",
    # meta. / cf.
    "meta.height:>=1080",
    "meta.duration:>3600",
    "meta.width:1920..3840",
    "cf.rating:>=4",
    "cf.shelf_location:A12",
    # Combine
    "kind:video meta.height:>=1080 -tag:archived",
    "kind:audio ext:flac size:>50M",
]

_DSL_HELP_TS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "lib"
    / "dslHelp.ts"
)


@pytest.mark.parametrize("example", DOC_EXAMPLES)
def test_documented_example_parses(example: str) -> None:
    """Each help example must be accepted by the normative reference parser."""
    try:
        parse(example)
    except ParseError as exc:  # pragma: no cover - failure detail
        pytest.fail(f"doc example {example!r} failed to parse: {exc.code} — {exc.reason}")


def test_dslhelp_ts_exists() -> None:
    assert _DSL_HELP_TS.is_file(), f"missing {_DSL_HELP_TS}"


@pytest.mark.parametrize("example", DOC_EXAMPLES)
def test_example_appears_verbatim_in_dslhelp(example: str) -> None:
    """Every fixture example must appear literally in dslHelp.ts (docs match tests)."""
    content = _DSL_HELP_TS.read_text(encoding="utf-8")
    assert example in content, f"{example!r} is not present verbatim in dslHelp.ts"


def test_no_undocumented_examples_in_dslhelp() -> None:
    """The number of chip examples in dslHelp.ts equals the fixture size, so a chip
    added without a parse-verified fixture entry is caught."""
    content = _DSL_HELP_TS.read_text(encoding="utf-8")
    chip_count = content.count("{ q:")
    assert chip_count == len(DOC_EXAMPLES), (
        f"dslHelp.ts has {chip_count} example chips but the parse-verified fixture "
        f"has {len(DOC_EXAMPLES)}; add/remove a DOC_EXAMPLES entry to match."
    )
