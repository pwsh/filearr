"""Phase 9 Meilisearch-adoption scaffolding tests (P9-T* pure cores).

Pure unit tests — no Meilisearch, no network, no Postgres. Guards the inert
``filearr.meili_ops`` scaffolding so the implementing tasks inherit green coverage
of: the fragmentation/compaction decision matrix (incl. zero/None), shadow-index
naming round-trip + staleness, settings-drift (list-as-set vs order-sensitive
ranking rules, nested typo dict), timing-safe webhook-secret comparison, and the
single-source-of-truth consistency between ``INDEX_SETTINGS_SPEC`` and
``hashx.HASH_ATTRIBUTES``.
"""

from datetime import UTC, datetime, timedelta

import pytest

from filearr.hashx import HASH_ATTRIBUTES
from filearr.meili_ops import (
    DEFAULT_COMPACTION_THRESHOLD,
    DEFAULT_SEARCH_CUTOFF_MS,
    DISABLE_TYPO_ATTRIBUTES,
    FACET_SEARCH_DISABLED,
    INDEX_SETTINGS_SPEC,
    MeiliTaskNotification,
    TypoToleranceSpec,
    WebhookTarget,
    compact_if_fragmented,
    ensure_webhook,
    fragmentation_ratio,
    is_shadow_uid,
    is_stale_shadow,
    parse_shadow_ts,
    settings_drift,
    shadow_uid,
    should_compact,
    verify_webhook_secret,
)


# --------------------------------------------------------------------------- #
# fragmentation_ratio / should_compact — matrix incl. zero + None              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("db", "used", "expected"),
    [
        (130, 100, 1.3),
        (200, 100, 2.0),
        (100, 100, 1.0),
        (0, 100, 0.0),
        # unmeasurable -> 0.0 sentinel, never a raise
        (100, 0, 0.0),
        (100, None, 0.0),
        (None, 100, 0.0),
        (None, None, 0.0),
        (100, -5, 0.0),
    ],
)
def test_fragmentation_ratio(db, used, expected):
    assert fragmentation_ratio(db, used) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (1.0, False),
        (1.3, False),   # strict > threshold, exactly-at does not trigger
        (1.31, True),
        (2.0, True),
        (0.0, False),   # unmeasurable sentinel never triggers
    ],
)
def test_should_compact_default_threshold(ratio, expected):
    assert should_compact(ratio) is expected
    assert DEFAULT_COMPACTION_THRESHOLD == 1.3


def test_should_compact_custom_threshold():
    assert should_compact(1.5, threshold=2.0) is False
    assert should_compact(2.5, threshold=2.0) is True


def test_fragmentation_then_compact_pipeline():
    # end-to-end pure pipeline as compact_if_fragmented will wire it
    ratio = fragmentation_ratio(150, 100)
    assert should_compact(ratio) is True
    assert should_compact(fragmentation_ratio(105, 100)) is False


# --------------------------------------------------------------------------- #
# shadow-index naming round-trip + staleness                                   #
# --------------------------------------------------------------------------- #
def test_shadow_uid_round_trip_datetime():
    ts = datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC)
    uid = shadow_uid("items", ts)
    assert uid == f"items_rebuild_{int(ts.timestamp())}"
    assert parse_shadow_ts(uid) == int(ts.timestamp())
    assert is_shadow_uid(uid) is True


def test_shadow_uid_round_trip_epoch():
    uid = shadow_uid("items", 1_700_000_000)
    assert uid == "items_rebuild_1700000000"
    assert parse_shadow_ts(uid) == 1_700_000_000


def test_parse_shadow_ts_rejects_non_shadow():
    assert parse_shadow_ts("items") is None
    assert parse_shadow_ts("items_rebuild_notanumber") is None
    assert parse_shadow_ts("plain_index") is None
    assert is_shadow_uid("items") is False


def test_shadow_uid_base_containing_separator_round_trips():
    # a base uid that itself contains the separator must still round-trip via the
    # LAST separator
    base = "items_rebuild_backup"
    uid = shadow_uid(base, 42)
    assert parse_shadow_ts(uid) == 42


