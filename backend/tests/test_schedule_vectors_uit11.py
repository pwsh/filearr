"""UI-T11 — pins the semantic contract of the frontend schedule builder.

The friendly scan-schedule builder (``frontend/src/lib/schedule.ts``) is a pure
LOCAL<->UTC cron generate/parse layer. The repo has no JS test runner, so the
contract is pinned language-neutrally by ``shared/schedule-vectors.json`` and
enforced here:

  * a Python port of the JS generate/parse (kept byte-for-byte equivalent —
    same day-shift / wrap arithmetic) reproduces every vector's cron and
    satisfies the round-trip invariant ``generate(parse(cron)) == cron``;
  * every generated cron is accepted by cronsim (``validate_cron``);
  * the declared UTC due-times agree with ``schedule.cron_is_due`` — this is the
    real semantic anchor (the scheduler evaluates cron in UTC, T5 decision), so
    a wrong day-shift in the JS would make the vector's cron fire on the wrong
    UTC instant and this test would fail.

Vectors deliberately include midnight crossings in BOTH directions (west-of-UTC
late-evening rolls the run to next-day UTC; east-of-UTC early-morning rolls it
to previous-day UTC), a weekly day-of-week shift, monthly day-of-month shifts
incl. the 1<->31 wrap boundary, a fractional-offset (UTC+5:30) minute shift, and
Advanced pass-through crons.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from filearr.schedule import cron_is_due, validate_cron

VECTORS_PATH = Path(__file__).resolve().parents[2] / "shared" / "schedule-vectors.json"
_DOC = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
_VECTORS = _DOC["vectors"]


# --------------------------------------------------------------------------- #
# Python port of frontend/src/lib/schedule.ts (generate + parse).
# Must stay behaviourally identical to the TS. Python's floor // and % already
# match the JS `Math.floor` + `mod = ((n%m)+m)%m` helpers used there for the
# day-shift and time-of-day arithmetic (verified for negative offsets by the
# cross-midnight vectors).
# --------------------------------------------------------------------------- #
def _mod(n: int, m: int) -> int:
    return ((n % m) + m) % m


def _wrap_dom(d: int) -> int:
    return _mod(d - 1, 31) + 1


def _local_to_utc(hour: int, minute: int, off: int) -> tuple[int, int]:
    raw = hour * 60 + minute + off
    return raw // 1440, raw % 1440  # dayShift, tod


def _utc_to_local(hour: int, minute: int, off: int) -> tuple[int, int]:
    raw = hour * 60 + minute - off
    return raw // 1440, raw % 1440


def generate_cron(b: dict, off: int) -> str | None:
    mode = b["mode"]
    if mode == "off":
        return None
    if mode == "hourly":
        return f"{_mod(b['minute'] + off, 60)} * * * *"
    if mode == "advanced":
        return (b.get("cron") or "").strip() or None
    ds, tod = _local_to_utc(b["hour"], b["minute"], off)
    uh, um = tod // 60, tod % 60
    if mode == "daily":
        return f"{um} {uh} * * *"
    if mode == "weekly":
        days = b.get("daysOfWeek") or []
        if not days:
            return None
        dows = sorted({_mod(d + ds, 7) for d in days})
        return f"{um} {uh} * * {','.join(map(str, dows))}"
    if mode == "monthly":
        return f"{um} {uh} {_wrap_dom(b['dayOfMonth'] + ds)} * *"
    raise ValueError(mode)


def _int_field(s: str, lo: int, hi: int) -> int | None:
    if not s.isdigit():
        return None
    n = int(s)
    return n if lo <= n <= hi else None


def parse_cron(cron: str | None, off: int) -> dict:
    raw = (cron or "").strip()
    if not raw:
        return {"mode": "off"}
    adv = {"mode": "advanced", "cron": raw}
    parts = raw.split()
    if len(parts) != 5:
        return adv
    mi_f, ho_f, dom_f, mon_f, dow_f = parts
    if mon_f != "*":
        return adv
    if ho_f == "*" and dom_f == "*" and dow_f == "*":
        mi = _int_field(mi_f, 0, 59)
        if mi is None:
            return adv
        return {"mode": "hourly", "minute": _mod(mi - off, 60)}
    mi = _int_field(mi_f, 0, 59)
    ho = _int_field(ho_f, 0, 23)
    if mi is None or ho is None:
        return adv
    if dom_f == "*" and dow_f == "*":
        _, tod = _utc_to_local(ho, mi, off)
        return {"mode": "daily", "hour": tod // 60, "minute": tod % 60}
    if dom_f == "*" and dow_f != "*":
        dows = []
        for tok in dow_f.split(","):
            d = _int_field(tok, 0, 7)
            if d is None:
                return adv
            dows.append(0 if d == 7 else d)
        ds, tod = _utc_to_local(ho, mi, off)
        loc = sorted({_mod(d + ds, 7) for d in dows})
        return {"mode": "weekly", "hour": tod // 60, "minute": tod % 60, "daysOfWeek": loc}
    if dow_f == "*" and dom_f != "*":
        dom = _int_field(dom_f, 1, 31)
        if dom is None:
            return adv
        ds, tod = _utc_to_local(ho, mi, off)
        return {
            "mode": "monthly",
            "hour": tod // 60,
            "minute": tod % 60,
            "dayOfMonth": _wrap_dom(dom + ds),
        }
    return adv


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def _id(v: dict) -> str:
    return v["name"]


def test_vectors_present():
    # Contract requires broad coverage; guard against an accidentally-truncated file.
    assert len(_VECTORS) >= 15


@pytest.mark.parametrize("vec", _VECTORS, ids=_id)
def test_builder_generates_expected_cron(vec):
    got = generate_cron(vec["builder"], vec["offset_minutes"])
    assert got == vec["cron"], f"{vec['name']}: generate -> {got!r}, expected {vec['cron']!r}"


@pytest.mark.parametrize("vec", _VECTORS, ids=_id)
def test_round_trip_generate_parse(vec):
    off = vec["offset_minutes"]
    cron = vec["cron"]
    # generate(parse(x)) == x for every builder-generated cron.
    assert generate_cron(parse_cron(cron, off), off) == cron


@pytest.mark.parametrize("vec", _VECTORS, ids=_id)
def test_generated_cron_is_valid(vec):
    if vec["cron"] is None:
        return
    validate_cron(vec["cron"])  # raises InvalidCronError on failure


@pytest.mark.parametrize("vec", _VECTORS, ids=_id)
def test_due_times_match_cron_is_due(vec):
    cron = vec["cron"]
    for sample in vec["due"]:
        dt = datetime.fromisoformat(sample["utc"])
        got = cron_is_due(cron, dt) if cron else False
        assert got is sample["due"], (
            f"{vec['name']}: cron {cron!r} @ {sample['utc']} "
            f"expected due={sample['due']} got {got}"
        )


def test_at_least_one_midnight_cross_each_direction():
    # Integrity guard: the day-shift math is the whole point — make sure both
    # directions and a weekly DOW shift are actually exercised.
    def parts(name):
        v = next(x for x in _VECTORS if x["name"] == name)
        return v

    # west, late-evening local -> next-day UTC (forward)
    assert parts("daily_west_crossfwd")["cron"] == "0 4 * * *"
    # east, early-morning local -> previous-day UTC (backward)
    assert parts("daily_east_crossback")["cron"] == "0 16 * * *"
    # weekly DOW shift: local Sunday -> UTC Monday
    assert parts("weekly_west_sun_to_mon")["cron"] == "0 3 * * 1"
