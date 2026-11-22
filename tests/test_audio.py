"""Tests for :mod:`relaxbook.audio` (CONTRACTS section 8).

Fixtures generate 24 000 Hz mono sine-wave WAVs with the standard library only;
no network or other project modules (besides ``config`` for tool lookup) needed.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from relaxbook import audio, config
from relaxbook.config import MIN_FFMPEG_MAJOR, Settings, ffmpeg_major


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def write_sine_wav(
    path: Path,
    seconds: float,
    *,
    frequency: float = 440.0,
    sample_rate: int = 24000,
    channels: int = 1,
) -> Path:
    """Write a PCM16 sine WAV; duration is exact to the sample."""
    frames = int(round(seconds * sample_rate))
    payload = bytearray()
    for index in range(frames):
        value = int(0.3 * 32767 * math.sin(2 * math.pi * frequency * index / sample_rate))
        sample = struct.pack("<h", value)
        payload.extend(sample * channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(payload))
    return path


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Resolved tool paths: FFmpeg 7+ (chapter titles) and the system ffprobe.

    Uses ``Settings.resolve_tools()`` instead of ``locate_tool("ffmpeg")`` so the
    test suite exercises the same selection logic as the application (which
    prefers the ``imageio-ffmpeg`` wheel over outdated system builds).
    """
    data_dir = tmp_path_factory.mktemp("audio-tools")
    settings = Settings(data_dir=str(data_dir))
    settings.ensure_dirs()
    settings.resolve_tools()
    if not settings.ffmpeg or not settings.ffprobe:
        pytest.skip("需要 ffmpeg 与 ffprobe")
    if ffmpeg_major(settings.ffmpeg) < MIN_FFMPEG_MAJOR:
        pytest.skip(f"需要 FFmpeg {MIN_FFMPEG_MAJOR}+ 才能写入 M4B 章节标题")
    return settings


@pytest.fixture()
def sample_wavs(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        write_sine_wav(tmp_path / "a.wav", 1.0, frequency=440),
        write_sine_wav(tmp_path / "b.wav", 1.5, frequency=550),
        write_sine_wav(tmp_path / "c.wav", 2.0, frequency=660),
    )


@pytest.fixture()
def joined_wav(tmp_path: Path, sample_wavs: tuple[Path, Path, Path]) -> Path:
    out = tmp_path / "joined.wav"
    audio.concat_wavs(list(sample_wavs), out)
    return out


def chapter_ranges() -> list[dict]:
    return [
        {"title": "序言", "start_ms": 0, "end_ms": 1000},
        {"title": "出发", "start_ms": 1000, "end_ms": 2500},
        {"title": "出发", "start_ms": 2500, "end_ms": 4500},
    ]


# --------------------------------------------------------------------------- #
# WAV helpers
# --------------------------------------------------------------------------- #
def test_wav_frames_and_duration(sample_wavs: tuple[Path, Path, Path]) -> None:
    first, second, third = sample_wavs
    assert audio.wav_frames(first) == 24000
    assert audio.wav_frames(second) == 36000
    assert audio.wav_frames(third) == 48000
    assert audio.wav_duration_ms(first) == 1000
    assert audio.wav_duration_ms(second) == 1500
    assert audio.wav_duration_ms(third) == 2000


