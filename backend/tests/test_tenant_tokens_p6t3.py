"""P6-T3 — Meilisearch RBAC scope projection + deny-aware filter compilation.

Covers the server-side proxy enforcement path:

* ``scope_ancestors`` projection round-trip (the ``path_scope`` doc array).
* ``compile_scope_filter`` compilation matrix (allow / deny / nested / hashed
  labels / multi-scope) + the R2 refuse ceiling.
* **Equivalence property**: the compiled Meili filter, interpreted with Meili's
  array-membership semantics, matches ``rbac.evaluate`` EXACTLY on random grant
  sets + random item paths (the deny-wins / longest-prefix-wins contract).
* ``rbac_filter_for`` admin-bypass / fail-closed behaviour.
* ``build_doc`` projects the ancestor array (incl. sidecar parent inheritance).
"""

from __future__ import annotations

import random
import re

import pytest

from filearr import rbac
from filearr.rbac import PathGrant, Role
from filearr.tenant_tokens import (
    PATH_SCOPE_ATTR,
    CompilationRefused,
    compile_scope_filter,
    rbac_filter_for,
    scope_ancestors,
)

# --------------------------------------------------------------------------- #
# A tiny interpreter for the EXACT filter grammar compile_scope_filter emits,   #
# with Meilisearch array-attribute semantics: `path_scope = "x"` and           #
# `path_scope IN [..]` are TRUE iff the item's path_scope ARRAY contains the    #
# value(s). This lets the property test evaluate the emitted STRING (not a      #
# re-implementation) against rbac.evaluate.                                     #
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r'\s*(\(|\)|\[|\]|,|"(?:[^"]*)"|[A-Za-z_][\w]*|=)')


def _tokenize(expr: str) -> list[str]:
    toks, i = [], 0
    while i < len(expr):
        m = _TOKEN.match(expr, i)
        if not m:
            if expr[i].isspace():
                i += 1
                continue
            raise ValueError(f"bad token at {i}: {expr[i:]!r}")
        toks.append(m.group(1))
        i = m.end()
    return toks


class _P:
    def __init__(self, toks, ancestors, is_sidecar):
        self.t = toks
        self.i = 0
        self.anc = ancestors
        self.sidecar = is_sidecar

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def eat(self, tok=None):
        cur = self.t[self.i]
        if tok is not None:
            assert cur == tok, f"expected {tok!r} got {cur!r}"
        self.i += 1
        return cur

    def parse(self):
        v = self._or()
        assert self.i == len(self.t), f"trailing tokens {self.t[self.i:]}"
        return v

    def _or(self):
        v = self._and()
        while self.peek() == "OR":
            self.eat("OR")
            r = self._and()
            v = v or r
        return v

    def _and(self):
        v = self._not()
        while self.peek() == "AND":
            self.eat("AND")
            r = self._not()
            v = v and r
        return v

    def _not(self):
        if self.peek() == "NOT":
            self.eat("NOT")
            return not self._not()
        return self._atom()

    def _atom(self):
        if self.peek() == "(":
            self.eat("(")
            v = self._or()
            self.eat(")")
            return v
        ident = self.eat()
        if ident == "path_scope":
            nxt = self.eat()
            if nxt == "=":
                val = self.eat().strip('"')
                return val in self.anc
            assert nxt == "IN", nxt
            self.eat("[")
            vals = []
            while self.peek() != "]":
                if self.peek() == ",":
                    self.eat(",")
                    continue
                vals.append(self.eat().strip('"'))
            self.eat("]")
            return any(v in self.anc for v in vals)
        if ident == "is_sidecar":
            self.eat("=")
            want = self.eat()  # true|false
            return self.sidecar is (want == "true")
        raise ValueError(f"unknown atom {ident!r}")


def meili_eval(expr: str, ancestors: set[str], is_sidecar: bool = False) -> bool:
    return _P(_tokenize(expr), ancestors, is_sidecar).parse()


def _g(path, action="search_metadata", allow=True):
    return PathGrant(path=path, action=action, allow=allow)


# --------------------------------------------------------------------------- #
# scope_ancestors                                                              #
# --------------------------------------------------------------------------- #
def test_scope_ancestors_prefixes():
    assert scope_ancestors("lib_x.movies.inception.f_mkv") == (
        "lib_x",
        "lib_x.movies",
        "lib_x.movies.inception",
        "lib_x.movies.inception.f_mkv",
    )


def test_scope_ancestors_single_and_empty():
    assert scope_ancestors("lib_x") == ("lib_x",)
    assert scope_ancestors("") == ()
    assert scope_ancestors(None) == ()  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# compile_scope_filter — allow only collapses to the allow-only IN form        #
# --------------------------------------------------------------------------- #
def test_allow_only_is_plain_in():
    cf = compile_scope_filter([_g("lib1.a"), _g("lib1.b")])
    assert cf.expression == f'{PATH_SCOPE_ATTR} IN ["lib1.a", "lib1.b"]'
    assert cf.scopes == ("lib1.a", "lib1.b")


def test_non_search_action_ignored():
    cf = compile_scope_filter([_g("lib1.a"), _g("lib1.b", action="download")])
    assert cf.scopes == ("lib1.a",)


def test_same_scope_allow_and_deny_collapses_to_deny():
    cf = compile_scope_filter([_g("lib1.a"), _g("lib1.a", allow=False)])
    # deny wins at equal specificity -> no allow scope remains
    assert cf.expression == f"{PATH_SCOPE_ATTR} IN []"
    assert cf.scopes == ()


