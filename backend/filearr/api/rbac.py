"""RBAC admin surface — groups, memberships, path grants, and a decision preview
(Phase 6, P6-T2). All endpoints require the ``admin`` scope.

This round **builds, stores, and evaluates** the two-layer RBAC model; it does
NOT yet enforce it on data endpoints (that is P6-T4's ``require_permission``).
The load-bearing surface here is ``GET /rbac/preview``: it runs the exact pure
``rbac.evaluate`` engine over the real stored grants, returning the winning grant
for auditability — the correctness contract P6-T4 will trust.

Grants are never created from raw ltree supplied by a client: the caller gives a
``(library_id, rel_path-prefix)`` pair and the server encodes it via
``rbac.path_to_ltree`` (P6-T2a directives baked in). ``action`` is validated
against ``rbac.ACTIONS`` and, for a principal subject, against the principal's
global-role ceiling at CREATION time (so grant-time and evaluation-time ceiling
checks agree — brief §2.5)."""

from __future__ import annotations

import unicodedata
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, grant_cache, rbac
from filearr.db import get_session
from filearr.models import (
    Library,
    PathGrant,
    Principal,
    PrincipalGroup,
    PrincipalGroupMember,
    User,
)
from filearr.security import require_scope

router = APIRouter()

SubjectKind = Literal["principal", "group"]
Effect = Literal["allow", "deny"]


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class GroupCreateIn(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None


class GroupPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    source: str
    member_count: int = 0


class MemberIn(BaseModel):
    principal_id: uuid.UUID


class MemberOut(BaseModel):
    principal_id: uuid.UUID
    username: str | None
    global_role: str | None


class GrantCreateIn(BaseModel):
    subject_kind: SubjectKind
    subject_id: uuid.UUID
    library_id: uuid.UUID
    # A rel_path PREFIX (folder) the grant covers; empty = whole library root.
    rel_path: str = ""
    action: str
    effect: Effect = "allow"


class GrantOut(BaseModel):
    id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    subject_label: str | None
    library_id: uuid.UUID
    scope: str
    action: str
    effect: str
    #: Non-blocking advisories (P6-T4 / R7): e.g. an NFC/NFD-sibling scope already
    #: exists as a separately-encoded grant (byte-exact encoding = distinct ACL).
    warnings: list[str] = []


class PreviewOut(BaseModel):
    allowed: bool
    reason: str
    action: str
    role: str
    item_scope: str
    winning_grant: GrantWinner | None = None


class GrantWinner(BaseModel):
    scope: str
    action: str
    effect: str
    subject_kind: str | None
    subject_id: str | None


PreviewOut.model_rebuild()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _scope_for(library_id: uuid.UUID, rel_path: str) -> str:
    """Server-side scope encoding — a client never sends raw ltree. Empty /
    root ``rel_path`` yields the bare ``lib_<hex>`` library-root scope."""
    rel = (rel_path or "").strip("/")
    if not rel:
        return rbac.library_label(library_id)
    return rbac.path_to_ltree(rel, library_id=library_id)


def _decode_scope_relpath(scope: str, library_id: uuid.UUID) -> str | None:
    """Best-effort inverse of :func:`_scope_for`: recover the raw ``rel_path`` a
    stored ltree ``scope`` encodes (dropping the ``lib_<hex>`` head label). Used
    only for the NFC/NFD-sibling advisory. Returns ``None`` when any label is a
    one-way hash sentinel (``h__…``) — an over-long segment that can't round-trip,
    so no meaningful sibling comparison is possible."""
    labels = scope.split(".")
    head = rbac.library_label(library_id)
    if labels and labels[0] == head:
        labels = labels[1:]
    parts: list[str] = []
    for lbl in labels:
        dec = rbac.decode_path_label(lbl)
        if dec == rbac.HASHED_LABEL:
            return None
        parts.append(dec)
    return "/".join(parts)


async def _nfc_sibling_warnings(
    session: AsyncSession,
    *,
    subject_kind: str,
    subject_id: uuid.UUID,
    library_id: uuid.UUID,
    new_rel: str,
) -> list[str]:
    """R7 / T2a directive: warn (never block) when the new grant's decoded path is
    an NFC/NFD sibling of an existing grant's path for the same subject+library —
    visually identical, byte-distinct, therefore SEPARATE ACL scopes (fails
    closed, but confusing). Byte-exact encoding is intentional (no normalization);
    this surfaces the ambiguity so an admin can reconcile."""
    new_raw = (new_rel or "").strip("/")
    new_norm = unicodedata.normalize("NFC", new_raw)
    rows = (
        await session.execute(
            select(PathGrant).where(
                PathGrant.subject_kind == subject_kind,
                PathGrant.subject_id == subject_id,
                PathGrant.library_id == library_id,
            )
        )
    ).scalars().all()
    warnings: list[str] = []
    for r in rows:
        other = _decode_scope_relpath(r.scope, library_id)
        if other is None:
            continue
        if other != new_raw and unicodedata.normalize("NFC", other) == new_norm:
            warnings.append(
                f"path '{new_rel}' is a Unicode NFC/NFD sibling of existing grant "
                f"{r.id} ('{other}') — they encode to DIFFERENT ltree scopes and "
                "are separate ACLs; reconcile if they were meant to be one folder"
            )
    return warnings


async def _subject_label(session: AsyncSession, kind: str, sid: uuid.UUID) -> str | None:
    if kind == "group":
        g = await session.get(PrincipalGroup, sid)
        return g.name if g else None
    user = (
        await session.execute(select(User).where(User.principal_id == sid))
    ).scalar_one_or_none()
    return user.username if user else None


async def _resolve_principal_grants(
    session: AsyncSession, principal_id: uuid.UUID
) -> tuple[rbac.Role, list[rbac.PathGrant]]:
    """Load a principal's effective grants (its own direct grants + those of
    every group it belongs to) as pure ``rbac.PathGrant`` objects, plus its
    global role. This is the exact input ``rbac.evaluate`` consumes."""
    principal = await session.get(Principal, principal_id)
    if principal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Principal not found")
    try:
        role = rbac.Role(principal.global_role)
    except ValueError:
        role = rbac.Role.VIEWER  # unknown role -> most restrictive (fail closed)
    group_ids = (
        await session.execute(
            select(PrincipalGroupMember.group_id).where(
                PrincipalGroupMember.principal_id == principal_id
            )
        )
    ).scalars().all()
    conds = [
        and_(PathGrant.subject_kind == "principal", PathGrant.subject_id == principal_id)
    ]
    if group_ids:
        conds.append(
            and_(PathGrant.subject_kind == "group", PathGrant.subject_id.in_(group_ids))
        )
    rows = (await session.execute(select(PathGrant).where(or_(*conds)))).scalars().all()
    grants = [
        rbac.PathGrant(
            path=r.scope,
            action=r.action,
            allow=(r.effect == "allow"),
            group_ref=str(r.subject_id) if r.subject_kind == "group" else None,
            principal_ref=str(r.subject_id) if r.subject_kind == "principal" else None,
        )
        for r in rows
    ]
    return role, grants


# --------------------------------------------------------------------------- #
# Action vocabulary                                                            #
# --------------------------------------------------------------------------- #
@router.get("/rbac/actions", dependencies=[Depends(require_scope("admin"))])
async def list_actions() -> dict:
    """The grantable-action vocabulary + per-role ceilings (drives the UI's
    action picker and the client-side ceiling hint)."""
    return {
        "actions": sorted(rbac.ACTIONS),
        "role_ceilings": {r.value: sorted(a) for r, a in rbac.ROLE_CEILINGS.items()},
    }


# --------------------------------------------------------------------------- #
# Groups                                                                       #
# --------------------------------------------------------------------------- #
@router.get(
    "/rbac/groups",
    response_model=list[GroupOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_groups(session: AsyncSession = Depends(get_session)) -> list[GroupOut]:
    groups = (
        await session.execute(select(PrincipalGroup).order_by(PrincipalGroup.name))
    ).scalars().all()
    out: list[GroupOut] = []
    for g in groups:
        n = (
            await session.execute(
                select(PrincipalGroupMember).where(PrincipalGroupMember.group_id == g.id)
            )
        ).scalars().all()
        out.append(
            GroupOut(
                id=g.id,
                name=g.name,
                description=g.description,
                source=g.source,
                member_count=len(n),
            )
        )
    return out


@router.post(
    "/rbac/groups",
    response_model=GroupOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_group(
    payload: GroupCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    name = payload.name.strip()
    existing = (
        await session.execute(select(PrincipalGroup).where(PrincipalGroup.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Group '{name}' already exists")
    g = PrincipalGroup(name=name, description=payload.description, source="local")
    session.add(g)
    await session.commit()
    await audit.emit(
        audit.GROUP_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(g.id), "name": g.name},
    )
    return GroupOut(id=g.id, name=g.name, description=g.description, source=g.source)


@router.patch(
    "/rbac/groups/{group_id}",
    response_model=GroupOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def patch_group(
    group_id: uuid.UUID,
    payload: GroupPatchIn,
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    g = await session.get(PrincipalGroup, group_id)
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    # An LDAP/SAML/OIDC-sourced group is owned by the IdP — no local hand-edits.
    if g.source != "local":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Group is {g.source}-sourced; edit it at the IdP"
        )
    if payload.name is not None:
        g.name = payload.name.strip()
    if payload.description is not None:
        g.description = payload.description
    await session.commit()
    return GroupOut(id=g.id, name=g.name, description=g.description, source=g.source)


@router.delete(
    "/rbac/groups/{group_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_group(
    group_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    g = await session.get(PrincipalGroup, group_id)
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    # Members + group-targeted grants cascade via the FK; drop them explicitly so
    # no orphaned grant lingers pointing at a vanished group id.
    await session.execute(
        PathGrant.__table__.delete().where(
            (PathGrant.subject_kind == "group") & (PathGrant.subject_id == group_id)
        )
    )
    await session.delete(g)
    await session.commit()
    grant_cache.bump_generation()  # group grants/members vanished (P6-T4)
    await audit.emit(
        audit.GROUP_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(group_id)},
    )


@router.get(
    "/rbac/groups/{group_id}/members",
    response_model=list[MemberOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_members(
    group_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> list[MemberOut]:
    if await session.get(PrincipalGroup, group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    rows = (
        await session.execute(
            select(PrincipalGroupMember.principal_id, User.username, Principal.global_role)
            .join(Principal, Principal.id == PrincipalGroupMember.principal_id)
            .join(User, User.principal_id == Principal.id, isouter=True)
            .where(PrincipalGroupMember.group_id == group_id)
        )
    ).all()
    return [
        MemberOut(principal_id=pid, username=uname, global_role=role)
        for pid, uname, role in rows
    ]


@router.post(
    "/rbac/groups/{group_id}/members",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def add_member(
    group_id: uuid.UUID,
    payload: MemberIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    if await session.get(PrincipalGroup, group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    if await session.get(Principal, payload.principal_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Principal not found")
    existing = (
        await session.execute(
            select(PrincipalGroupMember).where(
                PrincipalGroupMember.group_id == group_id,
                PrincipalGroupMember.principal_id == payload.principal_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            PrincipalGroupMember(group_id=group_id, principal_id=payload.principal_id)
        )
        await session.commit()
        grant_cache.bump_generation()  # membership changes effective grants (P6-T4)
        await audit.emit(
            audit.GROUP_MEMBERSHIP_CHANGED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "group_id": str(group_id),
                "principal_id": str(payload.principal_id),
                "action": "add",
            },
        )


@router.delete(
    "/rbac/groups/{group_id}/members/{principal_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def remove_member(
    group_id: uuid.UUID,
    principal_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    await session.execute(
        PrincipalGroupMember.__table__.delete().where(
            (PrincipalGroupMember.group_id == group_id)
            & (PrincipalGroupMember.principal_id == principal_id)
        )
    )
    await session.commit()
    grant_cache.bump_generation()  # membership changes effective grants (P6-T4)
    await audit.emit(
        audit.GROUP_MEMBERSHIP_CHANGED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "group_id": str(group_id),
            "principal_id": str(principal_id),
            "action": "remove",
        },
    )


# --------------------------------------------------------------------------- #
# Path grants                                                                  #
# --------------------------------------------------------------------------- #
@router.get(
    "/rbac/grants",
    response_model=list[GrantOut],
    dependencies=[Depends(require_scope("admin"))],
)
async def list_grants(
    subject_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[GrantOut]:
    q = select(PathGrant).order_by(PathGrant.scope, PathGrant.action)
    if subject_id is not None:
        q = q.where(PathGrant.subject_id == subject_id)
    if library_id is not None:
        q = q.where(PathGrant.library_id == library_id)
    rows = (await session.execute(q)).scalars().all()
    out: list[GrantOut] = []
    for r in rows:
        out.append(
            GrantOut(
                id=r.id,
                subject_kind=r.subject_kind,
                subject_id=r.subject_id,
                subject_label=await _subject_label(session, r.subject_kind, r.subject_id),
                library_id=r.library_id,
                scope=r.scope,
                action=r.action,
                effect=r.effect,
            )
        )
    return out


@router.post(
    "/rbac/grants",
    response_model=GrantOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_grant(
    payload: GrantCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> GrantOut:
    if payload.action not in rbac.ACTIONS:
        raise HTTPException(
            422,
            f"Unknown action '{payload.action}'; must be one of {sorted(rbac.ACTIONS)}",
        )
    if await session.get(Library, payload.library_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Library not found")

    # Validate the subject exists, and enforce the role ceiling at CREATION time
    # for a principal subject (a viewer can never be granted `modify` — brief
    # §2.5). A group subject's ceiling can only be enforced per-member at
    # evaluation (a group has no single role), so we validate existence only.
    if payload.subject_kind == "principal":
        principal = await session.get(Principal, payload.subject_id)
        if principal is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Principal subject not found")
        try:
            role = rbac.Role(principal.global_role)
        except ValueError:
            role = rbac.Role.VIEWER
        if payload.action not in rbac.ROLE_CEILINGS.get(role, frozenset()):
            raise HTTPException(
                422,
                f"Role '{role.value}' cannot be granted '{payload.action}' "
                "(exceeds its ceiling)",
            )
    else:
        if await session.get(PrincipalGroup, payload.subject_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Group subject not found")

    scope = _scope_for(payload.library_id, payload.rel_path)
    warnings = await _nfc_sibling_warnings(
        session,
        subject_kind=payload.subject_kind,
        subject_id=payload.subject_id,
        library_id=payload.library_id,
        new_rel=payload.rel_path,
    )
    grant = PathGrant(
        subject_kind=payload.subject_kind,
        subject_id=payload.subject_id,
        library_id=payload.library_id,
        scope=scope,
        action=payload.action,
        effect=payload.effect,
    )
    session.add(grant)
    await session.commit()
    grant_cache.bump_generation()  # invalidate cached grant sets (P6-T4)
    await audit.emit(
        audit.GRANT_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "grant_id": str(grant.id),
            "subject_kind": grant.subject_kind,
            "subject_id": str(grant.subject_id),
            "library_id": str(grant.library_id),
            "scope": grant.scope,
            "action": grant.action,
            "effect": grant.effect,
        },
    )
    return GrantOut(
        id=grant.id,
        subject_kind=grant.subject_kind,
        subject_id=grant.subject_id,
        subject_label=await _subject_label(session, grant.subject_kind, grant.subject_id),
        library_id=grant.library_id,
        scope=grant.scope,
        action=grant.action,
        effect=grant.effect,
        warnings=warnings,
    )


@router.delete(
    "/rbac/grants/{grant_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_grant(
    grant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    g = await session.get(PathGrant, grant_id)
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grant not found")
    await session.delete(g)
    await session.commit()
    grant_cache.bump_generation()  # invalidate cached grant sets (P6-T4)
    await audit.emit(
        audit.GRANT_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"grant_id": str(grant_id)},
    )


# --------------------------------------------------------------------------- #
# Decision preview (the correctness surface P6-T4 will trust)                  #
# --------------------------------------------------------------------------- #
@router.get(
    "/rbac/preview",
    response_model=PreviewOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def preview(
    principal: uuid.UUID,
    library: uuid.UUID,
    path: str,
    action: str,
    session: AsyncSession = Depends(get_session),
) -> PreviewOut:
    """Evaluate the effective permission for ``(principal, library+path, action)``
    against the REAL stored grants, using the pure ``rbac.evaluate`` engine.
    Returns the decision + the winning grant (auditability)."""
    if action not in rbac.ACTIONS:
        raise HTTPException(
            422, f"Unknown action '{action}'"
        )
    role, grants = await _resolve_principal_grants(session, principal)
    item_scope = _scope_for(library, path)
    decision = rbac.evaluate(grants, role, item_scope, action)
    winner: GrantWinner | None = None
    if decision.grant is not None:
        g = decision.grant
        sid = g.group_ref or g.principal_ref
        winner = GrantWinner(
            scope=g.path,
            action=g.action,
            effect="allow" if g.allow else "deny",
            subject_kind="group" if g.group_ref else ("principal" if g.principal_ref else None),
            subject_id=sid,
        )
    return PreviewOut(
        allowed=decision.allowed,
        reason=decision.reason,
        action=action,
        role=role.value,
        item_scope=item_scope,
        winning_grant=winner,
    )
