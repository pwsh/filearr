"""Agent configuration-group settings schema + validation (W6-D2).

The pure, request-free core behind the remote-configuration channel (config
groups, ``docs/ops/agents.md`` § "Configuration groups"):

* :class:`GroupSettings` / :func:`validate_settings` — typed, versioned v1
  validation of a config group's ``settings`` object. Unlike the per-agent
  *policy* body (:mod:`filearr.policy`, which PRESERVES unknown keys for
  forward-compat), a config group's ``settings`` REJECTS unknown top-level keys
  (``extra='forbid'`` → 422) so an operator typo never silently no-ops.
* :func:`merge_group_into_policy` / :func:`group_etag_tag` — how a group's
  settings ride the EXISTING policy channel: merged into the effective policy doc
  under a new top-level ``group`` section, with the group's ``updated_at`` folded
  into the policy ETag so an edit invalidates agent caches.

Path specs (``scan_selections[].paths``) MAY carry env tokens
(``%USERPROFILE%``, ``$HOME``, ``~``) and glob segments (``/home/*/documents``);
central VALIDATES SYNTAX ONLY (non-empty, balanced brackets/braces) and NEVER
resolves a path — final resolution is agent-side per-OS (W6-R1). Regexes are
compiled here as a Python ``re`` sanity gate only; the authoritative match engine
is the Go agent's RE2/``regexp`` (documented divergence: a pattern valid in one
is not guaranteed valid in the other, but the ``re`` gate catches the common
typo class).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from filearr.schedule import InvalidCronError, validate_cron

# --- W6-R1 preset vocabulary (docs/research/agent-inventory-presets.md §5) ---
#: The named folder-selection presets the distributed agent offers. ``custom`` is
#: the empty admin-defined scaffold. These validate ``scan_selections[].preset``;
#: they are NOT the central exclusion bundles (``filearr.presets.PRESET_BUNDLES``
#: — a separate, reused vocabulary the agent applies on top).
SCAN_PRESET_NAMES: frozenset[str] = frozenset(
    {
        "user-documents",
        "user-media",
        "user-profiles-full",
        "downloads",
        "server-data",
        "custom",
    }
)

LOG_LEVELS = ("error", "warn", "info", "verbose", "debug")

# --- Size / count caps (payload-size guards) --------------------------------
#: Whole-settings byte ceiling (compact JSON). Mirrors the per-agent policy cap.
MAX_SETTINGS_BYTES = 65536
MAX_SCAN_SELECTIONS = 100
MAX_PATHS_PER_SELECTION = 200
MAX_REGEX_PER_SELECTION = 200
MAX_PATH_LEN = 4096
MAX_REGEX_LEN = 4096
MAX_COLLECTORS = 64
MAX_COLLECTOR_LEN = 128


class GroupSettingsValidationError(ValueError):
    """A config group ``settings`` object that fails v1 validation (→ 422)."""


def _check_balanced(spec: str) -> None:
    """Raise ``ValueError`` if glob brackets ``[]`` / braces ``{}`` are unbalanced.

    A minimal syntax gate — central never resolves the path, so this only catches
    the obvious ``/home/[user`` / ``/data/{a,b`` typo class before the spec is
    shipped to the agent (whose per-OS resolver is the authority)."""
    depth_brace = 0
    depth_brack = 0
    for ch in spec:
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack -= 1
        if depth_brace < 0 or depth_brack < 0:
            raise ValueError(f"unbalanced glob brackets/braces in path spec: {spec!r}")
    if depth_brace != 0 or depth_brack != 0:
        raise ValueError(f"unbalanced glob brackets/braces in path spec: {spec!r}")


def _validate_regex_list(v: list[str], field: str) -> list[str]:
    if len(v) > MAX_REGEX_PER_SELECTION:
        raise ValueError(f"{field} has {len(v)}; max {MAX_REGEX_PER_SELECTION}")
    for i, pat in enumerate(v):
        if not isinstance(pat, str) or not pat.strip():
            raise ValueError(f"{field}[{i}] must be a non-empty regex string")
        if len(pat) > MAX_REGEX_LEN:
            raise ValueError(f"{field}[{i}] exceeds {MAX_REGEX_LEN} chars")
        try:
            re.compile(pat)  # Python `re` sanity gate; agent uses Go RE2.
        except re.error as err:
            raise ValueError(f"{field}[{i}] is not a valid regex: {err}") from err
    return v


class ScanSelection(BaseModel):
    """One path selection an agent walks (W6-D2). A ``preset`` names a W6-R1
    folder set the agent resolves per-OS; ``paths`` are explicit path specs (env
    tokens + glob segments, syntax-validated only); ``include_regex`` /
    ``exclude_regex`` refine matches. ``enabled`` gates the whole selection.

    Either ``preset`` or ``paths`` (or both) is expected in practice, but an
    all-empty selection is NOT rejected — an operator may stage a disabled
    scaffold (``enabled: false``) before filling it in."""

    model_config = ConfigDict(extra="forbid")

    preset: str | None = None
    paths: list[str] = Field(default_factory=list)
    include_regex: list[str] = Field(default_factory=list)
    exclude_regex: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, v: str | None) -> str | None:
        if v is not None and v not in SCAN_PRESET_NAMES:
            raise ValueError(
                f"unknown preset {v!r}; one of {sorted(SCAN_PRESET_NAMES)}"
            )
        return v

    @field_validator("paths")
    @classmethod
    def _valid_paths(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_PATHS_PER_SELECTION:
            raise ValueError(f"paths has {len(v)}; max {MAX_PATHS_PER_SELECTION}")
        for i, spec in enumerate(v):
            if not isinstance(spec, str) or not spec.strip():
                raise ValueError(f"paths[{i}] must be a non-empty path spec")
            if len(spec) > MAX_PATH_LEN:
                raise ValueError(f"paths[{i}] exceeds {MAX_PATH_LEN} chars")
            _check_balanced(spec)
        return v

    @field_validator("include_regex")
    @classmethod
    def _valid_include(cls, v: list[str]) -> list[str]:
        return _validate_regex_list(v, "include_regex")

    @field_validator("exclude_regex")
    @classmethod
    def _valid_exclude(cls, v: list[str]) -> list[str]:
        return _validate_regex_list(v, "exclude_regex")


class InventoryConfig(BaseModel):
    """Per-group inventory-collector toggle (W6-D2). ``collectors`` are FREE
    strings (W6-D3 defines the vocabulary; central does NOT hard-code it) — only
    length/count-capped so a hostile/buggy payload cannot bloat the row."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    collectors: list[str] = Field(default_factory=list)

    @field_validator("collectors")
    @classmethod
    def _valid_collectors(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_COLLECTORS:
            raise ValueError(f"collectors has {len(v)}; max {MAX_COLLECTORS}")
        for i, name in enumerate(v):
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"collectors[{i}] must be a non-empty string")
            if len(name) > MAX_COLLECTOR_LEN:
                raise ValueError(f"collectors[{i}] exceeds {MAX_COLLECTOR_LEN} chars")
        return v


class GroupSettings(BaseModel):
    """The typed, versioned v1 config-group ``settings`` object. Unknown top-level
    keys are REJECTED (``extra='forbid'`` → 422) so an operator typo never
    silently no-ops (contrast the per-agent policy body, which preserves unknown
    keys for forward-compat)."""

    model_config = ConfigDict(extra="forbid")

    log_level: Literal["error", "warn", "info", "verbose", "debug"] | None = None
    scan_selections: list[ScanSelection] | None = None
    inventory: InventoryConfig | None = None
    scan_schedule_cron: str | None = None

    @field_validator("scan_selections")
    @classmethod
    def _cap_selections(
        cls, v: list[ScanSelection] | None
    ) -> list[ScanSelection] | None:
        if v is not None and len(v) > MAX_SCAN_SELECTIONS:
            raise ValueError(
                f"scan_selections has {len(v)}; max {MAX_SCAN_SELECTIONS}"
            )
        return v

    @field_validator("scan_schedule_cron")
    @classmethod
    def _valid_cron(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("scan_schedule_cron must be a non-empty cron expression")
        try:
            validate_cron(v)
        except InvalidCronError as err:
            raise ValueError(f"invalid scan_schedule_cron: {err}") from err
        return v


def settings_json_len(settings: Any) -> int:
    """Compact-JSON byte length of a settings object (the oversize gate's measure)."""
    return len(
        json.dumps(settings, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def validate_settings(settings: Any) -> None:
    """Validate a config group ``settings`` object (W6-D2).

    Raises :class:`GroupSettingsValidationError` when ``settings`` is not a JSON
    object, exceeds :data:`MAX_SETTINGS_BYTES`, carries an unknown top-level key,
    or a known key violates its bound (bad preset / regex / cron / oversize). The
    caller stores the ORIGINAL ``settings`` verbatim — this is a gate, not a
    transform."""
    if not isinstance(settings, dict):
        raise GroupSettingsValidationError("settings must be a JSON object")
    if settings_json_len(settings) > MAX_SETTINGS_BYTES:
        raise GroupSettingsValidationError(
            f"settings exceeds {MAX_SETTINGS_BYTES} bytes"
        )
    try:
        GroupSettings(**settings)
    except ValidationError as err:
        raise GroupSettingsValidationError(_summarise(err)) from err


def _summarise(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e.get("loc", ())) or "settings"
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid settings"


# --------------------------------------------------------------------------- #
# Policy-channel delivery (merge group settings into the effective policy doc)  #
# --------------------------------------------------------------------------- #
def group_etag_tag(group: Any | None) -> str | None:
    """A compact, edit-sensitive tag for the agent's config group, or ``None``
    when the agent has no group. Folded into the policy ETag so ANY group edit
    (which bumps ``updated_at``) invalidates the agent's cached policy. ``None`` →
    the ETag stays the pre-W6 ``"<scope>/<version>"`` form (no group section)."""
    if group is None:
        return None
    import hashlib

    basis = f"{group.id}:{group.updated_at.isoformat()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def merge_group_into_policy(policy: dict, group: Any | None) -> dict:
    """Merge the agent's config group ``settings`` into the effective policy doc
    under a new top-level ``group`` section (W6-D2).

    Precedence (documented on the policy endpoint): per-agent explicit policy keys
    (the existing resolved ``policy``) > group settings > defaults. Concretely:

    * NULL group → the doc is returned UNCHANGED (no ``group`` section).
    * A group whose ``settings`` is empty ``{}`` → an empty ``group: {}`` section
      (the agent sees "a group is assigned, it just carries no overrides").
    * If the resolved per-agent policy ALREADY sets a top-level ``group`` key (an
      operator authored it explicitly), that explicit key WINS — the group
      settings are NOT injected (additive, non-clobbering).

    Backward compat: current agent binaries that ignore ``group`` are unaffected
    (the key is purely additive)."""
    if group is None:
        return policy
    if "group" in policy:
        return policy
    return {**policy, "group": group.settings or {}}
