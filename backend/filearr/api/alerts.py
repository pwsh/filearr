"""P8-T2 — admin CRUD for alert channels + rules, with secret-safe handling.

Two resource groups, both **admin-scope** (this is the security-sensitive
surface: outbound webhooks + stored credentials):

* ``/alert-channels`` — notification destinations. Secret sub-fields of a
  channel ``config`` (webhook HMAC ``secret``, SMTP ``password``, and — for an
  apprise channel — the whole ``url``) are AES-GCM encrypted at rest (P8-T4) and
  are **write-only**: a GET/list NEVER returns a plaintext secret (it shows a
  ``"__redacted__"`` marker), and a PATCH keeps the stored ciphertext unless the
  client sends a real new value (the ``"__unchanged__"`` sentinel or an absent
  field = keep). Encryption requires ``FILEARR_SECRET_KEY``; when it is unset the
  write/test endpoints return **503** with generation guidance (never a plaintext
  fallback). A per-channel ``POST /{id}/test`` fires a sample alert through the
  real driver.
* ``/alert-rules`` — file-watch (+ ``is_system``) rules. Validated on write:
  ``event_types`` against the fixed vocabulary, ``path_glob`` compiled through the
  same ``pathspec`` engine the scan uses, ``digest_window`` against its enum,
  ``library_id`` and every referenced ``channel_id`` must exist. ``group_by`` is
  NOT client-settable (fixed to the R1 vocabulary). Rule EVALUATION in scans is
  P8-T5, not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.alerts import crypto, render, webhook_formats
from filearr.alerts.dispatch import (
    ChannelDeliveryError,
    send_email,
    send_webhook_formatted,
)
from filearr.alerts.rules import DIGEST_WINDOWS, EVENT_TYPES, _compile_glob
from filearr.config import get_settings
from filearr.db import get_session
from filearr.errors import sanitize_error
from filearr.models import (
    AlertChannel,
    AlertEvent,
    AlertRule,
    AlertRuleChannel,
    Library,
)
from filearr.security import require_scope

router = APIRouter()

CHANNEL_TYPES = frozenset({"webhook", "email", "apprise"})
DISPATCH_LOCALITIES = frozenset({"central", "agent"})

# Which config sub-fields are secret per channel type. For apprise the WHOLE url
# is the secret (it embeds tokens inline, brief §7.2).
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "webhook": ("secret",),
    "email": ("password",),
    "apprise": ("url",),
}

REDACTED = "__redacted__"      # shown in place of any stored secret on read
UNCHANGED = "__unchanged__"    # client sentinel on edit = keep existing ciphertext
EVENT_STATUSES = frozenset({"delivered", "failed", "pending"})  # derived event status


class AlertChannelIn(BaseModel):
    name: str
    type: str
    config: dict = Field(default_factory=dict)
    dispatch_locality: str = "central"
    enabled: bool = True


class AlertChannelUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    dispatch_locality: str | None = None
    enabled: bool | None = None


class AlertChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    type: str
    config: dict
    dispatch_locality: str
    enabled: bool
    created_at: datetime


class AlertRuleIn(BaseModel):
    name: str
    enabled: bool = True
    is_system: bool = False
    library_id: uuid.UUID | None = None
    path_glob: str | None = None
    event_types: list[str]
    hash_change_only: bool = False
    group_wait_s: int = Field(default=30, ge=0)
    digest_window: str | None = None
    repeat_interval_s: int | None = Field(default=None, ge=0)
    threshold_count: int | None = Field(default=None, ge=0)
    threshold_window_s: int | None = Field(default=None, ge=0)
    channel_ids: list[uuid.UUID] = Field(default_factory=list)


class AlertRuleUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    library_id: uuid.UUID | None = None
    path_glob: str | None = None
    event_types: list[str] | None = None
    hash_change_only: bool | None = None
    group_wait_s: int | None = Field(default=None, ge=0)
    digest_window: str | None = None
    repeat_interval_s: int | None = Field(default=None, ge=0)
    threshold_count: int | None = Field(default=None, ge=0)
    threshold_window_s: int | None = Field(default=None, ge=0)
    channel_ids: list[uuid.UUID] | None = None


class AlertRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    enabled: bool
    is_system: bool
    library_id: uuid.UUID | None
    path_glob: str | None
    event_types: list[str]
    hash_change_only: bool
    group_by: list[str]
    group_wait_s: int
    digest_window: str | None
    repeat_interval_s: int | None
    threshold_count: int | None
    threshold_window_s: int | None
    channel_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime


class TestFireResult(BaseModel):
    ok: bool
    detail: str = ""
    status_code: int | None = None
    retryable: bool = False


def _redact_config(type_: str, config: dict) -> dict:
    """Return a copy of ``config`` with secret sub-fields masked for read."""
    masked = dict(config or {})
    for f in SECRET_FIELDS.get(type_, ()):
        if masked.get(f):
            masked[f] = REDACTED
    return masked


def _encrypt_config_secrets(
    type_: str, config: dict, key: bytes, *, prev: dict | None = None
) -> dict:
    """Return ``config`` with its secret sub-fields encrypted.

    A secret value equal to the ``UNCHANGED``/``REDACTED`` sentinel (or absent on
    an edit) keeps the previously stored ciphertext. A real new value is
    encrypted. This is what keeps a decrypted secret from ever round-tripping."""
    out = dict(config or {})
    prev = prev or {}
    for f in SECRET_FIELDS.get(type_, ()):
        incoming = out.get(f)
        if incoming in (None, "", UNCHANGED, REDACTED):
            if prev.get(f):
                out[f] = prev[f]  # keep existing ciphertext
            else:
                out.pop(f, None)
        else:
            out[f] = crypto.encrypt_secret(str(incoming), key)
    return out


def _decrypt_config_secrets(type_: str, config: dict, key: bytes) -> dict:
    out = dict(config or {})
    for f in SECRET_FIELDS.get(type_, ()):
        if out.get(f):
            out[f] = crypto.decrypt_secret(out[f], key)
    return out


def _channel_out(row: AlertChannel) -> AlertChannelOut:
    return AlertChannelOut(
        id=row.id,
        name=row.name,
        type=row.type_,
        config=_redact_config(row.type_, row.config),
        dispatch_locality=row.dispatch_locality,
        enabled=row.enabled,
        created_at=row.created_at,
    )


def _require_key() -> bytes:
    try:
        return crypto.require_content_key()
    except crypto.SecretKeyMissing as exc:
        raise HTTPException(503, str(exc)) from exc


def _validate_glob(path_glob: str | None) -> None:
    if not path_glob:
        return
    try:
        _compile_glob(path_glob)
    except Exception as exc:  # noqa: BLE001 - any compile failure is a 422
        raise HTTPException(422, f"invalid path_glob: {exc}") from exc


def _validate_event_types(event_types: list[str]) -> None:
    if not event_types:
        raise HTTPException(422, "event_types must be non-empty")
    bad = sorted(set(event_types) - EVENT_TYPES)
    if bad:
        raise HTTPException(
            422, f"unknown event_types {bad}; allowed: {sorted(EVENT_TYPES)}"
        )


def _validate_webhook_format(type_: str, config: dict | None) -> None:
    """FIX-16: a webhook channel may pin a payload ``webhook_format``.

    Absent/empty = ``generic`` (back-compat). Only ``webhook`` channels carry it;
    an unknown value is a 422 (never silently coerced)."""
    if type_ != "webhook" or not config:
        return
    fmt = config.get("webhook_format")
    if fmt in (None, ""):
        return
    if fmt not in webhook_formats.WEBHOOK_FORMATS:
        raise HTTPException(
            422,
            f"invalid webhook_format {fmt!r}; one of "
            f"{sorted(webhook_formats.WEBHOOK_FORMATS)}",
        )


def _validate_digest_window(digest_window: str | None) -> None:
    if digest_window is not None and digest_window not in DIGEST_WINDOWS:
        raise HTTPException(
            422,
            f"unknown digest_window {digest_window!r}; allowed: {sorted(DIGEST_WINDOWS)}",
        )


async def _validate_library(session: AsyncSession, library_id: uuid.UUID | None) -> None:
    if library_id is None:
        return
    exists = (
        await session.execute(select(Library.id).where(Library.id == library_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(422, f"library {library_id} does not exist")


async def _validate_channels(
    session: AsyncSession, channel_ids: list[uuid.UUID]
) -> None:
    if not channel_ids:
        return
    found = set(
        (
            await session.execute(
                select(AlertChannel.id).where(AlertChannel.id.in_(channel_ids))
            )
        )
        .scalars()
        .all()
    )
    missing = [str(c) for c in channel_ids if c not in found]
    if missing:
        raise HTTPException(422, f"unknown channel_ids: {missing}")


async def _rule_channel_ids(
    session: AsyncSession, rule_id: uuid.UUID
) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(AlertRuleChannel.channel_id).where(
                    AlertRuleChannel.rule_id == rule_id
                )
            )
        )
        .scalars()
        .all()
    )


async def _rule_out(session: AsyncSession, row: AlertRule) -> AlertRuleOut:
    out = AlertRuleOut.model_validate(row)
    out.channel_ids = await _rule_channel_ids(session, row.id)
    return out


async def _set_rule_channels(
    session: AsyncSession, rule_id: uuid.UUID, channel_ids: list[uuid.UUID]
) -> None:
    await session.execute(
        sa_delete(AlertRuleChannel).where(AlertRuleChannel.rule_id == rule_id)
    )
    for cid in dict.fromkeys(channel_ids):  # de-dup, preserve order
        session.add(AlertRuleChannel(rule_id=rule_id, channel_id=cid))


@router.get(
    "/alert-channels",
    response_model=list[AlertChannelOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_channels(session: AsyncSession = Depends(get_session)):
    rows = (
        (await session.execute(select(AlertChannel).order_by(AlertChannel.name)))
        .scalars()
        .all()
    )
    return [_channel_out(r) for r in rows]


@router.post(
    "/alert-channels",
    response_model=AlertChannelOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_channel(
    body: AlertChannelIn, session: AsyncSession = Depends(get_session)
):
    if body.type not in CHANNEL_TYPES:
        raise HTTPException(
            422, f"invalid type {body.type!r}; one of {sorted(CHANNEL_TYPES)}"
        )
    if body.dispatch_locality not in DISPATCH_LOCALITIES:
        raise HTTPException(
            422,
            f"invalid dispatch_locality {body.dispatch_locality!r}; "
            f"one of {sorted(DISPATCH_LOCALITIES)}",
        )
    _validate_webhook_format(body.type, body.config)
    key = _require_key()
    stored_config = _encrypt_config_secrets(body.type, body.config, key)
    row = AlertChannel(
        name=body.name,
        type_=body.type,
        config=stored_config,
        dispatch_locality=body.dispatch_locality,
        enabled=body.enabled,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, f"a channel named {body.name!r} already exists") from exc
    await session.refresh(row)
    return _channel_out(row)


@router.get(
    "/alert-channels/{channel_id}",
    response_model=AlertChannelOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def get_channel(
    channel_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = (
        await session.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "channel not found")
    return _channel_out(row)


@router.patch(
    "/alert-channels/{channel_id}",
    response_model=AlertChannelOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_channel(
    channel_id: uuid.UUID,
    body: AlertChannelUpdate,
    session: AsyncSession = Depends(get_session),
):
    row = (
        await session.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "channel not found")
    sent = body.model_fields_set
    if "dispatch_locality" in sent and body.dispatch_locality not in DISPATCH_LOCALITIES:
        raise HTTPException(422, f"invalid dispatch_locality {body.dispatch_locality!r}")
    if "config" in sent and body.config is not None:
        _validate_webhook_format(row.type_, body.config)
        key = _require_key()
        row.config = _encrypt_config_secrets(
            row.type_, body.config, key, prev=row.config
        )
    if "name" in sent and body.name is not None:
        row.name = body.name
    if "dispatch_locality" in sent and body.dispatch_locality is not None:
        row.dispatch_locality = body.dispatch_locality
    if "enabled" in sent and body.enabled is not None:
        row.enabled = body.enabled
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "a channel with that name already exists") from exc
    await session.refresh(row)
    return _channel_out(row)


@router.delete(
    "/alert-channels/{channel_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_channel(
    channel_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = (
        await session.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "channel not found")
    await session.execute(sa_delete(AlertChannel).where(AlertChannel.id == channel_id))
    await session.commit()


@router.post(
    "/alert-channels/{channel_id}/test",
    response_model=TestFireResult,
    dependencies=[Depends(require_scope("admin"))],
)
async def test_channel(
    channel_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Fire a sample alert through the real driver. Input/config errors are 4xx/
    503; a *delivery* failure is reported in the 200 body (ok=false) so a UI test
    button shows the reason without a 500."""
    row = (
        await session.execute(select(AlertChannel).where(AlertChannel.id == channel_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "channel not found")
    key = _require_key()
    settings = get_settings()
    try:
        runtime = _decrypt_config_secrets(row.type_, row.config, key)
    except crypto.SecretDecryptError as exc:
        raise HTTPException(503, f"cannot decrypt channel secret: {exc}") from exc
    rendered = render.render_test(row.name)
    try:
        if row.type_ == "webhook":
            url = runtime.get("url")
            if not url:
                raise HTTPException(422, "webhook channel missing 'url'")
            result = await send_webhook_formatted(
                url,
                rendered,
                config=runtime,  # FIX-16: test-fire uses the channel's webhook_format
                secret=runtime.get("secret"),
                allow_private=settings.webhook_allow_private_cidrs,
                timeout_s=settings.alert_webhook_timeout_s,
                max_response_bytes=settings.alert_webhook_max_response_bytes,
            )
        elif row.type_ == "email":
            result = await send_email(runtime, rendered)
        else:  # apprise — P8-T3
            raise HTTPException(
                501, "apprise dispatch not available; install filearr[apprise] (P8-T3)"
            )
    except ChannelDeliveryError as exc:
        return TestFireResult(
            ok=False,
            detail=exc.detail,
            status_code=exc.status_code,
            retryable=exc.retryable,
        )
    return TestFireResult(
        ok=result.ok, detail=result.detail, status_code=result.status_code
    )


@router.get(
    "/alert-rules",
    response_model=list[AlertRuleOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_rules(session: AsyncSession = Depends(get_session)):
    rows = (
        (await session.execute(select(AlertRule).order_by(AlertRule.name)))
        .scalars()
        .all()
    )
    return [await _rule_out(session, r) for r in rows]


@router.post(
    "/alert-rules",
    response_model=AlertRuleOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_rule(body: AlertRuleIn, session: AsyncSession = Depends(get_session)):
    _validate_event_types(body.event_types)
    _validate_glob(body.path_glob)
    _validate_digest_window(body.digest_window)
    await _validate_library(session, body.library_id)
    await _validate_channels(session, body.channel_ids)
    row = AlertRule(
        name=body.name,
        enabled=body.enabled,
        is_system=body.is_system,
        library_id=body.library_id,
        path_glob=body.path_glob or None,
        event_types=list(body.event_types),
        hash_change_only=body.hash_change_only,
        group_by=["event_type", "library_id", "rule_id"],  # R1 fixed
        group_wait_s=body.group_wait_s,
        digest_window=body.digest_window,
        repeat_interval_s=body.repeat_interval_s,
        threshold_count=body.threshold_count,
        threshold_window_s=body.threshold_window_s,
    )
    session.add(row)
    await session.flush()
    await _set_rule_channels(session, row.id, body.channel_ids)
    await session.commit()
    await session.refresh(row)
    return await _rule_out(session, row)


@router.get(
    "/alert-rules/{rule_id}",
    response_model=AlertRuleOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def get_rule(rule_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(select(AlertRule).where(AlertRule.id == rule_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "rule not found")
    return await _rule_out(session, row)


@router.patch(
    "/alert-rules/{rule_id}",
    response_model=AlertRuleOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_rule(
    rule_id: uuid.UUID,
    body: AlertRuleUpdate,
    session: AsyncSession = Depends(get_session),
):
    row = (
        await session.execute(select(AlertRule).where(AlertRule.id == rule_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "rule not found")
    sent = body.model_fields_set
    if "event_types" in sent and body.event_types is not None:
        _validate_event_types(body.event_types)
        row.event_types = list(body.event_types)
    if "path_glob" in sent:
        _validate_glob(body.path_glob)
        row.path_glob = body.path_glob or None
    if "digest_window" in sent:
        _validate_digest_window(body.digest_window)
        row.digest_window = body.digest_window
    if "library_id" in sent:
        await _validate_library(session, body.library_id)
        row.library_id = body.library_id
    for key in (
        "name",
        "enabled",
        "hash_change_only",
        "group_wait_s",
        "repeat_interval_s",
        "threshold_count",
        "threshold_window_s",
    ):
        if key in sent:
            setattr(row, key, getattr(body, key))
    if "channel_ids" in sent and body.channel_ids is not None:
        await _validate_channels(session, body.channel_ids)
        await _set_rule_channels(session, row.id, body.channel_ids)
    await session.commit()
    await session.refresh(row)
    return await _rule_out(session, row)


@router.delete(
    "/alert-rules/{rule_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_rule(rule_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(select(AlertRule).where(AlertRule.id == rule_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "rule not found")
    await session.execute(sa_delete(AlertEvent).where(AlertEvent.rule_id == rule_id))
    await session.execute(sa_delete(AlertRule).where(AlertRule.id == rule_id))
    await session.commit()


# --------------------------------------------------------------------------- #
# P8-T13/T15 — recent alert-events (read-only observability of the pump)       #
# --------------------------------------------------------------------------- #
# "My alert channel has been broken for three days" must be discoverable. This
# read-scope listing mirrors the failed-item surfacing (errors.py / T11): each
# row's derived delivery ``status`` (delivered / failed / pending) and its
# sanitized ``last_error`` (which also carries a P8-T15 ceiling-suppression note)
# make a permanently-failed or held dispatch visible without leaking internals.


class AlertEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    rule_id: uuid.UUID
    item_id: uuid.UUID | None
    library_id: uuid.UUID | None
    event_type: str
    dedup_key: str
    status: str  # delivered | failed | pending
    delivered: bool
    delivered_at: datetime | None
    delivery_attempts: int
    occurred_at: datetime
    last_error: str | None


def _event_status(row: AlertEvent, max_attempts: int) -> str:
    if row.delivered:
        return "delivered"
    if row.delivery_attempts >= max_attempts:
        return "failed"
    return "pending"


def _event_out(row: AlertEvent, max_attempts: int) -> AlertEventOut:
    return AlertEventOut(
        id=row.id,
        rule_id=row.rule_id,
        item_id=row.item_id,
        library_id=row.library_id,
        event_type=row.event_type,
        dedup_key=row.dedup_key,
        status=_event_status(row, max_attempts),
        delivered=row.delivered,
        delivered_at=row.delivered_at,
        delivery_attempts=row.delivery_attempts,
        occurred_at=row.occurred_at,
        last_error=sanitize_error(row.last_error) if row.last_error else None,
    )


@router.get(
    "/alert-events",
    response_model=list[AlertEventOut],
    dependencies=[Depends(require_scope("read"))],
)
async def list_alert_events(
    rule_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    delivered: bool | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Recent alert-events, newest first. ``limit`` is capped at 200 (P8-T13).

    Filterable by ``rule_id``, ``library_id`` and the derived ``status``
    (``delivered`` / ``failed`` / ``pending``) -- ``failed`` = retries exhausted,
    ``pending`` = still deliverable (includes ceiling-held). The legacy
    ``delivered`` boolean is kept for back-compat."""
    max_attempts = get_settings().alert_max_delivery_attempts
    if status is not None and status not in EVENT_STATUSES:
        raise HTTPException(
            422, f"unknown status {status!r}; one of {sorted(EVENT_STATUSES)}"
        )
    stmt = select(AlertEvent).order_by(AlertEvent.occurred_at.desc()).limit(limit)
    if rule_id is not None:
        stmt = stmt.where(AlertEvent.rule_id == rule_id)
    if library_id is not None:
        stmt = stmt.where(AlertEvent.library_id == library_id)
    if delivered is not None:
        stmt = stmt.where(AlertEvent.delivered.is_(delivered))
    if status == "delivered":
        stmt = stmt.where(AlertEvent.delivered.is_(True))
    elif status == "failed":
        stmt = stmt.where(
            AlertEvent.delivered.is_(False),
            AlertEvent.delivery_attempts >= max_attempts,
        )
    elif status == "pending":
        stmt = stmt.where(
            AlertEvent.delivered.is_(False),
            AlertEvent.delivery_attempts < max_attempts,
        )
    rows = (await session.execute(stmt)).scalars().all()
    return [_event_out(r, max_attempts) for r in rows]


class AlertEventSummary(BaseModel):
    delivered: int
    failed: int
    pending: int


@router.get(
    "/alert-events/summary",
    response_model=AlertEventSummary,
    dependencies=[Depends(require_scope("read"))],
)
async def alert_events_summary(
    library_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Derived delivery-state counts (P8-T13 failed-delivery banner). Optionally
    scoped to one library. ``failed`` powers the "channel broken for days" banner;
    ``pending`` includes ceiling-held rows."""
    max_attempts = get_settings().alert_max_delivery_attempts

    def _count():
        q = select(func.count()).select_from(AlertEvent)
        if library_id is not None:
            q = q.where(AlertEvent.library_id == library_id)
        return q

    delivered = (
        await session.execute(_count().where(AlertEvent.delivered.is_(True)))
    ).scalar_one()
    failed = (
        await session.execute(
            _count().where(
                AlertEvent.delivered.is_(False),
                AlertEvent.delivery_attempts >= max_attempts,
            )
        )
    ).scalar_one()
    pending = (
        await session.execute(
            _count().where(
                AlertEvent.delivered.is_(False),
                AlertEvent.delivery_attempts < max_attempts,
            )
        )
    ).scalar_one()
    return AlertEventSummary(
        delivered=int(delivered), failed=int(failed), pending=int(pending)
    )
