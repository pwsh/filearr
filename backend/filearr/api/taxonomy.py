"""W8-A — File Extension Similarity Taxonomy admin CRUD + read tree.

The DB-backed, editable taxonomy that REPLACES ``media_type`` (W8-B removes
media_type and routes extraction off a category's ``extractor``; W8-C ships the
frontend editor against THIS frozen API shape). Reads require the ``read`` scope;
every mutation requires ``admin``, is audited via ``security_events``, and bumps
``taxonomy_state.version`` (invalidating the :mod:`filearr.taxonomy` cache) so a
running worker's next classification sees the edit.

FROZEN API shape (W8-C builds against it — do not break):

    GET    /api/v1/taxonomy
             -> {"version": N, "tree": [{"category": {...}, "groups": [...]}]}
    POST   /api/v1/taxonomy/categories                 (create)
    PATCH  /api/v1/taxonomy/categories/{key}           (label/description/extractor/sort_order)
    DELETE /api/v1/taxonomy/categories/{key}           (refused if it still has groups)
    POST   /api/v1/taxonomy/groups                     (create; category by key)
    PATCH  /api/v1/taxonomy/groups/{key}               (label/description/category/sort_order)
    DELETE /api/v1/taxonomy/groups/{key}               (cascades its extension rows)
    POST   /api/v1/taxonomy/groups/{key}/extensions    (add; UPSERT reparents, returns prior group)
    DELETE /api/v1/taxonomy/extensions/{ext}           (remove)

Decisions (documented, per the task):
* **Category delete** is REFUSED (409) while it still parents groups (ON DELETE
  RESTRICT at the DB too) — reparent or delete the groups first.
* **Group delete** is ALLOWED even if items reference the group: items are re-derived
  on the next rescan, so a stale ``items.file_group`` string is self-healing. The
  delete CASCADEs the group's extension rows.
* **Extension add** is an UPSERT: adding an ext already mapped to another group
  REPARENTS it (an ext belongs to exactly one group) and returns the prior group.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, taxonomy
from filearr.db import get_session
from filearr.models import FileCategoryModel, FileGroupExtension, FileGroupModel
from filearr.security import require_scope

router = APIRouter()

# A category/group key is a slug: lowercase, starts alnum, then alnum or hyphen.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# A bare extension: lowercase alnum plus a few punctuation chars the research map
# actually uses (``c++`` / ``x_t`` / ``dvr-ms``). NO dots, NO spaces, NO uppercase.
_EXT_RE = re.compile(r"^[a-z0-9_+-]{1,32}$")
# The extraction pipelines a category may route to (W8-B). NULL = no extractor.
_EXTRACTORS = frozenset({"image", "audio", "video", "document", "model3d"})


def _valid_key(key: str) -> str:
    if not _KEY_RE.match(key or ""):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "key must be a lowercase slug (alnum + hyphen, <=64 chars)",
        )
    return key


def _valid_ext(ext: str) -> str:
    e = (ext or "").strip().lower().lstrip(".")
    if not _EXT_RE.match(e):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "ext must be lowercase alnum plus _ + - (no dots/spaces), 1-32 chars",
        )
    return e


def _valid_extractor(extractor: str | None) -> str | None:
    if extractor is None:
        return None
    if extractor not in _EXTRACTORS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"extractor must be one of {sorted(_EXTRACTORS)} or null",
        )
    return extractor


# --------------------------------------------------------------------------- #
# Pydantic bodies                                                              #
# --------------------------------------------------------------------------- #
class CategoryCreate(BaseModel):
    key: str
    label: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    extractor: str | None = None
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    # ``extractor`` uses present-vs-absent semantics (model_dump(exclude_unset)):
    # omit to leave unchanged, send ``null`` to CLEAR it, send a value to set it.
    extractor: str | None = None
    sort_order: int | None = None


class GroupCreate(BaseModel):
    key: str
    label: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    category: str  # parent category KEY
    sort_order: int = 0


class GroupUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = None  # move to a different parent category (by key)
    sort_order: int | None = None


class ExtensionAdd(BaseModel):
    ext: str


# --------------------------------------------------------------------------- #
# Serialization helpers                                                        #
# --------------------------------------------------------------------------- #
def _cat_out(c: FileCategoryModel) -> dict:
    return {
        "key": c.key,
        "label": c.label,
        "description": c.description or "",
        "extractor": c.extractor,
        "sort_order": c.sort_order,
        "is_builtin": bool(c.is_builtin),
    }


def _group_out(g: FileGroupModel, category_key: str) -> dict:
    return {
        "key": g.key,
        "label": g.label,
        "description": g.description or "",
        "category": category_key,
        "sort_order": g.sort_order,
        "is_builtin": bool(g.is_builtin),
    }


async def _get_category(session: AsyncSession, key: str) -> FileCategoryModel:
    c = (
        await session.execute(
            select(FileCategoryModel).where(FileCategoryModel.key == key)
        )
    ).scalar_one_or_none()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such category")
    return c


async def _get_group(session: AsyncSession, key: str) -> FileGroupModel:
    g = (
        await session.execute(
            select(FileGroupModel).where(FileGroupModel.key == key)
        )
    ).scalar_one_or_none()
    if g is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such group")
    return g


async def _finish(session: AsyncSession) -> int:
    """Bump the taxonomy version (cache-invalidation token) and commit. Returns the
    new version. Called by every mutation after its edit."""
    new_version = await taxonomy.bump_version(session)
    await session.commit()
    return new_version


# --------------------------------------------------------------------------- #
# Read                                                                          #
# --------------------------------------------------------------------------- #
@router.get("/taxonomy", dependencies=[Depends(require_scope("read"))])
async def get_taxonomy(session: AsyncSession = Depends(get_session)) -> dict:
    """The full live taxonomy tree (read scope) — ``{version, tree:[{category,
    groups}]}``. This is the editable successor to ``GET /system/file-groups``
    (which stays, serving the immutable SEED registry)."""
    return await taxonomy.tree(session)


# --------------------------------------------------------------------------- #
# Category CRUD                                                                 #
# --------------------------------------------------------------------------- #
@router.post(
    "/taxonomy/categories",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_category(
    body: CategoryCreate, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    key = _valid_key(body.key)
    extractor = _valid_extractor(body.extractor)
    c = FileCategoryModel(
        key=key,
        label=body.label,
        description=body.description or "",
        extractor=extractor,
        sort_order=body.sort_order,
        is_builtin=False,
    )
    session.add(c)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "category key already exists") from None
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_CATEGORY_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"key": key, "extractor": extractor, "version": version},
    )
    return _cat_out(c)


@router.patch(
    "/taxonomy/categories/{key}",
    dependencies=[Depends(require_scope("admin"))],
)
async def update_category(
    key: str,
    body: CategoryUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    c = await _get_category(session, key)
    fields = body.model_dump(exclude_unset=True)
    if "label" in fields and fields["label"] is not None:
        c.label = fields["label"]
    if "description" in fields and fields["description"] is not None:
        c.description = fields["description"]
    if "extractor" in fields:  # present => set (null clears)
        c.extractor = _valid_extractor(fields["extractor"])
    if "sort_order" in fields and fields["sort_order"] is not None:
        c.sort_order = fields["sort_order"]
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_CATEGORY_UPDATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"key": key, "version": version},
    )
    return _cat_out(c)


@router.delete(
    "/taxonomy/categories/{key}",
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_category(
    key: str, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    c = await _get_category(session, key)
    n_groups = (
        await session.execute(
            select(func.count())
            .select_from(FileGroupModel)
            .where(FileGroupModel.category_id == c.id)
        )
    ).scalar_one()
    if n_groups:
        # Decision: refuse while it still parents groups (DB FK is ON DELETE
        # RESTRICT too) — reparent or delete them first.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"category still has {n_groups} group(s); reparent or delete them first",
        )
    await session.delete(c)
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_CATEGORY_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"key": key, "version": version},
    )
    return {"deleted": key, "version": version}


# --------------------------------------------------------------------------- #
# Group CRUD                                                                    #
# --------------------------------------------------------------------------- #
@router.post(
    "/taxonomy/groups",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_group(
    body: GroupCreate, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    key = _valid_key(body.key)
    category = await _get_category(session, body.category)
    g = FileGroupModel(
        key=key,
        label=body.label,
        description=body.description or "",
        category_id=category.id,
        sort_order=body.sort_order,
        is_builtin=False,
    )
    session.add(g)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "group key already exists") from None
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_GROUP_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"key": key, "category": category.key, "version": version},
    )
    return _group_out(g, category.key)


@router.patch(
    "/taxonomy/groups/{key}",
    dependencies=[Depends(require_scope("admin"))],
)
async def update_group(
    key: str,
    body: GroupUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    g = await _get_group(session, key)
    fields = body.model_dump(exclude_unset=True)
    if "label" in fields and fields["label"] is not None:
        g.label = fields["label"]
    if "description" in fields and fields["description"] is not None:
        g.description = fields["description"]
    if "sort_order" in fields and fields["sort_order"] is not None:
        g.sort_order = fields["sort_order"]
    category_key = None
    if fields.get("category") is not None:
        category = await _get_category(session, fields["category"])
        g.category_id = category.id
        category_key = category.key
    if category_key is None:
        category_key = (await _get_category_by_id(session, g.category_id)).key
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_GROUP_UPDATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"key": key, "category": category_key, "version": version},
    )
    return _group_out(g, category_key)


@router.delete(
    "/taxonomy/groups/{key}",
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_group(
    key: str, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    g = await _get_group(session, key)
    # Allowed even if items reference the group (re-derived on rescan); the FK
    # CASCADEs the group's extension rows.
    await session.delete(g)
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_GROUP_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"key": key, "version": version},
    )
    return {"deleted": key, "version": version}


async def _get_category_by_id(session: AsyncSession, category_id) -> FileCategoryModel:
    c = (
        await session.execute(
            select(FileCategoryModel).where(FileCategoryModel.id == category_id)
        )
    ).scalar_one_or_none()
    if c is None:  # pragma: no cover — FK guarantees the parent exists
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "orphaned group")
    return c


# --------------------------------------------------------------------------- #
# Extension ops                                                                 #
# --------------------------------------------------------------------------- #
@router.post(
    "/taxonomy/groups/{key}/extensions",
    dependencies=[Depends(require_scope("admin"))],
)
async def add_extension(
    key: str,
    body: ExtensionAdd,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Add ``ext`` to group ``key``. UPSERT: an ext already mapped to ANOTHER group
    is REPARENTED here (an ext belongs to exactly one group) and its prior group is
    returned as ``previous_group``. A no-op re-add (already in this group) returns
    ``previous_group == key`` and still bumps the version (idempotent)."""
    g = await _get_group(session, key)
    ext = _valid_ext(body.ext)
    existing = (
        await session.execute(
            select(FileGroupExtension).where(FileGroupExtension.ext == ext)
        )
    ).scalar_one_or_none()
    previous_group: str | None = None
    if existing is None:
        session.add(FileGroupExtension(ext=ext, group_id=g.id))
    else:
        prev = await _get_group_by_id(session, existing.group_id)
        previous_group = prev.key if prev else None
        existing.group_id = g.id  # reparent (no-op if already this group)
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_EXTENSION_ADDED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "ext": ext,
            "group": key,
            "previous_group": previous_group,
            "version": version,
        },
    )
    return {"ext": ext, "group": key, "previous_group": previous_group, "version": version}


@router.delete(
    "/taxonomy/extensions/{ext}",
    dependencies=[Depends(require_scope("admin"))],
)
async def remove_extension(
    ext: str, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    e = _valid_ext(ext)
    row = (
        await session.execute(
            select(FileGroupExtension).where(FileGroupExtension.ext == e)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such extension")
    await session.delete(row)
    version = await _finish(session)
    await audit.emit(
        audit.TAXONOMY_EXTENSION_REMOVED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"ext": e, "version": version},
    )
    return {"removed": e, "version": version}


async def _get_group_by_id(session: AsyncSession, group_id) -> FileGroupModel | None:
    return (
        await session.execute(
            select(FileGroupModel).where(FileGroupModel.id == group_id)
        )
    ).scalar_one_or_none()