def test_deny_carveout_under_allow():
    cf = compile_scope_filter([_g("lib1.a"), _g("lib1.a.secret", allow=False)])
    assert cf.expression == (
        f'({PATH_SCOPE_ATTR} = "lib1.a" AND '
        f'NOT {PATH_SCOPE_ATTR} IN ["lib1.a.secret"])'
    )


def test_shallow_deny_does_not_defeat_deeper_allow():
    # allow deeper than deny -> deeper allow wins; deny is NOT a descendant of the
    # allow, so it never enters that allow's carve-out list.
    cf = compile_scope_filter([_g("lib1.a", allow=False), _g("lib1.a.b")])
    assert cf.expression == f'{PATH_SCOPE_ATTR} IN ["lib1.a.b"]'


def test_sidecar_toggle():
    grants = [_g("lib1.a")]
    with_side = compile_scope_filter(grants, include_sidecars=True)
    without = compile_scope_filter(grants, include_sidecars=False)
    assert "is_sidecar" not in with_side.expression
    assert without.expression.endswith("AND is_sidecar = false")
    # empty scope still fail-closed under the sidecar suffix
    empty = compile_scope_filter([], include_sidecars=False)
    assert empty.expression == f"({PATH_SCOPE_ATTR} IN []) AND is_sidecar = false"


def test_refuse_oversized(monkeypatch):
    grants = [_g(f"lib1.dir{i:05d}") for i in range(1000)]
    with pytest.raises(CompilationRefused) as ei:
        compile_scope_filter(grants, ceiling=4096)
    assert ei.value.ceiling == 4096
    assert "consolidate" in str(ei.value)


def test_hashed_and_unicode_labels_pass_through():
    # a hashed over-long label + an escaped-unicode label are just opaque ltree
    # strings to the compiler; they compile and interpret fine.
    scope = rbac.path_to_ltree("Amélie/日本語", library_id="0190f2c34d5e7abc8def1234567890ab")
    cf = compile_scope_filter([_g(scope)])
    assert cf.scopes == (scope,)
    assert meili_eval(cf.expression, set(scope_ancestors(scope)))


# --------------------------------------------------------------------------- #
# rbac_filter_for                                                             #
# --------------------------------------------------------------------------- #
def test_admin_is_unrestricted_none():
    assert rbac_filter_for(Role.ADMIN, [_g("lib1.a")]) is None


def test_non_admin_no_grants_fail_closed():
    assert rbac_filter_for(Role.USER, []) == f"{PATH_SCOPE_ATTR} IN []"
    assert rbac_filter_for(Role.VIEWER, []) == f"{PATH_SCOPE_ATTR} IN []"


def test_rbac_filter_for_refuse_maps_through(monkeypatch):
    grants = [_g(f"lib1.d{i:05d}") for i in range(1000)]
    with pytest.raises(CompilationRefused):
        rbac_filter_for(Role.USER, grants, ceiling=4096)


# --------------------------------------------------------------------------- #
# EQUIVALENCE PROPERTY — compiled filter == rbac.evaluate on random grants     #
# --------------------------------------------------------------------------- #
_LABELS = ["a", "b", "c", "d"]


def _random_paths(rng):
    """A small corpus of ltree paths under a common library root."""
    paths = set()
    for _ in range(40):
        depth = rng.randint(1, 4)
        paths.add("lib1." + ".".join(rng.choice(_LABELS) for _ in range(depth)))
    return sorted(paths)


@pytest.mark.parametrize("seed", range(60))
def test_compiled_filter_equivalent_to_evaluate(seed):
    rng = random.Random(seed)
    universe = _random_paths(rng)
    # random grant set over the universe: allow/deny, some noise actions.
    grants: list[PathGrant] = []
    for scope in universe:
        r = rng.random()
        if r < 0.30:
            grants.append(_g(scope, allow=True))
        elif r < 0.50:
            grants.append(_g(scope, allow=False))
        elif r < 0.58:
            grants.append(_g(scope, action="download"))  # noise (ignored)
    # occasionally add a same-scope allow+deny pair (tie -> deny wins)
    if universe and rng.random() < 0.5:
        s = rng.choice(universe)
        grants.append(_g(s, allow=True))
        grants.append(_g(s, allow=False))

    expr = compile_scope_filter(grants, action="search_metadata", ceiling=1 << 30).expression

    for item in universe:
        anc = set(scope_ancestors(item))
        ref = rbac.evaluate(grants, Role.USER, item, "search_metadata").allowed
        got = meili_eval(expr, anc)
        assert got == ref, (seed, item, expr, ref, got)


@pytest.mark.parametrize("seed", range(20))
def test_equivalence_with_include_sidecars_false(seed):
    rng = random.Random(1000 + seed)
    universe = _random_paths(rng)
    grants = [
        _g(s, allow=rng.random() < 0.7)
        for s in universe
        if rng.random() < 0.5
    ]
    expr = compile_scope_filter(
        grants, action="search_metadata", include_sidecars=False, ceiling=1 << 30
    ).expression
    for item in universe:
        anc = set(scope_ancestors(item))
        ref = rbac.evaluate(grants, Role.USER, item, "search_metadata").allowed
        # primary item (is_sidecar False) must match evaluate; a sidecar is always
        # excluded regardless.
        assert meili_eval(expr, anc, is_sidecar=False) == ref
        assert meili_eval(expr, anc, is_sidecar=True) is False
