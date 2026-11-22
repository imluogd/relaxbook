"""Tests for the TTS layer (CONTRACTS section 10).

The real Edge synthesis test hits the network on purpose: it is the one check
that the ``curl_cffi`` browser-fingerprint transport actually works on this
machine.  When the network (or the service) is unavailable the test is skipped
with the concrete reason instead of failing the suite.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import relaxbook.tts as tts
from relaxbook import audio
from relaxbook.config import Settings, locate_tool
from relaxbook.tts import TTSError, Voice
from relaxbook.tts import edge as edge_module
from relaxbook.tts.edge import EdgeEngine
from relaxbook.tts.kokoro import NOT_INSTALLED_MESSAGE, KokoroEngine

SHORT_SENTENCE = "清晨的列车缓缓驶出站台。"


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=str(tmp_path))
    settings.ensure_dirs()
    settings.resolve_tools()
    return settings


def _mp3_header_ok(data: bytes) -> bool:
    """Accept an ID3 tag or an MPEG audio frame sync at the very start."""
    if data[:3] == b"ID3":
        return True
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


# --------------------------------------------------------------------------- #
# catalogue / registry
# --------------------------------------------------------------------------- #
def test_voice_catalogue_has_chinese_and_english_voices(tmp_path):
    voices = tts.list_voices(_settings(tmp_path))
    assert voices, "默认 edge 引擎必须提供内置音色"
    assert all(isinstance(voice, Voice) for voice in voices)
    chinese = [voice for voice in voices if voice.language.startswith("zh")]
    english = [voice for voice in voices if voice.language.startswith("en")]
    assert len(chinese) >= 3
    assert len(english) >= 1
    for voice in voices:
        assert voice.id.endswith("Neural")
        assert voice.engine == "edge"
        assert voice.name and voice.name != voice.id
        assert voice.gender in ("Female", "Male")
    # Voice.name is the Chinese description shown in the UI.
    assert all(any(ord(ch) > 0x2FFF for ch in voice.name) for voice in chinese)


def test_get_engine_returns_edge_engine(tmp_path):
    engine = tts.get_engine("edge", _settings(tmp_path))
    assert isinstance(engine, EdgeEngine)
    assert engine.name == "edge"
    assert engine.model == "edge-neural"
    assert engine.voices() == list(edge_module.BUILTIN_VOICES)


def test_get_engine_defaults_to_settings_engine(tmp_path):
    settings = _settings(tmp_path)
    settings.engine = "kokoro"
    assert isinstance(tts.get_engine(None, settings), KokoroEngine)
    assert isinstance(tts.get_engine("EDGE", settings), EdgeEngine)


def test_get_engine_unknown_raises_tts_error(tmp_path):
    with pytest.raises(TTSError, match="未知语音引擎"):
        tts.get_engine("no-such-engine", _settings(tmp_path))


def test_kokoro_is_an_importable_placeholder(tmp_path):
    engine = tts.get_engine("kokoro", _settings(tmp_path))
    assert isinstance(engine, KokoroEngine)
    assert engine.voices() == []
    with pytest.raises(TTSError) as excinfo:
        engine.synthesize("测试", "kokoro-voice", "+0%")
    assert NOT_INSTALLED_MESSAGE in str(excinfo.value)


# --------------------------------------------------------------------------- #
# input validation (no network involved)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, voice, rate",
    [
        ("   ", "zh-CN-XiaoxiaoNeural", "+0%"),
        ("你好", "", "+0%"),
        ("你好", "za-ZZ-Oops'Neural", "+0%"),
        ("你好", "zh-CN-XiaoxiaoNeural", "fast"),
        ("你好", "zh-CN-XiaoxiaoNeural", "+0%; drop"),
    ],
)
def test_invalid_arguments_raise_tts_error(text, voice, rate):
    engine = EdgeEngine()
    with pytest.raises(TTSError):
        engine.synthesize(text, voice, rate)


# --------------------------------------------------------------------------- #
# retry / escaping / concurrency, with the network transport replaced
# --------------------------------------------------------------------------- #
def test_retry_backoff_and_ssml_escaping(monkeypatch):
    attempts: list[str] = []
    sleeps: list[float] = []

    async def flaky(text, voice, rate):
        attempts.append(text)
        if len(attempts) < 3:
            raise RuntimeError("模拟网络中断")
        return b"ID3-fake-mp3"

    monkeypatch.setattr(edge_module, "_synth_async", flaky)
    engine = EdgeEngine()
    engine._sleep = sleeps.append

    result = engine.synthesize("甲 & 乙 <丙> 丁", "zh-CN-XiaoxiaoNeural", "+0%")

    assert result == b"ID3-fake-mp3"
    assert sleeps == [0.5, 1.5], "必须按 0.5s、1.5s 退避重试两次"
    assert len(attempts) == 3
    assert attempts[0] == "甲 &amp; 乙 &lt;丙&gt; 丁", "SSML 文本必须做 XML 转义"


def test_retry_exhaustion_raises_tts_error(monkeypatch):
    attempts: list[str] = []

    async def always_fails(text, voice, rate):
        attempts.append(text)
        raise RuntimeError("连接被重置")

    monkeypatch.setattr(edge_module, "_synth_async", always_fails)
    engine = EdgeEngine()
    engine._sleep = lambda seconds: None

    with pytest.raises(TTSError) as excinfo:
        engine.synthesize("你好", "zh-CN-XiaoxiaoNeural", "+0%")
    assert "Edge 语音合成失败" in str(excinfo.value)
    assert "连接被重置" in str(excinfo.value)
    assert len(attempts) == 3


def test_synthesize_runs_concurrently_in_several_threads(monkeypatch):
    """The sync wrapper must be callable from a thread pool (one loop per call)."""

    async def slow(text, voice, rate):
        await asyncio.sleep(0.02)
        return b"ID3" + text.encode("utf-8")

    monkeypatch.setattr(edge_module, "_synth_async", slow)
    engine = EdgeEngine()
    texts = ["第一段", "第一段", "第三段"]  # duplicate text keeps the cache semantics real
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda text: engine.synthesize(text, "zh-CN-XiaoxiaoNeural", "+0%"), texts))
    elapsed = time.perf_counter() - started
    assert results == [b"ID3" + text.encode("utf-8") for text in texts]
    assert elapsed < 1.0, "三个合成应并行完成，而不是串行累加"


# --------------------------------------------------------------------------- #
# real network synthesis (the acceptance-critical check)
# --------------------------------------------------------------------------- #
def test_real_edge_synthesis(tmp_path, capsys):
    engine = tts.get_engine("edge", _settings(tmp_path))
    started = time.perf_counter()
    try:
        data = engine.synthesize(SHORT_SENTENCE, "zh-CN-XiaoxiaoNeural", "+0%")
    except TTSError as exc:
        pytest.skip(f"Edge 联网合成不可用（网络/服务问题）：{exc}")
    elapsed = time.perf_counter() - started

    assert isinstance(data, bytes)
    assert _mp3_header_ok(data), f"不是合法的 MP3 字节头：{data[:8]!r}"
    assert len(data) > 5000, f"返回音频过短：{len(data)} 字节"

    # Probe the returned bytes with ffprobe when available: 24 kHz mono MP3.
    ffprobe = locate_tool("ffprobe")
    probed = None
    if ffprobe:
        mp3_path = tmp_path / "edge-probe.mp3"
        mp3_path.write_bytes(data)
        probed = audio.probe(mp3_path, ffprobe=ffprobe)
        assert probed["sample_rate"] == 24000
        assert probed["channels"] == 1
        assert probed["duration_ms"] > 200

    print(
        f"\n[真实 Edge 合成] 文本={SHORT_SENTENCE!r} 音色=zh-CN-XiaoxiaoNeural "
        f"耗时={elapsed:.2f}s 字节数={len(data)} "
        f"ffprobe={probed}"
    )
