"""Central-side replication logic for the distributed agent architecture (Phase 5,
roadmap §1 / ``docs/research/phase-5-distributed-agents.md``).

**Inert scaffolding.** Nothing in the runtime imports this module yet — only its
tests do. It ships the *pure*, unit-testable core of the agent→central
replication contract (the pieces that need no Postgres, no network, no mTLS) plus
typed ``NotImplementedError`` stubs for the stateful endpoints, each tagged with
the Phase-5 task (``P5-Tk``) that will implement it. Wiring any of this into the
API/worker is P5-T1/T4/T5 — see the tasks doc.

What is *pure and implemented here*:

- :class:`AgentEvent` / :class:`ReplicationBatch` — the on-the-wire event shape
  (brief §7.3 replication-batch body). Per Architect ruling **R1**, an event
  carries only ``rel_path`` / ``size`` / ``mtime`` / ``quick_hash`` /
  ``content_hash`` (filename-derived title stays agent-local). Full extraction
  remains central and post-replication; the local index answers "where is it",
  not "what's in it".
- :func:`check_batch` — the ``seq_no`` gap/continuation guard behind the
  ``409 {expected_seq_no}`` contract (brief §4.2 / §7.3). Server tracks the
  highest contiguous ``seq_no`` per agent (``agents.last_contiguous_seq_no``);
  this decides accept vs. resend-request without any DB access.
- :func:`plan_upserts` — collapses a batch to the minimal upsert/delete set
  (last-event-per-``rel_path`` wins; a ``moved`` event is a delete+create pair;
  deletes ordered after upserts) so :func:`apply_batch` can apply it in one
  transaction.
- :func:`manifest_digest` — canonical hash of a full-reconciliation manifest
  (brief §4.4 full-manifest diff), sorted by ``rel_path`` and stably serialized
  so an agent and the server compute byte-identical digests over the same corpus.

Architect rulings baked in (see the tasks doc for the full list):

- **R1** — local index fidelity is path/size/mtime/hashes/filename-title only.
- **R2** — a late agent tombstone against an already-purged central row is an
  idempotent no-op (central purge always wins); handled in :func:`apply_batch`
  and counted in a reconciliation metric (P5-T4/T5, not modelled here).
- **R3** — enrollment ordering: register (one-time token) → server assigns
  ``agent_id`` → agent CSR embeds it → CA signs. No cert before registration.
  :func:`mint_enrollment_token` / :func:`register_agent` /
  :func:`bind_agent_certificate` implement this (P5-T1). ``apply_batch``
  (P5-T4) and the ``reconcile_*`` sweep (P5-T5) are now implemented too.
- **R4** — no policy-payload signing beyond mTLS in v3 (single-operator trust).
- **R5** — ``agents.rollout_group`` is a text column now; migrates to phase-6
  machine groups later (alias/supersede, never two parallel authorities).

The central-side DDL these models mirror (``agents``,
``agent_replication_log``, ``enrollment_tokens``) is *intended, not created* by
this pass — it is spelled out in the tasks doc so the implementing task writes
the Alembic revision. No ``models.py`` change, no migration lands here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger("filearr.agentsync")

# --- On-the-wire event shape (brief §7.3) ----------------------------------

EventType = Literal["created", "modified", "deleted", "moved"]


class AgentEvent(BaseModel):
    """A single filesystem change an agent reports for one item.

    ``seq_no`` is the agent's durable, per-agent monotonic outbox key (brief
    §4.1/§4.2). ``library_ref`` identifies which of the agent's local libraries
    the path belongs to (opaque to the transport; the server maps it to a
    central ``library_id`` at apply time). ``content_hash`` is nullable because
    the agent may ship a ``quick_hash`` only for large/networked files
    (mirrors the central T7 hash policy). A ``moved`` event carries
    ``from_rel_path`` (the old location) in addition to ``rel_path`` (the new
    location) so the server can pair a delete with a create (brief §4.5).
    """

    model_config = ConfigDict(frozen=True)

    seq_no: int
    event_type: EventType
    library_ref: str
    rel_path: str
    from_rel_path: str | None = None  # set only for event_type == "moved"
    size: int | None = None
    mtime: float | None = None
    quick_hash: str | None = None
    content_hash: str | None = None  # nullable per R1 / T7 hash policy
    # P10-T11: additive best-effort network-share hint the agent attaches to a
    # created/modified event ({share_url, unc, share_name, host, source:"agent"}).
    # Stored verbatim as an opaque dict so the shape stays additive/versionable; a
    # created/modified event may omit it (the normal case) and a delete never
    # carries one. AgentEvent does not forbid extras, so an OLD agent that predates
    # the field is unaffected (unknown-field-absent), and a NEW agent's extra hint
    # keys ride through into JSONB untouched.
    share_hint: dict[str, Any] | None = None


class ReplicationBatch(BaseModel):
    """A contiguous slice of an agent's outbox, POSTed to the replication
    endpoint (brief §7.3). ``entries`` are expected in ascending ``seq_no``
    order; :func:`check_batch` enforces that before any apply."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    entries: list[AgentEvent] = Field(default_factory=list)


# --- seq_no gap / continuation guard (brief §4.2, §7.3) --------------------


class BatchVerdict(BaseModel):
    """Result of :func:`check_batch`.

    On success, ``accepted_from``/``accepted_to`` span the batch. On failure,
    ``reason`` is one of ``empty`` / ``gap`` / ``stale`` / ``duplicate`` /
    ``non_monotonic`` / ``internal_gap`` and ``expected_seq_no`` is the seq the
    agent should resend from — the value the 409 response returns so the agent
    can rewind its outbox drain (brief §7.3 ``resend_from``)."""

    ok: bool
    reason: str | None = None
    expected_seq_no: int | None = None
    accepted_from: int | None = None
    accepted_to: int | None = None


def check_batch(batch: ReplicationBatch, last_seq: int) -> BatchVerdict:
    """Validate a batch's ``seq_no`` sequence against the server's last
    contiguous seq for this agent (``agents.last_contiguous_seq_no``).

    Accepts *only* an exact contiguous continuation: the first entry must be
    ``last_seq + 1`` and every subsequent entry exactly one more than its
    predecessor. Everything else is rejected with a reason and the seq to
    resume from:

    - ``empty`` — no entries.
    - ``gap`` — first seq > ``last_seq + 1`` (missing rows before this batch).
    - ``stale`` — first seq <= ``last_seq`` (already-applied replay; brief §4.2
      treats a lower seq as harmless, so the server would ACK-and-noop, but the
      pure verdict flags it so the caller can distinguish it from a true gap).
    - ``duplicate`` — a repeated seq inside the batch.
    - ``non_monotonic`` — a seq lower than its predecessor inside the batch.
    - ``internal_gap`` — a jump (> +1) inside the batch.

    Pure: no DB access. The server folds a ``BatchVerdict.ok`` decision into the
    same transaction that upserts rows + the replication ledger (P5-T4).
    """
    entries = batch.entries
    if not entries:
        return BatchVerdict(ok=False, reason="empty", expected_seq_no=last_seq + 1)

    first = entries[0].seq_no
    if first <= last_seq:
        return BatchVerdict(ok=False, reason="stale", expected_seq_no=last_seq + 1)
    if first != last_seq + 1:
        return BatchVerdict(ok=False, reason="gap", expected_seq_no=last_seq + 1)

    prev = first
    for ev in entries[1:]:
        s = ev.seq_no
        if s == prev:
            return BatchVerdict(ok=False, reason="duplicate", expected_seq_no=prev + 1)
        if s < prev:
            return BatchVerdict(ok=False, reason="non_monotonic", expected_seq_no=prev + 1)
        if s != prev + 1:
            return BatchVerdict(ok=False, reason="internal_gap", expected_seq_no=prev + 1)
        prev = s

    return BatchVerdict(ok=True, accepted_from=first, accepted_to=prev)


