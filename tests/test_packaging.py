"""Tests for :mod:`relaxbook.packaging` (CONTRACTS section 9).

The fixture builds a three-chapter book from freshly generated sine WAVs:
chapter 1 = 1.0 s, chapter 2 = 0.5 s + 1.0 s, chapter 3 = 2.0 s, giving exact
chapter boundaries 0/1000/2500/4500 ms (108 000 PCM frames at 24 kHz).
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from relaxbook import audio, config, naming, packaging
from relaxbook.config import MIN_FFMPEG_MAJOR, Settings, ffmpeg_major


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def write_sine_wav(path: Path, seconds: float, *, frequency: float = 440.0) -> Path:
    frames = int(round(seconds * 24000))
    payload = bytearray()
    for index in range(frames):
        value = int(0.3 * 32767 * math.sin(2 * math.pi * frequency * index / 24000))
        payload.extend(struct.pack("<h", value))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(bytes(payload))
    return path


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Resolved tools; FFmpeg 7+ is required for the M4B chapter text track."""
    data_dir = tmp_path_factory.mktemp("packaging-tools")
    settings = Settings(data_dir=str(data_dir))
    settings.ensure_dirs()
    settings.resolve_tools()
    if not settings.ffmpeg or not settings.ffprobe:
        pytest.skip("需要 ffmpeg 与 ffprobe")
    if ffmpeg_major(settings.ffmpeg) < MIN_FFMPEG_MAJOR:
        pytest.skip(f"需要 FFmpeg {MIN_FFMPEG_MAJOR}+ 才能写入 M4B 章节标题")
    return settings


@pytest.fixture()
def chapter_plan(tmp_path: Path) -> list[dict]:
    wav_dir = tmp_path / "segments"
    return [
        {
            "index": 1,
            "title": "序言",
            "segment_wavs": [write_sine_wav(wav_dir / "1-0.wav", 1.0, frequency=440)],
        },
        {
            "index": 2,
            "title": "出发",
            "segment_wavs": [
                write_sine_wav(wav_dir / "2-0.wav", 0.5, frequency=500),
                write_sine_wav(wav_dir / "2-1.wav", 1.0, frequency=550),
            ],
        },
        {
            "index": 3,
            "title": "出发",
            "segment_wavs": [write_sine_wav(wav_dir / "3-0.wav", 2.0, frequency=660)],
        },
    ]


def assemble(tools: Settings, chapters: list[dict], out: Path, **overrides) -> dict:
    kwargs = {
        "title": "示例书",
        "author": "测试作者",
        "cover_path": None,
        "bitrate": "64k",
        "ffmpeg": tools.ffmpeg,
        "ffprobe": tools.ffprobe,
    }
    kwargs.update(overrides)
    return packaging.assemble_m4b(chapters, out, **kwargs)