def test_concat_wavs_exact_sample_count(joined_wav: Path) -> None:
    assert audio.wav_frames(joined_wav) == 24000 + 36000 + 48000
    assert audio.wav_duration_ms(joined_wav) == 4500
    with wave.open(str(joined_wav), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 24000
        raw = wav.readframes(24000)
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    assert max(abs(value) for value in samples) > 1000  # actual audio, not silence
    # No stray temporary file left next to the target.
    assert [path.name for path in joined_wav.parent.iterdir() if "part" in path.name] == []


def test_concat_wavs_normalises_non_standard_input(tmp_path: Path) -> None:
    # 44.1 kHz stereo source must be folded to 24 kHz mono PCM16.
    stereo = write_sine_wav(
        tmp_path / "stereo.wav", 1.0, frequency=440, sample_rate=44100, channels=2
    )
    mono = write_sine_wav(tmp_path / "mono.wav", 1.0, frequency=550)
    out = tmp_path / "mixed.wav"
    audio.concat_wavs([stereo, mono], out)
    with wave.open(str(out), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.getnframes() == 48000
    assert audio.wav_duration_ms(out) == 2000


def test_wav_frames_reports_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav file at all")
    with pytest.raises(audio.AudioError):
        audio.wav_frames(bad)


# --------------------------------------------------------------------------- #
# probe / decode
# --------------------------------------------------------------------------- #
def test_probe_mp3(tools: Settings, tmp_path: Path, joined_wav: Path) -> None:
    mp3 = tmp_path / "book.mp3"
    audio.encode_mp3(joined_wav, mp3, bitrate="64k", ffmpeg=tools.ffmpeg)
    audio.verify_decodable(mp3, ffmpeg=tools.ffmpeg)

    info = audio.probe(mp3, ffprobe=tools.ffprobe)
    assert abs(info["duration_ms"] - 4500) <= 100  # MP3 encoder padding only
    assert info["sample_rate"] == 24000
    assert info["channels"] == 1
    assert info["bit_rate"] > 0
    assert "mp3" in info["format_name"]


def test_verify_decodable_rejects_broken_file(tools: Settings, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"this is definitely not audio")
    with pytest.raises(audio.AudioError) as excinfo:
        audio.verify_decodable(broken, ffmpeg=tools.ffmpeg)
    assert str(excinfo.value)


def test_probe_raises_for_missing_file(tools: Settings, tmp_path: Path) -> None:
    with pytest.raises(audio.AudioError):
        audio.probe(tmp_path / "missing.mp3", ffprobe=tools.ffprobe)


def test_probe_chapters_empty_for_mp3(tools: Settings, tmp_path: Path, joined_wav: Path) -> None:
    mp3 = tmp_path / "nochapters.mp3"
    audio.encode_mp3(joined_wav, mp3, bitrate="64k", ffmpeg=tools.ffmpeg)
    assert audio.probe_chapters(mp3, ffprobe=tools.ffprobe) == []


def test_decode_to_wav_normalises_format(tools: Settings, tmp_path: Path) -> None:
    source = write_sine_wav(
        tmp_path / "src.wav", 1.0, frequency=440, sample_rate=44100, channels=2
    )
    out = tmp_path / "decoded.wav"
    audio.decode_to_wav(source, out, ffmpeg=tools.ffmpeg)
    with wave.open(str(out), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.getnframes() == 24000
    assert audio.wav_duration_ms(out) == 1000


# --------------------------------------------------------------------------- #
# ffmetadata / M4B
# --------------------------------------------------------------------------- #
def test_write_ffmetadata_sidecar(tmp_path: Path) -> None:
    sidecar = tmp_path / "chapters.txt"
    audio.write_ffmetadata(
        sidecar,
        title="示例书",
        author="作者",
        chapters=chapter_ranges(),
    )
    text = sidecar.read_text(encoding="utf-8")
    assert text.startswith(";FFMETADATA1\n")
    assert "title=示例书" in text
    assert "artist=作者" in text
    assert text.count("[CHAPTER]") == 3
    assert "TIMEBASE=1/1000" in text
    assert "START=0" in text
    assert "END=4500" in text
    assert "title=序言" in text


def test_encode_m4b_embeds_chapter_titles(tools: Settings, tmp_path: Path, joined_wav: Path) -> None:
    m4b = tmp_path / "book.m4b"
    audio.encode_m4b(
        joined_wav,
        m4b,
        title="示例书",
        author="作者",
        chapters=chapter_ranges(),
        bitrate="64k",
        ffmpeg=tools.ffmpeg,
    )
    audio.verify_decodable(m4b, ffmpeg=tools.ffmpeg)
    info = audio.probe(m4b, ffprobe=tools.ffprobe)
    assert abs(info["duration_ms"] - 4500) <= 100
    assert info["sample_rate"] == 24000
    assert info["channels"] == 1

    chapters = audio.probe_chapters(m4b, ffprobe=tools.ffprobe)
    assert [chapter["title"] for chapter in chapters] == ["序言", "出发", "出发"]
    assert [chapter["start_ms"] for chapter in chapters] == [0, 1000, 2500]
    assert [chapter["end_ms"] for chapter in chapters] == [1000, 2500, 4500]


def test_encode_m4b_attaches_cover(tools: Settings, tmp_path: Path, joined_wav: Path) -> None:
    cover = tmp_path / "cover.jpg"
    subprocess.run(
        [
            tools.ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64",
            "-frames:v",
            "1",
            str(cover),
        ],
        check=True,
        capture_output=True,
    )
    m4b = tmp_path / "with_cover.m4b"
    warnings: list[str] = []
    audio.encode_m4b(
        joined_wav,
        m4b,
        title="示例书",
        author="作者",
        chapters=chapter_ranges(),
        bitrate="64k",
        ffmpeg=tools.ffmpeg,
        cover=cover,
        warnings=warnings,
    )
    assert warnings == []
    probe = subprocess.run(
        [tools.ffprobe, "-v", "error", "-show_streams", "-of", "json", str(m4b)],
        capture_output=True,
        check=False,
    )
    streams = json.loads(probe.stdout.decode("utf-8", "replace"))["streams"]
    assert any(stream.get("disposition", {}).get("attached_pic") == 1 for stream in streams)
    assert [chapter["title"] for chapter in audio.probe_chapters(m4b, ffprobe=tools.ffprobe)] == [
        "序言",
        "出发",
        "出发",
    ]


def test_encode_m4b_broken_cover_falls_back(
    tools: Settings, tmp_path: Path, joined_wav: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bogus = tmp_path / "cover.jpg"
    bogus.write_text("definitely not an image", encoding="utf-8")
    m4b = tmp_path / "bad_cover.m4b"
    warnings: list[str] = []
    audio.encode_m4b(
        joined_wav,
        m4b,
        title="示例书",
        author=None,
        chapters=chapter_ranges(),
        bitrate="64k",
        ffmpeg=tools.ffmpeg,
        cover=bogus,
        warnings=warnings,
    )
    assert m4b.is_file()
    assert warnings  # cover was dropped but recorded as a warning
    assert any("封面写入失败" in message for message in caplog.messages)
    assert [chapter["title"] for chapter in audio.probe_chapters(m4b, ffprobe=tools.ffprobe)] == [
        "序言",
        "出发",
        "出发",
    ]


def test_encode_m4b_rejects_old_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tools: Settings, tmp_path: Path, joined_wav: Path
) -> None:
    # Simulate a pre-7 FFmpeg (the old build drops chapter titles): we must fail
    # loudly instead of publishing an untitled chapter track.
    monkeypatch.setattr(config, "ffmpeg_major", lambda exe: 6)
    target = tmp_path / "old.m4b"
    with pytest.raises(audio.AudioError) as excinfo:
        audio.encode_m4b(
            joined_wav,
            target,
            title="示例书",
            author=None,
            chapters=chapter_ranges(),
            bitrate="64k",
            ffmpeg=tools.ffmpeg,
        )
    message = str(excinfo.value)
    assert "FFmpeg" in message
    assert str(MIN_FFMPEG_MAJOR) in message
    assert not target.exists()