# --- Batch collapse into an apply plan (brief §4.2, §4.5) ------------------


class UpsertPlan(BaseModel):
    """The minimal set of central-side operations for one batch.

    ``upserts`` holds the winning :class:`AgentEvent` per ``rel_path`` (last
    event wins). ``deletes`` holds ``rel_path`` strings to tombstone. Applied
    upserts-first-then-deletes (see :attr:`operations`) so a ``moved`` (delete
    old + create new) never races a same-path recreate."""

    upserts: list[AgentEvent] = Field(default_factory=list)
    deletes: list[str] = Field(default_factory=list)

    @property
    def operations(self) -> list[tuple[str, str]]:
        """(op, rel_path) tuples in apply order: all upserts, then all deletes."""
        return [("upsert", e.rel_path) for e in self.upserts] + [
            ("delete", p) for p in self.deletes
        ]


def plan_upserts(events: list[AgentEvent]) -> UpsertPlan:
    """Collapse an ordered event list into an :class:`UpsertPlan`.

    - **Last-event-per-``rel_path`` wins.** If a path is created then modified
      then deleted within one batch, only the delete survives.
    - **``moved`` = delete + create.** A ``moved`` event tombstones
      ``from_rel_path`` and upserts ``rel_path`` (brief §4.5).
    - **Deletes ordered after upserts** in :attr:`UpsertPlan.operations`.

    Results are sorted by ``rel_path`` within each group for a stable,
    order-independent plan. Pure: no DB access.
    """
    state: dict[str, tuple[str, AgentEvent | None]] = {}
    for ev in events:
        if ev.event_type == "moved":
            if ev.from_rel_path is not None:
                state[ev.from_rel_path] = ("delete", None)
            state[ev.rel_path] = ("upsert", ev)
        elif ev.event_type == "deleted":
            state[ev.rel_path] = ("delete", None)
        else:  # created | modified
            state[ev.rel_path] = ("upsert", ev)

    upserts = [ev for kind, ev in state.values() if kind == "upsert" and ev is not None]
    deletes = [path for path, (kind, _ev) in state.items() if kind == "delete"]
    upserts.sort(key=lambda e: e.rel_path)
    deletes.sort()
    return UpsertPlan(upserts=upserts, deletes=deletes)


# --- Full-reconciliation manifest digest (brief §4.4) ----------------------


class ManifestRow(BaseModel):
    """One row of a full-reconciliation manifest (brief §4.4). Deliberately the
    R1 field set: identity + size + mtime + hashes, nothing extracted.

    ``mtime`` stays a **float** epoch-seconds here (the natural producer shape),
    but the digest and every field-diff quantize it to INTEGER microseconds — see
    :func:`mtime_to_us` / :func:`manifest_digest` (P5-T5 cross-language ruling)."""

    model_config = ConfigDict(frozen=True)

    rel_path: str
    size: int
    mtime: float
    quick_hash: str | None = None
    content_hash: str | None = None


def mtime_to_us(mtime: float) -> int:
    """Quantize a float epoch-seconds ``mtime`` to INTEGER microseconds.

    **Cross-language digest contract (P5-T5 Architect ruling 2).** A raw float is
    NOT byte-stable across a Go producer and this Python server (each language's
    JSON float formatter differs, and Postgres ``timestamptz`` itself only stores
    microsecond resolution), so BOTH halves canonicalize ``mtime`` to
    ``round(mtime * 1e6)`` microseconds *before* it enters the digest AND before
    any anti-join field comparison. The µs quantum is therefore the finest mtime
    difference either the digest or the reconcile field-diff can observe."""
    return round(mtime * 1_000_000)


