"""Shared JIT-provisioning helpers for the federated identity providers
(P6-T5 OIDC, P6-T6 LDAP).

Both providers converge on the SAME two security-sensitive primitives — a
collision-safe username and a source-scoped ``principal_groups`` name-match sync
— so they live here once rather than being copy-pasted per provider (a drift in
the *removal-scope* rule between providers would be a silent authorization bug).

The load-bearing invariant preserved here (research §2.1, brief R4):

* A ``principal_groups`` row whose NAME matches an asserted external group is
  JOINED, regardless of its ``source`` (an admin may pre-create a local group and
  let the IdP populate it).
* Removal is scoped to ``source == <this provider>`` groups ONLY. An external
  group the user no longer has is dropped; a ``source='local'`` membership (an
  admin's manual grant) is NEVER auto-removed, and one provider never prunes
  another provider's groups.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


async def unique_username(session: AsyncSession, base: str) -> str:
    """A lower-cased username derived from ``base`` with a numeric collision
    suffix so a JIT-provisioned account never clashes with an existing one."""
    from filearr.models import User

    candidate = (base or "user").strip().lower() or "user"
    candidate = candidate[:60]  # avoid pathological length; usernames are free text
    n = 0
    while True:
        name = candidate if n == 0 else f"{candidate}{n}"
        exists = (
            await session.execute(select(User).where(User.username == name))
        ).scalar_one_or_none()
        if exists is None:
            return name
        n += 1


async def sync_external_groups(
    session: AsyncSession,
    principal_id,
    external_names: tuple[str, ...],
    *,
    source: str,
) -> bool:
    """Reconcile a principal's group memberships against the external group NAMES
    asserted by ``source`` ('oidc' | 'ldap' | 'saml').

    See the module docstring for the add-any / remove-only-own-source rule.
    Returns True if anything changed (→ the caller bumps the grant-cache
    generation)."""
    from filearr.models import PrincipalGroup, PrincipalGroupMember

    desired = {n for n in external_names if n}
    groups = (await session.execute(select(PrincipalGroup))).scalars().all()
    by_id = {g.id: g for g in groups}
    matched_ids = {g.id for g in groups if g.name in desired}

    current_ids = set(
        (
            await session.execute(
                select(PrincipalGroupMember.group_id).where(
                    PrincipalGroupMember.principal_id == principal_id
                )
            )
        ).scalars().all()
    )

    changed = False
    for gid in matched_ids - current_ids:
        session.add(PrincipalGroupMember(principal_id=principal_id, group_id=gid))
        changed = True
    for gid in current_ids:
        g = by_id.get(gid)
        if g is not None and g.source == source and gid not in matched_ids:
            await session.execute(
                delete(PrincipalGroupMember).where(
                    PrincipalGroupMember.principal_id == principal_id,
                    PrincipalGroupMember.group_id == gid,
                )
            )
            changed = True
    return changed
