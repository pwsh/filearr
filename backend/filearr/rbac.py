"""RBAC core — the pure permission-evaluation heart (Phase 6, roadmap §3 /
``docs/research/phase-6-identity-auth-rbac.md``).

**Inert scaffolding.** Nothing in the runtime imports this module yet — only its
tests do. It ships the *pure*, unit-testable core of the two-layer RBAC model
(global roles that set a *ceiling* + path-scoped ACL grants that narrow within
it) plus the ``ltree`` path-encoding function the whole permission hierarchy is
keyed on. Wiring any of this into ``security.py`` / the API is P6-T4
(``require_permission`` dependency) and later — see the tasks doc.

What is *pure and implemented here*:

- :data:`ACTIONS` — the grantable-action vocabulary (brief §2.3 CHECK list).
- :class:`Role` + :data:`ROLE_CEILINGS` — the global-role → maximum-action-set
  map. A path grant can never widen a principal beyond its role ceiling
  (brief §2.5 step 1: "ceiling model bounds path grants").
- :func:`encode_path_label` / :func:`decode_path_label` — the **injective**
  (Architect ruling **R1**) mapping between an arbitrary ``rel_path`` segment
  (any Unicode, spaces, dots, digits) and a valid ``ltree`` label
  (``[A-Za-z0-9_]``, ``.`` reserved as the level separator). Two distinct real
  directory names may **never** collide onto one label — a collision would
  silently merge or leak ACL scope (brief §2.4 / open question #1). The
  candidate scheme here is what the **P6-T2a spike** adversarially reviews
  before P6-T2 (RBAC core) is implemented for real.
- :func:`path_to_ltree` — join encoded segments into a dotted ``ltree`` path.
- :class:`PathGrant` — the in-memory shape of a ``path_grants`` row.
- :class:`Decision` + :func:`evaluate` — the effective-permission algorithm
  (brief §2.5): admin bypass, ceiling clamp, longest-prefix-wins,
  explicit-deny-wins at equal specificity, no-grant → deny for non-admins. The
  decision carries the winning grant for auditability.

Architect rulings baked in (see the tasks doc for the full list):

- **R1** — ``encode_path_label`` MUST be injective: lowercase passthrough for
  ``[a-z0-9_]``, everything else hex-escaped. Collision = correctness bug.
- **R2** — tenant-token filter-size ceiling lives in
  :mod:`filearr.tenant_tokens`, not here.

No ``models.py`` change, no migration, no runtime wiring lands in this pass.
"""

from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import dataclass
from typing import Literal

# --- Action vocabulary (brief §2.3) ----------------------------------------

#: The full set of grantable actions. A ``path_grants.actions[]`` value is a
#: subset of this; anything outside it is a bug at grant-creation time.
ACTIONS: frozenset[str] = frozenset(
    {
        "search_metadata",
        "search_content",
        "download",
        "upload",
        "modify",
        "delete",
        "edit_metadata",
        "manage_alerts",
    }
)


class Role(str, enum.Enum):
    """Global role — the coarse first RBAC layer (brief §2.3). Mixed-in ``str``
    for the same serialization reasons as ``models.py``'s enums."""

    ADMIN = "admin"
    USER = "user"
    VIEWER = "viewer"


#: Per-role *ceiling*: the maximum set of actions a principal with that global
#: role may ever be granted on any path. A path grant is clamped to this — it can
#: narrow, never widen (brief §2.5 step 1). ``admin`` is unbounded (all actions)
#: and additionally short-circuits path evaluation entirely.
ROLE_CEILINGS: dict[Role, frozenset[str]] = {
    Role.ADMIN: ACTIONS,
    Role.USER: frozenset(
        {
            "search_metadata",
            "search_content",
            "download",
            "upload",
            "modify",
            "edit_metadata",
            "manage_alerts",
        }
    ),
    # Viewer is read-only: it may see metadata/content but perform no mutation
    # and no download. A viewer can NEVER be handed `modify`/`download` via a
    # path grant — the ceiling clamps it to nothing (brief §2.5 step 1 test).
    Role.VIEWER: frozenset({"search_metadata", "search_content"}),
}


# --- ltree path encoding (brief §2.4, Architect ruling R1) ------------------

