"""Unit tests for the ffprobe video-metadata extractor (T1)."""

from __future__ import annotations

import subprocess

import pytest

from filearr.tasks import ffprobe
from filearr.tasks.ffprobe import FfprobeError, extract_video_tech, probe

from .conftest import requires_ffmpeg


@requires_ffmpeg
def test_happy_path_mp4(sample_mp4):
    meta = extract_video_tech(str(sample_mp4))
    assert meta["video_codec"] == "h264"
    assert meta["width"] == 320 and meta["height"] == 240
    assert meta["resolution"] == "320x240"
    assert meta["duration"] == pytest.approx(1.0, abs=0.2)
    assert meta["audio_codec"] == "aac"
    assert meta["audio_tracks"][0]["codec"] == "aac"
    assert "container" in meta


@requires_ffmpeg
def test_subtitle_and_audio_tracks_listed(sample_mkv):
    meta = extract_video_tech(str(sample_mkv))
    assert any(t["codec"] == "aac" for t in meta["audio_tracks"])
    subs = meta["subtitle_tracks"]
    assert subs and subs[0]["codec"] == "subrip"
    # frame rate parsed from ffprobe's "25/1"
    assert meta["frame_rate"] == pytest.approx(25.0)


@requires_ffmpeg
def test_corrupt_file_raises_ffprobe_error(corrupt_video):
    with pytest.raises(FfprobeError):
        extract_video_tech(str(corrupt_video))


def test_missing_binary_raises(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"\x00")
    with pytest.raises(FfprobeError, match="not found"):
        probe(str(f), ffprobe_path="definitely-not-a-real-ffprobe-binary")


@requires_ffmpeg
def test_timeout_is_caught(monkeypatch, sample_mp4):
    """A timeout kills the child and surfaces as FfprobeError, never TimeoutExpired."""
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(ffprobe.subprocess, "run", fake_run)
    with pytest.raises(FfprobeError, match="timed out"):
        probe(str(sample_mp4), timeout_s=0.001)
    monkeypatch.setattr(ffprobe.subprocess, "run", real_run)


@requires_ffmpeg
def test_oversized_output_rejected(monkeypatch, sample_mp4):
    with pytest.raises(FfprobeError, match="too large"):
        probe(str(sample_mp4), max_output_bytes=1)


def test_nonjson_output_rejected(monkeypatch, tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"\x00")

    class R:
        returncode = 0
        stdout = b"not json at all"
        stderr = b""

    monkeypatch.setattr(ffprobe.shutil, "which", lambda _: "/usr/bin/ffprobe")
    monkeypatch.setattr(ffprobe.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(FfprobeError, match="valid JSON"):
        probe(str(f))


def test_hdr_detection_hdr10():
    stream = {
        "codec_type": "video",
        "color_transfer": "smpte2084",
        "color_primaries": "bt2020",
    }
    hdr, fmt = ffprobe._detect_hdr(stream)
    assert hdr is True and fmt == "HDR10"


def test_hdr_detection_dolby_vision():
    stream = {
        "codec_type": "video",
        "side_data_list": [{"side_data_type": "DOVI configuration record"}],
    }
    hdr, fmt = ffprobe._detect_hdr(stream)
    assert hdr is True and fmt == "Dolby Vision"


def test_sdr_not_flagged():
    hdr, fmt = ffprobe._detect_hdr({"color_transfer": "bt709", "color_primaries": "bt709"})
    assert hdr is False and fmt is None


def test_fps_parsing():
    assert ffprobe._fps("24000/1001") == pytest.approx(23.976, abs=0.001)
    assert ffprobe._fps("0/0") is None
    assert ffprobe._fps("30") == 30.0
