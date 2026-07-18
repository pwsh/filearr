"""Phase 6 scaffolding tests — pure RBAC + tenant-token compilation.

No Postgres, no Meilisearch, no network, no argon2/LDAP/SAML/OIDC deps: only the
pure functions in ``filearr.rbac`` (``encode_path_label`` / ``path_to_ltree`` /
``evaluate``) and ``filearr.tenant_tokens`` (``compile_filter``). Guards the
inert scaffolding so the implementing tasks (P6-T1..T3) inherit green coverage of
the injective path encoder (Architect ruling R1), the ceiling+longest-prefix+
explicit-deny evaluation algorithm, and the R2 filter-size refusal.
"""

import re as _re
import unicodedata as _ud

import pytest

from filearr import authx
from filearr.rbac import (
    ACTIONS,
    HASHED_LABEL,
    LTREE_LABEL_MAX_ENCODED,
    ROLE_CEILINGS,
    Decision,
    PathGrant,
    Role,
    decode_path_label,
    encode_path_label,
    encode_rel_path,
    evaluate,
    library_label,
    path_to_ltree,
)
from filearr.tenant_tokens import (
    FILTER_SIZE_CEILING,
    CompilationRefused,
    compile_filter,
    mint_tenant_token,
)

# --- Action vocabulary / role ceilings -------------------------------------


def test_actions_and_ceilings_consistent():
    # Every ceiling is a subset of the full action vocabulary.
    for role, ceiling in ROLE_CEILINGS.items():
        assert ceiling <= ACTIONS, role
    # Admin ceiling is everything; viewer is strictly read-only; user in between.
    assert ROLE_CEILINGS[Role.ADMIN] == ACTIONS
    assert ROLE_CEILINGS[Role.VIEWER] == frozenset({"search_metadata", "search_content"})
    assert "modify" not in ROLE_CEILINGS[Role.VIEWER]
    assert "download" not in ROLE_CEILINGS[Role.VIEWER]
    assert "delete" not in ROLE_CEILINGS[Role.USER]  # delete is admin-only
    assert "modify" in ROLE_CEILINGS[Role.USER]


# --- Path-label encoder: injectivity (R1) ----------------------------------

ROUND_TRIP_SAMPLES = [
    "movies",
    "Movies",  # uppercase -> escaped, must differ from lowercase
    "foo",
    "Foo",
    "2020",  # pure digits pass through
    "007",
    "",  # empty segment
    "a.b",  # literal dot inside a segment (NOT a separator here)
    "hello world",  # space
    "  ",  # only spaces
    "Amélie",  # unicode accents
    "日本語",  # non-latin unicode
    "under_score",  # literal underscore must round-trip
    "_leading",
    "trailing_",
    "MiXeD_CaSe.123",
    "🎬movie",  # astral-plane emoji
    "a-b-c",  # hyphens
    "file (1)",  # parens + space
]


@pytest.mark.parametrize("seg", ROUND_TRIP_SAMPLES)
def test_encode_decode_round_trip(seg):
    label = encode_path_label(seg)
    # Label must be a valid ltree label: only [a-z0-9_], no dot.
    assert label, "no segment encodes to empty"
    assert all(c.islower() or c.isdigit() or c == "_" for c in label), label
    assert "." not in label
    assert decode_path_label(label) == seg


def test_encoder_is_injective_over_samples():
    labels = [encode_path_label(s) for s in ROUND_TRIP_SAMPLES]
    assert len(set(labels)) == len(labels), "collision in encoder"


def test_case_collision_avoided():
    # The headline R1 requirement: distinct case must encode differently.
    assert encode_path_label("Foo") != encode_path_label("foo")
    assert encode_path_label("MOVIES") != encode_path_label("movies")
    # Lowercase passes through verbatim; uppercase is hex-escaped.
    assert encode_path_label("foo") == "foo"
    assert encode_path_label("Foo") == "_46oo"


def test_empty_vs_nonempty_distinct():
    assert encode_path_label("") == "_"
    # "_" (empty) must not collide with any real segment's encoding.
    assert encode_path_label("") != encode_path_label("_")  # "_" -> "_5f"
    assert encode_path_label("_") == "_5f"


