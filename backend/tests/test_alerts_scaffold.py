"""Phase 8 alerting scaffolding tests (P8-T* pure cores).

Pure unit tests — no Postgres, no Procrastinate, no network, no SMTP. A synthetic
clock drives the window state machine; a fake resolver drives the SSRF guard.
Guards the inert scaffolding in ``filearr.alerts`` so the implementing tasks
inherit green coverage of the rule-match matrix, grouping, throttle/digest
windowing, the P8-T15 ceiling, the SSRF deny-matrix, and HMAC signing.
"""

from datetime import datetime, timedelta

import pytest

from filearr.alerts.dispatch import (
    DeliveryResult,
    RenderedAlert,
    decrypt_channel_secret,
    encrypt_channel_secret,
    send_via_apprise,
)
from filearr.alerts.rules import (
    EVENT_TYPES,
    GROUP_BY,
    AlertRule,
    FileEvent,
    group_key,
    match_rule,
)
from filearr.alerts.signing import (
    parse_signature_header,
    sign_payload,
    verify_signature,
    within_replay_window,
)
from filearr.alerts.ssrf import IpClass, check_webhook_url, classify_ip
from filearr.alerts.windows import (
    GroupState,
    assign_window,
    ceiling_exceeded,
    should_fire_now,
)

# --- helpers ---------------------------------------------------------------

LIB = "lib-1"


def rule(**kw) -> AlertRule:
    base = dict(id="r1", name="r", event_types=("created", "modified"))
    base.update(kw)
    return AlertRule(**base)


def event(**kw) -> FileEvent:
    base = dict(event_type="created", library_id=LIB, rel_path="Movies/a.mkv")
    base.update(kw)
    return FileEvent(**base)


# --- rule dataclass invariants (R1) ----------------------------------------


def test_event_types_vocabulary():
    assert EVENT_TYPES == {"created", "modified", "deleted", "moved"}


def test_group_by_is_fixed_r1():
    assert GROUP_BY == ("event_type", "library_id", "rule_id")


def test_rule_rejects_unknown_event_type():
    with pytest.raises(ValueError):
        rule(event_types=("created", "exploded"))


def test_rule_rejects_empty_event_types():
    with pytest.raises(ValueError):
        rule(event_types=())


def test_rule_rejects_foreign_group_by():
    with pytest.raises(ValueError):
        rule(group_by=("event_type", "path"))


def test_rule_rejects_unknown_digest_window():
    with pytest.raises(ValueError):
        rule(digest_window="weekly")


def test_fileevent_rejects_unknown_type():
    with pytest.raises(ValueError):
        event(event_type="nope")


# --- match_rule matrix -----------------------------------------------------


def test_match_basic_glob_and_event():
    r = rule(path_glob="Movies/**", event_types=("created",))
    assert match_rule(r, event(rel_path="Movies/a.mkv")) is True
    assert match_rule(r, event(rel_path="Shows/a.mkv")) is False


def test_match_none_glob_matches_all_paths():
    r = rule(path_glob=None, event_types=("created",))
    assert match_rule(r, event(rel_path="anything/deep/x.bin")) is True


def test_match_extension_glob():
    r = rule(path_glob="*.mkv", event_types=("created", "modified"))
    assert match_rule(r, event(rel_path="Movies/a.mkv")) is True
    assert match_rule(r, event(rel_path="Movies/a.srt")) is False


def test_match_event_type_gate():
    r = rule(event_types=("deleted",))
    assert match_rule(r, event(event_type="deleted")) is True
    assert match_rule(r, event(event_type="created")) is False


def test_match_disabled_rule_never_fires():
    r = rule(enabled=False, path_glob=None, event_types=("created",))
    assert match_rule(r, event()) is False


def test_match_library_scope():
    scoped = rule(library_id="lib-2", path_glob=None, event_types=("created",))
    assert match_rule(scoped, event(library_id="lib-2")) is True
    assert match_rule(scoped, event(library_id="lib-1")) is False
    allrule = rule(library_id=None, path_glob=None, event_types=("created",))
    assert match_rule(allrule, event(library_id="lib-9")) is True


def test_hash_change_gate_on_modified():
    r = rule(event_types=("modified",), path_glob=None, hash_change_only=True)
    # same hash -> no change -> no fire
    assert match_rule(r, event(event_type="modified", old_hash="a", new_hash="a")) is False
    # different hash -> fire
    assert match_rule(r, event(event_type="modified", old_hash="a", new_hash="b")) is True
    # unknown hash -> cannot prove change -> no fire
    assert match_rule(r, event(event_type="modified", old_hash=None, new_hash="b")) is False


def test_hash_change_gate_noop_for_nonmodified():
    r = rule(event_types=("created",), path_glob=None, hash_change_only=True)
    # created events are unaffected by the hash-change gate
    assert match_rule(r, event(event_type="created")) is True


# --- group_key -------------------------------------------------------------


def test_group_key_shape():
    r = rule(id="rule-x")
    e = event(event_type="modified", library_id="lib-7")
    assert group_key(r, e) == ("modified", "lib-7", "rule-x")


