"""P2-T5 — read-only presets/extension-groups catalogue endpoint.

Exposes the code-constant ``PRESET_BUNDLES`` and ``EXTENSION_GROUPS`` so the
Admin UI can render toggle lists (with pattern/extension disclosure) without
duplicating the data client-side. Read scope: the catalogue is not secret, but
it stays behind the same Bearer gate as the rest of the read API. Promotion to a
DB-backed, user-editable catalogue is P2-T7 (deferred, R4).
"""

from fastapi import APIRouter, Depends, HTTPException

from filearr.presets import EXTENSION_GROUPS, PRESET_BUNDLES, PresetBundle
from filearr.schemas import ExtensionGroupOut, PresetOut, PresetsResponse
from filearr.security import require_scope

router = APIRouter()


def _preset_out(name: str, bundle: PresetBundle) -> PresetOut:
    return PresetOut(
        name=name,
        label=bundle.label,
        patterns=list(bundle.exclude),
        default_enabled=bundle.default_enabled,
        caveat=bundle.caveat,
    )


@router.get("", response_model=PresetsResponse, dependencies=[Depends(require_scope("read"))])
async def list_presets() -> PresetsResponse:
    """All preset bundles + extension groups (names, labels, patterns/extensions,
    default_enabled)."""
    return PresetsResponse(
        presets=[_preset_out(name, b) for name, b in PRESET_BUNDLES.items()],
        extension_groups=[
            ExtensionGroupOut(
                name=name,
                label=g.label,
                file_category=g.file_category,
                extensions=list(g.extensions),
            )
            for name, g in EXTENSION_GROUPS.items()
        ],
    )


@router.get(
    "/{name}", response_model=PresetOut, dependencies=[Depends(require_scope("read"))]
)
async def get_preset_detail(name: str) -> PresetOut:
    """A single preset bundle by name; 404 if unknown."""
    bundle = PRESET_BUNDLES.get(name)
    if bundle is None:
        raise HTTPException(404, "preset not found")
    return _preset_out(name, bundle)
