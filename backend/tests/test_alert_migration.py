"""P8-T1: alerting-tables migration round-trip on a real Postgres (pgserver).

Upgrade to head creates the four tables + FKs + CHECKs + the partial pending
index; a one-step downgrade drops them cleanly and leaves the predecessor
(provenance revision) intact; re-upgrade is repeatable. Also asserts the FK
cascades (rule delete -> events/junction, channel delete -> junction).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Pin the upgrade target to the ALERT revision, NOT "head": a later migration
# (P3-T7 saved_searches, e2f4a6c8b0d1) now sits on top of it, so "head" would
# advance alembic_version past this and break the exact-rev assertion.
HEAD = "d9a1c4e6b8f3"
PROVENANCE_REV = "c1e3a5b7d9f2"


def _cfg() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


def _psycopg3(uri: str) -> str:
    if uri.startswith("postgresql://"):
        return uri.replace("postgresql://", "postgresql+psycopg://", 1)
    return uri


def _tables(engine):
    return set(inspect(engine).get_table_names())


def _cols(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _fks(engine, table):
    return {fk["name"] for fk in inspect(engine).get_foreign_keys(table)}


def _checks(engine, table):
    return {c["name"] for c in inspect(engine).get_check_constraints(table)}


def _indexes(engine, table):
    return {i["name"] for i in inspect(engine).get_indexes(table)}


ALERT_TABLES = {"alert_channels", "alert_rules", "alert_rule_channels", "alert_events"}


@pytest.mark.usefixtures("pg_uri")
def test_alert_tables_upgrade_downgrade_round_trip(pg_uri):
    cfg = _cfg()
    # The pgserver DB is session-shared and another test may have already
    # advanced it PAST the alert head (P3-T7 saved_searches sits on top). Go to
    # the true head first, then trim back DOWN to exactly the alert revision so
    # the exact-rev assertion is deterministic regardless of test order.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, HEAD)
    engine = create_engine(_psycopg3(pg_uri))
    try:
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert rev == HEAD
        assert ALERT_TABLES <= _tables(engine)

        assert {"name", "type", "config", "dispatch_locality", "enabled"} <= _cols(
            engine, "alert_channels"
        )
        assert {
            "event_types",
            "group_by",
            "group_wait_s",
            "digest_window",
            "hash_change_only",
            "is_system",
            "library_id",
        } <= _cols(engine, "alert_rules")
        assert {"rule_id", "channel_id"} <= _cols(engine, "alert_rule_channels")
        assert {
            "rule_id",
            "item_id",
            "library_id",
            "event_type",
            "dedup_key",
            "payload",
            "delivered",
            "delivered_at",
            "delivery_attempts",
            "last_error",
        } <= _cols(engine, "alert_events")

        assert "fk_alert_rules_library_id_libraries" in _fks(engine, "alert_rules")
        assert {
            "fk_alert_rule_channels_rule_id_alert_rules",
            "fk_alert_rule_channels_channel_id_alert_channels",
        } <= _fks(engine, "alert_rule_channels")
        assert {
            "fk_alert_events_rule_id_alert_rules",
            "fk_alert_events_item_id_items",
            "fk_alert_events_library_id_libraries",
        } <= _fks(engine, "alert_events")

        assert {
            "alert_channel_type_valid",
            "alert_channel_dispatch_locality_valid",
        } <= _checks(engine, "alert_channels")
        assert "alert_rule_digest_window_valid" in _checks(engine, "alert_rules")

        ev_idx = _indexes(engine, "alert_events")
        assert "ix_alert_events_pending" in ev_idx
        assert "ix_alert_events_rule_delivered_at" in ev_idx

        # The type CHECK rejects a bad kind.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO alert_channels (name, type, config) "
                        "VALUES ('bad', 'sms', '{}')"
                    )
                )

        command.downgrade(cfg, PROVENANCE_REV)
        assert not (ALERT_TABLES & _tables(engine))
        assert "items" in _tables(engine)
        assert "source_agent_id" in _cols(engine, "items")

        command.upgrade(cfg, HEAD)
        assert ALERT_TABLES <= _tables(engine)
    finally:
        engine.dispose()


@pytest.mark.usefixtures("pg_uri")
def test_alert_fk_cascades(pg_uri):
    cfg = _cfg()
    # The pgserver DB is session-shared and another test may have already
    # advanced it PAST the alert head (P3-T7 saved_searches sits on top). Go to
    # the true head first, then trim back DOWN to exactly the alert revision so
    # the exact-rev assertion is deterministic regardless of test order.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, HEAD)
    engine = create_engine(_psycopg3(pg_uri))
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM alert_events"))
            conn.execute(text("DELETE FROM alert_rule_channels"))
            conn.execute(text("DELETE FROM alert_rules"))
            conn.execute(text("DELETE FROM alert_channels"))
            ch = conn.execute(
                text(
                    "INSERT INTO alert_channels (name, type, config) "
                    "VALUES ('c', 'webhook', '{}') RETURNING id"
                )
            ).scalar()
            rule = conn.execute(
                text(
                    "INSERT INTO alert_rules (name, event_types) "
                    "VALUES ('r', ARRAY['created']) RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO alert_rule_channels (rule_id, channel_id) "
                    "VALUES (:r, :c)"
                ),
                {"r": rule, "c": ch},
            )
            conn.execute(
                text(
                    "INSERT INTO alert_events (rule_id, event_type, dedup_key) "
                    "VALUES (:r, 'created', 'k')"
                ),
                {"r": rule},
            )

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM alert_channels WHERE id = :c"), {"c": ch})
            n_junction = conn.execute(
                text("SELECT count(*) FROM alert_rule_channels WHERE channel_id = :c"),
                {"c": ch},
            ).scalar()
            n_rules = conn.execute(text("SELECT count(*) FROM alert_rules")).scalar()
        assert n_junction == 0
        assert n_rules == 1

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM alert_rules WHERE id = :r"), {"r": rule})
            n_events = conn.execute(
                text("SELECT count(*) FROM alert_events WHERE rule_id = :r"),
                {"r": rule},
            ).scalar()
        assert n_events == 0
    finally:
        engine.dispose()