def test_dot_and_separator_do_not_collide():
    # A literal dot inside a segment must not create a false level boundary.
    lit = encode_path_label("a.b")  # single segment "a.b"
    assert "." not in lit
    # Two-segment path a / b encodes with a real separator dot between labels.
    two = path_to_ltree("a/b")
    assert two == "a.b"
    assert two != lit  # distinct structures stay distinct


def test_path_to_ltree_preserves_empty_segments():
    # Leading/trailing/double separators are preserved as "_" labels so the
    # segment SEQUENCE stays injective.
    assert path_to_ltree("a//b") == "a._.b"
    assert path_to_ltree("/a") == "_.a"
    assert path_to_ltree("a/") == "a._"


def test_path_to_ltree_unicode_and_digits():
    got = path_to_ltree("Action/2020/Amélie (2001).mkv")
    # Every label valid; round-trips segment-by-segment.
    for label in got.split("."):
        assert all(c.islower() or c.isdigit() or c == "_" for c in label)
    assert [decode_path_label(x) for x in got.split(".")] == [
        "Action",
        "2020",
        "Amélie (2001).mkv",
    ]


# --- evaluate(): ceiling + longest-prefix + explicit-deny ------------------


def _g(path, action="search_metadata", allow=True):
    return PathGrant(path=path, action=action, allow=allow)


def test_admin_bypass_needs_no_grant():
    d = evaluate([], Role.ADMIN, "lib1.movies.action", "delete")
    assert d.allowed
    assert d.reason == "admin_bypass"


def test_no_grant_defaults_deny_for_non_admin():
    d = evaluate([], Role.USER, "lib1.movies", "download")
    assert not d.allowed
    assert d.reason == "no_grant_default_deny"


def test_ceiling_clamp_viewer_cannot_be_granted_modify():
    # Even with an explicit allow grant, a viewer's ceiling forbids modify.
    grants = [_g("lib1.movies", action="modify", allow=True)]
    d = evaluate(grants, Role.VIEWER, "lib1.movies.action", "modify")
    assert not d.allowed
    assert d.reason == "ceiling_clamped"


def test_ceiling_clamp_user_cannot_delete():
    grants = [_g("lib1.movies", action="delete", allow=True)]
    d = evaluate(grants, Role.USER, "lib1.movies", "delete")
    assert not d.allowed
    assert d.reason == "ceiling_clamped"


def test_allow_within_ceiling():
    grants = [_g("lib1.movies", action="search_metadata")]
    d = evaluate(grants, Role.VIEWER, "lib1.movies.action.2020", "search_metadata")
    assert d.allowed
    assert d.reason == "explicit_allow"
    assert d.grant is grants[0]


def test_grant_does_not_leak_to_sibling_subtree():
    grants = [_g("lib1.movies.action")]
    denied = evaluate(grants, Role.USER, "lib1.movies.comedy", "search_metadata")
    assert not denied.allowed
    assert denied.reason == "no_grant_default_deny"


def test_label_boundary_no_false_prefix_match():
    # "lib1.movies" must NOT be treated as an ancestor of "lib1.movies_extra".
    grants = [_g("lib1.movies")]
    d = evaluate(grants, Role.USER, "lib1.movies_extra.file", "search_metadata")
    assert not d.allowed


def test_longest_prefix_wins_allow_over_shallower():
    # Deny broad, allow deep -> allow wins because it is MORE specific.
    grants = [
        _g("lib1.movies", allow=False),
        _g("lib1.movies.action", allow=True),
    ]
    d = evaluate(grants, Role.USER, "lib1.movies.action.2020", "search_metadata")
    assert d.allowed
    assert d.reason == "explicit_allow"
    assert d.grant.path == "lib1.movies.action"


def test_longest_prefix_wins_deny_over_shallower_allow():
    # Allow broad, deny deep -> deny wins (more specific).
    grants = [
        _g("lib1.movies", allow=True),
        _g("lib1.movies.action.2020", allow=False),
    ]
    d = evaluate(grants, Role.USER, "lib1.movies.action.2020.film", "search_metadata")
    assert not d.allowed
    assert d.reason == "explicit_deny"
    assert d.grant.path == "lib1.movies.action.2020"