def _manifest_blob(rows: Any) -> bytes:
    """Canonical byte serialization of a manifest, shared by the pure
    :func:`manifest_digest` and the server-side staged-row recompute at reconcile
    finish. ``rows`` is an iterable of
    ``(rel_path, size, mtime_us, quick_hash, content_hash)`` tuples (``mtime_us``
    already an int). Rows are sorted by ``rel_path`` and each is emitted as a
    compact, key-sorted JSON object; ``ensure_ascii=True`` so a unicode rel_path
    escapes identically regardless of producer locale. The ``mtime`` JSON field
    holds the INTEGER microseconds (ruling 2), never the source float."""
    payload = [
        {
            "rel_path": rel_path,
            "size": size,
            "mtime": mtime_us,
            "quick_hash": quick_hash,
            "content_hash": content_hash,
        }
        for (rel_path, size, mtime_us, quick_hash, content_hash) in sorted(
            rows, key=lambda t: t[0]
        )
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return blob.encode("utf-8")


def manifest_digest(rows: list[ManifestRow]) -> str:
    """Canonical SHA-256 of a full manifest, for the anti-join reconciliation
    sweep (brief §4.4).

    Rows are sorted by ``rel_path`` and serialized as compact, key-sorted JSON
    so an agent and the server compute a byte-identical digest over the same
    corpus regardless of the order rows were produced in. ``mtime`` is quantized
    to integer microseconds (:func:`mtime_to_us`, ruling 2) so a float that is
    byte-different but microsecond-identical across languages still digests the
    same. Any change to any (µs-quantized) field of any row changes the digest.
    Pure: no DB access.
    """
    return hashlib.sha256(
        _manifest_blob(
            (r.rel_path, r.size, mtime_to_us(r.mtime), r.quick_hash, r.content_hash)
            for r in rows
        )
    ).hexdigest()


# --- Enrollment: token minting + register-first handshake (P5-T1, R3) -------

ENROLL_TOKEN_PREFIX = "fae"  # filearr agent enrollment (human-recognisable)
PLATFORMS = frozenset({"windows", "macos", "linux"})


class EnrollmentError(Exception):
    """A register/binding request the server refuses. ``reason`` is a stable
    machine string the API maps onto an HTTP status: ``unknown_token`` /
    ``consumed`` / ``expired`` (→ 401), ``bad_platform`` / ``already_bound`` /
    ``bad_secret`` / ``revoked`` (→ 400/409)."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_enrollment_token() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``. The raw token is shown to the operator
    ONCE and never persisted; only ``token_hash`` (sha256 hex) is stored —
    mirrors the API-key / session pattern (R3 presented-once)."""
    raw = f"{ENROLL_TOKEN_PREFIX}_{secrets.token_urlsafe(32)}"
    return raw, _sha256_hex(raw)


def hash_enrollment_token(raw: str) -> str:
    """Sha256-hex of a raw enrollment token (the ``enrollment_tokens`` PK)."""
    return _sha256_hex(raw)


def classify_token(
    *, consumed_at: Any, expires_at: Any, now: Any
) -> str | None:
    """Pure single-use/TTL verdict for an ``enrollment_tokens`` row. Returns a
    rejection reason (``consumed`` / ``expired``) or ``None`` when the token is
    still redeemable. ``consumed`` is checked before ``expired`` so a replay of
    an already-redeemed token reads as a replay, not merely stale."""
    if consumed_at is not None:
        return "consumed"
    if expires_at <= now:
        return "expired"
    return None


def generate_enroll_secret() -> tuple[str, str]:
    """Return ``(raw_secret, secret_hash)`` — the one-time nonce handed back from
    register that gates the later fingerprint-binding call (module note)."""
    raw = secrets.token_urlsafe(24)
    return raw, _sha256_hex(raw)


async def mint_enrollment_token(
    session: Any,
    *,
    rollout_group: str = "default",
    ttl_seconds: int = 3600,
) -> tuple[str, Any]:
    """P5-T1: mint + persist a single-use, short-TTL enrollment token (brief
    §7.1, R3). Returns ``(raw_token, EnrollmentToken row)``; the caller shows the
    raw token to the operator once and never again. The row stores only the hash.
    The agent presents the raw token at ``/agents/register`` *before* any cert
    exists (R3)."""
    from datetime import UTC, datetime, timedelta

    from filearr.models import EnrollmentToken

    raw, token_hash = generate_enrollment_token()
    row = EnrollmentToken(
        token_hash=token_hash,
        rollout_group=rollout_group or "default",
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    await session.flush()
    return raw, row


async def register_agent(
    session: Any,
    *,
    raw_token: str,
    hostname: str,
    platform: str,
    name: str | None = None,
    agent_version: str | None = None,
    config_group: str | None = None,
) -> tuple[Any, str, str | None]:
    """P5-T1: consume an enrollment token and assign the authoritative,
    server-side ``agents.id`` (brief §7.1, R3 — registration PRECEDES CSR/cert;
    the agent embeds the returned id in its CSR CN/SAN, then the CA signs).

    Returns ``(Agent row, raw_enroll_secret, config_group_warning)``. The agent is
    created **pending** (``cert_fingerprint`` NULL) — the fingerprint is bound
    later by :func:`bind_agent_certificate` once the CA has signed. Raises
    :class:`EnrollmentError` on an unknown / consumed / expired token or a bad
    platform. Single-use is enforced by stamping ``consumed_at``/``consumed_by``
    in the SAME transaction that creates the agent, so a replay finds the token
    already consumed.

    W6-D2: ``config_group`` (an installer-sidecar string) is resolved by NAME to
    ``agents.config_group_id`` at registration. FAIL-SAFE: an UNKNOWN name never
    blocks enrollment — the agent registers with a NULL group and the returned
    ``config_group_warning`` explains it (surfaced to the operator in the register
    response). ``None``/absent → no group, no warning."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from filearr.models import Agent, AgentConfigGroup, EnrollmentToken

    if platform not in PLATFORMS:
        raise EnrollmentError("bad_platform", f"platform must be one of {sorted(PLATFORMS)}")

    token_hash = hash_enrollment_token(raw_token)
    row = await session.get(EnrollmentToken, token_hash, with_for_update=True)
    if row is None:
        raise EnrollmentError("unknown_token", "no such enrollment token")
    verdict = classify_token(
        consumed_at=row.consumed_at, expires_at=row.expires_at, now=datetime.now(UTC)
    )
    if verdict is not None:
        raise EnrollmentError(verdict, f"enrollment token {verdict}")

    # Resolve the config group by name (fail-safe: unknown name -> NULL + warning).
    config_group_id = None
    warning: str | None = None
    if config_group:
        grp = (
            await session.execute(
                select(AgentConfigGroup).where(AgentConfigGroup.name == config_group)
            )
        ).scalar_one_or_none()
        if grp is not None:
            config_group_id = grp.id
        else:
            warning = f"unknown config group {config_group!r}; assigned built-in defaults"

    raw_secret, secret_hash = generate_enroll_secret()
    agent = Agent(
        name=(name or hostname),
        hostname=hostname,
        platform=platform,
        rollout_group=row.rollout_group,
        agent_version=agent_version,
        enroll_secret_hash=secret_hash,
        config_group_id=config_group_id,
    )
    session.add(agent)
    await session.flush()  # assigns agent.id (server-side, R3)

    row.consumed_at = datetime.now(UTC)
    row.consumed_by = agent.id
    await session.flush()
    return agent, raw_secret, warning


async def bind_agent_certificate(
    session: Any,
    *,
    agent_id: Any,
    raw_secret: str,
    cert_fingerprint: str,
) -> Any:
    """P5-T1 seam (finalized under mTLS by P5-T2): bind the CA-issued cert
    fingerprint to a pending agent, transitioning it to **active**. Guarded by
    the one-time ``enroll_secret`` returned from register so a guessed agent
    UUID cannot hijack a pending identity. Idempotency: re-binding the SAME
    fingerprint with the SAME secret is a no-op; a different fingerprint or a
    bad/spent secret raises. Raises :class:`EnrollmentError`."""
    from filearr.models import Agent

    agent = await session.get(Agent, agent_id, with_for_update=True)
    if agent is None:
        raise EnrollmentError("unknown_agent", "no such agent")
    if agent.revoked_at is not None:
        raise EnrollmentError("revoked", "agent is revoked")
    if agent.cert_fingerprint is not None:
        # Already active. Allow an idempotent replay of the exact same binding.
        if agent.cert_fingerprint == cert_fingerprint:
            return agent
        raise EnrollmentError("already_bound", "agent already has a certificate")
    if not agent.enroll_secret_hash or not secrets.compare_digest(
        agent.enroll_secret_hash, _sha256_hex(raw_secret)
    ):
        raise EnrollmentError("bad_secret", "invalid enrollment secret")

    agent.cert_fingerprint = cert_fingerprint
    agent.enroll_secret_hash = None  # one-time; spent on successful bind
    await session.flush()
    return agent


def agent_status(agent: Any) -> str:
    """Derive an agent's console status: ``revoked`` > ``active`` (cert bound) >
    ``pending`` (registered, awaiting its cert)."""
    if agent.revoked_at is not None:
        return "revoked"
    if agent.cert_fingerprint is not None:
        return "active"
    return "pending"


# --- P5-T2 (central half): step-ca JWK one-time token (OTT) minting ---------
# Once an agent has registered (pending), it must obtain a short-lived client
# cert from step-ca. Central brokers a scoped JWK-provisioner **one-time token**
# (OTT) signed with the provisioner's private JWK; the agent exchanges it
# DIRECTLY with step-ca's ``/1.0/sign`` endpoint (keys never leave the agent, no
# CSR proxying through central -- spike verdict 1,
# ``docs/research/phase-5-t2a-stepca-spike.md``).
#
# Claim shape mirrors what step-ca's JWK provisioner sign-token path expects (the
# same claims ``step ca token`` emits): ``iss`` = provisioner name (step-ca finds
# the provisioner by ``iss`` + the header ``kid``), ``aud`` = ``<ca_url>/1.0/sign``,
# ``sub`` = agent id (step-ca uses it as the default CSR CN), ``sans`` = ``[agent
# id]`` (step-ca validates the CSR SANs against this claim -- re-confirm on the
# step-ca version in use, spike "VERIFY CSR CN/SAN == OTT sub/sans"), plus
# ``iat``/``nbf``/``exp`` (short) and a unique ``jti`` (step-ca rejects a reused
# jti -> single-use). Header: ``alg`` ES256, ``typ`` JWT, ``kid`` = the JWK ``kid``
# (or its RFC7638 thumbprint when absent) so step-ca selects the right key.

CA_OTT_SIGN_PATH = "/1.0/sign"


class CaOttError(Exception):
    """The provisioner JWK cannot mint an OTT (unset/malformed key, or a signing
    failure). The register path handles this FAIL-SAFE (``ca_ott`` -> null so
    registration still succeeds); the explicit re-issue endpoint surfaces it as
    a 503."""


@lru_cache(maxsize=8)
def load_provisioner_jwk(raw_jwk: str | None) -> dict | None:
    """Parse + shape-validate the provisioner private JWK (a JSON string, EC
    P-256/ES256 expected).

    Returns the JWK as a ``dict``, or ``None`` when it is unset OR malformed --
    the documented FAIL-SAFE ruling: a missing/bad provisioner key never breaks
    registration, it only means ``ca_ott`` comes back null and agents cannot yet
    fetch certs. NEVER logs the key material (only the failure MODE). Memoised on
    the raw string so the shape check runs once per distinct value; the cache is
    keyed by value so a settings reload with a new key re-validates."""
    if not raw_jwk or not raw_jwk.strip():
        return None
    try:
        data = json.loads(raw_jwk)
    except (ValueError, TypeError):
        _log.error(
            "FILEARR_CA_PROVISIONER_JWK is not valid JSON -- ca_ott minting disabled "
            "(agents can register but cannot obtain certs until it is fixed)"
        )
        return None
    if not isinstance(data, dict):
        _log.error(
            "FILEARR_CA_PROVISIONER_JWK must be a JSON object -- ca_ott minting disabled"
        )
        return None
    if data.get("kty") != "EC" or data.get("crv") != "P-256":
        _log.error(
            "FILEARR_CA_PROVISIONER_JWK must be an EC P-256 (ES256) key -- "
            "ca_ott minting disabled"
        )
        return None
    if not all(data.get(k) for k in ("x", "y", "d")):
        _log.error(
            "FILEARR_CA_PROVISIONER_JWK is missing private EC members (x/y/d) -- "
            "ca_ott minting disabled"
        )
        return None
    return data


def mint_ca_ott(
    agent_id: Any,
    *,
    jwk: dict,
    ca_url: str,
    provisioner: str,
    ttl_seconds: int,
) -> tuple[str, str]:
    """Mint an ES256-signed step-ca JWK one-time token for ``agent_id``.

    Returns ``(token, jti)`` -- the caller audits ``jti`` (NEVER the token).
    Pure-ish: no DB, no network. Raises :class:`CaOttError` if the key cannot
    sign. ``sub`` and the sole ``sans`` entry are both the agent id string so the
    issued cert's CN/SAN carries the server-assigned identity (R3)."""
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import ECKey

    try:
        key = ECKey.import_key(jwk)
    except Exception as exc:  # noqa: BLE001 - normalise any joserfc key error
        raise CaOttError("invalid provisioner JWK") from exc

    sub = str(agent_id)
    now = int(time.time())
    jti = secrets.token_urlsafe(16)
    header = {"alg": "ES256", "typ": "JWT", "kid": jwk.get("kid") or key.thumbprint()}
    claims = {
        "iss": provisioner,
        "aud": ca_url.rstrip("/") + CA_OTT_SIGN_PATH,
        "sub": sub,
        "sans": [sub],
        "iat": now,
        "nbf": now,
        "exp": now + int(ttl_seconds),
        "jti": jti,
    }
    try:
        token = jose_jwt.encode(header, claims, key)
    except Exception as exc:  # noqa: BLE001 - normalise any joserfc signing error
        raise CaOttError("OTT signing failed") from exc
    return token, jti


def try_mint_ca_ott(agent_id: Any, settings: Any) -> tuple[str | None, str | None]:
    """Best-effort OTT mint for the register path. Returns ``(token, jti)`` or
    ``(None, None)`` when the provisioner JWK is unset/malformed OR ``ca_url`` is
    not configured -- the FAIL-SAFE: registration must still succeed with a null
    ``ca_ott``. Logs why the OTT was skipped so an operator can diagnose a null
    ``ca_ott`` (the register response), but never the key or the token."""
    jwk = load_provisioner_jwk(settings.ca_provisioner_jwk)
    if jwk is None:
        return None, None
    if not settings.ca_url:
        _log.warning("ca_ott mint skipped: FILEARR_CA_URL is unset")
        return None, None
    try:
        return mint_ca_ott(
            agent_id,
            jwk=jwk,
            ca_url=settings.ca_url,
            provisioner=settings.ca_provisioner,
            ttl_seconds=settings.ca_ott_ttl_seconds,
        )
    except CaOttError:
        _log.error("ca_ott mint failed for agent %s", agent_id, exc_info=True)
        return None, None


# --- Stateful endpoints: stubs, implemented by the tagged Phase-5 task ------


def _agent_library_basename(library_ref: str) -> str:
    """Last path segment of an agent-side ``library_ref`` for the display name.

    The ref is the agent's local absolute root path; it may use ``/`` (POSIX) or
    ``\\`` (Windows) separators regardless of the central host's OS, so we split
    on both rather than trusting ``os.path.basename``. Falls back to the whole ref
    when there is no usable trailing segment (e.g. a bare root)."""
    cleaned = library_ref.replace("\\", "/").rstrip("/")
    base = cleaned.rsplit("/", 1)[-1] if "/" in cleaned else cleaned
    return base or library_ref


async def _provision_agent_library(session: Any, agent: Any, library_ref: str, now: Any):
    """Auto-provision (or return the existing) central Library for one agent
    ``library_ref`` (ruling R1). Name = ``"{agent.name or hostname}: {basename}"``
    with ``" (2)"``/``" (3)"`` suffixing on collision (``libraries.name`` is
    UNIQUE); ``root_path`` = the ``library_ref`` verbatim (the agent-side absolute
    root — central never opens it). Keyed by (source_agent_id, agent_library_ref)
    via a partial unique index so a repeat batch reuses this row.

    Returns ``(library, created)`` where ``created`` is True only when a new row
    was inserted."""
    from sqlalchemy import select

    from filearr.models import Library

    existing = (
        await session.execute(
            select(Library).where(
                Library.source_agent_id == agent.id,
                Library.agent_library_ref == library_ref,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    base_name = f"{agent.name or agent.hostname}: {_agent_library_basename(library_ref)}"
    name = base_name
    suffix = 2
    while (
        await session.execute(select(Library.id).where(Library.name == name))
    ).first() is not None:
        name = f"{base_name} ({suffix})"
        suffix += 1

    library = Library(
        name=name,
        root_path=library_ref,  # agent-side absolute root, verbatim (R1)
        source_agent_id=agent.id,
        agent_library_ref=library_ref,
        enabled=True,
    )
    session.add(library)
    await session.flush()  # assign library.id for path_scope + item FKs
    return library, True


async def apply_batch(session: Any, agent: Any, batch: ReplicationBatch) -> dict:
    """P5-T4: apply a replication batch to central ``items`` + the
    ``agent_replication_log`` ledger in ONE transaction (brief §7.2).

    The caller (the endpoint) has already run :func:`check_batch` against
    ``agents.last_contiguous_seq_no``; this re-derives the plan via
    :func:`plan_upserts` and applies **upserts first, deletes after** (the plan
    order — a ``moved`` delete-of-old never races the create-of-new). All writes,
    the ledger inserts, and the seq-watermark advance commit together.

    Semantics (rulings R1/R2, CLAUDE.md invariants 3/4):

    - **Library materialization** — a Library is auto-provisioned per (agent,
      ``library_ref``) at apply time (:func:`_provision_agent_library`).
    - **Item identity stays central** — upsert by ``(library_id, rel_path)``;
      central owns the item id (the agent's local ids never cross the wire).
      ``items.source_agent_id`` is stamped on every agent-touched row. A create
      derives ``(file_category, file_group)`` via the DB taxonomy and ``path_scope``
      via the SAME ``rbac.path_to_ltree`` the scanner uses. ``mtime`` (float epoch) →
      UTC datetime (scan.py convention). NO extract is deferred (central cannot
      open agent files); agent-side sidecars simply arrive as plain items.
    - **Tombstone** — a deleted event (and the delete half of a moved) sets an
      existing row ``missing`` (never hard-delete, invariant 4); a tombstone
      against an ABSENT row is the R2 counted no-op (``noop_tombstones``).
    - **Ledger** — one ``agent_replication_log`` row per batch ENTRY seq
      (``ON CONFLICT DO NOTHING``); ``agents.last_contiguous_seq_no`` advances to
      the batch max — all in this transaction.

    Returns a dict ``{applied, upserted, tombstoned, noop_tombstones,
    libraries_created, last_seq}`` plus ``item_ids`` (the touched central item ids
    the caller defers to Meili AFTER commit, per invariant 5). Both active
    upserts and missing tombstones are included so ``sync_items`` projects/removes
    them exactly as a scan's incremental sync does."""
    from datetime import UTC, datetime

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from filearr import rbac, taxonomy
    from filearr.models import AgentReplicationLog, Item, ItemStatus

    now = datetime.now(UTC)
    plan = plan_upserts(batch.entries)
    # W8-A: load the (cached) taxonomy snapshot ONCE for the batch so a create can
    # stamp (file_category, file_group) alongside media_type (pure lookups after).
    tax = await taxonomy.load(session)

    # Reconstruct the (library_ref) for every delete target: plan.deletes is a bare
    # rel_path list (frozen contract), but a tombstone needs library context. A
    # deleted event tombstones its rel_path; a moved event tombstones from_rel_path.
    delete_ref: dict[str, str] = {}
    for ev in batch.entries:
        if ev.event_type == "moved" and ev.from_rel_path is not None:
            delete_ref[ev.from_rel_path] = ev.library_ref
        elif ev.event_type == "deleted":
            delete_ref[ev.rel_path] = ev.library_ref

    # --- resolve/provision every referenced library ONCE ---------------------
    lib_cache: dict[str, Any] = {}
    libraries_created = 0
    refs = {e.library_ref for e in plan.upserts} | {
        delete_ref[p] for p in plan.deletes if p in delete_ref
    }
    for ref in refs:
        library, created = await _provision_agent_library(session, agent, ref, now)
        lib_cache[ref] = library
        if created:
            libraries_created += 1

    # --- preload existing (library_id, rel_path) rows we may touch -----------
    # One query per library, rel_path IN (...) — bounded by the batch entry cap.
    want: dict[Any, set[str]] = {}
    for ev in plan.upserts:
        want.setdefault(lib_cache[ev.library_ref].id, set()).add(ev.rel_path)
    for p in plan.deletes:
        ref = delete_ref.get(p)
        if ref is not None:
            want.setdefault(lib_cache[ref].id, set()).add(p)
    existing: dict[tuple[Any, str], Item] = {}
    for lib_id, paths in want.items():
        rows = (
            await session.execute(
                select(Item).where(
                    Item.library_id == lib_id, Item.rel_path.in_(paths)
                )
            )
        ).scalars()
        for it in rows:
            existing[(lib_id, it.rel_path)] = it

    # rel_path -> touched item id, for per-entry ledger attribution below.
    item_id_by_rel: dict[str, Any] = {}
    touched_ids: list[str] = []
    upserted = tombstoned = noop_tombstones = 0

    # --- upserts first (plan order) ------------------------------------------
    for ev in plan.upserts:
        library = lib_cache[ev.library_ref]
        key = (library.id, ev.rel_path)
        row = existing.get(key)
        mtime = (
            datetime.fromtimestamp(ev.mtime, tz=UTC) if ev.mtime is not None else now
        )
        if row is None:
            filename = ev.rel_path.replace("\\", "/").rsplit("/", 1)[-1]
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
            file_category, file_group = tax.detect(ev.rel_path)
            row = Item(
                library_id=library.id,
                # W8-B: stored taxonomy classification (authoritative; media_type
                # is gone). Central classifies from the rel_path extension.
                file_category=file_category,
                file_group=file_group,
                path=ev.rel_path,  # agent-side absolute-ish path; refreshed per event
                rel_path=ev.rel_path,
                filename=filename,
                extension=ext,
                size=ev.size if ev.size is not None else 0,
                mtime=mtime,
                quick_hash=ev.quick_hash,
                content_hash=ev.content_hash,
                status=ItemStatus.active,
                source_agent_id=agent.id,
                first_seen=now,
                last_seen=now,
                # Same ltree RBAC scope key the scanner stamps on create.
                path_scope=rbac.path_to_ltree(ev.rel_path, library_id=library.id),
                # P10-T11: stamp the agent's share hint verbatim (None → NULL is
                # the normal case). A create sets it unconditionally.
                share_hint=ev.share_hint,
            )
            session.add(row)
            existing[key] = row
        else:
            if ev.size is not None:
                row.size = ev.size
            if ev.mtime is not None:
                row.mtime = mtime
            row.quick_hash = ev.quick_hash
            row.content_hash = ev.content_hash
            row.path = ev.rel_path
            row.status = ItemStatus.active
            row.last_seen = now
            row.source_agent_id = agent.id
            # P10-T11: refresh the share hint only when this event carries one, so
            # a hint-less modified event (agent could not resolve a share this
            # time) does NOT clobber a previously-good hint (R1: the hint is a
            # convenience; the central mapping is the deterministic fallback).
            if ev.share_hint is not None:
                row.share_hint = ev.share_hint
        await session.flush()  # assign id
        item_id_by_rel[ev.rel_path] = row.id
        touched_ids.append(str(row.id))
        upserted += 1

    # --- deletes after (plan order) ------------------------------------------
    for p in plan.deletes:
        ref = delete_ref.get(p)
        library = lib_cache.get(ref) if ref is not None else None
        row = existing.get((library.id, p)) if library is not None else None
        if row is None:
            # R2: tombstone against an already-purged/never-seen row — counted no-op.
            noop_tombstones += 1
            continue
        row.status = ItemStatus.missing
        row.last_seen = now
        row.source_agent_id = agent.id
        item_id_by_rel[p] = row.id
        touched_ids.append(str(row.id))
        tombstoned += 1

    # --- ledger: one row per batch ENTRY seq (idempotency backstop) ----------
    for ev in batch.entries:
        rel = ev.rel_path  # the entry's own target path (new location for moved)
        await session.execute(
            pg_insert(AgentReplicationLog)
            .values(
                agent_id=agent.id,
                seq_no=ev.seq_no,
                item_id=item_id_by_rel.get(rel),
                op=ev.event_type,
                applied_at=now,
            )
            .on_conflict_do_nothing(index_elements=["agent_id", "seq_no"])
        )

    # --- advance the per-agent contiguous watermark + last_seen --------------
    last_seq = batch.entries[-1].seq_no
    if last_seq > agent.last_contiguous_seq_no:
        agent.last_contiguous_seq_no = last_seq
    agent.last_seen_at = now

    await session.commit()

    return {
        "applied": len(batch.entries),
        "upserted": upserted,
        "tombstoned": tombstoned,
        "noop_tombstones": noop_tombstones,
        "libraries_created": libraries_created,
        "last_seq": agent.last_contiguous_seq_no,
        # Internal: touched central item ids for the post-commit Meili defer
        # (invariant 5). Filtered out of the endpoint's response_model.
        "item_ids": touched_ids,
    }


# --- P5-T5: full-manifest reconciliation sweep (brief §4.4 / §4.5) ----------
# The anti-join that corrects drift the incremental replication path can miss
# (a lost tombstone; a post-local-rebuild seq reset) WITHOUT a full local index
# rebuild, and advances the per-agent purge-safety watermark (agents.
# last_reconcile_at, §4.5) that — alongside the recycle-bin retention window —
# gates permanent purge (R2). Three-phase, one live session per agent:
#   start  -> compare the agent's whole-library digest to central's projection;
#             equal -> "match" (stamp watermark, done); else open a session.
#   rows   -> the agent pages its full manifest into a staging table.
#   finish -> verify the staged digest/count, run the set-based anti-join in ONE
#             transaction, stamp the watermark, drop the session.


class ReconcileError(Exception):
    """A reconcile request the server refuses. ``reason`` maps onto an HTTP
    status at the endpoint: ``unknown_session`` (missing / wrong-agent / expired
    → 404) and ``digest_mismatch`` (staged digest/count disagree with the
    finish body → 409, and the session is destroyed so the agent re-sweeps)."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


async def _agent_library(session: Any, agent: Any, library_ref: str) -> Any | None:
    """The central Library materializing (agent, library_ref), or ``None`` when
    none exists yet (a brand-new / renamed agent root — its projection is empty
    and it reconciles into existence at finish via :func:`_provision_agent_library`)."""
    from sqlalchemy import select

    from filearr.models import Library

    return (
        await session.execute(
            select(Library).where(
                Library.source_agent_id == agent.id,
                Library.agent_library_ref == library_ref,
            )
        )
    ).scalar_one_or_none()


async def compute_central_digest(
    session: Any, agent: Any, library_ref: str
) -> tuple[str, int]:
    """The server's manifest digest + row_count over its OWN projection for
    (agent, library_ref): the ``status='active'`` items of the library the agent
    root maps to (ruling 1 — ``missing``/``trashed`` are excluded). An unknown
    library_ref projects the empty manifest (digest of ``[]``, count 0), so a
    non-empty agent naturally mismatches into a session. ``mtime`` is the item's
    stored ``timestamptz`` back to float epoch seconds (``.timestamp()``), then
    µs-quantized inside the digest — the same quantum central stored it at."""
    from sqlalchemy import select

    from filearr.models import Item, ItemStatus

    library = await _agent_library(session, agent, library_ref)
    if library is None:
        return manifest_digest([]), 0

    rows = (
        await session.execute(
            select(
                Item.rel_path, Item.size, Item.mtime, Item.quick_hash, Item.content_hash
            ).where(
                Item.library_id == library.id,
                Item.status == ItemStatus.active,
            )
        )
    ).all()
    manifest = [
        ManifestRow(
            rel_path=r.rel_path,
            size=r.size,
            mtime=r.mtime.timestamp(),
            quick_hash=r.quick_hash,
            content_hash=r.content_hash,
        )
        for r in rows
    ]
    return manifest_digest(manifest), len(manifest)


def _expiry_cutoff(now: Any, ttl_seconds: int) -> Any:
    return now - timedelta(seconds=ttl_seconds)


async def _sweep_expired_sessions(session: Any, now: Any, ttl_seconds: int) -> None:
    """Opportunistically delete reconcile sessions whose ``started_at`` aged past
    the TTL (staging rows cascade). Runs at ``start`` so an abandoned sweep frees
    its staging without a dedicated periodic task (frozen protocol)."""
    from sqlalchemy import delete

    from filearr.models import AgentReconcileSession

    await session.execute(
        delete(AgentReconcileSession).where(
            AgentReconcileSession.started_at < _expiry_cutoff(now, ttl_seconds)
        )
    )


async def _load_live_session(
    session: Any, agent: Any, session_id: Any, now: Any, ttl_seconds: int
) -> Any:
    """Load a reconcile session that MUST belong to ``agent`` and be un-expired,
    else raise :class:`ReconcileError('unknown_session')` (a wrong-agent id never
    leaks another agent's session; an expired one is deleted so a fresh sweep
    starts clean)."""
    from filearr.models import AgentReconcileSession

    row = await session.get(AgentReconcileSession, session_id)
    if row is None or str(row.agent_id) != str(agent.id):
        raise ReconcileError("unknown_session", "no such reconcile session")
    if row.started_at < _expiry_cutoff(now, ttl_seconds):
        await session.delete(row)
        await session.commit()
        raise ReconcileError("unknown_session", "reconcile session expired")
    return row


async def reconcile_start(
    session: Any,
    agent: Any,
    *,
    library_ref: str,
    digest: str,
    row_count: int,
    rebuilt: bool,
    now: Any,
    ttl_seconds: int,
) -> dict:
    """Phase 1. Compare the agent's whole-library ``digest``/``row_count`` to
    central's projection (:func:`compute_central_digest`).

    Equal digests AND equal counts → ``{"status":"match"}``: stamp
    ``agents.last_reconcile_at`` and, when ``rebuilt``, reset
    ``last_contiguous_seq_no`` to 0 (the §4.2 "local index rebuilt → resync"
    fast-forward). Otherwise open exactly ONE live session for this agent
    (superseding any prior unfinished one — ``unique(agent_id)``) and return
    ``{"status":"mismatch","session_id":...}``. Commits."""
    from sqlalchemy import delete

    from filearr.models import AgentReconcileSession

    await _sweep_expired_sessions(session, now, ttl_seconds)

    central_digest, central_count = await compute_central_digest(
        session, agent, library_ref
    )
    agent.last_seen_at = now

    if central_digest == digest and central_count == row_count:
        # In sync: stamp the watermark, honour a post-rebuild seq reset, and clear
        # any stale session left over from an earlier aborted sweep.
        agent.last_reconcile_at = now
        if rebuilt:
            agent.last_contiguous_seq_no = 0
        await session.execute(
            delete(AgentReconcileSession).where(
                AgentReconcileSession.agent_id == agent.id
            )
        )
        await session.commit()
        return {"status": "match"}

    # Mismatch (or unknown/renamed root): one live session per agent — drop any
    # prior unfinished one first (the unique(agent_id) guarantees at most one).
    await session.execute(
        delete(AgentReconcileSession).where(
            AgentReconcileSession.agent_id == agent.id
        )
    )
    row = AgentReconcileSession(
        agent_id=agent.id, library_ref=library_ref, started_at=now, staged_rows=0
    )
    session.add(row)
    await session.flush()
    session_id = row.id
    await session.commit()
    return {"status": "mismatch", "session_id": str(session_id)}


async def reconcile_stage_rows(
    session: Any,
    agent: Any,
    *,
    session_id: Any,
    rows: list[Any],
    now: Any,
    ttl_seconds: int,
) -> int:
    """Phase 2. Accumulate one page of the agent's manifest into staging (PK
    ``(session_id, rel_path)``; a re-sent page upserts, so a retried page is
    idempotent). ``mtime`` is µs-quantized at rest (ruling 2). Returns the running
    distinct staged-row count. Commits. Raises ``unknown_session`` (404) for a
    missing / wrong-agent / expired session."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from filearr.models import AgentReconcileSession, AgentReconcileStaging

    row = await _load_live_session(session, agent, session_id, now, ttl_seconds)
    for r in rows:
        await session.execute(
            pg_insert(AgentReconcileStaging)
            .values(
                session_id=row.id,
                rel_path=r.rel_path,
                size=r.size,
                mtime_us=mtime_to_us(r.mtime),
                quick_hash=r.quick_hash,
                content_hash=r.content_hash,
            )
            .on_conflict_do_update(
                index_elements=["session_id", "rel_path"],
                set_={
                    "size": r.size,
                    "mtime_us": mtime_to_us(r.mtime),
                    "quick_hash": r.quick_hash,
                    "content_hash": r.content_hash,
                },
            )
        )
    staged = (
        await session.execute(
            select(func.count()).select_from(AgentReconcileStaging).where(
                AgentReconcileStaging.session_id == row.id
            )
        )
    ).scalar_one()
    await session.execute(
        AgentReconcileSession.__table__.update()
        .where(AgentReconcileSession.id == row.id)
        .values(staged_rows=staged)
    )
    await session.commit()
    return staged


async def reconcile_finish(
    session: Any,
    agent: Any,
    *,
    session_id: Any,
    digest: str,
    row_count: int,
    reset_seq: bool,
    now: Any,
    ttl_seconds: int,
) -> dict:
    """Phase 3. Verify the staged manifest, run the anti-join, advance the
    watermark, drop the session — all in ONE transaction (brief §4.4/§4.5,
    ruling 3).

    The staged distinct-row count and the digest recomputed over the staged rows
    must equal the ``row_count``/``digest`` the agent asserts, else the session is
    destroyed and ``ReconcileError('digest_mismatch')`` (409) tells the agent to
    re-sweep. On success:

    - agent row NOT central-active → reactivate a ``missing`` row (``reactivated``),
      LEAVE a ``trashed`` row untouched (``trashed_conflicts`` — user intent wins,
      R2), else create (``upserted``).
    - central-active NOT in the manifest → ``missing`` (``tombstoned``).
    - both present, any µs-quantized field differs → update (``updated``).
    - identical → ``unchanged``.

    Stamps ``agents.last_reconcile_at`` (the §4.5 purge-safety watermark); resets
    ``last_contiguous_seq_no`` when ``reset_seq``. Returns the counters plus an
    internal ``item_ids`` list (touched central ids the caller defers to Meili
    AFTER commit, invariant 5)."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from filearr import rbac, taxonomy
    from filearr.models import (
        AgentReconcileStaging,
        Item,
        ItemStatus,
    )

    sess = await _load_live_session(session, agent, session_id, now, ttl_seconds)
    # W8-B: load the taxonomy snapshot once so a reconcile-created row stamps its
    # (file_category, file_group) exactly like apply_batch / the scanner do.
    tax = await taxonomy.load(session)

    staged = (
        await session.execute(
            select(AgentReconcileStaging).where(
                AgentReconcileStaging.session_id == sess.id
            )
        )
    ).scalars().all()

    staged_digest = hashlib.sha256(
        _manifest_blob(
            (s.rel_path, s.size, s.mtime_us, s.quick_hash, s.content_hash)
            for s in staged
        )
    ).hexdigest()
    if len(staged) != row_count or staged_digest != digest:
        await session.delete(sess)
        await session.commit()
        raise ReconcileError("digest_mismatch", "staged manifest does not verify")

    # Materialize (or reuse) the central library for this root. A brand-new /
    # renamed root reconciles into existence here (ruling 1 note).
    library, _created = await _provision_agent_library(
        session, agent, sess.library_ref, now
    )

    central = {
        it.rel_path: it
        for it in (
            await session.execute(
                select(Item).where(Item.library_id == library.id)
            )
        ).scalars()
    }
    agent_paths = {s.rel_path for s in staged}

    counts = {
        "upserted": 0,
        "tombstoned": 0,
        "reactivated": 0,
        "updated": 0,
        "trashed_conflicts": 0,
        "unchanged": 0,
    }
    touched_ids: list[str] = []

    for s in staged:
        mtime = datetime.fromtimestamp(s.mtime_us / 1_000_000, tz=UTC)
        row = central.get(s.rel_path)
        if row is None:
            filename = s.rel_path.replace("\\", "/").rsplit("/", 1)[-1]
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
            file_category, file_group = tax.detect(s.rel_path)
            row = Item(
                library_id=library.id,
                # W8-B: authoritative taxonomy classification (media_type is gone).
                file_category=file_category,
                file_group=file_group,
                path=s.rel_path,
                rel_path=s.rel_path,
                filename=filename,
                extension=ext,
                size=s.size,
                mtime=mtime,
                quick_hash=s.quick_hash,
                content_hash=s.content_hash,
                status=ItemStatus.active,
                source_agent_id=agent.id,
                first_seen=now,
                last_seen=now,
                path_scope=rbac.path_to_ltree(s.rel_path, library_id=library.id),
            )
            session.add(row)
            await session.flush()
            touched_ids.append(str(row.id))
            counts["upserted"] += 1
            continue

        if row.status == ItemStatus.trashed:
            # User intent wins (R2): a trashed row is NEVER resurrected by a sweep.
            counts["trashed_conflicts"] += 1
            continue

        differs = (
            row.size != s.size
            or mtime_to_us(row.mtime.timestamp()) != s.mtime_us
            or row.quick_hash != s.quick_hash
            or row.content_hash != s.content_hash
        )
        if row.status == ItemStatus.missing:
            row.status = ItemStatus.active
            row.size = s.size
            row.mtime = mtime
            row.quick_hash = s.quick_hash
            row.content_hash = s.content_hash
            row.path = s.rel_path
            row.last_seen = now
            row.source_agent_id = agent.id
            touched_ids.append(str(row.id))
            counts["reactivated"] += 1
        elif differs:
            row.size = s.size
            row.mtime = mtime
            row.quick_hash = s.quick_hash
            row.content_hash = s.content_hash
            row.path = s.rel_path
            row.last_seen = now
            row.source_agent_id = agent.id
            touched_ids.append(str(row.id))
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1

    # central-active absent from the manifest -> tombstone (invariant 4).
    for rel_path, row in central.items():
        if row.status == ItemStatus.active and rel_path not in agent_paths:
            row.status = ItemStatus.missing
            row.last_seen = now
            row.source_agent_id = agent.id
            touched_ids.append(str(row.id))
            counts["tombstoned"] += 1

    agent.last_reconcile_at = now
    agent.last_seen_at = now
    if reset_seq:
        agent.last_contiguous_seq_no = 0

    await session.delete(sess)
    await session.commit()

    return {
        "status": "reconciled",
        **counts,
        # Internal: touched ids for the post-commit Meili defer (invariant 5),
        # filtered out of the endpoint response_model.
        "item_ids": touched_ids,
    }


# --------------------------------------------------------------------------- #
# P10-T1 — agent_commands lifecycle: pure state machine + TTL/redelivery sweep  #
# --------------------------------------------------------------------------- #
# The on-demand command primitive (research §3.1) is a SEPARATE channel from    #
# replication/policy above (osquery ``distributed_interval`` precedent). This   #
# section is the pure, unit-testable core of its lifecycle; the Postgres-backed #
# admin/agent API is :mod:`filearr.api.agent_commands` and the periodic sweep   #
# wraps :func:`run_agent_command_sweep` in :mod:`filearr.worker`.               #

from datetime import timedelta  # noqa: E402 (grouped with the P10 additions)

CommandState = Literal[
    "pending", "picked_up", "done", "failed", "expired", "cancelled"
]
CommandEvent = Literal[
    "deliver",  # agent poll picked it up: pending -> picked_up
    "ack",  # in-flight lease heartbeat (slow command): picked_up -> picked_up
    "complete",  # agent reported success: picked_up -> done
    "fail",  # agent reported failure: picked_up -> failed
    "redeliver",  # unacked past lease, re-queue (at-least-once): picked_up -> pending
    "expire",  # TTL lapsed: pending|picked_up -> expired
    "cancel",  # admin abandons a pre-terminal command: pending|picked_up -> cancelled
]

#: Terminal states accept no further transitions (immutable once reached).
COMMAND_TERMINAL: frozenset[str] = frozenset({"done", "failed", "expired", "cancelled"})

# (current_state, event) -> next_state. Absent keys are invalid transitions and
# raise ValueError. ``deliver`` is the delivery-bookkeeping edge (poll); ``ack``
# is a self-edge that only refreshes the lease clock (status unchanged) so a slow
# multi-minute command is not reclaimed by the redelivery sweep mid-work.
_COMMAND_TRANSITIONS: dict[tuple[CommandState, CommandEvent], CommandState] = {
    ("pending", "deliver"): "picked_up",
    ("pending", "expire"): "expired",
    ("pending", "cancel"): "cancelled",
    ("picked_up", "ack"): "picked_up",
    ("picked_up", "complete"): "done",
    ("picked_up", "fail"): "failed",
    ("picked_up", "redeliver"): "pending",
    ("picked_up", "expire"): "expired",
    ("picked_up", "cancel"): "cancelled",
}


def command_state_machine(current: CommandState, event: CommandEvent) -> CommandState:
    """Advance an ``agent_commands`` row one lifecycle step (P10-T1). PURE.

    Legal path: ``pending → picked_up → done`` (or ``failed``). ``ack`` refreshes
    a ``picked_up`` lease without changing state; ``redeliver`` returns an unacked
    ``picked_up`` row to ``pending`` (at-least-once delivery); ``expire`` and
    ``cancel`` move any pre-terminal row to ``expired`` / ``cancelled``.
    ``done`` / ``failed`` / ``expired`` / ``cancelled`` are TERMINAL — any event
    from a terminal state (or any out-of-order event, e.g. ``complete`` before
    ``deliver``) raises ``ValueError`` rather than being silently absorbed."""
    try:
        return _COMMAND_TRANSITIONS[(current, event)]
    except KeyError:
        raise ValueError(
            f"invalid agent-command transition: {current!r} --{event!r}-->"
        ) from None


def command_is_terminal(state: str) -> bool:
    """True iff ``state`` is a terminal ``agent_commands`` status (immutable)."""
    return state in COMMAND_TERMINAL


def sweep_decision(
    *,
    status: str,
    expires_at: Any,
    picked_up_at: Any,
    attempts: int,
    now: Any,
    lease_seconds: int,
    max_attempts: int,
) -> str | None:
    """Decide the single maintenance action for one non-terminal command. PURE.

    Returns one of:
    - ``"expire"``  — TTL lapsed (``expires_at <= now``); applies to a ``pending``
      row the agent never picked up OR a ``picked_up`` row whose whole window ran
      out. Expiry OUTRANKS redelivery: a command past its TTL must not be
      re-queued into a still-expired loop.
    - ``"exhaust"`` — a ``picked_up`` delivery went unacked past its lease AND has
      already been delivered ``max_attempts`` times: give up (→ ``failed``).
    - ``"redeliver"`` — a ``picked_up`` delivery went unacked past its lease with
      attempts left: re-queue (→ ``pending``) for the next poll (at-least-once).
    - ``None`` — nothing to do (terminal, or still within TTL and lease).

    ``lease_seconds`` is how long a delivered-but-unacked command is presumed
    in-flight before the agent is assumed to have dropped it; ``ack`` heartbeats
    push ``picked_up_at`` forward so a genuinely-working slow command is never
    reclaimed."""
    if status not in ("pending", "picked_up"):
        return None
    if expires_at <= now:
        return "expire"
    if status == "picked_up" and picked_up_at is not None:
        if picked_up_at <= now - timedelta(seconds=lease_seconds):
            return "exhaust" if attempts >= max_attempts else "redeliver"
    return None


async def run_agent_command_sweep(
    session: Any,
    *,
    now: Any,
    lease_seconds: int,
    max_attempts: int,
    limit: int = 500,
) -> dict[str, int]:
    """Apply :func:`sweep_decision` to the actionable non-terminal commands in one
    transaction (P10-T1 TTL sweep; research §3.1 "flip stale pending → expired"
    + at-least-once redelivery). Bounded: at most ``limit`` rows per run, selected
    ``FOR UPDATE SKIP LOCKED`` so concurrent sweeps/polls never block each other.
    Returns per-action counts. Idempotent: a row transitions at most once per tick
    and terminal rows are never touched."""
    from sqlalchemy import and_, or_, select

    from filearr.models import AgentCommand

    lease_cutoff = now - timedelta(seconds=lease_seconds)
    rows = (
        await session.execute(
            select(AgentCommand)
            .where(
                AgentCommand.status.in_(("pending", "picked_up")),
                or_(
                    AgentCommand.expires_at <= now,
                    and_(
                        AgentCommand.status == "picked_up",
                        AgentCommand.picked_up_at <= lease_cutoff,
                    ),
                ),
            )
            .order_by(AgentCommand.expires_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    counts = {"expired": 0, "redelivered": 0, "exhausted": 0}
    for cmd in rows:
        decision = sweep_decision(
            status=cmd.status,
            expires_at=cmd.expires_at,
            picked_up_at=cmd.picked_up_at,
            attempts=cmd.attempts,
            now=now,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        if decision is None:
            continue
        if decision == "expire":
            cmd.status = command_state_machine(cmd.status, "expire")
            cmd.completed_at = now
            counts["expired"] += 1
        elif decision == "exhaust":
            cmd.status = command_state_machine(cmd.status, "fail")
            cmd.result = {"error": "max_delivery_attempts", "attempts": cmd.attempts}
            cmd.completed_at = now
            counts["exhausted"] += 1
        elif decision == "redeliver":
            cmd.status = command_state_machine(cmd.status, "redeliver")
            cmd.picked_up_at = None
            counts["redelivered"] += 1
        cmd.updated_at = now
    await session.commit()
    return counts
