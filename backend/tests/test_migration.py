"""Alembic upgrade/downgrade round-trip on a real Postgres (pgserver) with a
uuidv7() shim. Verifies the T3 sidecar_of column + FK apply and revert cleanly."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

# HEAD ASSERTION (P10-T6/T8): the staging dedup-index + activity-watermark
# revision (d2f4b6a8c0e1) is the new head, chained on P10-T4's staging_transfers
# (d1e5b9c3a7f2, now its predecessor).
HEAD = "c7d9e1f3a5b8"
# P10-T6/T8 revision's predecessor (downgrade target that drops the updated_at
# column + the active-item partial-unique index, leaving staging_transfers itself
# intact).
STAGING_BASE = "d1e5b9c3a7f2"
# P10-T4 revision's predecessor (downgrade target that drops staging_transfers,
# leaving agent_releases intact).
P10T4_PRED = "c4f2a6b8d1e3"
# P5-T7 revision's predecessor (downgrade target that drops agent_releases,
# leaving items.last_verified_at intact).
P5T7_PRED = "b7e3d1f9a2c4"
# P10-T3 revision's predecessor (downgrade target that drops items.last_verified_at,
# leaving policy_versions intact).
P10T3_PRED = "a3f7c1e9b5d4"
# P5-T6 revision's predecessor (downgrade target that drops policy_versions,
# leaving the P5-T5 reconcile tables intact).
P5T6_PRED = "f8a1c3e5b7d9"
# P5-T5 revision's predecessor (downgrade target that drops agent_reconcile_*
# + agents.last_reconcile_at, leaving agent_replication_log intact).
P5T5_PRED = "e7f1a9c3d5b2"
# P5-T4 revision's predecessor (downgrade target that drops agent_replication_log
# + libraries.source_agent_id/agent_library_ref, leaving agent_share_maps intact).
P5T4_PRED = "d4e6f8a0c2b4"
# P10-T1 agent_commands = the P10-T12 revision's predecessor (downgrade target
# that drops agent_share_maps).
SHAREMAP_PRED = "c1d2e3f4a5b6"
# P6-T5 OIDC identity/login-state revision's predecessor (FIX-8 last_cron_fired_at).
OIDC_PRED = "a4c8e1f6b2d9"
# FIX-8 revision's predecessor (P11-T5 report_definitions) -- downgrade target
# that drops libraries/scan_paths.last_cron_fired_at.
FIX8_PRED = "e5f2a8c4b6d3"
# Predecessor of the OCR/GPS flags revision (alert_events dedup-unique = the old head).
OCRGPS_PRED = "f3b8d2a41c5e"
# P3-T7 saved_searches revision's predecessor (P8-T1 alert tables).
ALERT_REV = "d9a1c4e6b8f3"
# P4-T7/T8 provenance revision's predecessor (UI-T12 share_prefix).
UIT12_REV = "f8b3c1d05a29"
# UI-T12 revision's predecessor: the P4 combined revision.
P4_REV = "e7c2b9a4d6f1"
# P4 combined revision's predecessor (P2-T6 scan_paths).
SCANPATHS_REV = "d5f9a3c1e8b0"
# P2-T6 scan_paths revision's predecessor (P2-T1 preset columns).
PRESET_REV = "c4e8a1b2f307"
# P2-T1 preset-columns revision's predecessor (hash-policy).
HASHPOLICY_REV = "b7d2e4f6a891"
BASELINE = "2bfb3fd1d09a"
# T3 sidecar revision (intermediate) — hash-policy tests downgrade to here.
SIDECAR_REV = "a1c3f7e9b204"


def _cfg() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


def _psycopg3(uri: str) -> str:
    # pgserver hands back a bare postgresql:// URI (defaults to psycopg2); the
    # project uses psycopg3 everywhere.
    if uri.startswith("postgresql://"):
        return uri.replace("postgresql://", "postgresql+psycopg://", 1)
    return uri


def _fk_names(engine, table):
    insp = inspect(engine)
    return {fk["name"] for fk in insp.get_foreign_keys(table)}


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"] for c in insp.get_columns(table)}


def _tables(engine):
    return set(inspect(engine).get_table_names())


@pytest.mark.usefixtures("pg_uri")
def test_upgrade_downgrade_round_trip(pg_uri):
    cfg = _cfg()

    command.upgrade(cfg, "head")
    engine = create_engine(_psycopg3(pg_uri))
    try:
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == HEAD
        # W6-D3: agents.capabilities + the inventory command kind at head; a
        # one-step downgrade to the W6-D2 revision drops the column and restores
        # the narrower kind CHECK, leaving agents intact.
        assert "capabilities" in _columns(engine, "agents")
        command.downgrade(cfg, "f5c8a2b4d6e0")
        assert "capabilities" not in _columns(engine, "agents")
        assert "agents" in _tables(engine)
        command.upgrade(cfg, "head")
        # W6-D2: agent_config_groups + agents.config_group_id at head; a one-step
        # downgrade to the P10-T11 revision drops both cleanly.
        assert "agent_config_groups" in _tables(engine)
        assert "config_group_id" in _columns(engine, "agents")
        command.downgrade(cfg, "e4a7c2f1b9d6")
        assert "agent_config_groups" not in _tables(engine)
        assert "config_group_id" not in _columns(engine, "agents")
        command.upgrade(cfg, "head")
        # P10-T11: items.share_hint present at head; a one-step downgrade to the
        # P10-T6/T8 staging revision drops it cleanly and leaves items intact.
        assert "share_hint" in _columns(engine, "items")
        command.downgrade(cfg, "d2f4b6a8c0e1")
        assert "share_hint" not in _columns(engine, "items")
        assert "items" in _tables(engine)
        command.upgrade(cfg, "head")
        # P10-T6/T8 (this task): the active-item partial-unique index +
        # updated_at watermark present at head; a one-step downgrade to the
        # P10-T4 staging_transfers revision drops BOTH cleanly and leaves the
        # table itself intact (round-trip).
        assert "updated_at" in _columns(engine, "staging_transfers")
        st_idx_head = {i["name"] for i in inspect(engine).get_indexes("staging_transfers")}
        assert "uq_staging_transfers_active_item" in st_idx_head
        command.downgrade(cfg, STAGING_BASE)
        assert "updated_at" not in _columns(engine, "staging_transfers")
        assert "uq_staging_transfers_active_item" not in {
            i["name"] for i in inspect(engine).get_indexes("staging_transfers")
        }
        assert "staging_transfers" in _tables(engine)  # table itself intact
        command.upgrade(cfg, "head")
        # P10-T4: staging_transfers present at head; a downgrade to the P5-T7
        # predecessor drops it cleanly and leaves agent_releases intact (round-trip).
        assert "staging_transfers" in _tables(engine)
        st_cols = _columns(engine, "staging_transfers")
        assert {
            "id", "item_id", "agent_id", "command_id", "state", "bytes_transferred",
            "total_bytes", "staged_path", "verified_hash", "verified", "expires_at",
            "last_range_request_at", "created_at",
        } <= st_cols
        st_uq = {
            c["name"] for c in inspect(engine).get_unique_constraints("staging_transfers")
        }
        assert "uq_staging_transfers_command" in st_uq
        command.downgrade(cfg, P10T4_PRED)
        assert "staging_transfers" not in _tables(engine)
        assert "agent_releases" in _tables(engine)  # P5-T7 predecessor intact
        command.upgrade(cfg, "head")
        # P5-T7: agent_releases present at head; a downgrade drops it cleanly and
        # leaves the P10-T3 predecessor (items.last_verified_at) intact.
        assert "agent_releases" in _tables(engine)
        ar_cols = _columns(engine, "agent_releases")
        assert {"id", "version", "stage", "manifest", "created_at", "promoted_at"} <= ar_cols
        ar_uq = {c["name"] for c in inspect(engine).get_unique_constraints("agent_releases")}
        assert "uq_agent_releases_version" in ar_uq
        command.downgrade(cfg, P5T7_PRED)
        assert "agent_releases" not in _tables(engine)
        assert "last_verified_at" in _columns(engine, "items")  # P10-T3 pred intact
        command.upgrade(cfg, "head")
        # P10-T3: items.last_verified_at present at head; a one-step
        # downgrade drops it cleanly and leaves the policy_versions predecessor
        # intact (round-trip).
        assert "last_verified_at" in _columns(engine, "items")
        command.downgrade(cfg, P10T3_PRED)
        assert "last_verified_at" not in _columns(engine, "items")
        assert "policy_versions" in set(inspect(engine).get_table_names())  # pred intact
        command.upgrade(cfg, "head")
        # P5-T6: the policy_versions table present at head; a one-step
        # downgrade drops it cleanly and leaves the P5-T5 predecessor (the
        # reconcile sweep tables) intact (round-trip).
        assert "policy_versions" in set(inspect(engine).get_table_names())
        pv_cols = _columns(engine, "policy_versions")
        assert {
            "id", "scope_type", "scope_id", "version", "policy", "actor", "created_at",
        } <= pv_cols
        pv_idx = {i["name"] for i in inspect(engine).get_indexes("policy_versions")}
        assert "ix_policy_versions_scope_version" in pv_idx
        pv_uq = {
            c["name"] for c in inspect(engine).get_unique_constraints("policy_versions")
        }
        assert "uq_policy_versions_scope_version" in pv_uq
        command.downgrade(cfg, P5T6_PRED)
        assert "policy_versions" not in set(inspect(engine).get_table_names())
        assert "agent_reconcile_sessions" in set(  # P5-T5 predecessor intact
            inspect(engine).get_table_names()
        )
        command.upgrade(cfg, "head")
        # P5-T5: the reconcile sweep tables + agents.last_reconcile_at
        # watermark present at head; a one-step downgrade drops them cleanly and
        # leaves the P5-T4 predecessor (agent_replication_log) intact (round-trip).
        head_tables = set(inspect(engine).get_table_names())
        assert {"agent_reconcile_sessions", "agent_reconcile_staging"} <= head_tables
        assert "last_reconcile_at" in _columns(engine, "agents")
        ars_cols = _columns(engine, "agent_reconcile_sessions")
        assert {"id", "agent_id", "library_ref", "started_at", "staged_rows"} <= ars_cols
        stg_cols = _columns(engine, "agent_reconcile_staging")
        assert {
            "session_id", "rel_path", "size", "mtime_us", "quick_hash", "content_hash",
        } <= stg_cols
        assert "uq_agent_reconcile_sessions_agent" in {
            i["name"] for i in inspect(engine).get_indexes("agent_reconcile_sessions")
        }
        command.downgrade(cfg, P5T5_PRED)
        pred_tables = set(inspect(engine).get_table_names())
        assert "agent_reconcile_sessions" not in pred_tables
        assert "agent_reconcile_staging" not in pred_tables
        assert "last_reconcile_at" not in _columns(engine, "agents")
        assert "agent_replication_log" in pred_tables  # P5-T4 predecessor intact
        command.upgrade(cfg, "head")
        # P5-T4: agent_replication_log + the two agent-owned libraries columns
        # present at head; a downgrade drops them cleanly and leaves the
        # agent_share_maps predecessor intact (round-trip).
        assert "agent_replication_log" in set(inspect(engine).get_table_names())
        arl_cols = _columns(engine, "agent_replication_log")
        assert {"agent_id", "seq_no", "item_id", "op", "applied_at"} <= arl_cols
        assert {"source_agent_id", "agent_library_ref"} <= _columns(engine, "libraries")
        assert "uq_libraries_source_agent_ref" in {
            i["name"] for i in inspect(engine).get_indexes("libraries")
        }
        command.downgrade(cfg, P5T4_PRED)
        assert "agent_replication_log" not in set(inspect(engine).get_table_names())
        assert "source_agent_id" not in _columns(engine, "libraries")
        assert "agent_library_ref" not in _columns(engine, "libraries")
        assert "agent_share_maps" in set(inspect(engine).get_table_names())  # pred intact
        command.upgrade(cfg, "head")
        # P10-T12: agent_share_maps present at head; its unique index +
        # both FKs applied, and a one-step downgrade drops it cleanly (round-trip).
        asm_tables = set(inspect(engine).get_table_names())
        assert "agent_share_maps" in asm_tables
        asm_cols = _columns(engine, "agent_share_maps")
        assert {
            "agent_id", "library_id", "local_prefix", "share_prefix", "unc",
            "storage_type", "host", "created_at", "updated_at",
        } <= asm_cols
        asm_idx = {i["name"] for i in inspect(engine).get_indexes("agent_share_maps")}
        assert "uq_agent_share_maps_scope_prefix" in asm_idx
        assert "ix_agent_share_maps_agent" in asm_idx
        assert {"fk_agent_share_maps_agent_id_agents",
                "fk_agent_share_maps_library_id_libraries"} <= _fk_names(
            engine, "agent_share_maps"
        )
        command.downgrade(cfg, SHAREMAP_PRED)
        assert "agent_share_maps" not in set(inspect(engine).get_table_names())
        assert "agent_commands" in set(inspect(engine).get_table_names())  # pred intact
        command.upgrade(cfg, "head")
        # P6-T5 (OIDC): external_issuer + oidc_login_states present at head, and a
        # one-step downgrade drops them (additive round-trip).
        assert "external_issuer" in _columns(engine, "users")
        assert "oidc_login_states" in _tables(engine)
        command.downgrade(cfg, OIDC_PRED)
        assert "external_issuer" not in _columns(engine, "users")
        assert "oidc_login_states" not in _tables(engine)
        command.upgrade(cfg, "head")
        # FIX-8: last_cron_fired_at present at head on BOTH libraries + scan_paths,
        # and dropped by a one-step downgrade to the predecessor (round-trip).
        assert "last_cron_fired_at" in _columns(engine, "libraries")
        assert "last_cron_fired_at" in _columns(engine, "scan_paths")
        command.downgrade(cfg, FIX8_PRED)
        assert "last_cron_fired_at" not in _columns(engine, "libraries")
        assert "last_cron_fired_at" not in _columns(engine, "scan_paths")
        command.upgrade(cfg, "head")
        # P3-T6/T11: the two per-library opt-in flags present at head.
        lib_cols_head = _columns(engine, "libraries")
        assert "ocr_enabled" in lib_cols_head
        assert "expose_gps" in lib_cols_head
        # Downgrade one step (head -> alert dedup rev) drops ONLY the two flags,
        # leaving saved_searches + the alert tables intact.
        command.downgrade(cfg, OCRGPS_PRED)
        lib_cols_pred = _columns(engine, "libraries")
        assert "ocr_enabled" not in lib_cols_pred
        assert "expose_gps" not in lib_cols_pred
        assert "saved_searches" in set(inspect(engine).get_table_names())
        command.upgrade(cfg, "head")
        # UI-T12: share_prefix column + rel_path text_pattern_ops index present.
        assert "share_prefix" in _columns(engine, "libraries")
        assert "ix_items_library_rel_path_pattern" in {
            i["name"] for i in inspect(engine).get_indexes("items")
        }
        assert "sidecar_of" in _columns(engine, "items")
        assert "fk_items_sidecar_of_items" in _fk_names(engine, "items")
        # P2-T1: both preset columns present at head.
        lib_cols = _columns(engine, "libraries")
        assert "enabled_presets" in lib_cols
        assert "enabled_extension_groups" in lib_cols
        # P2-T6: scan_paths table + scan_runs.rel_path scope present at head.
        assert "scan_paths" in inspect(engine).get_table_names()
        sp_cols = _columns(engine, "scan_paths")
        assert {"library_id", "rel_path", "scan_cron", "watch_mode", "enabled"} <= sp_cols
        assert "fk_scan_paths_library_id_libraries" in _fk_names(engine, "scan_paths")
        assert "rel_path" in _columns(engine, "scan_runs")

        # P4-T1/T3: profile + custom-field tables present at head.
        tables = set(inspect(engine).get_table_names())
        assert "metadata_profiles" in tables
        assert "custom_fields" in tables
        mp_cols = _columns(engine, "metadata_profiles")
        assert {"media_type", "version", "schema"} <= mp_cols
        cf_cols = _columns(engine, "custom_fields")
        assert {"name", "data_type", "applies_to", "library_ids", "facetable"} <= cf_cols
        # P4-T5: GIN index + jsonb-object CHECK on items.user_metadata present.
        idx_names = {i["name"] for i in inspect(engine).get_indexes("items")}
        assert "ix_items_user_metadata" in idx_names
        checks = {c["name"] for c in inspect(engine).get_check_constraints("items")}
        assert "user_metadata_is_object" in checks

        # P4-T7: provenance columns on items present at head (all nullable).
        item_cols = _columns(engine, "items")
        assert {"source_agent_id", "replication_seq", "policy_version"} <= item_cols
        # P4-T8: item_versions.source discriminator present + defaults to 'user'.
        assert "source" in _columns(engine, "item_versions")

        # P3-T7: saved_searches table present at head.
        assert "saved_searches" in tables
        ss_cols = _columns(engine, "saved_searches")
        assert {"name", "owner_principal", "params"} <= ss_cols

        # Downgrade one step (head -> alert rev) drops ONLY saved_searches, leaving
        # the alert tables + provenance columns intact.
        command.downgrade(cfg, ALERT_REV)
        assert "saved_searches" not in set(inspect(engine).get_table_names())
        assert "alert_events" in set(inspect(engine).get_table_names())
        assert "source_agent_id" in _columns(engine, "items")

        # Downgrade further (alert rev -> UI-T12 rev) drops the provenance columns +
        # item_versions.source but leaves the UI-T12 predecessor intact.
        command.downgrade(cfg, UIT12_REV)
        item_cols = _columns(engine, "items")
        assert "source_agent_id" not in item_cols
        assert "replication_seq" not in item_cols
        assert "policy_version" not in item_cols
        assert "source" not in _columns(engine, "item_versions")
        assert "share_prefix" in _columns(engine, "libraries")  # predecessor intact
        command.upgrade(cfg, "head")

        # Downgrade one more step (head -> P4 rev) drops share_prefix + the pattern
        # index but leaves the P4 objects intact.
        command.downgrade(cfg, P4_REV)
        assert "share_prefix" not in _columns(engine, "libraries")
        assert "ix_items_library_rel_path_pattern" not in {
            i["name"] for i in inspect(engine).get_indexes("items")
        }
        assert "metadata_profiles" in inspect(engine).get_table_names()  # intact
        command.upgrade(cfg, "head")

        # Downgrade one step (head -> scan_paths rev) drops the P4 objects but
        # leaves the P2-T6 predecessor intact.
        command.downgrade(cfg, SCANPATHS_REV)
        tables = set(inspect(engine).get_table_names())
        assert "metadata_profiles" not in tables
        assert "custom_fields" not in tables
        assert "ix_items_user_metadata" not in {
            i["name"] for i in inspect(engine).get_indexes("items")
        }
        assert "user_metadata_is_object" not in {
            c["name"] for c in inspect(engine).get_check_constraints("items")
        }
        assert "scan_paths" in inspect(engine).get_table_names()  # predecessor intact
        command.upgrade(cfg, "head")

        # Downgrade one step (head -> preset rev) drops scan_paths + the scope col.
        command.downgrade(cfg, PRESET_REV)
        assert "scan_paths" not in inspect(engine).get_table_names()
        assert "rel_path" not in _columns(engine, "scan_runs")
        assert "enabled_presets" in _columns(engine, "libraries")  # predecessor intact
        command.upgrade(cfg, "head")

        # Downgrade two steps (head -> hash-policy) drops the preset columns too.
        command.downgrade(cfg, HASHPOLICY_REV)
        lib_cols = _columns(engine, "libraries")
        assert "enabled_presets" not in lib_cols
        assert "enabled_extension_groups" not in lib_cols
        assert "hash_policy" in lib_cols  # predecessor column intact
        command.upgrade(cfg, "head")

        # Downgrade to baseline: column + FK gone, base tables intact.
        command.downgrade(cfg, BASELINE)
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == BASELINE
        assert "sidecar_of" not in _columns(engine, "items")
        assert "items" in inspect(engine).get_table_names()

        # Re-upgrade to confirm the migration is repeatable.
        command.upgrade(cfg, "head")
        assert "sidecar_of" in _columns(engine, "items")
    finally:
        engine.dispose()


@pytest.mark.usefixtures("pg_uri")
def test_cascade_delete_removes_orphan_sidecars(pg_uri):
    """ondelete=CASCADE: hard-deleting a parent removes its sidecar rows."""
    cfg = _cfg()
    command.upgrade(cfg, "head")
    engine = create_engine(_psycopg3(pg_uri))
    try:
        with engine.begin() as conn:
            lib = conn.execute(
                text(
                    "INSERT INTO libraries (name, root_path) VALUES "
                    "('t3lib', '/data') RETURNING id"
                )
            ).scalar()
            parent = conn.execute(
                text(
                    "INSERT INTO items (library_id, media_type, path, rel_path, "
                    "filename, size, mtime) VALUES "
                    "(:lib, 'video', '/data/a.mkv', 'a.mkv', 'a.mkv', 1, now()) "
                    "RETURNING id"
                ),
                {"lib": lib},
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO items (library_id, media_type, path, rel_path, "
                    "filename, size, mtime, sidecar_of) VALUES "
                    "(:lib, 'other', '/data/a.nfo', 'a.nfo', 'a.nfo', 1, now(), :p)"
                ),
                {"lib": lib, "p": parent},
            )
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM items WHERE id = :p"), {"p": parent})
            # only THIS library's rows should be gone (shared session DB)
            remaining = conn.execute(
                text("SELECT count(*) FROM items WHERE library_id = :lib"),
                {"lib": lib},
            ).scalar()
        assert remaining == 0  # sidecar cascaded away with its parent
    finally:
        engine.dispose()