# ltree labels permit only [A-Za-z0-9_] with '.' reserved as the level
# separator. We keep the passthrough set to lowercase [a-z0-9] and escape
# EVERYTHING else (uppercase, '_', '.', space, any Unicode) as one '_XX' group
# per UTF-8 byte, two lowercase-hex digits each. This is INJECTIVE across case
# and across byte content, and — crucially — decodes UNAMBIGUOUSLY because every
# escape is FIXED WIDTH (exactly '_' + 2 hex): a following passthrough char that
# happens to be a hex letter (a-f) can never be swallowed into the previous
# escape's hex run (the classic variable-width-escape bug).
#
# Injectivity / round-trip argument: passthrough chars are ASCII [a-z0-9], whose
# bytes are all < 0x80 and never collide with a UTF-8 continuation/lead byte
# (>= 0x80, always escaped). Reconstruction concatenates passthrough ASCII bytes
# and escaped raw bytes into one buffer, then UTF-8-decodes — a bijection.
# "Foo": 'F'(0x46)->"_46", 'o'/'o' passthrough -> "_46oo"; "foo" -> "foo":
# distinct (R1 case-collision requirement). '_'(0x5f)->"_5f", '.'(0x2e)->"_2e",
# space(0x20)->"_20", 'é'(U+00E9, UTF-8 c3 a9)->"_c3_a9".
#
# Label-length caveat (brief §2.4 step 3): worst-case ~9-12 chars per Unicode
# char can exceed ltree's per-label ceiling for very long segments; hashing long
# segments to a fixed-width label is deferred and flagged for the P6-T2a spike —
# it does NOT affect injectivity of THIS encoder, which is the R1 contract.

_PASSTHROUGH = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_HEXDIGITS = frozenset("0123456789abcdef")

# --- Over-long segment hashing (P6-T2a directive 1) -------------------------
#
# The per-UTF-8-byte escape inflates a segment ~3x (all-ASCII) to ~9-12x (all
# 4-byte Unicode). ltree's per-label ceiling is 256 bytes (Postgres <16) /
# 1000 bytes (Postgres >=16, our PG18 target). We use a CONSERVATIVE 200-byte
# encoded-length threshold (safe under both ceilings): any segment whose ENCODED
# form would exceed it is replaced by a fixed-width hash label instead. On a real
# filesystem a single path segment is <=255 bytes (NAME_MAX), whose worst-case
# all-escaped encoding is 765 bytes — so hashing only ever fires for pathological
# / synthetic inputs, never a real directory name (documented for the reviewer).
#
# The hash label is ``h__<blake2b-64 hex>`` (the ``h__`` double-underscore
# sentinel is UNREACHABLE by the base encoder: a normal encoding only ever emits
# ``_`` as part of a fixed-width ``_XX`` escape, so two adjacent underscores can
# never occur — this keeps the codec injective ACROSS the hashed and non-hashed
# label spaces, not just within each). Collision odds: a 64-bit blake2b digest
# collides between two DISTINCT over-long segments at the birthday bound (~1 in
# 2**32 for ~4 billion such segments); because only >200-byte-encoded segments
# are ever eligible and those don't occur on real filesystems, the residual
# access-control-merge risk is negligible. Hashing is applied to the RAW segment
# bytes, so injectivity of the *input* is what the digest preserves.
LTREE_LABEL_MAX_ENCODED = 200
_HASH_SENTINEL = "h__"  # unreachable by encode_path_label's base output

#: Returned by :func:`decode_path_label` for a hashed (one-way) label. A hashed
#: label is deliberately NOT reversible — the canonical ``rel_path`` lives on
#: ``items``/``libraries`` for display; the ltree value is purely an index key
#: (brief §2.4: "deterministic, not reversible"). Decode returns this marker so
#: callers can detect the lossy case explicitly rather than get a wrong string.
HASHED_LABEL: str = "�hashed�"


def _hash_label(segment: str) -> str:
    """Fixed-width hash label for an over-long segment (P6-T2a directive 1)."""
    digest = hashlib.blake2b(segment.encode("utf-8"), digest_size=8).hexdigest()
    return f"{_HASH_SENTINEL}{digest}"