def test_group_key_collapses_files_in_same_group():
    r = rule()
    a = event(rel_path="Movies/a.mkv")
    b = event(rel_path="Movies/b.mkv")
    # same event_type + library + rule -> identical key regardless of path
    assert group_key(r, a) == group_key(r, b)


# --- digest window bucketing -----------------------------------------------


def test_assign_window_hourly():
    ts = datetime(2026, 7, 7, 14, 37, 12)
    assert assign_window(ts, "hourly") == datetime(2026, 7, 7, 14, 0, 0)


def test_assign_window_daily():
    ts = datetime(2026, 7, 7, 14, 37, 12)
    assert assign_window(ts, "daily") == datetime(2026, 7, 7, 0, 0, 0)


def test_assign_window_rejects_bad():
    with pytest.raises(ValueError):
        assign_window(datetime(2026, 1, 1), "weekly")


# --- should_fire_now state machine -----------------------------------------

T0 = datetime(2026, 7, 7, 12, 0, 0)


def test_inactive_group_never_fires():
    st = GroupState(first_match_at=T0, has_new_matches=True, active=False)
    assert should_fire_now(st, 30, 300, 3600, T0 + timedelta(hours=5)) is False


def test_group_wait_delays_first_notification():
    st = GroupState(first_match_at=T0, has_new_matches=True)
    # before group_wait elapses -> hold
    assert should_fire_now(st, 30, 300, None, T0 + timedelta(seconds=10)) is False
    # exactly at group_wait -> fire
    assert should_fire_now(st, 30, 300, None, T0 + timedelta(seconds=30)) is True


def test_no_pending_no_fire_before_first_notify():
    st = GroupState(first_match_at=T0, has_new_matches=False)
    assert should_fire_now(st, 30, 300, None, T0 + timedelta(minutes=10)) is False


def test_group_interval_gates_new_matches_after_notify():
    st = GroupState(last_notified_at=T0, has_new_matches=True)
    assert should_fire_now(st, 30, 300, None, T0 + timedelta(seconds=120)) is False
    assert should_fire_now(st, 30, 300, None, T0 + timedelta(seconds=300)) is True


def test_repeat_interval_for_unchanged_group():
    st = GroupState(last_notified_at=T0, has_new_matches=False)
    # no repeat configured -> silent
    assert should_fire_now(st, 30, 300, None, T0 + timedelta(hours=10)) is False
    # repeat configured -> resend only after repeat_interval
    assert should_fire_now(st, 30, 300, 3600, T0 + timedelta(seconds=1800)) is False
    assert should_fire_now(st, 30, 300, 3600, T0 + timedelta(seconds=3600)) is True


# --- P8-T15 hourly ceiling -------------------------------------------------


def test_ceiling_exceeded():
    assert ceiling_exceeded(99, 100) is False
    assert ceiling_exceeded(100, 100) is True
    assert ceiling_exceeded(101, 100) is True


def test_ceiling_none_means_no_limit():
    assert ceiling_exceeded(10_000, None) is False


# --- SSRF classify_ip matrix -----------------------------------------------


@pytest.mark.parametrize(
    "ip,expected",
    [
        ("8.8.8.8", IpClass.PUBLIC),
        ("2606:4700:4700::1111", IpClass.PUBLIC),
        ("127.0.0.1", IpClass.LOOPBACK),
        ("::1", IpClass.LOOPBACK),
        ("10.0.0.5", IpClass.PRIVATE),
        ("172.16.3.4", IpClass.PRIVATE),
        ("192.168.1.10", IpClass.PRIVATE),
        ("169.254.169.254", IpClass.LINK_LOCAL),
        ("fe80::1", IpClass.LINK_LOCAL),
        ("fc00::1", IpClass.PRIVATE),  # IPv6 ULA
        ("::ffff:10.0.0.1", IpClass.PRIVATE),  # IPv4-mapped IPv6 -> embedded v4
        ("0.0.0.0", IpClass.UNSPECIFIED),
        ("224.0.0.1", IpClass.RESERVED),  # multicast
    ],
)
def test_classify_ip(ip, expected):
    assert classify_ip(ip) is expected


# --- SSRF check_webhook_url matrix -----------------------------------------


def fake_resolver(mapping):
    def _resolve(host):
        return mapping.get(host, [])
    return _resolve


def test_public_url_allowed():
    r = fake_resolver({"hooks.example.com": ["93.184.216.34"]})
    v = check_webhook_url("https://hooks.example.com/x", r)
    assert v.allowed is True


def test_loopback_denied():
    r = fake_resolver({"evil.test": ["127.0.0.1"]})
    assert check_webhook_url("http://evil.test/", r).allowed is False


def test_private_rfc1918_denied_by_default():
    r = fake_resolver({"lan.test": ["10.1.2.3"]})
    assert check_webhook_url("http://lan.test/hook", r).allowed is False


def test_link_local_metadata_denied():
    r = fake_resolver({"meta.test": ["169.254.169.254"]})
    assert check_webhook_url("http://meta.test/", r).allowed is False


def test_ipv6_ula_denied():
    r = fake_resolver({"v6.test": ["fc00::1"]})
    assert check_webhook_url("http://v6.test/", r).allowed is False


