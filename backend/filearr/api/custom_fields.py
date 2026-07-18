"""P4-T3 — admin-scope CRUD for admin-defined custom fields.

Custom fields are the freeform, per-library-applicable extension point whose
typed values live ONLY in ``Item.user_metadata`` (invariant 2 — extractors
cannot write there, so a rescan never clobbers an admin field). This router owns
the field *definitions*; validating the values on write is P4-T4 (``api/items``).

Security note (priority: security > integrity): a field ``name`` becomes both a
JSONB key and later a Meili attribute (``cf_<name>``, P4-T6), so it is NEVER
trusted raw — every create routes the name through
``custom_fields.normalize_field_name`` (lowercase ``[a-z0-9_]``, no reserved
prefix such as ``cf_``/``_``, no collision with a core/reserved attribute)
before it touches the DB. ``data_type`` is checked against the fixed table
vocabulary ``custom_fields.CUSTOM_FIELD_TYPES``.

``name`` + ``data_type`` are IMMUTABLE after creation (a rename/retype would
orphan or misinterpret existing ``user_metadata`` values) — enforced here with a
422, not a DB trigger, so the "why" stays a readable message.

DELETE drops the definition row. Per invariant 4 this is "soft on the data": the
``custom_fields`` table has no ``deleted_at``/``enabled`` column (see migration
``e7c2b9a4d6f1``) and there is NO FK/cascade from the ``user_metadata`` JSONB
overlay to it, so removing the definition NEVER touches the values already
written under the field's key on any item — it simply stops being offered,
validated (P4-T4), or faceted (P4-T6).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.custom_fields import CUSTOM_FIELD_TYPES, normalize_field_name
from filearr.db import get_session
from filearr.models import CustomField
from filearr.schemas import CustomFieldIn, CustomFieldOut, CustomFieldUpdate
from filearr.security import require_scope

router = APIRouter()

# Fields that are IMMUTABLE after creation — sending either (even unchanged) is a
# 422, because renaming/retyping would orphan or misinterpret existing values.
_IMMUTABLE_FIELDS = ("name", "data_type")


@router.get(
    "", response_model=list[CustomFieldOut], dependencies=[Depends(require_scope("read"))]
)
async def list_custom_fields(
    applies_to: str | None = None,
    library_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[CustomField]:
    """All custom-field definitions (read scope), newest-name-stable order.

    Optional ``applies_to`` (media type) / ``library_id`` narrow the list to the
    definitions relevant to an item in view — matching the applicability rule the
    validator enforces (empty ``applies_to``/``library_ids`` = "all"). Filtering
    is done in Python (the set is small: admin-defined field definitions)."""
    rows = list(
        (await session.execute(select(CustomField).order_by(CustomField.name)))
        .scalars()
        .all()
    )
    if applies_to is not None:
        rows = [r for r in rows if not r.applies_to or applies_to in r.applies_to]
    if library_id is not None:
        rows = [
            r
            for r in rows
            if not r.library_ids or library_id in (r.library_ids or [])
        ]
    return rows


@router.post(
    "",
    response_model=CustomFieldOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_custom_field(
    body: CustomFieldIn, session: AsyncSession = Depends(get_session)
) -> CustomField:
    """Create a definition. Validates ``data_type`` against the table vocabulary
    and normalises/guards ``name`` BEFORE persisting; a duplicate name is 409."""
    if body.data_type not in CUSTOM_FIELD_TYPES:
        raise HTTPException(
            422,
            f"invalid data_type {body.data_type!r}; must be one of "
            f"{sorted(CUSTOM_FIELD_TYPES)}",
        )
    try:
        name = normalize_field_name(body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # select_options only carry meaning for a 'select' field; drop an empty list
    # to a clean NULL so the stored shape is unambiguous.
    select_options = body.select_options or None

    row = CustomField(
        name=name,
        label=body.label,
        data_type=body.data_type,
        select_options=select_options,
        applies_to=list(body.applies_to),
        library_ids=list(body.library_ids),
        facetable=body.facetable,
        sortable=body.sortable,
        required=body.required,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, f"a custom field named {name!r} already exists") from exc
    await session.refresh(row)
    return row


@router.patch(
    "/{field_id}",
    response_model=CustomFieldOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_custom_field(
    field_id: uuid.UUID,
    body: CustomFieldUpdate,
    session: AsyncSession = Depends(get_session),
) -> CustomField:
    """Edit label / applicability / faceting / required. Reject any attempt to
    change the immutable ``name`` or ``data_type`` with a 422."""
    row = (
        await session.execute(select(CustomField).where(CustomField.id == field_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "custom field not found")

    sent = body.model_fields_set
    blocked = [f for f in _IMMUTABLE_FIELDS if f in sent]
    if blocked:
        raise HTTPException(
            422,
            f"{' and '.join(blocked)} cannot be changed after creation "
            "(a rename/retype would orphan or misinterpret existing "
            "user_metadata values); create a new custom field instead",
        )

    dumped = body.model_dump(mode="json")
    for key in ("label", "select_options", "applies_to", "library_ids",
                "facetable", "sortable", "required"):
        if key in sent:
            value = dumped[key]
            if key in ("applies_to", "library_ids") and value is None:
                value = []  # NOT NULL columns; explicit null means "clear to all"
            setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/{field_id}", status_code=204, dependencies=[Depends(require_scope("admin"))]
)
async def delete_custom_field(
    field_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    """Drop the definition. Existing ``user_metadata`` values written under the
    field's key are NEVER touched (invariant 4 — no cascade from the JSONB
    overlay to this table). 404 if the definition is unknown."""
    row = (
        await session.execute(select(CustomField).where(CustomField.id == field_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "custom field not found")
    await session.execute(sa_delete(CustomField).where(CustomField.id == field_id))
    await session.commit()
