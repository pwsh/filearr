"""Indexing-control preset bundles + extension groups (Phase 2, roadmap §4).

This module is **inert scaffolding** for Phase 2 (see
``docs/tasks/phase-2-indexing-controls-tasks.md``). It ships the real data
(``PRESET_BUNDLES``, ``EXTENSION_GROUPS``) and the pure helpers that can be
implemented and unit-tested today (``get_preset``, ``validate_preset_names``,
``build_exclusion_spec``, ``is_cachedir_tagged``). Anything that would touch the
scan walk, the ORM models, the API, or the scheduler is a typed stub raising
``NotImplementedError`` tagged with the task that will implement it.

No runtime module imports this file yet — only its tests do. Wiring it into
``scan.py`` is P2-T1.

Engine decision (brief §2): matching is delegated to ``pathspec.GitIgnoreSpec``
(MPL-2.0), gitignore last-match-wins semantics. Directory-prune patterns are
plain gitignore directory patterns (trailing ``/``); the walk checks a directory
entry by matching its path **with a trailing separator** so a directory-only
pattern fires and descent can be short-circuited.

Case sensitivity (Architect ruling R2): builtin OS-junk name patterns ship
**pre-bracket-expanded** (``[Tt]humbs.db``, ``[Dd]esktop.ini``) so they match the
common case variants seen across case-preserving SMB/NFS mounts without relying
on a per-pattern case-insensitive flag (pathspec's ``GitIgnoreSpec`` has none).
User-supplied patterns remain gitignore-case-sensitive — a documented gap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pathspec import GitIgnoreSpec

from filearr.models import MediaType

# --- CACHEDIR.TAG (brief §1.7 / P2-T4) -------------------------------------
# bford.info spec (public domain): a directory containing a file literally named
# ``CACHEDIR.TAG`` whose first 43 bytes are exactly this signature is a
# regenerable cache directory and must be pruned. The signature check (not mere
# filename presence) is the false-positive guard.
CACHEDIR_TAG_NAME = "CACHEDIR.TAG"
CACHEDIR_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
assert len(CACHEDIR_TAG_SIGNATURE) == 43  # spec: exactly the first 43 bytes


@dataclass(frozen=True)
class PresetBundle:
    """A named, independently-toggleable set of gitignore-style exclude patterns.

    ``exclude`` entries are gitignore patterns; a trailing ``/`` marks a
    directory-prune pattern (matched at directory-entry time to stop descent).
    ``default_enabled`` preserves a shipped-on default (only ``hidden_dotfiles``
    uses it, to keep today's unconditional dotfile skip from silently changing
    on upgrade — see P2-T1). ``caveat`` is surfaced in the Admin UI next to the
    toggle.
    """

    label: str
    exclude: tuple[str, ...]
    default_enabled: bool = False
    caveat: str | None = None


@dataclass(frozen=True)
class ExtensionGroup:
    """A named group of file extensions that refines a single ``MediaType``.

    Ruling R5: multiple groups may target the same ``MediaType`` and combine with
    **union** semantics; enabling ≥1 group for a type switches that type from
    "all extensions in the bucket" to "union of the enabled groups' extensions".
    Extensions are stored bare (no leading dot), lower-case, matching
    ``media_types.detect`` / ``Item.extension`` normalisation.
    """

    label: str
    media_type: MediaType
    extensions: tuple[str, ...]


# --- Preset bundles (brief §1.9 / §4.4 Stage A) ----------------------------
# Patterns are transcribed from the github/gitignore templates (CC0-1.0) and the
# brief's curated lists. OS-junk name patterns are pre-bracket-expanded per R2.
# CACHEDIR.TAG-tagged dirs are handled by ``is_cachedir_tagged`` (signature
# check), NOT by a pattern here — a filename pattern cannot verify the signature.

PRESET_BUNDLES: dict[str, PresetBundle] = {
    "system_files": PresetBundle(
        label="System files",
        exclude=(
            "$RECYCLE.BIN/",
            "System Volume Information/",
            ".Trashes/",
            ".Trash-*/",
            ".fseventsd/",
            ".Spotlight-V100/",
            ".DocumentRevisions-V100/",
            ".TemporaryItems/",
            "lost+found/",
            "[Dd]esktop.ini",
            "[Tt]humbs.db",
            "[Tt]humbs.db:encryptable",
            "ehthumbs.db",
            "ehthumbs_vista.db",
            "*.lnk",
            ".directory",
            ".fuse_hidden*",
            ".nfs*",
            "*~",
            "nohup.out",
        ),
    ),
    "hidden_dotfiles": PresetBundle(
        label="Hidden / dotfiles",
        exclude=(".*",),
        # default_enabled=True reproduces today's unconditional walk()-level
        # ``entry.name.startswith(".")`` skip as this preset's shipped default,
        # so introducing the preset system is not a silent behaviour change on
        # upgrade (P2-T1 regression requirement).
        default_enabled=True,
        caveat=(
            "Hides all dot-prefixed files and folders. May swallow companion "
            "files (e.g. AppleDouble ._* sidecars). Disabling will surface "
            "previously hidden files on the next scan."
        ),
    ),
    "caches_temp": PresetBundle(
        label="Caches & temp",
        exclude=(
            "tmp/",
            "temp/",
            ".cache/",
            "__pycache__/",
            ".pytest_cache/",
            ".tox/",
            ".direnv/",
            ".ccls-cache/",
            ".parcel-cache/",
            ".nyc_output/",
            "*.tmp",
            "*.cache",
            "*.pyc",
            # emacs autosave; ``#`` must be escaped or gitignore treats it as a
            # comment line.
            r"\#*",
        ),
        caveat=(
            "CACHEDIR.TAG-tagged directories are pruned separately by a "
            "signature check, in addition to these patterns."
        ),
    ),
    "node_modules_build": PresetBundle(
        label="node_modules & build output",
        exclude=(
            "node_modules/",
            "jspm_packages/",
            "bower_components/",
            ".pnpm-store/",
            "dist/",
            ".next/",
            ".nuxt/",
            ".svelte-kit/",
            ".vite/",
            "target/",
            ".venv/",
            "venv/",
            ".eslintcache",
            "*.tsbuildinfo",
            "*.pyc",
            "npm-debug.log*",
        ),
        caveat=(
            "Prunes venv/target/dist by name. If you keep real data under a "
            "folder literally named venv/ or target/, leave this off."
        ),
    ),
    "os_metadata": PresetBundle(
        label="OS metadata",
        exclude=(
            ".DS_Store",
            "._*",
            # Literal Finder ``Icon`` file ending in a carriage-return byte. The
            # character-class form ``Icon[\r]`` is required: a bare ``Icon\r``
            # line has its trailing CR stripped by gitignore whitespace rules.
            "Icon[\r]",
            "[Tt]humbs.db",
            "[Dd]esktop.ini",
        ),
        caveat=(
            "._* overlaps AppleDouble sidecar detection; sidecar classification "
            "wins (R1), so a ._* file linked as a sidecar is kept, not dropped."
        ),
    ),
}


# --- Extension groups (brief §4.4 / P2-T3) ---------------------------------
EXTENSION_GROUPS: dict[str, ExtensionGroup] = {
    "raw_photos": ExtensionGroup(
        label="RAW photos",
        media_type=MediaType.image,
        extensions=("cr2", "cr3", "nef", "arw", "dng", "raf"),
    ),
    "editable_images": ExtensionGroup(
        label="Editable images (Photoshop/TIFF)",
        media_type=MediaType.image,
        # Layered/authoring image formats (OPS-T4): Adobe Photoshop documents
        # and TIFF masters. Pillow reads a flattened composite of a PSD.
        extensions=("psd", "tif", "tiff"),
    ),
    "jpeg_family": ExtensionGroup(
        label="JPEG family",
        media_type=MediaType.image,
        extensions=("jpg", "jpeg"),
    ),
    "office_docs": ExtensionGroup(
        label="Office documents",
        media_type=MediaType.document,
        extensions=("doc", "docx", "odt", "rtf"),
    ),
    "ebooks": ExtensionGroup(
        label="E-books",
        media_type=MediaType.document,
        extensions=("epub", "mobi", "azw3", "cbz", "cbr"),
    ),
    "lossless_audio": ExtensionGroup(
        label="Lossless audio",
        media_type=MediaType.audio,
        extensions=("flac", "alac", "wav", "ape", "wv"),
    ),
    "lossy_audio": ExtensionGroup(
        label="Lossy audio",
        media_type=MediaType.audio,
        extensions=("mp3", "ogg", "opus", "m4a", "wma"),
    ),
}


# --- Pure helpers (implemented + unit-tested now) --------------------------

def get_preset(name: str) -> PresetBundle:
    """Return the bundle named ``name``. Raises ``KeyError`` if unknown."""
    return PRESET_BUNDLES[name]


def get_extension_group(name: str) -> ExtensionGroup:
    """Return the extension group named ``name``. Raises ``KeyError`` if unknown."""
    return EXTENSION_GROUPS[name]


def validate_preset_names(names: list[str]) -> list[str]:
    """Return the subset of ``names`` that are not known preset bundles.

    An empty return means every name is valid. The API layer (P2-T5) turns a
    non-empty result into a 422, mirroring today's ``scan_cron`` validation.
    """
    return [n for n in names if n not in PRESET_BUNDLES]


def validate_extension_group_names(names: list[str]) -> list[str]:
    """Return the subset of ``names`` that are not known extension groups."""
    return [n for n in names if n not in EXTENSION_GROUPS]


def build_exclusion_spec(
    enabled_presets: list[str],
    extra_excludes: list[str] | None = None,
    includes: list[str] | None = None,
) -> GitIgnoreSpec:
    """Build one ``GitIgnoreSpec`` from enabled presets + per-library patterns.

    Ordering (gitignore last-match-wins): enabled preset excludes (in the
    canonical ``PRESET_BUNDLES`` order for determinism), then per-library
    ``extra_excludes`` (``library.exclude_globs``), then ``includes``
    (``library.include_globs``) converted to gitignore negations (``!pattern``)
    so a custom include can re-admit a single file a preset would exclude —
    satisfying the roadmap's "preset X, but keep this one file" requirement via
    native negation rather than a bespoke override path.

    Unknown preset names are skipped silently here (validation is the API's job
    via :func:`validate_preset_names`); this keeps the matcher total.

    Pure: no filesystem access, no ORM. The scan walk (P2-T1) will call
    ``spec.match_file(rel)`` per file and ``spec.match_file(rel + "/")`` per
    directory (to prune before recursing).
    """
    lines: list[str] = []
    for name in PRESET_BUNDLES:  # canonical order, only the enabled ones
        if name in enabled_presets:
            lines.extend(PRESET_BUNDLES[name].exclude)
    if extra_excludes:
        lines.extend(extra_excludes)
    if includes:
        for inc in includes:
            lines.append(inc if inc.startswith("!") else "!" + inc)
    return GitIgnoreSpec.from_lines(lines)


def resolve_effective_presets(enabled_presets: list[str] | None) -> list[str]:
    """Resolve a library's stored ``enabled_presets`` column into the effective set.

    The column default ``'{}'`` means "no explicit configuration" — so effective
    presets are the union of every ``default_enabled`` bundle (today only
    ``hidden_dotfiles``) and the stored *positive* entries, MINUS any negative
    sentinel ``-name`` entry that explicitly disables a shipped-on default.

    Examples::

        resolve_effective_presets([])                 -> ["hidden_dotfiles"]
        resolve_effective_presets(["node_modules_build"])
              -> ["hidden_dotfiles", "node_modules_build"]  # canonical order
        resolve_effective_presets(["-hidden_dotfiles"])     -> []  # opt out of default

    Returned in canonical ``PRESET_BUNDLES`` order for deterministic spec builds.
    Unknown names (positive or negative) are ignored here — validation is the API's
    job (:func:`validate_preset_names`); this keeps resolution total for the walk.
    """
    active = {name for name, b in PRESET_BUNDLES.items() if b.default_enabled}
    disabled: set[str] = set()
    for entry in enabled_presets or []:
        if entry.startswith("-"):
            disabled.add(entry[1:])
        else:
            active.add(entry)
    active -= disabled
    return [name for name in PRESET_BUNDLES if name in active]


def is_cachedir_tagged(path: str) -> bool:
    """True if directory ``path`` holds a valid CACHEDIR.TAG (P2-T4).

    Reads only the first 43 bytes of ``<path>/CACHEDIR.TAG`` and compares them to
    the exact bford.info signature. A directory that merely contains a file named
    ``CACHEDIR.TAG`` with wrong/missing signature bytes is **not** tagged (the
    false-positive guard). Any OS error (missing file, unreadable, not a
    directory) yields ``False`` — absence of a valid tag, never an exception into
    the walk.
    """
    tag = os.path.join(path, CACHEDIR_TAG_NAME)
    try:
        with open(tag, "rb") as fh:
            return fh.read(len(CACHEDIR_TAG_SIGNATURE)) == CACHEDIR_TAG_SIGNATURE
    except OSError:
        return False


# --- Stubs: touch scan flow / models / API / scheduling (NOT implemented) --

def build_library_spec(library) -> GitIgnoreSpec:  # noqa: ANN001
    """P2-T1: build a library's effective GitIgnoreSpec from its ORM row.

    Resolves ``library.enabled_presets`` (new column) into the effective preset
    set via :func:`resolve_effective_presets` (union of default-on bundles + stored
    positives, minus ``-name`` disables), then delegates to
    :func:`build_exclusion_spec` with ``library.exclude_globs`` appended and
    ``library.include_globs`` as gitignore negations (re-include overrides).
    """
    return build_exclusion_spec(
        resolve_effective_presets(getattr(library, "enabled_presets", None)),
        extra_excludes=list(library.exclude_globs or []),
        includes=list(library.include_globs or []),
    )


def resolve_enabled_extensions(
    media_type: MediaType,
    enabled_types: list[str],
    enabled_extension_groups: list[str],
) -> set[str] | None:
    """P2-T3: resolve the effective extension allow-list for a MediaType.

    Returns ``None`` when the type imposes no extension-group refinement (all
    extensions in the bucket allowed -- today's behaviour), else the **union** of
    every enabled group that targets ``media_type`` (ruling R5: multiple groups
    per type combine with union semantics; enabling >=1 group for a type switches
    that type from "all extensions in the bucket" to "union of the enabled
    groups' extensions").

    ``enabled_types`` is honoured defensively: when it is non-empty and
    ``media_type`` is not in it, the type is gated off entirely by the scan's
    ``enabled_types`` filter, so no refinement applies and ``None`` is returned
    (there is nothing to narrow). An empty ``enabled_types`` means "all types",
    so refinement still applies to every group's target type.

    Extensions are returned bare (no leading dot), lower-case, matching
    ``media_types.detect`` / ``Item.extension`` normalisation. Unknown group
    names are ignored here (validation is the API's job via
    :func:`validate_extension_group_names`); this keeps resolution total for the
    walk.
    """
    if enabled_types and media_type.value not in enabled_types:
        return None
    allowed: set[str] = set()
    for name in enabled_extension_groups:
        group = EXTENSION_GROUPS.get(name)
        if group is not None and group.media_type == media_type:
            allowed.update(group.extensions)
    return allowed or None


def prune_dir(spec: GitIgnoreSpec, rel_dir: str, abs_dir: str) -> bool:
    """P2-T1/T4: decide whether the walk should stop descending into a directory.

    Returns True (prune, never descend) when EITHER a directory-only gitignore
    pattern matches (checked by appending a trailing ``/`` so directory patterns
    fire) OR ``abs_dir`` holds a signature-verified ``CACHEDIR.TAG`` (P2-T4). The
    pattern check is cheap and pure; the CACHEDIR.TAG check reads at most 43 bytes
    and is only reached when the pattern check misses (short-circuit).

    Ruling R1: directory pruning always wins — a pruned directory is never
    descended into, regardless of anything a file inside it might claim.
    """
    rel = rel_dir.replace(os.sep, "/")
    if spec.match_file(rel + "/"):
        return True
    return is_cachedir_tagged(abs_dir)