def encode_path_label(segment: str) -> str:
    """Encode one ``rel_path`` segment into a valid, injective ``ltree`` label.

    Injective per Architect ruling **R1**: distinct ``segment`` inputs always
    produce distinct outputs, so two real directories can never share an ACL
    scope by accident. Passthrough ``[a-z0-9]`` stay verbatim; every other
    character (uppercase, ``_``, ``.``, space, any Unicode) becomes a
    ``_<hex>`` escape of its Unicode code point.

    The empty segment encodes as ``_`` (a lone sentinel), distinct from every
    non-empty encoding (which either starts with a passthrough char or with a
    ``_`` immediately followed by hex digits).
    """
    if segment == "":
        return "_"
    out: list[str] = []
    for ch in segment:
        if ch in _PASSTHROUGH:
            out.append(ch)
        else:
            for byte in ch.encode("utf-8"):
                out.append(f"_{byte:02x}")
    label = "".join(out)
    # P6-T2a directive 1: hash segments whose encoded form exceeds the ltree
    # label ceiling. The hash is over the RAW segment (injective input) so
    # distinct over-long segments still map to distinct labels (modulo the
    # documented 64-bit collision bound); the ``h__`` sentinel keeps hashed
    # labels disjoint from every base encoding.
    if len(label) > LTREE_LABEL_MAX_ENCODED:
        return _hash_label(segment)
    return label


def decode_path_label(label: str) -> str:
    """Inverse of :func:`encode_path_label` (round-trips exactly).

    Not required by the permission check itself (which only ancestor-matches on
    encoded labels), but implemented and tested to *prove* injectivity: a
    function with a total left inverse is injective by construction.
    """
    if label == "_":
        return ""
    if label.startswith(_HASH_SENTINEL):
        # One-way hashed label (P6-T2a directive 1): not reversible by design.
        return HASHED_LABEL
    buf = bytearray()
    i = 0
    n = len(label)
    while i < n:
        ch = label[i]
        if ch != "_":
            # Passthrough is ASCII [a-z0-9] -> a single byte.
            buf.append(ord(ch))
            i += 1
            continue
        # Fixed-width escape: '_' + exactly two lowercase-hex digits = one byte.
        pair = label[i + 1 : i + 3]
        if len(pair) != 2 or any(c not in _HEXDIGITS for c in pair):
            raise ValueError(f"malformed escape at index {i} in {label!r}")
        buf.append(int(pair, 16))
        i += 3
    return buf.decode("utf-8")


def library_label(library_id: uuid.UUID | str) -> str:
    """The top ltree label for a library (brief §2.4 step 1, P6-T2a directive 2).

    Built from the HYPHEN-FREE ``uuid.hex`` (32 hex chars), never the canonical
    dashed uuid text form — ``-`` is not a valid ltree label character, so
    ``lib_<canonical-uuid>`` would be an INVALID label. ``lib_<uuid.hex>`` is a
    valid ``[a-z0-9_]`` label (e.g. ``lib_0190f2c34d5e7abc8def1234567890ab``) and
    sidesteps encoding the library name. Accepts a ``uuid.UUID`` or any string
    parseable as one; a malformed value raises (fail-closed, no silent scope)."""
    hexid = library_id.hex if isinstance(library_id, uuid.UUID) else uuid.UUID(str(library_id)).hex
    return f"lib_{hexid}"


def encode_rel_path(rel_path: str, *, sep: str = "/") -> str:
    """Encode a ``rel_path`` into dotted, library-UNPREFIXED ltree labels.

    Each path segment is independently run through :func:`encode_path_label`;
    the encoded labels are joined by ``.`` (the ltree level separator). Empty
    segments (leading/trailing/double separators) are preserved as ``_`` labels
    so the mapping stays injective over the segment *sequence*."""
    segments = rel_path.split(sep)
    return ".".join(encode_path_label(s) for s in segments)


def path_to_ltree(
    rel_path: str, *, library_id: uuid.UUID | str | None = None, sep: str = "/"
) -> str:
    """Encode a full item path into a dotted ``ltree`` string (brief §2.4).

    When ``library_id`` is given, the ``lib_<uuid.hex>`` prefix label (P6-T2a
    directive 2) is prepended — the canonical scope form stored on
    ``items.path_scope`` and matched by ``path_grants.scope``. When omitted the
    result is path-only (the caller supplies its own prefix; kept for the pure
    unit tests and any prefix-agnostic use). The library prefix and every path
    segment are individually valid ltree labels, so the join is a valid
    ``ltree`` value end to end."""
    encoded = encode_rel_path(rel_path, sep=sep)
    if library_id is None:
        return encoded
    return f"{library_label(library_id)}.{encoded}"


