"""Phase 5 scaffolding tests — pure central-side replication logic.

No Postgres, no Procrastinate, no network, no mTLS: only the pure functions in
``filearr.agentsync`` (``check_batch`` / ``plan_upserts`` / ``manifest_digest``)
and the pydantic wire models. Guards the inert scaffolding so the implementing
tasks (P5-T1/T4/T5) inherit green coverage of the seq-continuation contract, the
batch-collapse rules, and the manifest-digest canonicalization.
"""

from filearr.agentsync import (
    AgentEvent,
    ManifestRow,
    ReplicationBatch,
    check_batch,
    classify_token,
    generate_enrollment_token,
    hash_enrollment_token,
    manifest_digest,
    mtime_to_us,
    plan_upserts,
)


def _ev(seq_no, event_type, rel_path, from_rel_path=None):
    return AgentEvent(
        seq_no=seq_no,
        event_type=event_type,
        library_ref="lib-a",
        rel_path=rel_path,
        from_rel_path=from_rel_path,
        size=10,
        mtime=1.0,
        quick_hash="q",
    )


def _batch(*events):
    return ReplicationBatch(agent_id="agent-1", entries=list(events))


# --- check_batch: seq gap / duplicate / continuation matrix ----------------


def test_exact_continuation_accepted():
    b = _batch(_ev(6, "created", "a"), _ev(7, "created", "b"), _ev(8, "created", "c"))
    v = check_batch(b, last_seq=5)
    assert v.ok is True
    assert v.reason is None
    assert v.accepted_from == 6
    assert v.accepted_to == 8


def test_single_entry_continuation_accepted():
    v = check_batch(_batch(_ev(1, "created", "a")), last_seq=0)
    assert v.ok is True
    assert (v.accepted_from, v.accepted_to) == (1, 1)


def test_empty_batch_rejected():
    v = check_batch(_batch(), last_seq=5)
    assert v.ok is False
    assert v.reason == "empty"
    assert v.expected_seq_no == 6


def test_forward_gap_rejected_with_expected_seq():
    # last contiguous 5, batch starts at 8 -> two rows missing.
    v = check_batch(_batch(_ev(8, "created", "a")), last_seq=5)
    assert v.ok is False
    assert v.reason == "gap"
    assert v.expected_seq_no == 6  # the 409 resend_from anchor


def test_stale_replay_rejected_distinct_from_gap():
    # first seq <= last_seq -> already applied, not a true forward gap.
    v = check_batch(_batch(_ev(4, "created", "a"), _ev(5, "created", "b")), last_seq=5)
    assert v.ok is False
    assert v.reason == "stale"
    assert v.expected_seq_no == 6


def test_duplicate_seq_within_batch_rejected():
    v = check_batch(_batch(_ev(6, "created", "a"), _ev(6, "modified", "a")), last_seq=5)
    assert v.ok is False
    assert v.reason == "duplicate"


def test_non_monotonic_within_batch_rejected():
    b = _batch(_ev(6, "created", "a"), _ev(7, "created", "b"), _ev(6, "created", "c"))
    v = check_batch(b, last_seq=5)
    assert v.ok is False
    assert v.reason == "non_monotonic"


def test_internal_gap_within_batch_rejected():
    v = check_batch(_batch(_ev(6, "created", "a"), _ev(8, "created", "b")), last_seq=5)
    assert v.ok is False
    assert v.reason == "internal_gap"
    assert v.expected_seq_no == 7


# --- plan_upserts: collapse + ordering + move pairing ----------------------


def test_last_event_per_path_wins_collapse():
    # created -> modified -> deleted for the same path collapses to a delete.
    plan = plan_upserts([
        _ev(1, "created", "a"),
        _ev(2, "modified", "a"),
        _ev(3, "deleted", "a"),
    ])
    assert plan.upserts == []
    assert plan.deletes == ["a"]


def test_last_event_upsert_wins_when_recreated():
    # deleted -> created for the same path collapses to an upsert.
    plan = plan_upserts([_ev(1, "deleted", "a"), _ev(2, "created", "a")])
    assert [e.rel_path for e in plan.upserts] == ["a"]
    assert plan.deletes == []


