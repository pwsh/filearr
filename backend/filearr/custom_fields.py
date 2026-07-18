"""Admin-defined custom fields (Phase 4, roadmap §7 — P4-T3 / P4-T4 / P4-T6).

**Inert scaffolding.** Only tests import this module. Custom fields are the
Paperless-``CustomField``-shaped, admin-defined extension point (research brief
§6.1): a central definition table plus typed values written **only** into
``Item.user_metadata`` (never ``metadata_`` — invariant 2 keeps extractors off
the edit side of the wall, and ``effective_metadata``'s overlay means a custom
field naturally wins over a same-named extracted key with no new merge logic).

This module ships the *pure* logic P4-T4 wires into ``PATCH /items/{id}`` /
``POST /items/batch``:
- :class:`CustomFieldDef` mirrors the intended ``custom_fields`` table.
- :func:`validate_custom_values` reuses ``profiles.build_validator`` so custom
  fields go through the *same* validator as profiles (Paperless #7361: one
  shared validation layer, never per-endpoint duplication).
- :func:`normalize_field_name` enforces the key naming rules (lowercase,
  ``[a-z0-9_]``, no collision with core/reserved attributes).
- :func:`cf_meili_attribute` gives the collision-safe Meili attribute name.

Architect rulings baked in:
- **R1** — applicability is a per-library UUID array (``library_ids``; empty =
  all libraries), matching the include/exclude-glob per-library pattern already
  in ``Library``. (Revisit only if machine-groups land in phase 5/6.)
- **R3** — ``required`` is a display-only hint in v1; :func:`validate_custom_values`
  does NOT reject a missing/omitted required field.

Value-type / data_type note: custom fields use the Paperless-shaped vocabulary
(``string|integer|float|boolean|date|url|select``); those map onto the smaller
``FieldSpec`` type set for validation (``date`` -> ``datetime``, ``url`` and
``select`` -> ``string``; select-option *membership* enforcement is deferred to
the implementing task — flagged, not silently dropped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from filearr.models import CustomField, MediaType  # noqa: F401  (re-exported for API typing)
from filearr.profiles import FieldError, FieldSpec, build_validator

# Paperless-shaped custom-field data types -> FieldSpec.data_type used to build
# the shared validator. ``select`` option membership is not enforced in v1.
_CF_TO_FIELDSPEC_TYPE: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "float": "float",
    "boolean": "boolean",
    "date": "datetime",
    "url": "string",
    "select": "string",
}
CUSTOM_FIELD_TYPES = frozenset(_CF_TO_FIELDSPEC_TYPE)

# Custom-field key naming rule: lowercase snake, no leading digit.
_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")

# Reserved prefixes: ``cf_`` is the Meili projection prefix (a raw ``cf_x``
# custom field would double-prefix / collide); ``_`` marks sentinels like
# ``_extract_error``.
RESERVED_PREFIXES = ("cf_", "_")

# Core attribute names a custom field must never shadow: Item columns +
# ItemVersion/provenance columns + the hardcoded search.py::build_doc() Meili
# fields + the phase-3 recency projection. Collision here would corrupt either
# the JSONB overlay semantics or the Meili document shape.
RESERVED_ATTRIBUTES = frozenset(
    {
        # Item core columns
        "id", "library_id", "media_type", "status", "path", "rel_path",
        "filename", "extension", "size", "mtime", "quick_hash", "content_hash",
        "title", "year", "external_ids", "metadata", "user_metadata", "tags",
        "first_seen", "last_seen", "deleted_at", "sidecar_of",
        # provenance columns (P4-T7 / P4-T8)
        "source_agent_id", "replication_seq", "policy_version", "source",
        # hardcoded search.py::build_doc() top-level Meili fields
        "artist", "album", "author", "codec", "resolution", "genre",
        # phase-3 Meili-only projections
        "recency_bucket",
    }
)


@dataclass
class CustomFieldDef:
    """Admin-defined custom field (mirrors the intended ``custom_fields`` table).

    ``data_type`` is immutable after creation (Paperless precedent) — enforced at
    the API layer (P4-T3), not modelled here. ``applies_to`` / ``library_ids``
    are empty == "all media types" / "all libraries" (R1).
    """

    name: str
    label: str
    data_type: str
    select_options: list[str] | None = None
    applies_to: list[str] = field(default_factory=list)   # media_type values; [] = all
    library_ids: list[str] = field(default_factory=list)  # library UUIDs; [] = all
    facetable: bool = False
    sortable: bool = False
    required: bool = False

    def to_field_spec(self) -> FieldSpec:
        """Project this definition onto a :class:`FieldSpec` for validation."""
        return FieldSpec(
            name=self.name,
            data_type=_CF_TO_FIELDSPEC_TYPE.get(self.data_type, "string"),
            label=self.label,
            facetable=self.facetable,
            sortable=self.sortable,
            required=self.required,
        )


def normalize_field_name(raw: str) -> str:
    """Canonicalise/validate a custom-field name or raise ``ValueError``.

    Rules: trimmed + lowercased (so ``"Rating"`` -> ``"rating"``), must match
    ``[a-z][a-z0-9_]*`` after normalisation (no spaces/punctuation, no leading
    digit), and must not collide with a reserved core attribute or reserved
    prefix. The returned value is the ``user_metadata`` key the field governs.
    """
    name = raw.strip().lower()
    if not name:
        raise ValueError("custom-field name must not be empty")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid custom-field name {raw!r}: use lowercase [a-z0-9_], "
            "no leading digit, no spaces/punctuation"
        )
    if any(name.startswith(p) for p in RESERVED_PREFIXES):
        raise ValueError(
            f"custom-field name {name!r} uses a reserved prefix "
            f"({', '.join(RESERVED_PREFIXES)!r})"
        )
    if name in RESERVED_ATTRIBUTES:
        raise ValueError(
            f"custom-field name {name!r} collides with a core/reserved attribute"
        )
    return name


def cf_meili_attribute(name: str) -> str:
    """Return the collision-safe Meili attribute name for a custom field
    (``rating`` -> ``cf_rating``), per brief §6.4. Never collides with a
    hardcoded static field or a future profile field."""
    return f"cf_{name}"


def validate_custom_values(
    defs: list[CustomFieldDef], user_metadata: dict[str, Any]
) -> list[FieldError]:
    """Validate the ``user_metadata`` values that match a registered custom field.

    Reuses ``profiles.build_validator`` (shared validation layer). Only keys
    corresponding to a definition are type-checked; unregistered ``user_metadata``
    keys pass through unvalidated (full backward compat with ad-hoc metadata).
    Per R3, a missing/omitted ``required`` field is NOT rejected here. Pure: no IO.
    """
    if not defs:
        return []
    validator = build_validator([d.to_field_spec() for d in defs], name="CustomFieldModel")
    from pydantic import ValidationError

    try:
        validator.model_validate(user_metadata)
    except ValidationError as exc:
        return [
            FieldError(
                field=str(e["loc"][0]) if e.get("loc") else "",
                msg=e["msg"],
                type=e["type"],
            )
            for e in exc.errors()
        ]
    return []


# --- Persistence bridges (P4-T3/T4) + projection stub (P4-T6) ---------------
def def_from_model(row: CustomField) -> CustomFieldDef:
    """Project a persisted ``custom_fields`` row onto the pure
    :class:`CustomFieldDef` the validator consumes.

    ``library_ids`` UUIDs are stringified so the dataclass stays comparable to
    the request-supplied library id strings used in :func:`applicable_defs`."""
    return CustomFieldDef(
        name=row.name,
        label=row.label,
        data_type=row.data_type,
        select_options=list(row.select_options) if row.select_options else None,
        applies_to=list(row.applies_to or []),
        library_ids=[str(x) for x in (row.library_ids or [])],
        facetable=row.facetable,
        sortable=row.sortable,
        required=row.required,
    )


def applicable_defs(
    defs: list[CustomFieldDef], *, media_type: str | None, library_id: str | None
) -> list[CustomFieldDef]:
    """Filter definitions down to the ones that APPLY to one item (P4-T4).

    A definition applies when its ``applies_to`` is empty ("all media types") or
    contains the item's media type, AND its ``library_ids`` is empty ("all
    libraries", R1) or contains the item's library. A non-applicable definition
    is excluded, so the key it governs is treated as *unregistered* for that item
    and passes through :func:`validate_custom_values` unvalidated. Pure: no IO."""
    out: list[CustomFieldDef] = []
    for d in defs:
        if d.applies_to and (media_type is None or media_type not in d.applies_to):
            continue
        if d.library_ids and (library_id is None or str(library_id) not in d.library_ids):
            continue
        out.append(d)
    return out


def _date_to_epoch(value: Any) -> int | None:
    """Coerce a ``date`` custom-field value to integer epoch seconds for a STABLE
    Meili facet type, or ``None`` if unparseable.

    A ``date`` value can arrive as a ``datetime``/``date`` object (rare — JSONB
    round-trips to strings), an ISO-8601 string (``"2024-05-01"`` /
    ``"2024-05-01T12:00:00Z"``), or an already-numeric epoch. Anything else is
    dropped (returns ``None``) rather than projected as a string, so the facet
    never oscillates between int and string typing across rebuilds. Never raises."""
    from datetime import date, datetime

    if isinstance(value, bool):  # bool is an int subclass — never a date
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day).timestamp())
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def project_custom_fields_to_meili(
    item_effective_metadata: dict[str, Any], defs: list[CustomFieldDef]
) -> dict[str, Any]:
    """P4-T6: project ``facetable``/``sortable`` custom-field values into a Meili
    doc under :func:`cf_meili_attribute` names, mapping ``date`` -> epoch int for
    stable facet typing.

    Pure (no IO). Only ``facetable``/``sortable`` definitions are projected (a
    plain custom field stays visible via the Raw tab / PATCH response but is never
    indexed). A field whose key is absent from ``item_effective_metadata`` (or is
    ``None``) is skipped, so the ``cf_<name>`` attribute is simply omitted for that
    doc rather than projected as null. ``date`` values are coerced to epoch ints —
    a value that will not coerce is dropped so a facet's type never flips between
    int and string across rebuilds regardless of item indexing order.

    The FILTERABLE/SORTABLE *settings* consequence (adding/removing a
    ``cf_<name>`` filterable attribute) is applied by ``search.ensure_index`` /
    ``rebuild_index`` at settings-apply time, and ONLY takes effect via the
    phase-9 rebuild-and-swap path — never an in-place
    ``update_filterable_attributes()`` (invariant 1 / brief §3)."""
    out: dict[str, Any] = {}
    for d in defs:
        if not (d.facetable or d.sortable):
            continue
        if d.name not in item_effective_metadata:
            continue
        value = item_effective_metadata[d.name]
        if value is None:
            continue
        if d.data_type == "date":
            value = _date_to_epoch(value)
            if value is None:
                continue
        out[cf_meili_attribute(d.name)] = value
    return out