# --- Grants + evaluation (brief §2.5) --------------------------------------


@dataclass(frozen=True, slots=True)
class PathGrant:
    """One ``path_grants`` row, resolved for the requesting principal.

    ``path`` is an already-encoded ``ltree`` string (output of
    :func:`path_to_ltree`, optionally with the ``lib_<uuid>`` prefix). ``action``
    is a single member of :data:`ACTIONS`. ``allow`` False means an explicit
    deny (``path_grants.is_deny = true``). ``group_ref`` / ``principal_ref`` are
    opaque identifiers carried only for auditability — evaluation already
    receives the grants pre-filtered to the principal, so they are not used in
    the decision.
    """

    path: str
    action: str
    allow: bool = True
    group_ref: str | None = None
    principal_ref: str | None = None

    @property
    def specificity(self) -> int:
        """Number of ltree labels — the longest-prefix-wins sort key. A deeper
        (more specific) grant path has more labels and beats a shallower one."""
        return len(self.path.split("."))


DecisionReason = Literal[
    "admin_bypass",
    "explicit_deny",
    "explicit_allow",
    "no_grant_default_deny",
    "ceiling_clamped",
]


@dataclass(frozen=True, slots=True)
class Decision:
    """Result of :func:`evaluate`. ``grant`` is the winning grant (for the audit
    log), or ``None`` for admin-bypass / default-deny outcomes that no single
    grant produced."""

    allowed: bool
    reason: DecisionReason
    grant: PathGrant | None = None


def _is_ancestor_or_self(grant_path: str, item_path: str) -> bool:
    """True if ``grant_path`` is an ancestor of (or equal to) ``item_path`` in
    ltree terms — i.e. ``item_path <@ grant_path``. Label-boundary aware so
    ``lib_1.movies`` does NOT match ``lib_1.movies_extra`` (string-prefix would
    wrongly match; label-sequence prefix does not)."""
    g = grant_path.split(".")
    it = item_path.split(".")
    if len(g) > len(it):
        return False
    return it[: len(g)] == g


def evaluate(
    grants: list[PathGrant],
    role: Role,
    item_path: str,
    action: str,
) -> Decision:
    """Compute the effective permission for ``(role, item_path, action)`` given
    the principal's resolved ``grants`` (brief §2.5). PURE — no I/O.

    Algorithm:

    1. **Admin bypass** — ``admin`` global role short-circuits allow; its
       ceiling is "everything", so no path lookup is needed (step 1).
    2. **Ceiling clamp** — if ``action`` is outside the role's ceiling
       (:data:`ROLE_CEILINGS`), deny immediately; a grant can never widen past
       the role (step 1). This is why a ``viewer`` handed a ``modify`` grant is
       still denied.
    3. **Longest-prefix wins** — among grants for this ``action`` whose path is
       an ancestor-or-self of ``item_path``, only those at the maximum
       specificity (deepest matching path) are considered (step 4).
    4. **Explicit-deny-wins** — at that most-specific level, if any deny is
       present it wins over any allow (step 5, AWS-style).
    5. **Default deny** — no matching grant → deny for non-admins (step 6).
    """
    if role is Role.ADMIN:
        return Decision(True, "admin_bypass")

    if action not in ROLE_CEILINGS.get(role, frozenset()):
        # Action exceeds the role ceiling — no grant can rescue it.
        return Decision(False, "ceiling_clamped")

    matching = [g for g in grants if g.action == action and _is_ancestor_or_self(g.path, item_path)]
    if not matching:
        return Decision(False, "no_grant_default_deny")

    best_specificity = max(g.specificity for g in matching)
    finalists = [g for g in matching if g.specificity == best_specificity]

    # Explicit deny wins at equal specificity.
    denies = [g for g in finalists if not g.allow]
    if denies:
        return Decision(False, "explicit_deny", denies[0])

    allow = finalists[0]
    return Decision(True, "explicit_allow", allow)
