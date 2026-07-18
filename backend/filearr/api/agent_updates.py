"""P5-T7 — signed agent update manifest + staged rollout (central-side).

The central half of the agent self-updater. Central STORES and SERVES signed
release manifests + artifact binaries but is UNTRUSTED for update integrity
(research §8): it cannot re-sign a manifest (the Ed25519 private key never
reaches central), so the agent verifies the signature against its build-time
pinned public key. A compromised central therefore cannot push a wrongly-signed
binary — the worst it can do is withhold or corrupt a download, which the
agent's sha256 check catches.

Two planes, both behind ``FILEARR_AGENTS_ENABLED`` (404 when off):

* **Operator/admin plane** (``admin`` scope): upload a release (the signed
  manifest, then each artifact binary), promote canary→general (the R5 / §6.3
  operator-confirmation gate), and list releases with the per-agent
  confirmed-version rollup ("which version has each agent confirmed").
* **Agent plane** (``_authenticate_agent`` reused from ``api.agent_commands`` —
  interim bearer / mTLS-header per ``FILEARR_AGENT_AUTH_MODE``): fetch the newest
  covering manifest for THIS agent, and download an artifact by filename (served
  ONLY when listed in the stored manifest — no path traversal).

Upload is TWO-PHASE (no multipart dependency, friendlier to large binaries):
``POST /agent-releases`` registers the signed manifest; ``PUT
/agent-releases/{version}/artifacts/{filename}`` streams each binary (verified
against the manifest sha256/size). A release is only OFFERED / PROMOTABLE once
every manifest artifact is present on disk.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit
from filearr.api.agent_commands import _authenticate_agent
from filearr.api.agents import require_agents_enabled
from filearr.config import Settings, get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentRelease
from filearr.security import require_scope

router = APIRouter()

# A version/filename that will become a filesystem path component must be a plain
# token — no separators, no traversal. Both are operator/manifest-controlled, but
# validated defensively regardless (the download endpoint's primary defence is
# the "must be in the manifest" check; this is belt-and-braces).
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")


# --------------------------------------------------------------------------- #
# Release storage helpers                                                      #
# --------------------------------------------------------------------------- #
def _releases_root(settings: Settings) -> Path:
    base = settings.agent_releases_dir or f"{settings.config_dir}/agent-releases"
    return Path(base)


def _release_dir(settings: Settings, version: str) -> Path:
    return _releases_root(settings) / version


def _artifact_path(settings: Settings, version: str, filename: str) -> Path:
    """Resolve a release artifact path, refusing any traversal. ``version`` and
    ``filename`` MUST already have passed the safe-token check."""
    root = _release_dir(settings, version).resolve()
    target = (root / filename).resolve()
    if root != target.parent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid artifact path")
    return target


def _manifest_artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    arts = manifest.get("artifacts")
    return arts if isinstance(arts, list) else []


def _release_ready(settings: Settings, rel: AgentRelease) -> bool:
    """True when every artifact named in the stored manifest exists on disk — the
    precondition for OFFERING the release to an agent or PROMOTING it."""
    arts = _manifest_artifacts(rel.manifest)
    if not arts:
        return False
    for a in arts:
        name = a.get("url")
        if not isinstance(name, str) or not _SAFE_FILENAME.match(name):
            return False
        if not _artifact_path(settings, rel.version, name).exists():
            return False
    return True


def _version_newer(candidate: str, current: str) -> bool:
    """Semver-ish "is candidate strictly newer than current". Mirrors the Go
    ``update.CompareVersions`` rule byte-for-byte (documented there): strip a
    leading v, drop build metadata after '+', split release/prerelease on the
    first '-', compare release components numerically (missing == 0, non-numeric
    falls back to string), and rank a prerelease BELOW the equivalent release."""
    return _compare_versions(candidate, current) > 0


def _compare_versions(a: str, b: str) -> int:
    ar, ap = _split_version(a)
    br, bp = _split_version(b)
    c = _compare_release(ar, br)
    if c != 0:
        return c
    if ap == "" and bp == "":
        return 0
    if ap == "":
        return 1
    if bp == "":
        return -1
    return (ap > bp) - (ap < bp)


def _split_version(v: str) -> tuple[str, str]:
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    v = v.split("+", 1)[0]
    if "-" in v:
        rel, pre = v.split("-", 1)
        return rel, pre
    return v, ""


def _compare_release(a: str, b: str) -> int:
    ap = a.split(".")
    bp = b.split(".")
    for i in range(max(len(ap), len(bp))):
        as_ = ap[i] if i < len(ap) else "0"
        bs_ = bp[i] if i < len(bp) else "0"
        if as_.isdigit() and bs_.isdigit():
            an, bn = int(as_), int(bs_)
            if an != bn:
                return (an > bn) - (an < bn)
        elif as_ != bs_:
            return (as_ > bs_) - (as_ < bs_)
    return 0


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class ReleaseOut(BaseModel):
    id: uuid.UUID
    version: str
    stage: str
    created_at: datetime
    promoted_at: datetime | None
    artifacts: list[dict[str, Any]]
    ready: bool
    confirmed_count: int

    @classmethod
    def of(cls, rel: AgentRelease, ready: bool, confirmed: int) -> ReleaseOut:
        return cls(
            id=rel.id,
            version=rel.version,
            stage=rel.stage,
            created_at=rel.created_at,
            promoted_at=rel.promoted_at,
            artifacts=_manifest_artifacts(rel.manifest),
            ready=ready,
            confirmed_count=confirmed,
        )


class AgentVersionOut(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str
    rollout_group: str
    agent_version: str | None
    last_seen_at: datetime | None


class ReleaseListOut(BaseModel):
    releases: list[ReleaseOut]
    agents: list[AgentVersionOut]


# --------------------------------------------------------------------------- #
# Operator/admin plane                                                         #
# --------------------------------------------------------------------------- #
@router.post(
    "/agent-releases",
    response_model=ReleaseOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def register_release(
    manifest: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReleaseOut:
    """Register a SIGNED release manifest (phase 1 of upload). The manifest is
    stored VERBATIM (including its ``signature``); central never validates the
    signature (it holds no key). ``stage`` is forced to ``canary``. Artifacts are
    uploaded next via PUT. A duplicate version is a 409 (releases are immutable —
    re-cut a new version rather than mutating one)."""
    version = manifest.get("version")
    if not isinstance(version, str) or not _SAFE_VERSION.match(version):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "manifest version missing or invalid")
    if not manifest.get("signature"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "manifest is unsigned")
    arts = _manifest_artifacts(manifest)
    if not arts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "manifest lists no artifacts")
    for a in arts:
        name = a.get("url")
        if not isinstance(name, str) or not _SAFE_FILENAME.match(name):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid artifact filename: {name!r}")
        if not isinstance(a.get("sha256"), str):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "artifact missing sha256")

    existing = (
        await session.execute(select(AgentRelease).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"release {version} already exists")

    settings = get_settings()
    _release_dir(settings, version).mkdir(parents=True, exist_ok=True)

    rel = AgentRelease(version=version, stage="canary", manifest=manifest)
    session.add(rel)
    await session.commit()
    await audit.emit(
        audit.AGENT_RELEASE_UPLOADED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"version": version, "stage": "canary", "artifacts": len(arts)},
    )
    return ReleaseOut.of(rel, _release_ready(settings, rel), 0)


@router.put(
    "/agent-releases/{version}/artifacts/{filename}",
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def upload_artifact(
    version: str,
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Stream one release artifact binary (phase 2). The raw request body is the
    file. The filename MUST be listed in the release's manifest; the streamed
    bytes are verified against the manifest's sha256 + size (mismatch → 400 and
    the partial file is removed). Idempotent (re-upload overwrites)."""
    if not _SAFE_VERSION.match(version) or not _SAFE_FILENAME.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid version or filename")
    rel = (
        await session.execute(select(AgentRelease).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    if rel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release")
    art = next(
        (a for a in _manifest_artifacts(rel.manifest) if a.get("url") == filename), None
    )
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "filename not in manifest")

    settings = get_settings()
    dest = _artifact_path(settings, version, filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    max_bytes = settings.agent_update_max_artifact_bytes
    h = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "artifact too large"
                    )
                h.update(chunk)
                fh.write(chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    got = h.hexdigest()
    want = str(art.get("sha256", "")).lower()
    declared = art.get("size")
    if got != want or (isinstance(declared, int) and declared != size):
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"artifact does not match manifest (sha256 got {got}, want {want}; size {size})",
        )
    tmp.replace(dest)
    return {"version": version, "filename": filename, "size": size, "sha256": got}


@router.post(
    "/agent-releases/{version}/promote",
    response_model=ReleaseOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def promote_release(
    version: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReleaseOut:
    """Promote a canary release to general (R5 / §6.3 operator-confirmation gate):
    the whole fleet sees it after this. 409 if already general or if artifacts are
    not all uploaded yet (a half-uploaded release must not go fleet-wide)."""
    rel = (
        await session.execute(select(AgentRelease).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    if rel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release")
    if rel.stage == "general":
        raise HTTPException(status.HTTP_409_CONFLICT, "release already general")
    settings = get_settings()
    if not _release_ready(settings, rel):
        raise HTTPException(status.HTTP_409_CONFLICT, "release artifacts incomplete")
    rel.stage = "general"
    rel.promoted_at = datetime.now(UTC)
    await session.commit()
    await audit.emit(
        audit.AGENT_RELEASE_PROMOTED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"version": version, "stage": "general"},
    )
    confirmed = await _confirmed_count(session, rel.version)
    return ReleaseOut.of(rel, True, confirmed)


@router.get(
    "/agent-releases",
    response_model=ReleaseListOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def list_releases(
    session: AsyncSession = Depends(get_session),
) -> ReleaseListOut:
    """List releases (newest first) with each release's confirmed-agent count, and
    the per-agent running-version rollup (§6.3: "which version has each agent
    confirmed"). ``agent_version`` is what each agent last reported running via
    its manifest poll — the confirmed-version signal."""
    settings = get_settings()
    releases = (
        await session.execute(select(AgentRelease).order_by(AgentRelease.created_at.desc()))
    ).scalars().all()
    agents = (
        await session.execute(select(Agent).where(Agent.revoked_at.is_(None)))
    ).scalars().all()

    counts: dict[str, int] = {}
    for ag in agents:
        if ag.agent_version:
            counts[ag.agent_version] = counts.get(ag.agent_version, 0) + 1

    return ReleaseListOut(
        releases=[
            ReleaseOut.of(r, _release_ready(settings, r), counts.get(r.version, 0))
            for r in releases
        ],
        agents=[
            AgentVersionOut(
                id=ag.id,
                name=ag.name,
                hostname=ag.hostname,
                rollout_group=ag.rollout_group,
                agent_version=ag.agent_version,
                last_seen_at=ag.last_seen_at,
            )
            for ag in agents
        ],
    )


async def _confirmed_count(session: AsyncSession, version: str) -> int:
    agents = (
        await session.execute(
            select(Agent).where(Agent.revoked_at.is_(None), Agent.agent_version == version)
        )
    ).scalars().all()
    return len(agents)


# --------------------------------------------------------------------------- #
# Agent plane                                                                  #
# --------------------------------------------------------------------------- #
def _covers(rel: AgentRelease, agent: Agent, canary_group: str) -> bool:
    """A general release covers every agent; a canary release covers only agents
    in the canary rollout_group (R5)."""
    if rel.stage == "general":
        return True
    return rel.stage == "canary" and agent.rollout_group == canary_group


@router.get(
    "/agents/{agent_id}/update-manifest",
    dependencies=[Depends(require_agents_enabled)],
)
async def get_update_manifest(
    agent_id: uuid.UUID,
    request: Request,
    current: str = "",
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return the newest covering release strictly newer than the agent's reported
    ``current`` version, as the stored signed manifest (the agent verifies the
    signature). 204 when up to date / nothing covers this agent / the newest
    covering release's artifacts are not all present.

    Reporting ``current`` here is ALSO the §6.3 confirmed-version signal: the
    agent's running version is recorded on ``agents.agent_version`` (+ a
    ``last_seen_at`` refresh) on every poll — a running, polling agent has by
    definition booted that version."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()

    # Record the confirmed running version + liveness (agent is demonstrably up).
    now = datetime.now(UTC)
    if current:
        agent.agent_version = current[:256]
    agent.last_seen_at = now
    await session.commit()

    releases = (
        await session.execute(select(AgentRelease).order_by(AgentRelease.created_at.desc()))
    ).scalars().all()
    for rel in releases:  # newest first
        if not _covers(rel, agent, settings.agent_canary_group):
            continue
        if current and not _version_newer(rel.version, current):
            # The newest covering release is not newer than what we run -> done.
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if not _release_ready(settings, rel):
            # Manifest registered but artifacts not fully uploaded — do not offer
            # a manifest whose download would 404. Skip to older covering ones.
            continue
        return Response(content=_manifest_json(rel.manifest), media_type="application/json")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _manifest_json(manifest: dict[str, Any]) -> bytes:
    import json

    return json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@router.get(
    "/agents/{agent_id}/releases/{version}/artifacts/{filename}",
    dependencies=[Depends(require_agents_enabled)],
)
async def download_artifact(
    agent_id: uuid.UUID,
    version: str,
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Stream a release artifact to an authenticated agent. The filename MUST be
    listed in that release's stored manifest (no path traversal — the filename is
    validated against the manifest, then resolved and confirmed to sit directly
    in the release dir)."""
    agent = await _authenticate_agent(session, agent_id, request)
    _ = agent  # authenticated; any enrolled agent may download any covering release
    if not _SAFE_VERSION.match(version) or not _SAFE_FILENAME.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid version or filename")
    rel = (
        await session.execute(select(AgentRelease).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    if rel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release")
    if not any(a.get("url") == filename for a in _manifest_artifacts(rel.manifest)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "filename not in manifest")
    settings = get_settings()
    path = _artifact_path(settings, version, filename)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not uploaded")
    return FileResponse(
        str(path), media_type="application/octet-stream", filename=filename
    )
