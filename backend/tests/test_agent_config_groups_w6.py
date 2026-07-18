"""W6-D2 — agent configuration groups + remote configuration + installer
distribution (central-side).

Covers: config-group CRUD + audit; the typed/versioned settings validation matrix
(unknown key / bad preset / bad regex / bad cron / oversize → 422; valid env/glob
path specs + presets pass); policy-channel delivery (group settings under
``group``, ETag folds the group tag so an edit invalidates caches, per-agent
policy precedence, NULL group = no section); assignment + SET NULL on delete;
the installer-config endpoint (token minted + frozen sidecar shape + install_hint
release-URL pattern + admin gate + audit); register with a config_group name
(resolved + unknown-name warning).

Runs against the migrated pgserver Postgres (mirrors test_policy_p5t6's harness).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import agent_config
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# DB harness                                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM policy_versions"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("UPDATE agents SET config_group_id = NULL"))
        await conn.execute(text("DELETE FROM agents"))
        await conn.execute(text("DELETE FROM agent_config_groups"))
        await conn.execute(text("DELETE FROM enrollment_tokens"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(maker, *, rollout_group="default", config_group_id=None):
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            rollout_group=rollout_group,
            cert_fingerprint=fp,
            config_group_id=config_group_id,
        )
        s.add(agent)
        await s.commit()
        return agent.id, fp


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


async def _events(maker, event_type: str) -> list[dict]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT details FROM security_events WHERE event_type = :et"
                ),
                {"et": event_type},
            )
        ).all()
    return [r.details for r in rows]


# --------------------------------------------------------------------------- #
# Pure — settings validation matrix                                            #
# --------------------------------------------------------------------------- #
def test_validate_settings_accepts_env_and_glob_specs():
    agent_config.validate_settings(
        {
            "log_level": "debug",
            "scan_selections": [
                {
                    "preset": "user-documents",
                    "paths": [
                        "%USERPROFILE%/Documents",
                        "$HOME/documents",
                        "~/Documents",
                        "/home/*/documents",
                        "/data/{a,b}/[abc]*",
                    ],
                    "include_regex": [r".*\.pdf$"],
                    "exclude_regex": [r"^~\$"],
                    "enabled": True,
                }
            ],
            "inventory": {"enabled": True, "collectors": ["stat", "owner"]},
            "scan_schedule_cron": "0 3 * * *",
        }
    )


def test_validate_settings_all_presets_ok():
    for name in agent_config.SCAN_PRESET_NAMES:
        agent_config.validate_settings({"scan_selections": [{"preset": name}]})


def test_validate_settings_unknown_top_level_key():
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings({"log_levl": "debug"})


def test_validate_settings_bad_preset():
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings({"scan_selections": [{"preset": "nope"}]})


def test_validate_settings_bad_regex():
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings(
            {"scan_selections": [{"include_regex": ["("]}]}
        )


def test_validate_settings_bad_cron():
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings({"scan_schedule_cron": "not a cron"})


def test_validate_settings_unbalanced_glob():
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings(
            {"scan_selections": [{"paths": ["/home/[user"]}]}
        )


def test_validate_settings_oversize():
    # 50 selections * ~4000-char path spec > 64 KiB compact-JSON ceiling
    big = {"scan_selections": [{"paths": ["/p/" + "a" * 4000]} for _ in range(50)]}
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings(big)


# --------------------------------------------------------------------------- #
# CRUD + audit                                                                 #
# --------------------------------------------------------------------------- #
async def test_create_get_list_group(client):
    c, maker, _ = client
    r = await c.post(
        "/api/v1/agents/config-groups",
        json={
            "name": "workstations",
            "description": "office desktops",
            "settings": {"log_level": "info"},
        },
    )
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    assert r.json()["member_count"] == 0
    assert r.json()["settings"] == {"log_level": "info"}

    got = await c.get(f"/api/v1/agents/config-groups/{gid}")
    assert got.status_code == 200
    assert got.json()["name"] == "workstations"

    lst = await c.get("/api/v1/agents/config-groups")
    assert lst.status_code == 200
    assert [g["name"] for g in lst.json()] == ["workstations"]

    assert any(
        d["name"] == "workstations" for d in await _events(maker, "agent_config_group_created")
    )


async def test_create_duplicate_name_409(client):
    c, _, _ = client
    await c.post("/api/v1/agents/config-groups", json={"name": "dup", "settings": {}})
    r = await c.post("/api/v1/agents/config-groups", json={"name": "dup", "settings": {}})
    assert r.status_code == 409


async def test_create_invalid_settings_422(client):
    c, _, _ = client
    r = await c.post(
        "/api/v1/agents/config-groups",
        json={"name": "bad", "settings": {"scan_schedule_cron": "nope"}},
    )
    assert r.status_code == 422


async def test_update_group_revalidates_and_audits(client):
    c, maker, _ = client
    gid = (
        await c.post("/api/v1/agents/config-groups", json={"name": "g", "settings": {}})
    ).json()["id"]
    ok = await c.patch(
        f"/api/v1/agents/config-groups/{gid}",
        json={"settings": {"log_level": "warn"}, "description": "d"},
    )
    assert ok.status_code == 200
    assert ok.json()["settings"] == {"log_level": "warn"}
    bad = await c.patch(
        f"/api/v1/agents/config-groups/{gid}", json={"settings": {"log_level": "loud"}}
    )
    assert bad.status_code == 422
    assert await _events(maker, "agent_config_group_updated")


async def test_delete_group_sets_members_null(client):
    c, maker, _ = client
    gid = (
        await c.post("/api/v1/agents/config-groups", json={"name": "g", "settings": {}})
    ).json()["id"]
    agent_id, _ = await _seed_agent(maker, config_group_id=uuid.UUID(gid))

    # member_count reflects the assigned agent
    got = await c.get(f"/api/v1/agents/config-groups/{gid}")
    assert got.json()["member_count"] == 1

    d = await c.delete(f"/api/v1/agents/config-groups/{gid}")
    assert d.status_code == 204
    # the FK SET NULL fell the member back to defaults
    async with maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.config_group_id is None
    ev = await _events(maker, "agent_config_group_deleted")
    assert any(e["members_reset"] == 1 for e in ev)


# --------------------------------------------------------------------------- #
# Assignment + SET NULL                                                         #
# --------------------------------------------------------------------------- #
async def test_assign_and_clear_group(client):
    c, maker, _ = client
    gid = (
        await c.post("/api/v1/agents/config-groups", json={"name": "g", "settings": {}})
    ).json()["id"]
    agent_id, _ = await _seed_agent(maker)

    a = await c.put(
        f"/api/v1/agents/{agent_id}/config-group", json={"config_group_id": gid}
    )
    assert a.status_code == 200 and a.json()["id"] == gid
    async with maker() as s:
        assert str((await s.get(Agent, agent_id)).config_group_id) == gid

    clear = await c.put(
        f"/api/v1/agents/{agent_id}/config-group", json={"config_group_id": None}
    )
    assert clear.status_code == 200 and clear.json() is None
    async with maker() as s:
        assert (await s.get(Agent, agent_id)).config_group_id is None
    assert await _events(maker, "agent_config_group_assigned")


async def test_assign_unknown_group_404(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    r = await c.put(
        f"/api/v1/agents/{agent_id}/config-group",
        json={"config_group_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Policy-channel delivery (merge / ETag / precedence / NULL)                     #
# --------------------------------------------------------------------------- #
async def test_null_group_no_section_and_plain_etag(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    r = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert r.json()["policy"] == {}
    assert "group" not in r.json()["policy"]
    assert r.headers["etag"] == '"none/0"'  # unchanged pre-W6 form


async def test_group_settings_appear_under_group(client):
    c, maker, _ = client
    gid = (
        await c.post(
            "/api/v1/agents/config-groups",
            json={"name": "g", "settings": {"log_level": "verbose"}},
        )
    ).json()["id"]
    agent_id, fp = await _seed_agent(maker, config_group_id=uuid.UUID(gid))
    r = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert r.status_code == 200
    assert r.json()["policy"]["group"] == {"log_level": "verbose"}
    # ETag now carries the group tag
    assert r.headers["etag"].startswith('"none/0/g:')


async def test_group_edit_changes_etag(client):
    c, maker, _ = client
    gid = (
        await c.post(
            "/api/v1/agents/config-groups",
            json={"name": "g", "settings": {"log_level": "info"}},
        )
    ).json()["id"]
    agent_id, fp = await _seed_agent(maker, config_group_id=uuid.UUID(gid))
    first = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    etag1 = first.headers["etag"]
    # a matching If-None-Match is a 304 before the edit
    again = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag1},
    )
    assert again.status_code == 304
    # edit the group -> updated_at bumps -> ETag changes -> old validator no longer 304s
    await c.patch(
        f"/api/v1/agents/config-groups/{gid}", json={"settings": {"log_level": "debug"}}
    )
    after = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag1},
    )
    assert after.status_code == 200
    assert after.headers["etag"] != etag1
    assert after.json()["policy"]["group"] == {"log_level": "debug"}


async def test_per_agent_policy_key_precedence(client):
    """An explicit per-agent policy ``group`` key WINS over the config group."""
    c, maker, _ = client
    gid = (
        await c.post(
            "/api/v1/agents/config-groups",
            json={"name": "g", "settings": {"log_level": "info"}},
        )
    ).json()["id"]
    agent_id, fp = await _seed_agent(maker, config_group_id=uuid.UUID(gid))
    # operator authors an explicit top-level `group` key on the agent-scope policy
    await c.put(
        f"/api/v1/agent-policies/agent:{agent_id}",
        json={"policy": {"group": {"explicit": True}}},
    )
    r = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert r.json()["policy"]["group"] == {"explicit": True}  # not clobbered


# --------------------------------------------------------------------------- #
# Installer-config                                                              #
# --------------------------------------------------------------------------- #
async def test_installer_config_frozen_shape(client):
    c, maker, _ = client
    gid = (
        await c.post("/api/v1/agents/config-groups", json={"name": "wg", "settings": {}})
    ).json()["id"]
    r = await c.post(
        "/api/v1/agents/installer-config",
        json={
            "agent_name": "lab-01",
            "config_group_id": gid,
            "log_level": "info",
            "central_url_override": "https://filearr.example.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # frozen contract shape
    assert set(body) == {"sidecar", "token_hash", "expires_at", "install_hint"}
    sc = body["sidecar"]
    assert sc["central_url"] == "https://filearr.example.com"
    assert sc["enrollment_token"]  # raw token present (show-once)
    assert sc["agent_name"] == "lab-01"
    assert sc["config_group"] == "wg"
    assert sc["log_level"] == "info"
    assert set(body["install_hint"]) == {"windows", "linux", "macos"}
    # install_hint references the P5-T7 release-artifact download path + install cmd
    for os_hint in body["install_hint"].values():
        assert "/releases/" in os_hint and "/artifacts" in os_hint
        assert "install --config filearr-agent.json" in os_hint
    # token was actually minted + persisted (by hash)
    async with maker() as s:
        from filearr.models import EnrollmentToken

        assert await s.get(EnrollmentToken, body["token_hash"]) is not None
    # audited by token hash + config group, never the raw token
    ev = await _events(maker, "agent_installer_config_issued")
    assert ev and ev[0]["config_group"] == "wg"
    assert all("enrollment_token" not in str(e) for e in ev)


async def test_installer_config_base_url_default(client):
    c, _, _ = client
    r = await c.post("/api/v1/agents/installer-config", json={})
    assert r.status_code == 201
    assert r.json()["sidecar"]["central_url"].startswith("http://t")
    assert r.json()["sidecar"]["config_group"] is None


async def test_installer_config_bad_group_and_log_level_422(client):
    c, _, _ = client
    r1 = await c.post(
        "/api/v1/agents/installer-config",
        json={"config_group_id": str(uuid.uuid4())},
    )
    assert r1.status_code == 422
    r2 = await c.post("/api/v1/agents/installer-config", json={"log_level": "loud"})
    assert r2.status_code == 422


async def test_installer_config_admin_gated(client):
    """With auth ENABLED and no admin credential the endpoint is refused (the
    ``admin`` scope dependency runs before any work)."""
    c, _, settings = client
    settings.auth_enabled = True  # same cached Settings object require_scope reads
    try:
        r = await c.post("/api/v1/agents/installer-config", json={})
        assert r.status_code in (401, 403)
    finally:
        settings.auth_enabled = False


# --------------------------------------------------------------------------- #
# Register with a config_group name (resolved / unknown-name warning)           #
# --------------------------------------------------------------------------- #
async def test_register_resolves_config_group_by_name(client):
    c, maker, _ = client
    await c.post("/api/v1/agents/config-groups", json={"name": "fleet-a", "settings": {}})
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    reg = await c.post(
        "/api/v1/agents/register",
        json={
            "token": raw,
            "hostname": "h",
            "platform": "linux",
            "config_group": "fleet-a",
        },
    )
    assert reg.status_code == 201, reg.text
    assert reg.json()["config_group_warning"] is None
    async with maker() as s:
        a = await s.get(Agent, uuid.UUID(reg.json()["agent_id"]))
        grp = (
            await s.execute(
                text("SELECT name FROM agent_config_groups WHERE id = :i"),
                {"i": a.config_group_id},
            )
        ).scalar_one()
        assert grp == "fleet-a"


async def test_register_unknown_config_group_warns_not_blocks(client):
    c, maker, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    reg = await c.post(
        "/api/v1/agents/register",
        json={
            "token": raw,
            "hostname": "h",
            "platform": "linux",
            "config_group": "ghost",
        },
    )
    assert reg.status_code == 201  # never blocks enrollment
    assert "ghost" in reg.json()["config_group_warning"]
    async with maker() as s:
        a = await s.get(Agent, uuid.UUID(reg.json()["agent_id"]))
        assert a.config_group_id is None  # fell back to defaults
