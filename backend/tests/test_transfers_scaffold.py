"""Phase 10 scaffolding tests — pure share-resolution + transfer-lifecycle core.

Pure unit tests (no Postgres, no network, no filesystem): guards the inert
scaffolding in :mod:`filearr.transfers` so the implementing tasks
(P10-T11/T12/T13) inherit green coverage of the share-mapping resolver, the
retrieve state machine, and the traversal-proof staging path.
"""

from __future__ import annotations

import uuid

import pytest

from filearr.transfers import (
    STAGING_ROOT,
    AgentCommand,
    ShareHint,
    ShareMapping,
    TransferRequest,
    classify_prefix,
    resolve_share_url,
    staging_path_for,
    transfer_state_machine,
)

# --- classify_prefix (server mirror of frontend pathlinks) -----------------


@pytest.mark.parametrize(
    "prefix,kind",
    [
        (r"\\tower\media", "unc"),
        ("smb://tower/media", "url"),
        ("ftp://host/pub", "url"),
        ("file:///Volumes/media", "url"),
        ("/Volumes/media", "posix"),
        ("/data/l", "posix"),
        ("", "unknown"),
        ("tower/media", "unknown"),
        ("C:/media", "unknown"),  # bare drive-letter is not a URL scheme
    ],
)
def test_classify_prefix(prefix, kind):
    assert classify_prefix(prefix) == kind


# --- resolve_share_url: longest-prefix matrix ------------------------------


def _m(local, share, agent_id=None):
    return ShareMapping(local_prefix=local, share_prefix=share, agent_id=agent_id)


def test_unc_prefix_uses_backslash_separators():
    maps = [_m("/srv/media", r"\\tower\media")]
    assert resolve_share_url(maps, "a1", "/srv/media/Movies/x.mkv") == r"\\tower\media\Movies\x.mkv"


def test_smb_url_prefix_uses_forward_slashes():
    maps = [_m("/srv/media", "smb://tower/media")]
    assert (
        resolve_share_url(maps, "a1", "/srv/media/Movies/x.mkv")
        == "smb://tower/media/Movies/x.mkv"
    )


def test_posix_prefix_join():
    maps = [_m("/srv/media", "/Volumes/media")]
    assert resolve_share_url(maps, "a1", "/srv/media/a/b.txt") == "/Volumes/media/a/b.txt"


def test_windows_local_path_separator_safe():
    # Agent reports a native Windows path; mapping local_prefix is Windows too.
    maps = [_m(r"C:\media", r"\\tower\media")]
    assert (
        resolve_share_url(maps, "a1", r"C:\media\TV\ep.mkv") == r"\\tower\media\TV\ep.mkv"
    )


def test_longest_prefix_wins():
    maps = [
        _m("/srv", "smb://tower/root"),
        _m("/srv/media", "smb://tower/media"),
    ]
    # The deeper mapping wins even though both cover the path.
    assert (
        resolve_share_url(maps, "a1", "/srv/media/x") == "smb://tower/media/x"
    )


def test_shallower_prefix_used_when_only_it_covers():
    maps = [
        _m("/srv", "smb://tower/root"),
        _m("/srv/media", "smb://tower/media"),
    ]
    assert resolve_share_url(maps, "a1", "/srv/other/y") == "smb://tower/root/other/y"


def test_agent_scoped_mapping_only_matches_that_agent():
    maps = [_m("/srv/media", "smb://tower/media", agent_id="a1")]
    assert resolve_share_url(maps, "a1", "/srv/media/x") == "smb://tower/media/x"
    assert resolve_share_url(maps, "a2", "/srv/media/x") is None


def test_global_mapping_matches_any_agent():
    maps = [_m("/srv/media", "smb://tower/media", agent_id=None)]
    assert resolve_share_url(maps, "whoever", "/srv/media/x") == "smb://tower/media/x"


def test_agent_specific_beats_global_at_equal_length():
    maps = [
        _m("/srv/media", "smb://global/media", agent_id=None),
        _m("/srv/media", "smb://agent/media", agent_id="a1"),
    ]
    assert resolve_share_url(maps, "a1", "/srv/media/x") == "smb://agent/media/x"


def test_no_covering_mapping_returns_none():
    maps = [_m("/srv/media", "smb://tower/media")]
    assert resolve_share_url(maps, "a1", "/other/path") is None


def test_empty_mappings_returns_none():
    assert resolve_share_url([], "a1", "/srv/media/x") is None


def test_prefix_boundary_not_string_prefix():
    # /srv/media must NOT match /srv/media-extra (segment boundary, not substring).
    maps = [_m("/srv/media", "smb://tower/media")]
    assert resolve_share_url(maps, "a1", "/srv/media-extra/x") is None