def test_mapped_private_denied():
    r = fake_resolver({"m.test": ["::ffff:10.0.0.1"]})
    assert check_webhook_url("http://m.test/", r).allowed is False


def test_allow_private_flips_private_only():
    r = fake_resolver({"lan.test": ["10.1.2.3"]})
    assert check_webhook_url("http://lan.test/", r, allow_private=True).allowed is True


def test_allow_private_does_not_flip_loopback():
    r = fake_resolver({"lo.test": ["127.0.0.1"]})
    assert check_webhook_url("http://lo.test/", r, allow_private=True).allowed is False


def test_allow_private_does_not_flip_link_local():
    r = fake_resolver({"meta.test": ["169.254.169.254"]})
    assert check_webhook_url("http://meta.test/", r, allow_private=True).allowed is False


def test_mixed_records_fail_closed():
    # rebinding-style answer set: one public, one private -> whole URL denied.
    r = fake_resolver({"mix.test": ["93.184.216.34", "10.0.0.1"]})
    assert check_webhook_url("http://mix.test/", r).allowed is False


def test_scheme_must_be_http_s():
    r = fake_resolver({"x.test": ["8.8.8.8"]})
    assert check_webhook_url("ftp://x.test/", r).allowed is False
    assert check_webhook_url("file:///etc/passwd", r).allowed is False


def test_no_dns_records_denied():
    assert check_webhook_url("https://nowhere.test/", fake_resolver({})).allowed is False


def test_ip_literal_host_needs_no_resolver():
    # resolver that would explode if called -> proves the literal path skips DNS
    def boom(_):
        raise AssertionError("resolver should not be called for an IP literal")

    assert check_webhook_url("https://8.8.8.8/", boom).allowed is True
    assert check_webhook_url("http://127.0.0.1/", boom).allowed is False


def test_bad_port_denied():
    r = fake_resolver({"x.test": ["8.8.8.8"]})
    assert check_webhook_url("http://x.test:99999/", r).allowed is False


# --- HMAC signing round-trip / tamper / replay -----------------------------

SECRET = "s3cr3t-key"
BODY = '{"event":"created","path":"Movies/a.mkv"}'
NOW = 1_800_000_000


def test_sign_verify_round_trip():
    header = sign_payload(SECRET, BODY, NOW)
    assert verify_signature(SECRET, BODY, header, now=NOW, max_age_s=300) is True


def test_header_parse():
    header = sign_payload(SECRET, BODY, NOW)
    parsed = parse_signature_header(header)
    assert parsed is not None
    ts, sig = parsed
    assert ts == NOW and len(sig) == 64


def test_tampered_body_fails():
    header = sign_payload(SECRET, BODY, NOW)
    assert verify_signature(SECRET, BODY + " ", header, now=NOW, max_age_s=300) is False


def test_wrong_secret_fails():
    header = sign_payload(SECRET, BODY, NOW)
    assert verify_signature("other", BODY, header, now=NOW, max_age_s=300) is False


def test_replay_expiry_fails():
    header = sign_payload(SECRET, BODY, NOW)
    # signature is valid, but timestamp is outside the freshness window
    assert verify_signature(SECRET, BODY, header, now=NOW + 3600, max_age_s=300) is False


def test_future_timestamp_rejected():
    header = sign_payload(SECRET, BODY, NOW + 10_000)
    assert verify_signature(SECRET, BODY, header, now=NOW, max_age_s=300) is False


def test_within_replay_window_pure():
    assert within_replay_window(NOW, NOW + 100, 300) is True
    assert within_replay_window(NOW, NOW + 400, 300) is False
    assert within_replay_window(NOW, NOW - 400, 300) is False  # future skew


def test_malformed_header_rejected():
    assert parse_signature_header("garbage") is None
    assert verify_signature(SECRET, BODY, "garbage", now=NOW, max_age_s=300) is False


def test_bytes_and_str_bodies_equivalent():
    header = sign_payload(SECRET, BODY.encode(), NOW)
    assert verify_signature(SECRET, BODY, header, now=NOW, max_age_s=300) is True


# --- dispatch: dataclasses + remaining stub (apprise) ----------------------
# P8-T2 implemented send_webhook/send_email and the P8-T4 crypto helpers; their
# behaviour is covered in test_alert_drivers.py / test_alert_crypto.py. Only the
# apprise adapter (P8-T3) remains a tagged stub, asserted here.


def test_render_and_result_dataclasses():
    ra = RenderedAlert(subject="s", body_text="b")
    assert ra.payload == {}
    dr = DeliveryResult(ok=True)
    assert dr.retryable is False


async def test_apprise_stub_still_raises():
    with pytest.raises(NotImplementedError):
        await send_via_apprise("json://x", RenderedAlert("s", "b"))


def test_crypto_helpers_round_trip():
    # P8-T4 slice: encrypt/decrypt are implemented (AES-GCM). A 32-byte key.
    key = b"\x11" * 32
    token = encrypt_channel_secret("s3cret", key)
    assert token != "s3cret"  # not plaintext
    assert decrypt_channel_secret(token, key) == "s3cret"