def test_is_stale_shadow_boundaries():
    ts = datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC)
    max_age = timedelta(hours=6)
    uid = shadow_uid("items", ts)
    # exactly at creation -> not stale
    assert is_stale_shadow(uid, ts, max_age) is False
    # just past max_age -> stale
    assert is_stale_shadow(uid, ts + max_age + timedelta(seconds=1), max_age) is True
    # exactly at max_age boundary -> not stale (strict >)
    assert is_stale_shadow(uid, ts + max_age, max_age) is False
    # non-shadow names are never stale
    assert is_stale_shadow("items", ts + timedelta(days=99), max_age) is False


def test_is_stale_shadow_accepts_epoch_and_seconds():
    uid = shadow_uid("items", 1000)
    assert is_stale_shadow(uid, 2000, 500) is True
    assert is_stale_shadow(uid, 1400, 500) is False


# --------------------------------------------------------------------------- #
# settings_drift — set-wise lists, ordered ranking rules, nested typo dict      #
# --------------------------------------------------------------------------- #
def test_settings_drift_filterable_is_order_insensitive():
    desired = {"filterableAttributes": ["a", "b", "c"]}
    current = {"filterableAttributes": ["c", "a", "b"]}
    assert settings_drift(current, desired) == []


def test_settings_drift_filterable_detects_missing_member():
    desired = {"filterableAttributes": ["a", "b", "c"]}
    current = {"filterableAttributes": ["a", "b"]}
    assert settings_drift(current, desired) == ["filterableAttributes"]


def test_settings_drift_ranking_rules_are_order_sensitive():
    desired = {"rankingRules": ["words", "typo", "proximity"]}
    same = {"rankingRules": ["words", "typo", "proximity"]}
    reordered = {"rankingRules": ["typo", "words", "proximity"]}
    assert settings_drift(same, desired) == []
    assert settings_drift(reordered, desired) == ["rankingRules"]


def test_settings_drift_searchable_is_order_sensitive():
    desired = {"searchableAttributes": ["title", "filename"]}
    reordered = {"searchableAttributes": ["filename", "title"]}
    assert settings_drift(reordered, desired) == ["searchableAttributes"]


def test_settings_drift_nested_typo_disable_list_is_set_wise():
    desired = {"typoTolerance": {"enabled": True, "disableOnAttributes": ["year", "size"]}}
    reordered = {"typoTolerance": {"enabled": True, "disableOnAttributes": ["size", "year"]}}
    changed = {"typoTolerance": {"enabled": True, "disableOnAttributes": ["size"]}}
    assert settings_drift(reordered, desired) == []
    assert settings_drift(changed, desired) == ["typoTolerance"]


def test_settings_drift_nested_enabled_flag_change():
    desired = {"typoTolerance": {"enabled": True, "disableOnAttributes": []}}
    current = {"typoTolerance": {"enabled": False, "disableOnAttributes": []}}
    assert settings_drift(current, desired) == ["typoTolerance"]


def test_settings_drift_missing_key_counts_as_drift():
    desired = {"searchCutoffMs": 1500}
    assert settings_drift({}, desired) == ["searchCutoffMs"]
    assert settings_drift({"searchCutoffMs": 1500}, desired) == []


def test_settings_drift_ignores_extra_current_keys():
    desired = {"searchCutoffMs": 1500}
    current = {"searchCutoffMs": 1500, "somethingMeiliReturns": 99}
    assert settings_drift(current, desired) == []


def test_settings_drift_multiple_sorted():
    desired = {"searchCutoffMs": 1500, "filterableAttributes": ["a"]}
    current = {"searchCutoffMs": 100, "filterableAttributes": ["b"]}
    assert settings_drift(current, desired) == ["filterableAttributes", "searchCutoffMs"]


def test_index_spec_has_no_self_drift():
    # the spec must be stable against itself
    assert settings_drift(dict(INDEX_SETTINGS_SPEC), INDEX_SETTINGS_SPEC) == []


# --------------------------------------------------------------------------- #
# webhook secret — timing-safe compare (equal / unequal / empty)               #
# --------------------------------------------------------------------------- #
def test_verify_webhook_secret_equal():
    assert verify_webhook_secret("Bearer s3cr3t", "s3cr3t") is True