def test_exact_prefix_match_yields_base_only():
    maps = [_m("/srv/media", "smb://tower/media")]
    assert resolve_share_url(maps, "a1", "/srv/media") == "smb://tower/media"


def test_case_preserved_in_output():
    maps = [_m("/srv/Media", r"\\Tower\Media")]
    assert resolve_share_url(maps, "a1", "/srv/Media/Foo/Bar.MKV") == r"\\Tower\Media\Foo\Bar.MKV"


def test_case_sensitive_matching():
    # Matching is case-sensitive (same discipline as resolve_scan_path).
    maps = [_m("/srv/media", "smb://tower/media")]
    assert resolve_share_url(maps, "a1", "/SRV/MEDIA/x") is None


# --- transfer_state_machine matrix -----------------------------------------


@pytest.mark.parametrize(
    "current,event,expected",
    [
        ("pending", "start_upload", "uploading"),
        ("pending", "expire", "expired"),
        ("pending", "fail", "failed"),
        ("uploading", "staged", "staged"),
        ("uploading", "expire", "expired"),
        ("uploading", "fail", "failed"),
        ("staged", "download", "downloaded"),
        ("staged", "expire", "expired"),
        ("staged", "fail", "failed"),
    ],
)
def test_state_machine_valid_transitions(current, event, expected):
    assert transfer_state_machine(current, event) == expected


def test_state_machine_full_happy_path():
    s = "pending"
    for ev, nxt in [
        ("start_upload", "uploading"),
        ("staged", "staged"),
        ("download", "downloaded"),
    ]:
        s = transfer_state_machine(s, ev)
        assert s == nxt


@pytest.mark.parametrize(
    "current,event",
    [
        ("staged", "start_upload"),  # out of order
        ("pending", "download"),  # skip ahead
        ("pending", "staged"),  # skip ahead
        ("downloaded", "download"),  # terminal
        ("downloaded", "fail"),  # terminal
        ("expired", "start_upload"),  # terminal
        ("failed", "start_upload"),  # terminal
        ("uploading", "download"),  # must go via staged
    ],
)
def test_state_machine_invalid_transitions_raise(current, event):
    with pytest.raises(ValueError):
        transfer_state_machine(current, event)


# --- staging_path_for: uuid-only, traversal-proof --------------------------


def test_staging_path_is_under_root_and_uuid_named():
    tid = str(uuid.uuid4())
    path = staging_path_for(tid)
    assert path == f"{STAGING_ROOT}/{tid}.bin"
    assert path.startswith(STAGING_ROOT + "/")


def test_staging_path_canonicalises_uuid():
    # Upper-case / brace-wrapped forms canonicalise to the same lower-case path.
    tid = uuid.uuid4()
    assert staging_path_for(str(tid).upper()) == f"{STAGING_ROOT}/{tid}.bin"


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "..",
        "/etc/passwd",
        "a/b",
        "not-a-uuid",
        "",
        "12345",
    ],
)
def test_staging_path_rejects_non_uuid(bad):
    with pytest.raises(ValueError):
        staging_path_for(bad)


def test_staging_path_no_traversal_possible():
    # Even a crafted string cannot escape the root: it must parse as a UUID first.
    for tid in [str(uuid.uuid4()) for _ in range(5)]:
        p = staging_path_for(tid)
        assert ".." not in p
        assert p.count("/") == STAGING_ROOT.count("/") + 1


# --- contract models are real ----------------------------------------------


def test_transfer_request_defaults():
    req = TransferRequest()
    assert req.verify_hash is True
    assert req.max_bytes_per_sec is None


def test_transfer_request_rejects_nonpositive_rate():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        TransferRequest(max_bytes_per_sec=0)
    with pytest.raises(pydantic.ValidationError):
        TransferRequest(max_bytes_per_sec=-1)


def test_transfer_request_forbids_extra():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        TransferRequest(bogus=1)


def test_agent_command_kind_vocabulary():
    import pydantic

    for kind in ("stat_check", "rehash_check", "stage_upload"):
        cmd = AgentCommand(id="c", agent_id="a", kind=kind, item_id="i")
        assert cmd.kind == kind
        assert cmd.status == "pending"
    with pytest.raises(pydantic.ValidationError):
        AgentCommand(id="c", agent_id="a", kind="bogus", item_id="i")


def test_share_hint_defaults_best_effort():
    hint = ShareHint(share_url=r"\\tower\media\x", scheme="unc", source="windows_net_share")
    assert hint.best_effort is True