def test_equal_specificity_deny_wins():
    # Allow and deny at the SAME path/specificity -> deny wins (AWS-style).
    grants = [
        _g("lib1.movies.action", allow=True),
        _g("lib1.movies.action", allow=False),
    ]
    d = evaluate(grants, Role.USER, "lib1.movies.action.2020", "search_metadata")
    assert not d.allowed
    assert d.reason == "explicit_deny"


def test_root_grant_covers_deep_item():
    grants = [_g("lib1")]
    d = evaluate(grants, Role.USER, "lib1.movies.action.2020.film", "search_metadata")
    assert d.allowed


def test_deny_at_root_allow_deeper_allow_wins_by_specificity():
    grants = [
        _g("lib1", allow=False),
        _g("lib1.movies.action", allow=True),
    ]
    d = evaluate(grants, Role.USER, "lib1.movies.action.2020", "search_metadata")
    assert d.allowed
    assert d.grant.path == "lib1.movies.action"


def test_action_is_scoped_grant_for_other_action_does_not_apply():
    grants = [_g("lib1.movies", action="search_metadata")]
    d = evaluate(grants, Role.USER, "lib1.movies", "download")
    assert not d.allowed
    assert d.reason == "no_grant_default_deny"


def test_evaluate_over_encoded_unicode_path():
    # Full pipeline: encode a real rel_path, grant on an ancestor, evaluate.
    item = "lib1." + path_to_ltree("Amélie/2001/film.mkv")
    grant_path = "lib1." + path_to_ltree("Amélie")
    grants = [_g(grant_path)]
    d = evaluate(grants, Role.USER, item, "search_metadata")
    assert d.allowed
    # A sibling unicode directory is not covered.
    other = "lib1." + path_to_ltree("Amelie/2001/film.mkv")  # no accent -> distinct
    assert not evaluate(grants, Role.USER, other, "search_metadata").allowed


# --- tenant_tokens.compile_filter ------------------------------------------


def test_compile_filter_basic():
    grants = [_g("lib1.movies.action"), _g("lib1.docs")]
    cf = compile_filter(grants)
    assert cf.expression.startswith("path_scope IN [")
    assert '"lib1.movies.action"' in cf.expression
    assert '"lib1.docs"' in cf.expression
    assert cf.scopes == ("lib1.docs", "lib1.movies.action")  # sorted


def test_compile_filter_dedups_and_ignores_non_search_and_denies():
    grants = [
        _g("lib1.a"),
        _g("lib1.a"),  # dup
        _g("lib1.b", action="download"),  # non-search action -> excluded
        _g("lib1.c", allow=False),  # deny -> excluded
    ]
    cf = compile_filter(grants)
    assert cf.scopes == ("lib1.a",)


def test_compile_filter_accepts_decisions():
    g = _g("lib1.movies")
    dec = Decision(True, "explicit_allow", g)
    cf = compile_filter([dec])
    assert cf.scopes == ("lib1.movies",)


def test_compile_filter_empty_is_fail_closed():
    cf = compile_filter([])
    assert cf.expression == "path_scope IN []"
    assert cf.scopes == ()


def test_compile_filter_sidecar_toggle():
    grants = [_g("lib1.movies")]
    with_side = compile_filter(grants, include_sidecars=True)
    without = compile_filter(grants, include_sidecars=False)
    assert "is_sidecar" not in with_side.expression
    assert without.expression.endswith("AND is_sidecar = false")


def test_compile_filter_refuses_oversized(monkeypatch):
    # R2: many discrete narrow grants blow the size ceiling -> refuse, never
    # silently coarsen.
    grants = [_g(f"lib1.dir{i:05d}") for i in range(1000)]
    with pytest.raises(CompilationRefused) as ei:
        compile_filter(grants)
    assert ei.value.ceiling == FILTER_SIZE_CEILING
    assert ei.value.clause_count == 1000
    assert "consolidate" in str(ei.value)


# --- stubs raise, tagged with their task -----------------------------------


def test_mint_tenant_token_stub_raises():
    with pytest.raises(NotImplementedError):
        mint_tenant_token(
            compile_filter([_g("lib1.a")]),
            parent_key="x",
            parent_key_uid="y",
            expires_in_seconds=60,
        )


# NOTE: ``authx.hash_password`` / ``verify_password`` / ``create_session`` /
# ``validate_session`` are IMPLEMENTED as of P6-T1 (argon2 + Postgres sessions);
# their behaviour is covered by ``tests/test_auth_p6.py``. Only the federated
# providers below remain stubs.