def test_verify_webhook_secret_unequal():
    assert verify_webhook_secret("Bearer wrong", "s3cr3t") is False
    assert verify_webhook_secret("s3cr3t", "s3cr3t") is False  # missing "Bearer "


@pytest.mark.parametrize(
    ("header", "secret"),
    [
        ("", "s3cr3t"),
        (None, "s3cr3t"),
        ("Bearer s3cr3t", ""),
        ("Bearer s3cr3t", None),
        (None, None),
    ],
)
def test_verify_webhook_secret_empty(header, secret):
    assert verify_webhook_secret(header, secret) is False


def test_webhook_target_auth_headers():
    t = WebhookTarget(url="http://app:8000/x", secret="abc", headers={"X-Env": "test"})
    hdrs = t.auth_headers()
    assert hdrs["Authorization"] == "Bearer abc"
    assert hdrs["X-Env"] == "test"
    assert verify_webhook_secret(hdrs["Authorization"], "abc") is True


# --------------------------------------------------------------------------- #
# webhook notification model (inert receiver contract)                          #
# --------------------------------------------------------------------------- #
def test_meili_task_notification_parses_camelcase():
    n = MeiliTaskNotification.model_validate(
        {
            "uid": 12,
            "batchUid": 3,
            "indexUid": "items",
            "status": "failed",
            "type": "documentAdditionOrUpdate",
            "finishedAt": "2026-07-07T00:00:00Z",
        }
    )
    assert n.index_uid == "items"
    assert n.batch_uid == 3
    assert n.failed is True


def test_meili_task_notification_tolerant_and_succeeded():
    n = MeiliTaskNotification.model_validate({"status": "succeeded", "unknownField": 1})
    assert n.failed is False
    assert n.uid is None


# --------------------------------------------------------------------------- #
# INDEX_SETTINGS_SPEC ↔ hashx.HASH_ATTRIBUTES consistency (single source)       #
# --------------------------------------------------------------------------- #
def test_typo_disable_includes_all_hash_attributes():
    # single source of truth: every hashx attribute is typo-disabled
    for attr in HASH_ATTRIBUTES:
        assert attr in DISABLE_TYPO_ATTRIBUTES, attr


def test_typo_disable_includes_numeric_fields():
    assert "year" in DISABLE_TYPO_ATTRIBUTES
    assert "size" in DISABLE_TYPO_ATTRIBUTES


def test_typo_disable_is_sorted_and_deduped():
    assert list(DISABLE_TYPO_ATTRIBUTES) == sorted(set(DISABLE_TYPO_ATTRIBUTES))


def test_index_spec_typo_matches_disable_constant():
    typo = INDEX_SETTINGS_SPEC["typoTolerance"]
    assert set(typo["disableOnAttributes"]) == set(DISABLE_TYPO_ATTRIBUTES)
    assert typo["enabled"] is True


def test_index_spec_search_cutoff_and_facet_optouts():
    assert INDEX_SETTINGS_SPEC["searchCutoffMs"] == DEFAULT_SEARCH_CUTOFF_MS
    # numeric/high-cardinality fields + the P6-T3 path_scope scope key + the
    # near-unique P3-T1 hash digests must be facet-search-disabled (path_scope is a
    # filter key, never a human facet; the hashes are opaque exact-match targets).
    assert set(FACET_SEARCH_DISABLED) == {
        "size", "mtime", "year", "path_scope", "quick_hash", "content_hash",
    }
    assert set(INDEX_SETTINGS_SPEC["facetSearchDisabled"]) == set(FACET_SEARCH_DISABLED)


def test_typo_tolerance_spec_render():
    spec = TypoToleranceSpec()
    rendered = spec.as_meili()
    assert rendered["enabled"] is True
    assert set(rendered["disableOnAttributes"]) == set(DISABLE_TYPO_ATTRIBUTES)


# --------------------------------------------------------------------------- #
# stubs raise NotImplementedError tagged with their task                        #
# --------------------------------------------------------------------------- #
# rebuild_via_swap is implemented (P9-T5); the remaining STUBS still raise.
@pytest.mark.parametrize("coro", [compact_if_fragmented, ensure_webhook])
async def test_stubs_raise_not_implemented(coro):
    with pytest.raises(NotImplementedError):
        await coro()