def test_moved_expands_to_delete_plus_create():
    plan = plan_upserts([_ev(5, "moved", "new/b", from_rel_path="old/a")])
    assert [e.rel_path for e in plan.upserts] == ["new/b"]
    assert plan.deletes == ["old/a"]


def test_operations_order_upserts_before_deletes():
    plan = plan_upserts([
        _ev(1, "deleted", "gone"),
        _ev(2, "created", "here"),
        _ev(3, "moved", "moved_to", from_rel_path="moved_from"),
    ])
    ops = plan.operations
    kinds = [k for k, _ in ops]
    # every upsert precedes every delete
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "upsert" else 1)
    assert ("upsert", "here") in ops
    assert ("upsert", "moved_to") in ops
    assert ("delete", "gone") in ops
    assert ("delete", "moved_from") in ops


def test_plan_is_order_independent_by_rel_path():
    a = plan_upserts([_ev(1, "created", "b"), _ev(2, "created", "a")])
    b = plan_upserts([_ev(1, "created", "a"), _ev(2, "created", "b")])
    assert [e.rel_path for e in a.upserts] == [e.rel_path for e in b.upserts] == ["a", "b"]


# --- manifest_digest: stability / order-independence / sensitivity ---------


def _row(rel_path, size=100, mtime=1234.5, content_hash="h"):
    return ManifestRow(rel_path=rel_path, size=size, mtime=mtime, content_hash=content_hash)


def test_manifest_digest_stable_repeat():
    rows = [_row("a"), _row("b"), _row("c")]
    assert manifest_digest(rows) == manifest_digest(rows)


def test_manifest_digest_order_independent():
    forward = [_row("a"), _row("b"), _row("c")]
    shuffled = [_row("c"), _row("a"), _row("b")]
    assert manifest_digest(forward) == manifest_digest(shuffled)


def test_manifest_digest_sensitive_to_size():
    base = [_row("a", size=100)]
    changed = [_row("a", size=101)]
    assert manifest_digest(base) != manifest_digest(changed)


def test_manifest_digest_sensitive_to_hash():
    a = manifest_digest([_row("a", content_hash="h1")])
    b = manifest_digest([_row("a", content_hash="h2")])
    assert a != b


def test_manifest_digest_sensitive_to_mtime_and_path():
    assert manifest_digest([_row("a", mtime=1.0)]) != manifest_digest([_row("a", mtime=2.0)])
    assert manifest_digest([_row("a")]) != manifest_digest([_row("z")])


def test_manifest_digest_empty_is_stable():
    assert manifest_digest([]) == manifest_digest([])


def test_manifest_digest_null_hash_distinct_from_empty_string():
    null_hash = manifest_digest([_row("a", content_hash=None)])
    empty_hash = manifest_digest([_row("a", content_hash="")])
    assert null_hash != empty_hash


# --- P5-T5 µs canonicalization (ruling 2, cross-language digest contract) ----


def test_mtime_to_us_rounds_to_microseconds():
    assert mtime_to_us(1.0) == 1_000_000
    assert mtime_to_us(1_700_000_000.5) == 1_700_000_000_500_000
    assert mtime_to_us(0.000001) == 1
    assert mtime_to_us(0.0000004) == 0  # below the µs quantum rounds to 0


def test_manifest_digest_ignores_sub_microsecond_mtime_noise():
    # Two mtimes inside the SAME microsecond bucket digest identically: the float
    # is quantized to integer µs before hashing (byte-stable across Go/Python).
    a = manifest_digest([_row("a", mtime=1_700_000_000.000001)])
    b = manifest_digest([_row("a", mtime=1_700_000_000.0000011)])
    assert a == b
    # A different µs bucket flips the digest.
    c = manifest_digest([_row("a", mtime=1_700_000_000.000002)])
    assert a != c