@pytest.mark.parametrize(
    "provider_cls",
    [
        authx.LocalPasswordProvider,
        # authx.OIDCProvider is IMPLEMENTED as of P6-T5 (see tests/test_oidc_p6t5.py);
        # only LDAP/SAML remain stubs.
        authx.LDAPProvider,
        authx.SAMLProvider,
    ],
)
def test_authx_provider_stubs_raise(provider_cls):
    p = provider_cls()
    assert isinstance(p, authx.AuthProvider)  # runtime_checkable Protocol
    with pytest.raises(NotImplementedError):
        p.authenticate({"username": "u", "password": "p"})
    with pytest.raises(NotImplementedError):
        p.resolve_groups("subject")


# --------------------------------------------------------------------------- #
# P6-T2a spike — adversarial injectivity vectors (R1) + byte-exact NFC/NFD    #
# (R7). See docs/tasks/phase-6-identity-auth-rbac-tasks.md § P6-T2a findings.  #
# --------------------------------------------------------------------------- #
_LTREE_LABEL_RE = _re.compile(r"^[A-Za-z0-9_]+$")

# Every vector below is a distinct real directory name an attacker might use to
# try to collide two ACL scopes onto one ltree label (a silent access-control
# breach). The corpus deliberately includes invisible/normalization/control/
# boundary cases.
SPIKE_VECTORS = [
    "​",              # U+200B zero-width space (invisible)
    "‍",              # U+200D zero-width joiner
    "‮",              # U+202E right-to-left override
    "‪",              # U+202A left-to-right embedding
    "é",             # U+00E9 precomposed (NFC)
    "é",        # e + U+0301 combining acute (NFD) — MUST differ from NFC
    "\x00",           # null byte
    "\n",             # newline
    "\t",             # tab
    "\x7f",           # DEL control char
    "a\\b",           # backslash (Windows sep) — ONE label, not a hierarchy
    "a/b",            # forward slash inside a pre-split segment
    "a b",            # space
    "a.b",            # literal dot inside a segment
    "a_b",            # literal underscore
    "Ff",             # uppercase then lowercase hex-letter (escape-boundary)
    "fF",
    "_46",            # looks like an escape but is literal text
    "😀",             # non-BMP emoji (4-byte UTF-8)
    "café",          # mixed ASCII + Latin-1
    "CAFÉ",
    "Москва",         # Cyrillic
    "东京",            # CJK
    "",               # empty
    "_",              # lone underscore
]


@pytest.mark.parametrize("seg", SPIKE_VECTORS)
def test_spike_vectors_round_trip_and_valid_label(seg):
    label = encode_path_label(seg)
    assert _LTREE_LABEL_RE.match(label), f"{seg!r} produced non-ltree label {label!r}"
    assert decode_path_label(label) == seg  # total left inverse => injective here


def test_spike_corpus_is_collision_free():
    labels = [encode_path_label(s) for s in SPIKE_VECTORS]
    assert len(set(labels)) == len(labels), "collision in adversarial corpus"


def test_spike_nfc_nfd_stay_distinct_r7():
    """R7: byte-exact, no Unicode normalization. Precomposed 'é' (NFC) and
    decomposed 'e'+U+0301 (NFD) are visually identical but distinct ACL scopes —
    the encoder must keep them apart (fails closed: under-grant possible,
    over-grant impossible)."""
    nfc = "é"
    nfd = "é"
    assert _ud.normalize("NFC", nfd) == nfc  # they ARE visually equivalent
    assert encode_path_label(nfc) != encode_path_label(nfd)  # ...but never merged


def test_spike_zero_width_not_swallowed():
    # A zero-width char makes "ab" and "a<zwsp>b" different real names → must not
    # collide (an invisible-character scope-confusion attack).
    assert encode_path_label("ab") != encode_path_label("a​b")


def test_spike_escape_boundary_unambiguous():
    # Fixed-width escapes: a passthrough hex-letter after an escape is never
    # absorbed into the previous escape's hex run.
    assert encode_path_label("Ff") == "_46f"  # 'F'->_46, 'f' passthrough
    assert decode_path_label("_46f") == "Ff"
    # Literal "_46" text (underscore + digits) is escaped, not confused with an
    # escape sequence on decode.
    assert decode_path_label(encode_path_label("_46")) == "_46"


