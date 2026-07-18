"""P8-T2: alert-channels + alert-rules admin API.

Secret redaction on read, the "__unchanged__" edit sentinel, 503 without
FILEARR_SECRET_KEY, test-fire through the (patched) driver, and the rule-CRUD
validation matrix (event-type vocabulary, glob compile, channel/library refs,
digest window). Auth is disabled (trusted-LAN test mode); the driver is patched
so nothing hits the network.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.alerts.dispatch import DeliveryResult
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent
CH = "/api/v1/alert-channels"
RULES = "/api/v1/alert-rules"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def ctx(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM alert_events"))
        await conn.execute(text("DELETE FROM alert_rule_channels"))
        await conn.execute(text("DELETE FROM alert_rules"))
        await conn.execute(text("DELETE FROM alert_channels"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(get_settings(), "secret_key", "unit-test-secret-key")

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, monkeypatch
    app.dependency_overrides.clear()
    await engine.dispose()


async def _mk_channel(client, **over):
    body = {
        "name": over.pop("name", "hook1"),
        "type": over.pop("type", "webhook"),
        "config": over.pop("config", {"url": "https://hook.test/x", "secret": "sign-me"}),
    }
    body.update(over)
    return await client.post(CH, json=body)


async def _seed_library(maker, name="L", root="/data/l") -> str:
    async with maker() as s:
        row = (
            await s.execute(
                text(
                    "INSERT INTO libraries (name, root_path) VALUES (:n, :r) RETURNING id"
                ),
                {"n": name, "r": root},
            )
        ).first()
        await s.commit()
        return str(row[0])


# --- channels: secret handling --------------------------------------------- #

async def test_create_channel_redacts_secret_on_read(ctx):
    client, _, _ = ctx
    r = await _mk_channel(client)
    assert r.status_code == 201, r.text
    ch = r.json()
    # The write-response is already redacted.
    assert ch["config"]["secret"] == "__redacted__"
    assert ch["config"]["url"] == "https://hook.test/x"  # url is not a secret for webhook

    # GET never leaks the plaintext secret.
    g = await client.get(f"{CH}/{ch['id']}")
    assert g.status_code == 200
    assert g.json()["config"]["secret"] == "__redacted__"
    assert "sign-me" not in g.text

    # ...and it is stored as ciphertext at rest (not the plaintext).
    from filearr.db import SessionLocal

    async with SessionLocal() as s:
        stored = (
            await s.execute(
                text("SELECT config FROM alert_channels WHERE id = :i"), {"i": ch["id"]}
            )
        ).scalar_one()
    assert stored["secret"] != "sign-me"
    assert stored["secret"] not in ("__redacted__", "__unchanged__")


async def test_create_channel_503_without_secret_key(ctx):
    client, _, monkeypatch = ctx
    monkeypatch.setattr(get_settings(), "secret_key", None)
    r = await _mk_channel(client)
    assert r.status_code == 503
    assert "FILEARR_SECRET_KEY" in r.text


async def test_patch_channel_unchanged_sentinel_keeps_secret(ctx):
    client, _, _ = ctx
    r = await _mk_channel(client)
    cid = r.json()["id"]

    from filearr.db import SessionLocal

    async with SessionLocal() as s:
        before = (
            await s.execute(
                text("SELECT config FROM alert_channels WHERE id = :i"), {"i": cid}
            )
        ).scalar_one()

    # Edit only the url, keeping the secret via the sentinel.
    p = await client.patch(
        f"{CH}/{cid}",
        json={"config": {"url": "https://hook.test/y", "secret": "__unchanged__"}},
    )
    assert p.status_code == 200
    async with SessionLocal() as s:
        after = (
            await s.execute(
                text("SELECT config FROM alert_channels WHERE id = :i"), {"i": cid}
            )
        ).scalar_one()
    assert after["secret"] == before["secret"]  # ciphertext preserved
    assert after["url"] == "https://hook.test/y"

    # Providing a real new secret re-encrypts (ciphertext changes).
    p2 = await client.patch(
        f"{CH}/{cid}", json={"config": {"url": "https://hook.test/y", "secret": "rotated"}}
    )
    assert p2.status_code == 200
    async with SessionLocal() as s:
        rotated = (
            await s.execute(
                text("SELECT config FROM alert_channels WHERE id = :i"), {"i": cid}
            )
        ).scalar_one()
    assert rotated["secret"] != before["secret"]


async def test_duplicate_channel_name_409(ctx):
    client, _, _ = ctx
    assert (await _mk_channel(client, name="dup")).status_code == 201
    assert (await _mk_channel(client, name="dup")).status_code == 409


async def test_invalid_type_and_locality_422(ctx):
    client, _, _ = ctx
    assert (await _mk_channel(client, type="sms")).status_code == 422
    assert (
        await _mk_channel(client, dispatch_locality="mars")
    ).status_code == 422


# --- test-fire ------------------------------------------------------------- #

async def test_test_fire_webhook_uses_decrypted_secret(ctx):
    client, _, monkeypatch = ctx
    r = await _mk_channel(client)
    cid = r.json()["id"]

    seen = {}

    async def fake_send_webhook(url, rendered, *, config=None, secret=None, **kw):
        seen["url"] = url
        seen["secret"] = secret
        seen["rendered"] = rendered
        return DeliveryResult(ok=True, status_code=200, detail="ok")

    import filearr.api.alerts as alerts_api

    monkeypatch.setattr(alerts_api, "send_webhook_formatted", fake_send_webhook)
    t = await client.post(f"{CH}/{cid}/test")
    assert t.status_code == 200, t.text
    assert t.json()["ok"] is True
    assert seen["url"] == "https://hook.test/x"
    assert seen["secret"] == "sign-me"  # decrypted before dispatch


async def test_test_fire_reports_delivery_failure_in_body(ctx):
    client, _, monkeypatch = ctx
    r = await _mk_channel(client)
    cid = r.json()["id"]

    from filearr.alerts.dispatch import ChannelDeliveryError

    async def boom(url, rendered, **kw):
        raise ChannelDeliveryError("target refused (blocked:private)", retryable=False)

    import filearr.api.alerts as alerts_api

    monkeypatch.setattr(alerts_api, "send_webhook_formatted", boom)
    t = await client.post(f"{CH}/{cid}/test")
    assert t.status_code == 200
    j = t.json()
    assert j["ok"] is False
    assert "refused" in j["detail"]


# --- rules ----------------------------------------------------------------- #

async def test_create_rule_end_to_end(ctx):
    client, maker, _ = ctx
    lib = await _seed_library(maker)
    ch = (await _mk_channel(client, name="c-for-rule")).json()["id"]
    r = await client.post(
        RULES,
        json={
            "name": "movies-created",
            "library_id": lib,
            "path_glob": "Movies/**",
            "event_types": ["created", "modified"],
            "hash_change_only": True,
            "digest_window": "hourly",
            "channel_ids": [ch],
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["group_by"] == ["event_type", "library_id", "rule_id"]  # R1 fixed
    assert rule["channel_ids"] == [ch]

    g = await client.get(f"{RULES}/{rule['id']}")
    assert g.status_code == 200
    assert g.json()["path_glob"] == "Movies/**"


async def test_rule_validation_matrix(ctx):
    client, maker, _ = ctx
    ch = (await _mk_channel(client, name="c1")).json()["id"]

    # bad event type
    assert (
        await client.post(
            RULES, json={"name": "r", "event_types": ["exploded"]}
        )
    ).status_code == 422
    # empty event types
    assert (
        await client.post(RULES, json={"name": "r", "event_types": []})
    ).status_code == 422
    # bad digest window
    assert (
        await client.post(
            RULES,
            json={"name": "r", "event_types": ["created"], "digest_window": "weekly"},
        )
    ).status_code == 422
    # unknown channel ref
    assert (
        await client.post(
            RULES,
            json={
                "name": "r",
                "event_types": ["created"],
                "channel_ids": ["00000000-0000-0000-0000-000000000000"],
            },
        )
    ).status_code == 422
    # unknown library ref
    assert (
        await client.post(
            RULES,
            json={
                "name": "r",
                "event_types": ["created"],
                "library_id": "00000000-0000-0000-0000-000000000000",
            },
        )
    ).status_code == 422
    # invalid glob (a bare "!" is an invalid gitwildmatch pattern)
    bad = await client.post(
        RULES, json={"name": "r", "event_types": ["created"], "path_glob": "!"}
    )
    assert bad.status_code == 422
    # a valid minimal rule still works, referencing the good channel
    ok = await client.post(
        RULES,
        json={"name": "ok", "event_types": ["created"], "channel_ids": [ch]},
    )
    assert ok.status_code == 201


async def test_update_and_delete_rule(ctx):
    client, _, _ = ctx
    rid = (
        await client.post(RULES, json={"name": "r", "event_types": ["created"]})
    ).json()["id"]
    p = await client.patch(
        f"{RULES}/{rid}", json={"event_types": ["deleted"], "enabled": False}
    )
    assert p.status_code == 200
    assert p.json()["event_types"] == ["deleted"]
    assert p.json()["enabled"] is False
    d = await client.delete(f"{RULES}/{rid}")
    assert d.status_code == 204
    assert (await client.get(f"{RULES}/{rid}")).status_code == 404


# --- P8-T13: alert-events listing (status filter + summary banner counts) --- #

EVENTS = "/api/v1/alert-events"


async def _seed_events(maker, rule_id, lib_id, max_attempts):
    """Insert one delivered, one failed-terminal, one pending event."""
    from datetime import UTC, datetime

    from filearr.models import AlertEvent

    async with maker() as s:
        s.add(AlertEvent(rule_id=rule_id, library_id=lib_id, event_type="created",
                         dedup_key="e-del", payload={}, delivered=True,
                         delivered_at=datetime.now(UTC)))
        s.add(AlertEvent(rule_id=rule_id, library_id=lib_id, event_type="created",
                         dedup_key="e-fail", payload={}, delivered=False,
                         delivery_attempts=max_attempts, last_error="boom\x07"))
        s.add(AlertEvent(rule_id=rule_id, library_id=lib_id, event_type="created",
                         dedup_key="e-pend", payload={}, delivered=False,
                         delivery_attempts=1))
        await s.commit()


async def _rule_and_lib(client, maker):
    lib = await _seed_library(maker)
    rid = (
        await client.post(
            RULES, json={"name": "watch", "event_types": ["created"], "library_id": lib}
        )
    ).json()["id"]
    return rid, lib


async def test_alert_events_status_filter(ctx):
    client, maker, _ = ctx
    rid, lib = await _rule_and_lib(client, maker)
    max_attempts = get_settings().alert_max_delivery_attempts
    await _seed_events(maker, rid, lib, max_attempts)

    failed = (await client.get(f"{EVENTS}?status=failed")).json()
    assert len(failed) == 1 and failed[0]["status"] == "failed"
    # last_error is sanitized (control chars stripped) before it leaves the API.
    assert "\x07" not in (failed[0]["last_error"] or "")

    pending = (await client.get(f"{EVENTS}?status=pending")).json()
    assert len(pending) == 1 and pending[0]["status"] == "pending"

    delivered = (await client.get(f"{EVENTS}?status=delivered")).json()
    assert len(delivered) == 1 and delivered[0]["status"] == "delivered"

    bad = await client.get(f"{EVENTS}?status=bogus")
    assert bad.status_code == 422


async def test_alert_events_summary_counts(ctx):
    client, maker, _ = ctx
    rid, lib = await _rule_and_lib(client, maker)
    max_attempts = get_settings().alert_max_delivery_attempts
    await _seed_events(maker, rid, lib, max_attempts)

    summ = (await client.get(f"{EVENTS}/summary")).json()
    assert summ == {"delivered": 1, "failed": 1, "pending": 1}
    # library scoping works.
    scoped = (await client.get(f"{EVENTS}/summary?library_id={lib}")).json()
    assert scoped["failed"] == 1


# --- FIX-16: webhook_format on channel config ------------------------------ #

async def test_webhook_format_roundtrips_in_config(ctx):
    client, _, _ = ctx
    r = await _mk_channel(
        client,
        name="discordhook",
        config={
            "url": "https://discord.com/api/webhooks/1/x",
            "webhook_format": "discord",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["config"]["webhook_format"] == "discord"
    g = await client.get(f"{CH}/{r.json()['id']}")
    assert g.json()["config"]["webhook_format"] == "discord"


async def test_webhook_format_invalid_rejected(ctx):
    client, _, _ = ctx
    r = await _mk_channel(
        client,
        name="badfmt",
        config={"url": "https://hook.test/x", "webhook_format": "telegram"},
    )
    assert r.status_code == 422
    assert "webhook_format" in r.text


async def test_webhook_format_absent_is_backcompat_generic(ctx):
    # An existing-style channel with no webhook_format key resolves to generic.
    client, _, _ = ctx
    r = await _mk_channel(client, name="legacy", config={"url": "https://hook.test/x"})
    assert r.status_code == 201
    from filearr.alerts import webhook_formats as wf
    from filearr.db import SessionLocal

    async with SessionLocal() as s:
        cfg = (
            await s.execute(
                text("SELECT config FROM alert_channels WHERE id = :i"),
                {"i": r.json()["id"]},
            )
        ).scalar_one()
    assert "webhook_format" not in cfg
    assert wf.resolve_format(cfg) == "generic"


async def test_patch_channel_sets_webhook_format(ctx):
    client, _, _ = ctx
    r = await _mk_channel(client, name="tochange", config={"url": "https://hook.test/x"})
    cid = r.json()["id"]
    p = await client.patch(
        f"{CH}/{cid}",
        json={"config": {"url": "https://hook.test/x", "webhook_format": "slack"}},
    )
    assert p.status_code == 200, p.text
    assert p.json()["config"]["webhook_format"] == "slack"