def test_manifest_digest_us_rounding_edge_half_microsecond():
    # x.9999995 s == 999999.5 µs; round()-to-even lands it in a stable bucket that
    # both language halves must reproduce identically (fixture-pinned below).
    quantum = mtime_to_us(1.9999995)
    assert quantum == round(1.9999995 * 1_000_000)
    # digest is a pure function of the quantized value.
    assert manifest_digest([_row("a", mtime=1.9999995)]) == manifest_digest(
        [ManifestRow(rel_path="a", size=100, mtime=quantum / 1_000_000, content_hash="h")]
    )


# --- P5-T5 CROSS-LANGUAGE FIXTURE VECTORS (Go half embeds identical hex) ------
# These exact (rows -> hex) pairs are the frozen digest contract. If any changes,
# the Go agent's manifest_digest must change in lockstep (and vice versa). See the
# P5-T5 report. Canonical form: JSON array sorted by rel_path, each object
# key-sorted (content_hash, mtime, quick_hash, rel_path, size), compact separators
# (",",":"), ensure_ascii=True, mtime = round(mtime*1e6) as an INTEGER, null for a
# missing hash. sha256 hex of the UTF-8 bytes.
_FIXTURES = {
    # Empty manifest. Canonical blob: []
    "empty": ([], "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    # Clean single row, both hashes present.
    "simple": (
        [ManifestRow(rel_path="movies/x.mkv", size=1024, mtime=1_700_000_000.0,
                     quick_hash="qh", content_hash="ch")],
        "e8c1b36232eaa6cfbec07206ea7674d43ba582fc72eedb5df4c635f4d6b0b09b",
    ),
    # None hashes + unicode rel_path (ensure_ascii \uXXXX escaping is contractual).
    "unicode_null": (
        [ManifestRow(rel_path="音楽/曲.flac", size=42, mtime=1.5,
                     quick_hash=None, content_hash=None)],
        "68aa77cfd2599b51711be99dc8c961415ab6f83e9a34393af2b7d111fb276aa1",
    ),
    # Sub-µs mtime quantized deterministically (b.txt .9999995 -> round-to-even up),
    # multi-row (order-independent).
    "subus_multi": (
        [
            ManifestRow(rel_path="b.txt", size=2, mtime=1_700_000_000.9999995,
                        quick_hash="q", content_hash=None),
            ManifestRow(rel_path="a.txt", size=1, mtime=0.0000004,
                        quick_hash=None, content_hash="c"),
        ],
        "c015d50ee92b854bace2d4d8bb8735ad06b0d7a95debdfc457ee2415f88c324d",
    ),
}


def test_fixture_vectors_pinned_and_order_independent():
    # The frozen cross-language digest contract: these exact hexes are embedded in
    # the Go agent half. Any change here MUST change the Go half in lockstep.
    for _name, (rows, hexval) in _FIXTURES.items():
        assert manifest_digest(rows) == hexval
        assert manifest_digest(list(reversed(rows))) == hexval


# --- P5-T1 pure enrollment helpers (no DB) ---------------------------------


def test_enrollment_token_hashes_and_is_prefixed():
    raw, token_hash = generate_enrollment_token()
    assert raw.startswith("fae_")
    assert token_hash == hash_enrollment_token(raw)
    assert len(token_hash) == 64  # sha256 hex
    assert raw != token_hash  # never store the raw token


def test_enrollment_tokens_are_unique():
    a, _ = generate_enrollment_token()
    b, _ = generate_enrollment_token()
    assert a != b


def test_classify_token_valid_consumed_expired():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 7, 16, tzinfo=UTC)
    future = now + timedelta(minutes=30)
    past = now - timedelta(minutes=1)
    # redeemable
    assert classify_token(consumed_at=None, expires_at=future, now=now) is None
    # consumed wins even if not yet expired
    assert classify_token(consumed_at=now, expires_at=future, now=now) == "consumed"
    # expired (exact-boundary is expired: expires_at <= now)
    assert classify_token(consumed_at=None, expires_at=past, now=now) == "expired"
    assert classify_token(consumed_at=None, expires_at=now, now=now) == "expired"
