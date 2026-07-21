"""W7 — permissions scaffold: pure-core tests (no DB, no OS).

Covers the tested surface of ``filearr.permissions`` (the record schema
round-trip/validation, the well-known exclusion filter — the feature's headline
knob, and the snapshot-diff engine) PLUS the ``PermissionsConfig``/``AuditConfig``
additions to ``filearr.agent_config`` (defaults, validation, and that omitting
them leaves an existing ``GroupSettings`` valid). Also pins the two scaffold
invariants: the report builders are inert (raise, unregistered) and the intended
storage table is documented-only (not on ``Base.metadata``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from filearr import agent_config, permissions
from filearr.permissions import (
    Ace,
    AceScope,
    AceSource,
    AceType,
    Fidelity,
    NativeKind,
    PermissionRecord,
    Posture,
    Principal,
    PrincipalKind,
    Verb,
    diff_records,
    filter_entries,
    is_well_known,
)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _p(
    canonical_id: str,
    *,
    kind: PrincipalKind = PrincipalKind.user,
    source_identifier: str | None = None,
    resolved: bool = True,
    display: str | None = None,
    domain: str | None = None,
) -> Principal:
    return Principal(
        kind=kind,
        canonical_id=canonical_id,
        source_identifier=source_identifier or canonical_id,
        resolved=resolved,
        display=display,
        domain=domain,
    )


def _ace(
    principal: Principal,
    *,
    type: AceType = AceType.allow,
    verbs: tuple[Verb, ...] = (Verb.read,),
    raw_mask: str = "0x120089",
    inherited: bool = False,
    scope: AceScope = AceScope.this,
    source: AceSource = AceSource.local,
    native_kind: NativeKind | None = NativeKind.ntfs,
    inherit_flags: tuple[str, ...] = (),
    order_index: int = 0,
) -> Ace:
    return Ace(
        principal=principal,
        type=type,
        verbs=verbs,
        raw_mask=raw_mask,
        inherited=inherited,
        scope=scope,
        source=source,
        native_kind=native_kind,
        inherit_flags=inherit_flags,
        order_index=order_index,
    )


# --------------------------------------------------------------------------- #
# 1. record schema: round-trip + validation                                   #
# --------------------------------------------------------------------------- #
def test_record_json_round_trip():
    rec = PermissionRecord(
        owner=_p("S-1-5-21-1-2-3-1001", display="CORP\\jsmith", domain="CORP"),
        group=_p("S-1-5-21-1-2-3-513", kind=PrincipalKind.group),
        entries=(
            _ace(_p("S-1-5-21-1-2-3-1001"), verbs=(Verb.read, Verb.write)),
            _ace(
                _p("S-1-1-0", kind=PrincipalKind.well_known),
                inherited=True,
                scope=AceScope.subtree,
                inherit_flags=("container_inherit", "object_inherit"),
            ),
        ),
        fidelity=Fidelity.full_native,
        posture=Posture(dacl_present=True, dacl_canonical=True),
    )
    dumped = rec.model_dump_json()
    back = PermissionRecord.model_validate_json(dumped)
    assert back == rec
    # verbs/enums round-trip as their string values
    assert back.entries[0].verbs == (Verb.read, Verb.write)
    assert back.entries[1].scope is AceScope.subtree


def test_principal_and_ace_are_frozen():
    p = _p("uid:1000")
    with pytest.raises(ValidationError):
        p.canonical_id = "uid:2"  # frozen
    a = _ace(p)
    with pytest.raises(ValidationError):
        a.raw_mask = "0x0"


def test_record_rejects_unknown_field_and_bad_enum():
    with pytest.raises(ValidationError):
        Principal(
            kind="user",
            canonical_id="x",
            source_identifier="x",
            bogus=True,  # extra=forbid
        )
    with pytest.raises(ValidationError):
        Ace(principal=_p("x"), type="maybe")  # not allow|deny
    with pytest.raises(ValidationError):
        Ace(principal=_p("x"), type=AceType.allow, verbs=("fly",))  # not a Verb


def test_record_defaults_minimal():
    rec = PermissionRecord()
    assert rec.owner is None and rec.group is None
    assert rec.entries == ()
    assert rec.fidelity is Fidelity.full_native
    assert rec.raw_native is None


# --------------------------------------------------------------------------- #
# 2. verb enum                                                                 #
# --------------------------------------------------------------------------- #
def test_verb_enum_covers_headline_verbs():
    for name in ("read", "write", "execute", "delete", "change_perms",
                 "take_ownership", "full"):
        assert Verb(name).value == name


# --------------------------------------------------------------------------- #
# 3. well-known table + exclusion filter (the headline knob)                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cid",
    [
        "S-1-5-18",  # SYSTEM
        "S-1-1-0",  # Everyone
        "S-1-3-0",  # CREATOR OWNER
        "S-1-5-32-544",  # Administrators (BUILTIN prefix)
        "S-1-5-32-545",  # Users (BUILTIN prefix)
        "S-1-5-11",  # Authenticated Users
        "0",  # POSIX root uid
        "uid:0",  # tag-wrapped root
        "local:nas01:0",  # host-qualified root
        "root",  # POSIX name
    ],
)
def test_is_well_known_matches_table(cid):
    assert is_well_known(_p(cid, kind=PrincipalKind.user)) is True


@pytest.mark.parametrize(
    "cid",
    ["S-1-5-21-1-2-3-1001", "uid:1000", "local:nas01:1000", "CORP\\jsmith"],
)
def test_is_well_known_leaves_real_principals(cid):
    assert is_well_known(_p(cid)) is False


def test_is_well_known_honors_agent_classification():
    # even a non-table canonical id is well-known if the agent classified it so
    assert is_well_known(_p("weird-id", kind=PrincipalKind.well_known)) is True


def test_filter_defaults_drop_well_known_and_inherited():
    real = _ace(_p("S-1-5-21-1-2-3-1001"), verbs=(Verb.read, Verb.write))
    system = _ace(_p("S-1-5-18", kind=PrincipalKind.well_known))
    inherited = _ace(_p("S-1-5-21-1-2-3-1002"), inherited=True)
    rec = PermissionRecord(
        owner=_p("S-1-5-18", kind=PrincipalKind.well_known),  # owner NOT filtered
        entries=(real, system, inherited),
    )
    out = filter_entries(rec)  # defaults: exclude_well_known=True, include_inherited=False
    assert out.entries == (real,)
    # owner is never filtered (a report always shows who owns the object)
    assert out.owner == rec.owner
    # input untouched (pure)
    assert len(rec.entries) == 3


def test_filter_include_inherited_keeps_inherited_non_wellknown():
    real = _ace(_p("S-1-5-21-1-2-3-1001"))
    inherited = _ace(_p("S-1-5-21-1-2-3-1002"), inherited=True)
    system = _ace(_p("S-1-5-18", kind=PrincipalKind.well_known), inherited=True)
    rec = PermissionRecord(entries=(real, inherited, system))
    out = filter_entries(rec, include_inherited=True)
    # inherited kept, but the well-known system ACE still dropped
    assert out.entries == (real, inherited)


def test_filter_keep_well_known_when_disabled():
    system = _ace(_p("S-1-5-18", kind=PrincipalKind.well_known))
    real = _ace(_p("S-1-5-21-1-2-3-1001"))
    rec = PermissionRecord(entries=(system, real))
    out = filter_entries(rec, exclude_well_known=False)
    assert out.entries == (system, real)


def test_filter_explicit_exclude_principals():
    a = _ace(_p("S-1-5-21-1-2-3-1001"))
    b = _ace(_p("S-1-5-21-1-2-3-1002", source_identifier="raw-b"))
    rec = PermissionRecord(entries=(a, b))
    # exclude by canonical id
    assert filter_entries(rec, exclude_principals=["S-1-5-21-1-2-3-1001"]).entries == (b,)
    # exclude by raw source identifier
    assert filter_entries(rec, exclude_principals=["raw-b"]).entries == (a,)
    # empty/blank entries are ignored
    assert filter_entries(rec, exclude_principals=["", "  "]).entries == (a, b)


# --------------------------------------------------------------------------- #
# 4. snapshot-diff engine                                                      #
# --------------------------------------------------------------------------- #
def test_diff_first_snapshot_all_added():
    new = PermissionRecord(
        owner=_p("S-1-5-21-1-2-3-1001"),
        entries=(_ace(_p("S-1-5-21-1-2-3-1001")),),
    )
    d = diff_records(None, new)
    assert len(d.added) == 1 and not d.removed and not d.modified
    assert d.owner_changed is True
    assert d.owner_before is None and d.owner_after == new.owner
    assert not d.is_empty


def test_diff_deletion_all_removed():
    old = PermissionRecord(
        owner=_p("S-1-5-21-1-2-3-1001"),
        entries=(_ace(_p("S-1-5-21-1-2-3-1001")),),
    )
    d = diff_records(old, None)
    assert len(d.removed) == 1 and not d.added and not d.modified
    assert d.owner_changed is True and d.owner_after is None


def test_diff_both_none_is_empty():
    d = diff_records(None, None)
    assert d.is_empty
    assert not d.owner_changed and not d.group_changed


def test_diff_add_remove_and_stable():
    p_keep = _p("S-1-5-21-1-2-3-1001")
    p_gone = _p("S-1-5-21-1-2-3-1002")
    p_new = _p("S-1-5-21-1-2-3-1003")
    keep = _ace(p_keep)
    old = PermissionRecord(owner=p_keep, entries=(keep, _ace(p_gone)))
    new = PermissionRecord(owner=p_keep, entries=(keep, _ace(p_new)))
    d = diff_records(old, new)
    assert {a.principal.canonical_id for a in d.added} == {"S-1-5-21-1-2-3-1003"}
    assert {a.principal.canonical_id for a in d.removed} == {"S-1-5-21-1-2-3-1002"}
    assert not d.modified
    assert d.owner_changed is False  # same owner canonical id


def test_diff_mask_and_rights_change_is_modification():
    p = _p("S-1-5-21-1-2-3-1001")
    old = PermissionRecord(entries=(_ace(p, verbs=(Verb.read,), raw_mask="0x120089"),))
    new = PermissionRecord(
        entries=(_ace(p, verbs=(Verb.read, Verb.write), raw_mask="0x1301bf"),)
    )
    d = diff_records(old, new)
    assert not d.added and not d.removed
    assert len(d.modified) == 1
    mod = d.modified[0]
    assert mod.before.verbs == (Verb.read,)
    assert mod.after.verbs == (Verb.read, Verb.write)


def test_diff_verb_reorder_is_not_a_modification():
    p = _p("S-1-5-21-1-2-3-1001")
    old = PermissionRecord(entries=(_ace(p, verbs=(Verb.read, Verb.write)),))
    new = PermissionRecord(entries=(_ace(p, verbs=(Verb.write, Verb.read)),))
    d = diff_records(old, new)
    assert d.is_empty  # order-independent verb comparison


def test_diff_owner_change_only():
    p = _p("S-1-5-21-1-2-3-1001")
    ace = _ace(p)
    old = PermissionRecord(owner=_p("S-1-5-21-1-2-3-1001"), entries=(ace,))
    new = PermissionRecord(owner=_p("S-1-5-21-1-2-3-2002"), entries=(ace,))
    d = diff_records(old, new)
    assert not d.added and not d.removed and not d.modified
    assert d.owner_changed is True
    assert not d.is_empty


def test_diff_allow_vs_deny_same_principal_are_distinct_keys():
    p = _p("S-1-5-21-1-2-3-1001")
    old = PermissionRecord(entries=(_ace(p, type=AceType.allow),))
    new = PermissionRecord(entries=(_ace(p, type=AceType.deny),))
    d = diff_records(old, new)
    # different `type` -> different key -> one added + one removed, no modify
    assert len(d.added) == 1 and len(d.removed) == 1 and not d.modified


# --------------------------------------------------------------------------- #
# 5. scaffold invariants: reports inert + DDL documented-only                   #
# --------------------------------------------------------------------------- #
def test_report_builders_are_inert_stubs():
    for fn in (
        permissions.permissions_report_access_by_principal,
        permissions._broad_access,
        permissions._explicit_ace_outliers,
        permissions._permission_drift,
    ):
        with pytest.raises(NotImplementedError, match="scaffold, W7-"):
            fn(None)  # type: ignore[arg-type] — raises before touching the arg


def test_permission_snapshots_not_registered_on_metadata():
    from filearr.models import Base

    assert "permission_snapshots" not in Base.metadata.tables
    # the intended DDL is documented as an inert source string
    assert "class PermissionSnapshot(Base)" in permissions.INTENDED_PERMISSION_SNAPSHOTS_DDL


def test_reports_registry_has_no_permissions_report():
    from filearr.reports import CANNED_REPORTS

    assert not any("perm" in rid for rid in CANNED_REPORTS)


# --------------------------------------------------------------------------- #
# 6. PermissionsConfig / AuditConfig (agent_config additions)                   #
# --------------------------------------------------------------------------- #
def test_permissions_config_defaults_off():
    cfg = agent_config.PermissionsConfig()
    assert cfg.enabled is False
    assert cfg.resolve_names is True
    assert cfg.include_inherited is False
    assert cfg.include_effective_access is False
    assert cfg.exclude_well_known is True  # opt-in default: hide base permissions
    assert cfg.exclude_principals == []
    assert cfg.collect_share_acls is False
    assert cfg.audit is None


def test_audit_config_defaults():
    ac = agent_config.AuditConfig()
    assert ac.enabled is False
    assert ac.retain_snapshots == 10
    assert ac.alert_on_change is False
    assert ac.watch_paths == []


def test_permissions_config_extra_forbid():
    with pytest.raises(ValidationError):
        agent_config.PermissionsConfig(bogus=True)
    with pytest.raises(ValidationError):
        agent_config.AuditConfig(bogus=True)


def test_permissions_config_bounds():
    with pytest.raises(ValidationError):
        agent_config.AuditConfig(retain_snapshots=0)
    with pytest.raises(ValidationError):
        agent_config.AuditConfig(retain_snapshots=agent_config.MAX_RETAIN_SNAPSHOTS + 1)
    with pytest.raises(ValidationError):
        agent_config.PermissionsConfig(
            exclude_principals=["x"] * (agent_config.MAX_EXCLUDE_PRINCIPALS + 1)
        )
    with pytest.raises(ValidationError):
        agent_config.AuditConfig(watch_paths=["/data/[unbalanced"])


# --------------------------------------------------------------------------- #
# 7. settings integration: additive, opt-in, backward compatible               #
# --------------------------------------------------------------------------- #
def test_settings_valid_with_permissions_block():
    agent_config.validate_settings(
        {
            "inventory": {
                "enabled": True,
                "collectors": ["stat", "owner", "permissions"],
                "permissions": {
                    "enabled": True,
                    "exclude_well_known": True,
                    "include_inherited": False,
                    "exclude_principals": ["S-1-5-21-1-2-3-1001"],
                    "collect_share_acls": True,
                    "audit": {
                        "enabled": True,
                        "retain_snapshots": 20,
                        "alert_on_change": True,
                        "watch_paths": ["/data/share", "%USERPROFILE%/Documents"],
                    },
                },
            }
        }
    )


def test_settings_still_valid_without_permissions_block():
    # existing shape (no permissions field) stays valid — purely additive
    agent_config.validate_settings(
        {"inventory": {"enabled": True, "collectors": ["stat", "owner"]}}
    )


def test_settings_rejects_bad_permissions_block():
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings(
            {"inventory": {"enabled": True, "permissions": {"bogus": 1}}}
        )
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings(
            {
                "inventory": {
                    "enabled": True,
                    "permissions": {"audit": {"retain_snapshots": 0}},
                }
            }
        )
