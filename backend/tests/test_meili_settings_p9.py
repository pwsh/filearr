"""P9-T1/T2: ensure_index() applies the meili_ops settings spec via typed SDK
models, idempotently (settings_drift skips no-op re-applies). Meili fully mocked."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from meilisearch_python_sdk.models.settings import (
    Faceting,
    FilterableAttributes,
    MeilisearchSettings,
    TypoTolerance,
)

from filearr import search as search_mod
from filearr.config import Settings, get_settings
from filearr.meili_ops import (
    DEFAULT_SEARCH_CUTOFF_MS,
    DISABLE_TYPO_ATTRIBUTES,
    FACET_SEARCH_CANDIDATES,
    FACET_SEARCH_DISABLED,
    FILTERABLE_ATTRIBUTES,
)


def _index_mock(current):
    index = MagicMock()
    # FIX-2: ensure_index enforces primary_key == 'id'; a correct pk is a no-op.
    index.primary_key = "id"
    index.get_settings = AsyncMock(return_value=current)
    index.update_searchable_attributes = AsyncMock()
    index.update_filterable_attributes = AsyncMock()
    index.update_sortable_attributes = AsyncMock()
    index.update_ranking_rules = AsyncMock()
    index.update_typo_tolerance = AsyncMock()
    index.update_faceting = AsyncMock()
    index.update_search_cutoff_ms = AsyncMock()
    return index


def _client_for(index):
    c = MagicMock()
    c.get_or_create_index = AsyncMock(return_value=index)
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    return c


@pytest.mark.asyncio
async def test_ensure_index_applies_typed_settings_on_fresh_index(monkeypatch):
    index = _index_mock(MeilisearchSettings())  # defaults -> drift fires
    monkeypatch.setattr(search_mod, "client", lambda: _client_for(index))
    await search_mod.ensure_index()

    # per-attribute typo tolerance (typed model, correct disable list)
    tt = index.update_typo_tolerance.call_args.args[0]
    assert isinstance(tt, TypoTolerance)
    assert set(tt.disable_on_attributes) == set(DISABLE_TYPO_ATTRIBUTES)
    assert {"year", "size", "extension", "mtime", "sidecar_of"} <= set(tt.disable_on_attributes)

    # facetSearch=false on the high-cardinality set; all attrs present; typed objects
    filt = index.update_filterable_attributes.call_args.args[0]
    assert all(isinstance(f, FilterableAttributes) for f in filt)
    disabled = {p for f in filt for p in f.attribute_patterns if not f.features.facet_search}
    assert disabled == set(FACET_SEARCH_DISABLED)
    # P6-T3 added path_scope (a scope key, never a human facet); the queued hash
    # facet decision added the near-unique P3-T1 digests (opaque exact-match only).
    assert disabled == {"size", "mtime", "year", "path_scope", "quick_hash", "content_hash"}
    # ...but the hashes stay FILTERABLE (exact-match hash search must still work):
    # facet-search-disabled only flips features.facet_search, not filterability.
    hash_feats = [
        f for f in filt for p in f.attribute_patterns if p in ("quick_hash", "content_hash")
    ]
    assert hash_feats and all(
        f.features.filter.equality and not f.features.facet_search for f in hash_feats
    )
    patterns = {p for f in filt for p in f.attribute_patterns}
    assert patterns == set(FILTERABLE_ATTRIBUTES)

    # sortFacetValuesBy: count for the type-ahead candidates (R2), alpha default
    fac = index.update_faceting.call_args.args[0]
    assert isinstance(fac, Faceting)
    for cand in FACET_SEARCH_CANDIDATES:
        assert fac.sort_facet_values_by[cand] == "count"
    assert fac.sort_facet_values_by["*"] == "alpha"

    # searchCutoffMs guard (P9-T2) from the FILEARR_ setting
    cutoff = index.update_search_cutoff_ms.call_args.args[0]
    assert cutoff == DEFAULT_SEARCH_CUTOFF_MS == get_settings().meili_search_cutoff_ms


@pytest.mark.asyncio
async def test_ensure_index_is_idempotent_when_settings_match(monkeypatch):
    # feed back exactly what _apply_settings would produce -> settings_drift empty
    current = MeilisearchSettings(
        searchable_attributes=list(search_mod.SEARCHABLE),
        filterable_attributes=search_mod._filterable_settings(),
        sortable_attributes=list(search_mod.SORTABLE),
        ranking_rules=list(search_mod.RANKING_RULES),
        typo_tolerance=search_mod._typo_tolerance(),
        faceting=search_mod._faceting(),
        search_cutoff_ms=get_settings().meili_search_cutoff_ms,
    )
    index = _index_mock(current)
    monkeypatch.setattr(search_mod, "client", lambda: _client_for(index))
    await search_mod.ensure_index()

    index.update_searchable_attributes.assert_not_called()
    index.update_filterable_attributes.assert_not_called()
    index.update_sortable_attributes.assert_not_called()
    index.update_ranking_rules.assert_not_called()
    index.update_typo_tolerance.assert_not_called()
    index.update_faceting.assert_not_called()
    index.update_search_cutoff_ms.assert_not_called()


def test_search_cutoff_setting_mirrors_meili_ops_default():
    assert Settings().meili_search_cutoff_ms == DEFAULT_SEARCH_CUTOFF_MS


def test_facet_candidates_are_filterable_and_facet_enabled():
    for cand in FACET_SEARCH_CANDIDATES:
        assert cand in FILTERABLE_ATTRIBUTES
        assert cand not in FACET_SEARCH_DISABLED