def test_overlong_segment_is_hashed_to_fixed_width_label():
    """P6-T2 directive 1: a segment whose ENCODED form exceeds
    ``LTREE_LABEL_MAX_ENCODED`` (200) is replaced by a fixed-width ``h__<hex>``
    hash label instead of the raw escape, so no label ever exceeds the ltree
    ceiling. 255 uppercase 'A' would escape to 765 chars (``_41`` x255); it is
    hashed instead."""
    seg255 = "A" * 255
    label = encode_path_label(seg255)
    assert label.startswith("h__")
    assert len(label) <= 20  # h__ + 16 hex
    assert _LTREE_LABEL_RE.match(label)  # still a valid ltree label
    # A hashed label is ONE-WAY: decode returns the marker, not the original.
    assert decode_path_label(label) == HASHED_LABEL
    # Distinct over-long segments still map to distinct labels (injective input).
    assert encode_path_label("A" * 256) != label
    assert encode_path_label("B" * 255) != label


def test_hash_threshold_boundary():
    """A segment encoding to exactly the ceiling stays literal; one char over
    tips into the hashed form. Lowercase passthrough encodes 1:1 so a 200-char
    lowercase segment is literal; 201 is still literal (<=200 is the ENCODED
    length, and 201 lowercase = 201 encoded > 200 -> hashed)."""
    at_ceiling = "a" * LTREE_LABEL_MAX_ENCODED  # 200 passthrough -> 200 encoded
    assert not encode_path_label(at_ceiling).startswith("h__")
    assert decode_path_label(encode_path_label(at_ceiling)) == at_ceiling
    over = "a" * (LTREE_LABEL_MAX_ENCODED + 1)
    assert encode_path_label(over).startswith("h__")


def test_hash_sentinel_disjoint_from_base_encoding():
    """The ``h__`` sentinel is UNREACHABLE by the base encoder (a normal encoding
    emits ``_`` only inside a fixed-width ``_XX`` escape, so adjacent ``__`` never
    occurs). A literal segment that LOOKS like the sentinel encodes its
    underscores, so it can never collide with a real hashed label."""
    looks_like = encode_path_label("h__deadbeefdeadbeef")
    assert "__" not in looks_like  # underscores were escaped to _5f
    assert not looks_like.startswith("h__")


def test_spike_lib_uuid_prefix_must_be_hyphen_free():
    """P6-T2 directive 2: ``library_label`` builds the prefix from the
    hyphen-free ``uuid.hex`` — the canonical dashed uuid form is an INVALID ltree
    label (``-`` is outside ``[A-Za-z0-9_]``)."""
    import uuid as _uuid

    canonical = "lib_0190f2c3-4d5e-7abc-8def-1234567890ab"
    assert not _LTREE_LABEL_RE.match(canonical)  # hyphens are invalid
    lid = _uuid.UUID("0190f2c3-4d5e-7abc-8def-1234567890ab")
    label = library_label(lid)
    assert label == "lib_0190f2c34d5e7abc8def1234567890ab"
    assert _LTREE_LABEL_RE.match(label)  # the real function yields a valid label
    assert library_label(str(lid)) == label  # accepts a string uuid too


def test_path_to_ltree_prefixes_library():
    """``path_to_ltree(rel, library_id=...)`` prepends the ``lib_<hex>`` label;
    without it the result is path-only (== ``encode_rel_path``)."""
    import uuid as _uuid

    lid = _uuid.UUID("0190f2c3-4d5e-7abc-8def-1234567890ab")
    scoped = path_to_ltree("Action/2020", library_id=lid)
    assert scoped == library_label(lid) + "." + encode_rel_path("Action/2020")
    assert path_to_ltree("Action/2020") == encode_rel_path("Action/2020")


def test_spike_ltree_depth_note():
    """ltree caps a path at 65535 labels; a real rel_path is far below that.
    path_to_ltree preserves one label per segment (empty segments become the '_'
    sentinel) so depth == segment count — no silent truncation."""
    deep = "/".join(str(i) for i in range(300))
    encoded = path_to_ltree(deep)
    assert encoded.count(".") + 1 == 300
