"""P3-T11 — EXIF extraction + mapping + the namespaced GPS gate.

The exiftool SUBPROCESS is MOCKED (the sandbox has no exiftool binary); the pure
tag-mapping + strip_gps logic is exercised for real. Live verification of the real
exiftool output needs an image rebuild with libimage-exiftool-perl present.
"""

from __future__ import annotations

import json

import pytest

from filearr import exif as exif_mod
from filearr.config import Settings
from filearr.exif import ExifError, map_exif_tags, strip_gps
from filearr.tasks.exif_run import exif_metadata

# A representative exiftool -json -n payload (numeric GPS via -n).
_RAW = {
    "Make": "Canon",
    "Model": "EOS R5",
    "LensModel": "RF24-70mm F2.8",
    "ISO": 400,
    "ExposureTime": 0.008,
    "FNumber": 2.8,
    "FocalLength": 50.0,
    "ImageWidth": 8192,
    "ImageHeight": 5464,
    "DateTimeOriginal": "2026:06:01 12:00:00",
    "GPSLatitude": 37.7749,
    "GPSLongitude": -122.4194,
    "GPSAltitude": 12.0,
    "SomethingIrrelevant": "ignored",
}


def _settings() -> Settings:
    return Settings(
        exiftool_path="exiftool", exif_timeout_s=30.0, exif_max_output_bytes=8_388_608
    )


def test_map_exif_tags_namespaced_curated_keys():
    out = map_exif_tags(_RAW)
    assert out["exif.camera_make"] == "Canon"
    assert out["exif.camera_model"] == "EOS R5"
    assert out["exif.lens_model"] == "RF24-70mm F2.8"
    assert out["exif.iso"] == 400
    assert out["exif.f_number"] == 2.8
    assert out["exif.focal_length"] == 50.0
    assert out["exif.width"] == 8192
    assert out["exif.height"] == 5464
    assert out["exif.taken_at"] == "2026:06:01 12:00:00"
    # GPS stored RAW (numeric) in metadata_ — exposure decided by the gate.
    assert out["exif.gps_latitude"] == 37.7749
    assert out["exif.gps_longitude"] == -122.4194
    assert out["exif.gps_altitude"] == 12.0
    # non-curated tags dropped; every emitted key is exif.*-namespaced.
    assert all(k.startswith("exif.") for k in out)
    assert "SomethingIrrelevant" not in out


def test_map_exif_tags_createdate_fallback_does_not_override():
    raw = {"DateTimeOriginal": "2020:01:01 00:00:00", "CreateDate": "1999:01:01 00:00:00"}
    out = map_exif_tags(raw)
    assert out["exif.taken_at"] == "2020:01:01 00:00:00"  # first non-empty wins


def test_map_exif_tags_missing_fields_ok():
    assert map_exif_tags({}) == {}
    assert map_exif_tags({"Make": ""}) == {}  # empty string dropped


def test_strip_gps_removes_namespaced_gps_keeps_camera():
    mapped = map_exif_tags(_RAW)
    stripped = strip_gps(mapped)
    assert "exif.gps_latitude" not in stripped
    assert "exif.gps_longitude" not in stripped
    assert "exif.gps_altitude" not in stripped
    # camera/lens/exposure survive
    assert stripped["exif.camera_make"] == "Canon"
    assert stripped["exif.focal_length"] == 50.0
    # non-mutating
    assert "exif.gps_latitude" in mapped


# --- subprocess-mocked run_exiftool / exif_metadata ------------------------


def _fake_proc(stdout: bytes, rc: int = 0, stderr: bytes = b""):
    class P:
        returncode = rc

    P.stdout = stdout
    P.stderr = stderr
    return P()


def test_run_exiftool_parses_json_list(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _fake_proc(json.dumps([_RAW]).encode())

    monkeypatch.setattr(exif_mod.shutil, "which", lambda b: "/usr/bin/exiftool")
    monkeypatch.setattr(exif_mod.subprocess, "run", fake_run)
    got = exif_mod.run_exiftool("/a/photo.jpg")
    assert got["Make"] == "Canon"
    assert "-json" in captured["argv"] and "-n" in captured["argv"]


def test_run_exiftool_missing_binary(monkeypatch):
    monkeypatch.setattr(exif_mod.shutil, "which", lambda b: None)
    with pytest.raises(ExifError):
        exif_mod.run_exiftool("/a/photo.jpg")


def test_run_exiftool_oversized_output(monkeypatch):
    monkeypatch.setattr(exif_mod.shutil, "which", lambda b: "/usr/bin/exiftool")
    monkeypatch.setattr(
        exif_mod.subprocess, "run", lambda argv, **kw: _fake_proc(b"x" * 100)
    )
    with pytest.raises(ExifError):
        exif_mod.run_exiftool("/a/photo.jpg", max_output_bytes=10)


def test_exif_metadata_success(monkeypatch):
    monkeypatch.setattr(exif_mod.shutil, "which", lambda b: "/usr/bin/exiftool")
    monkeypatch.setattr(
        exif_mod.subprocess, "run", lambda argv, **kw: _fake_proc(json.dumps([_RAW]).encode())
    )
    out = exif_metadata("/a/photo.jpg", settings=_settings())
    assert out["exif.camera_make"] == "Canon"
    assert out["exif.gps_latitude"] == 37.7749


def test_exif_metadata_degrades_on_error(monkeypatch):
    monkeypatch.setattr(exif_mod.shutil, "which", lambda b: None)  # missing binary
    out = exif_metadata("/a/photo.jpg", settings=_settings())
    assert "_exif_error" in out
    assert not any(k.startswith("exif.") for k in out)