# --------------------------------------------------------------------------- #
# chapter MP3
# --------------------------------------------------------------------------- #
def test_assemble_chapter_publishes_valid_mp3(
    tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    out = tmp_path / "v01" / naming.mp3_filename(1, "序言", 3)
    assert out.name == "001 - 序言.mp3"
    result = packaging.assemble_chapter(
        chapter_plan[0]["segment_wavs"],
        out,
        bitrate="64k",
        ffmpeg=tools.ffmpeg,
        ffprobe=tools.ffprobe,
    )

    assert result["path"] == str(out)
    assert out.is_file()
    assert result["bytes"] == out.stat().st_size > 0
    assert abs(result["duration_ms"] - 1000) <= 100
    audio.verify_decodable(out, ffmpeg=tools.ffmpeg)
    info = audio.probe(out, ffprobe=tools.ffprobe)
    assert info["duration_ms"] == result["duration_ms"]
    assert info["sample_rate"] == 24000
    assert info["channels"] == 1
    assert info["bit_rate"] > 0
    # Only the published artifact is left in the directory (no temp files).
    assert [path.name for path in out.parent.iterdir()] == [out.name]


def test_assemble_chapter_accepts_multiple_segments(
    tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    out = tmp_path / "002 - 出发.mp3"
    result = packaging.assemble_chapter(
        chapter_plan[1]["segment_wavs"],
        out,
        bitrate="64k",
        ffmpeg=tools.ffmpeg,
        ffprobe=tools.ffprobe,
    )
    assert abs(result["duration_ms"] - 1500) <= 100


def test_assemble_chapter_rejects_empty_segments(tools: Settings, tmp_path: Path) -> None:
    with pytest.raises(audio.AudioError):
        packaging.assemble_chapter(
            [],
            tmp_path / "empty.mp3",
            bitrate="64k",
            ffmpeg=tools.ffmpeg,
            ffprobe=tools.ffprobe,
        )
    assert not (tmp_path / "empty.mp3").exists()


# --------------------------------------------------------------------------- #
# M4B assembly
# --------------------------------------------------------------------------- #
def test_assemble_m4b_reports_measured_chapters(
    tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    out = tmp_path / "v01" / naming.m4b_filename("示例书", "晓晓", 1)
    assert out.name == "示例书 - 晓晓 - v01.m4b"
    result = assemble(tools, chapter_plan, out)

    assert result["path"] == str(out)
    assert out.is_file()
    assert result["bytes"] == out.stat().st_size > 0
    assert abs(result["duration_ms"] - 4500) <= 1000
    assert result["chapters"] == [
        {"index": 1, "title": "序言", "start_ms": 0, "end_ms": 1000, "duration_ms": 1000},
        {"index": 2, "title": "出发", "start_ms": 1000, "end_ms": 2500, "duration_ms": 1500},
        {"index": 3, "title": "出发", "start_ms": 2500, "end_ms": 4500, "duration_ms": 2000},
    ]
    # Measured total duration agrees with the sum of chapter durations (<= 1 s,
    # the A3 acceptance rule; AAC priming / container padding can add a little).
    assert abs(sum(chapter["duration_ms"] for chapter in result["chapters"]) - result["duration_ms"]) <= 1000
    # Chapters are back-to-back with the first starting at 0.
    assert result["chapters"][0]["start_ms"] == 0
    for previous, current in zip(result["chapters"], result["chapters"][1:]):
        assert current["start_ms"] == previous["end_ms"]
    # Only the published artifact is left in the directory.
    assert [path.name for path in out.parent.iterdir()] == [out.name]


def test_assemble_m4b_probed_chapters_match_input(
    tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    out = tmp_path / "book.m4b"
    result = assemble(tools, chapter_plan, out)

    probed = audio.probe_chapters(out, ffprobe=tools.ffprobe)
    assert len(probed) == 3
    assert [chapter["title"] for chapter in probed] == ["序言", "出发", "出发"]
    assert [chapter["start_ms"] for chapter in probed] == [0, 1000, 2500]
    assert [chapter["end_ms"] for chapter in probed] == [1000, 2500, 4500]
    # Returned metadata is the measured (probed) metadata.
    assert [(chapter["title"], chapter["start_ms"], chapter["end_ms"]) for chapter in result["chapters"]] == [
        (chapter["title"], chapter["start_ms"], chapter["end_ms"]) for chapter in probed
    ]
    info = audio.probe(out, ffprobe=tools.ffprobe)
    assert abs(info["duration_ms"] - result["duration_ms"]) <= 100
    assert abs(info["duration_ms"] - probed[-1]["end_ms"]) <= 1000  # A3: last end vs total


def test_assemble_m4b_rejects_empty_chapter(
    tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    chapters = [dict(chapter) for chapter in chapter_plan]
    chapters[1] = {"index": 2, "title": "出发", "segment_wavs": []}
    with pytest.raises(audio.AudioError):
        assemble(tools, chapters, tmp_path / "broken.m4b")
    assert not (tmp_path / "broken.m4b").exists()


def test_assemble_m4b_rejects_empty_chapter_list(tools: Settings, tmp_path: Path) -> None:
    with pytest.raises(audio.AudioError):
        assemble(tools, [], tmp_path / "empty.m4b")
    assert not (tmp_path / "empty.m4b").exists()


def test_assemble_m4b_broken_cover_keeps_audio(
    tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    bogus_cover = tmp_path / "cover.jpg"
    bogus_cover.write_text("not an image", encoding="utf-8")
    out = tmp_path / "book.m4b"
    result = assemble(tools, chapter_plan, out, cover_path=bogus_cover)

    assert out.is_file()
    assert abs(result["duration_ms"] - 4500) <= 1000
    assert [chapter["title"] for chapter in result["chapters"]] == ["序言", "出发", "出发"]
    verified = packaging.verify_m4b(
        out, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe, expected_chapters=3
    )
    assert verified["ok"] is True


def test_assemble_m4b_rejects_old_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    monkeypatch.setattr(config, "ffmpeg_major", lambda exe: 6)
    target = tmp_path / "old.m4b"
    with pytest.raises(audio.AudioError) as excinfo:
        assemble(tools, chapter_plan, target)
    message = str(excinfo.value)
    assert "FFmpeg" in message
    assert str(MIN_FFMPEG_MAJOR) in message
    assert not target.exists()


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def test_verify_m4b_reports_ok(tools: Settings, tmp_path: Path, chapter_plan: list[dict]) -> None:
    out = tmp_path / "book.m4b"
    assemble(tools, chapter_plan, out)

    verified = packaging.verify_m4b(
        out, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe, expected_chapters=3
    )
    assert verified["ok"] is True
    assert "error" not in verified
    assert abs(verified["duration_ms"] - 4500) <= 1000
    assert [chapter["title"] for chapter in verified["chapters"]] == ["序言", "出发", "出发"]
    assert [chapter["start_ms"] for chapter in verified["chapters"]] == [0, 1000, 2500]
    assert [chapter["end_ms"] for chapter in verified["chapters"]] == [1000, 2500, 4500]


def test_verify_m4b_reports_wrong_chapter_count(
    tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    out = tmp_path / "book.m4b"
    assemble(tools, chapter_plan, out)
    verified = packaging.verify_m4b(
        out, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe, expected_chapters=2
    )
    assert verified["ok"] is False
    assert verified["error"]


def test_verify_m4b_reports_broken_file(tools: Settings, tmp_path: Path) -> None:
    broken = tmp_path / "broken.m4b"
    broken.write_bytes(b"not an m4b")
    verified = packaging.verify_m4b(broken, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe)
    assert verified["ok"] is False
    assert verified["error"]


def test_verify_m4b_reports_missing_file(tools: Settings, tmp_path: Path) -> None:
    verified = packaging.verify_m4b(tmp_path / "missing.m4b", ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe)
    assert verified["ok"] is False
    assert verified["error"]


def test_assemble_m4b_failure_keeps_existing_mp3(
    monkeypatch: pytest.MonkeyPatch, tools: Settings, tmp_path: Path, chapter_plan: list[dict]
) -> None:
    """A failed M4B packaging run must not damage already published chapter MP3s."""
    out_dir = tmp_path / "v01"
    out_dir.mkdir()
    mp3 = out_dir / "001 - 序言.mp3"
    packaging.assemble_chapter(
        chapter_plan[0]["segment_wavs"],
        mp3,
        bitrate="64k",
        ffmpeg=tools.ffmpeg,
        ffprobe=tools.ffprobe,
    )
    published = mp3.read_bytes()

    def fail_encode(*args, **kwargs):
        raise audio.AudioError("模拟 M4B 封装编码失败")

    monkeypatch.setattr(audio, "encode_m4b", fail_encode)
    target = out_dir / "示例书 - 晓晓 - v01.m4b"
    with pytest.raises(audio.AudioError, match="模拟 M4B 封装编码失败"):
        assemble(tools, chapter_plan, target)

    assert mp3.is_file()
    assert mp3.read_bytes() == published  # byte-identical, not re-encoded or deleted
    assert not target.exists()
    # Only the previously published MP3 remains (temp WAV/M4B cleaned up).
    assert [path.name for path in out_dir.iterdir()] == [mp3.name]


def test_verify_m4b_detects_non_monotonic_chapters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "fake.m4b"
    target.write_bytes(b"placeholder")
    monkeypatch.setattr(audio, "verify_decodable", lambda path, *, ffmpeg: None)
    monkeypatch.setattr(audio, "probe", lambda path, *, ffprobe: {"duration_ms": 4500})
    monkeypatch.setattr(
        audio,
        "probe_chapters",
        lambda path, *, ffprobe: [
            {"id": 0, "title": "序言", "start_ms": 0, "end_ms": 1000},
            {"id": 1, "title": "出发", "start_ms": 1000, "end_ms": 2500},
            {"id": 2, "title": "出发", "start_ms": 1000, "end_ms": 4500},
        ],
    )
    verified = packaging.verify_m4b(target, ffmpeg="ffmpeg", ffprobe="ffprobe")
    assert verified["ok"] is False
    assert "递增" in verified["error"]


def test_verify_m4b_detects_broken_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "fake.m4b"
    target.write_bytes(b"placeholder")
    monkeypatch.setattr(audio, "verify_decodable", lambda path, *, ffmpeg: None)
    monkeypatch.setattr(audio, "probe", lambda path, *, ffprobe: {"duration_ms": 4500})
    monkeypatch.setattr(
        audio,
        "probe_chapters",
        lambda path, *, ffprobe: [
            {"id": 0, "title": "序言", "start_ms": 0, "end_ms": 1000},
            {"id": 1, "title": "出发", "start_ms": 1200, "end_ms": 2500},
            {"id": 2, "title": "出发", "start_ms": 2500, "end_ms": 4500},
        ],
    )
    verified = packaging.verify_m4b(target, ffmpeg="ffmpeg", ffprobe="ffprobe")
    assert verified["ok"] is False
    assert "首尾相接" in verified["error"]
