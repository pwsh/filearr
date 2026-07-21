"""P10-T5 — integrity verification on staging completion.

When the final byte of a ``stage_upload`` commits, central RE-READS the staged
file from disk in one streaming pass, computes the xxh3 content + quick hashes,
and checks them against the catalog BEFORE the row is ever marked downloadable:

* matching bytes → ``state=staged``, ``verified=True`` (same transaction);
* a post-scan file change (catalog hash disagrees with the uploaded bytes) →
  ``state=failed``, ``verified=False``, the staged file DELETED, a self-correcting
  ``rehash_check`` enqueued (deduped), and an audit row with expected-vs-computed;
* ``content_hash`` is preferred over ``quick_hash`` when both are present;
* an item with NEITHER hash fails closed (``no_catalog_hash``) — integrity over
  availability.

The P10-T4 wire behaviour is unchanged: a stage_upload WITHOUT a ``verify`` flag
stages with ``verified=False`` exactly as before.

Harness mirrors test_agent_staging.py (migrated pgserver + httpx ASGI app +
interim agent-bearer auth).
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import transfers
from filearr.api import agent_staging
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import (
    Agent,
    AgentCommand,
    Item,
    Library,
    SecurityEvent,
    StagingTransfer,
)
from filearr.tasks.extract import full_hash, quick_hash

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def _hashes(data: bytes) -> tuple[str, str]:
    """(content_hash, quick_hash) of ``data`` via the exact extract contract."""
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(data)
        name = fh.name
    try:
        return full_hash(name), quick_hash(name, len(data))
    finally:
        Path(name).unlink(missing_ok=True)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM staging_transfers"))
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(
    maker,
    *,
    content_hash: str | None,
    quick_hash_val: str | None,
    size: int,
    verify: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """Seed an agent-owned library + item (with the given catalog hashes/size)
    and a picked-up stage_upload command. Returns
    (agent_id, item_id, command_id, fingerprint)."""
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        s.add(agent)
        await s.flush()
        lib = Library(
            name="lib-" + uuid.uuid4().hex[:8],
            root_path="/agentroot",
            source_agent_id=agent.id,
            agent_library_ref="/agentroot",
        )
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="video", file_group="video",
            path="/agentroot/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=size,
            mtime=datetime.now(UTC),
            content_hash=content_hash,
            quick_hash=quick_hash_val,
        )
        s.add(item)
        await s.flush()
        now = datetime.now(UTC)
        payload = {"library_ref": "/agentroot", "rel_path": "x.mkv"}
        if verify:
            payload["verify"] = True
        cmd = AgentCommand(
            agent_id=agent.id,
            kind="stage_upload",
            item_id=item.id,
            payload=payload,
            status="picked_up",
            created_at=now,
            updated_at=now,
            picked_up_at=now,
            expires_at=now + timedelta(hours=1),
        )
        s.add(cmd)
        await s.commit()
        return agent.id, item.id, cmd.id, fp


@pytest.fixture
async def client(db_maker, monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "staging_dir", str(tmp_path / "staging"))
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


def _auth(fp: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {fp}"}


async def _upload(c, agent_id, command_id, fp, data: bytes) -> str:
    r = await c.post(
        f"/api/v1/agents/{agent_id}/staging",
        json={"command_id": str(command_id), "total_bytes": len(data)},
        headers=_auth(fp),
    )
    assert r.status_code in (200, 201), r.text
    tid = r.json()["id"]
    r = await c.patch(
        f"/api/v1/agents/{agent_id}/staging/{tid}",
        content=data,
        headers={**_auth(fp), "Upload-Offset": "0"},
    )
    return tid, r


def _staged_file(settings, tid: str) -> Path:
    return Path(settings.staging_dir) / f"{uuid.UUID(tid)}.bin"


# --------------------------------------------------------------------------- #
# Match path                                                                    #
# --------------------------------------------------------------------------- #
async def test_matching_bytes_verify_true(client):
    c, maker, settings = client
    data = b"the real bytes" * 1000
    ch, qh = _hashes(data)
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=ch, quick_hash_val=qh, size=len(data)
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "staged"
    assert body["verified"] is True
    # verified_hash stamped with the computed content hash.
    async with maker() as s:
        t = await s.get(StagingTransfer, uuid.UUID(tid))
        assert t.verified is True
        assert t.verified_hash == ch
    # File is intact on disk (it will be served).
    assert _staged_file(settings, tid).read_bytes() == data


async def test_quick_hash_used_when_no_content(client):
    c, maker, settings = client
    data = b"q" * 200_000  # > 128 KiB so quick uses head+tail
    ch, qh = _hashes(data)
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=None, quick_hash_val=qh, size=len(data)
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.json()["state"] == "staged" and r.json()["verified"] is True


async def test_legacy_16hex_content_hash_verifies(client):
    """QH-T3 migration window: a catalog row hashed pre-xxh3-128 stores a 16-hex
    xxh3-64 content_hash. Verify must dispatch on the stored length and compare
    the legacy digest — a legitimate upload of a not-yet-rehashed item passes."""
    import xxhash

    c, maker, settings = client
    data = b"legacy-hashed bytes" * 500
    legacy_ch = xxhash.xxh3_64(data).hexdigest()  # the pre-QH-T3 full-file digest
    assert len(legacy_ch) == 16
    _, qh = _hashes(data)
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=legacy_ch, quick_hash_val=qh, size=len(data)
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "staged" and r.json()["verified"] is True
    async with maker() as s:
        t = await s.get(StagingTransfer, uuid.UUID(tid))
        # verified_hash records the CURRENT-algorithm (32-hex) digest even when
        # the comparison ran against the legacy value.
        assert t.verified is True and len(t.verified_hash) == 32


async def test_legacy_16hex_content_hash_still_catches_mismatch(client):
    """The legacy branch stays a real byte check: wrong bytes against a 16-hex
    stored content_hash fail verification."""
    import xxhash

    c, maker, settings = client
    stale_legacy = xxhash.xxh3_64(b"the ORIGINAL bytes").hexdigest()
    data = b"CHANGED bytes since the scan" * 300
    _, qh = _hashes(data)
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=stale_legacy, quick_hash_val=qh, size=len(data)
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "failed" and body["verified"] is False


# --------------------------------------------------------------------------- #
# Mismatch / fail-closed paths                                                  #
# --------------------------------------------------------------------------- #
async def test_post_scan_change_fails_and_cleans_up(client):
    c, maker, settings = client
    data = b"uploaded bytes" * 500
    # Catalog hash reflects a DIFFERENT (pre-change) file version.
    stale_ch, stale_qh = _hashes(b"the ORIGINAL bytes" * 500)
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=stale_ch, quick_hash_val=stale_qh, size=len(data)
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "failed"
    assert r.json()["verified"] is False
    # Staged bytes deleted (never served).
    assert not _staged_file(settings, tid).exists()
    async with maker() as s:
        # A self-correcting rehash_check was enqueued for the item.
        cmds = (
            await s.execute(
                select(AgentCommand).where(
                    AgentCommand.item_id == item_id,
                    AgentCommand.kind == "rehash_check",
                )
            )
        ).scalars().all()
        assert len(cmds) == 1
        assert cmds[0].status == "pending"
        assert cmds[0].payload["content"] is True
        assert cmds[0].payload["library_ref"] == "/agentroot"
        # An audit row records expected-vs-computed.
        evs = (
            await s.execute(
                select(SecurityEvent).where(
                    SecurityEvent.event_type == "agent_transfer_verify_failed"
                )
            )
        ).scalars().all()
        assert len(evs) == 1
        d = evs[0].details
        assert d["reason"] == "content_hash_mismatch"
        assert d["expected"] == stale_ch
        assert d["computed"] != stale_ch
        assert d["rehash_enqueued"] is True


async def test_size_mismatch_fails(client):
    c, maker, settings = client
    data = b"z" * 100
    ch, qh = _hashes(data)
    # Catalog size says 999 but the uploaded/declared size is 100.
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=ch, quick_hash_val=qh, size=100
    )
    async with maker() as s:
        it = await s.get(Item, item_id)
        it.size = 999
        await s.commit()
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.json()["state"] == "failed"
    async with maker() as s:
        ev = (
            await s.execute(
                select(SecurityEvent).where(
                    SecurityEvent.event_type == "agent_transfer_verify_failed"
                )
            )
        ).scalar_one()
        assert ev.details["reason"] == "size_mismatch"


async def test_content_preferred_over_quick(client):
    """content_hash mismatches but quick_hash matches → still FAILS (content wins)."""
    c, maker, settings = client
    data = b"payload bytes here" * 300
    _, qh = _hashes(data)  # correct quick hash for the uploaded bytes
    wrong_ch = "deadbeefdeadbeef"  # deliberately wrong content hash
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=wrong_ch, quick_hash_val=qh, size=len(data)
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.json()["state"] == "failed"
    assert r.json()["verified"] is False


async def test_no_catalog_hash_fail_closed(client):
    c, maker, settings = client
    data = b"n" * 4096
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=None, quick_hash_val=None, size=len(data)
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.json()["state"] == "failed"
    assert r.json()["verified"] is False
    assert not _staged_file(settings, tid).exists()
    async with maker() as s:
        ev = (
            await s.execute(
                select(SecurityEvent).where(
                    SecurityEvent.event_type == "agent_transfer_verify_failed"
                )
            )
        ).scalar_one()
        assert ev.details["reason"] == "no_catalog_hash"


async def test_duplicate_rehash_check_guarded(client):
    c, maker, settings = client
    data = b"a" * 512
    stale_ch, stale_qh = _hashes(b"other" * 512)
    # Two separate transfers/commands for the SAME item, both mismatching.
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=stale_ch, quick_hash_val=stale_qh, size=len(data)
    )
    # Second command for the same item (reuse agent + item).
    async with maker() as s:
        now = datetime.now(UTC)
        cmd2 = AgentCommand(
            agent_id=agent_id,
            kind="stage_upload",
            item_id=item_id,
            payload={"library_ref": "/agentroot", "rel_path": "x.mkv", "verify": True},
            status="picked_up",
            created_at=now,
            updated_at=now,
            picked_up_at=now,
            expires_at=now + timedelta(hours=1),
        )
        s.add(cmd2)
        await s.commit()
        cmd2_id = cmd2.id

    await _upload(c, agent_id, cmd_id, fp, data)
    await _upload(c, agent_id, cmd2_id, fp, data)
    async with maker() as s:
        cmds = (
            await s.execute(
                select(AgentCommand).where(
                    AgentCommand.item_id == item_id,
                    AgentCommand.kind == "rehash_check",
                )
            )
        ).scalars().all()
        # Only ONE rehash_check despite two failed verifications.
        assert len(cmds) == 1


async def test_no_verify_flag_stages_unverified(client):
    """A stage_upload WITHOUT the verify flag keeps the P10-T4 behaviour."""
    c, maker, settings = client
    data = b"whatever" * 100
    stale_ch, stale_qh = _hashes(b"different" * 100)
    agent_id, item_id, cmd_id, fp = await _seed(
        maker, content_hash=stale_ch, quick_hash_val=stale_qh, size=len(data), verify=False
    )
    tid, r = await _upload(c, agent_id, cmd_id, fp, data)
    assert r.json()["state"] == "staged"
    assert r.json()["verified"] is False
    assert _staged_file(settings, tid).read_bytes() == data


# --------------------------------------------------------------------------- #
# Pure helpers: hash identity + state-machine edge                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "size",
    [0, 1, 100, 65_536, 65_537, 131_072, 131_073, 300_000],
)
def test_compute_staged_hashes_identical_to_extract(size, tmp_path):
    data = bytes((i * 7 + 3) % 256 for i in range(size))
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    content, content_legacy, quick, n = agent_staging._compute_staged_hashes(p)
    assert n == size
    assert content == full_hash(str(p))
    assert quick == quick_hash(str(p), size)
    # The migration-window legacy digest is the whole-file xxh3-64.
    import xxhash

    assert content_legacy == xxhash.xxh3_64(data).hexdigest()


def test_state_machine_uploading_to_failed_edge():
    # P10-T5 mismatch path uses the EXISTING uploading--fail-->failed edge; no
    # new edge was required. Assert the legal edges the verify path relies on.
    assert transfers.transfer_state_machine("uploading", "fail") == "failed"
    assert transfers.transfer_state_machine("uploading", "staged") == "staged"
    assert transfers.transfer_state_machine("staged", "fail") == "failed"
    # failed is terminal — no event leaves it.
    with pytest.raises(ValueError):
        transfers.transfer_state_machine("failed", "staged")
