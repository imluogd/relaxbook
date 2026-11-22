"""Tests for the TTS layer (CONTRACTS section 10).

The real Edge synthesis test hits the network on purpose: it is the one check
that the ``curl_cffi`` browser-fingerprint transport actually works on this
machine.  When the network (or the service) is unavailable the test is skipped
with the concrete reason instead of failing the suite.

Retry/timeout behaviour is tested offline by monkeypatching the single-attempt
transport (``relaxbook.tts.edge._synth_async``), which also keeps the suite
deterministic while the public Edge endpoint is flaky.
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
CONNECTION_RESET = "curl: (35) Recv failure: Connection was reset"


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


def test_default_worker_concurrency_is_three(tmp_path):
    """High concurrency makes the Edge endpoint reset connections; default is 3."""
    assert Settings(data_dir=str(tmp_path)).concurrency == 3


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
# retry / error classification / escaping / concurrency, transport replaced
# --------------------------------------------------------------------------- #
def test_retry_recovers_after_two_connection_errors(monkeypatch):
    calls: list[str] = []
    sleeps: list[float] = []

    async def flaky(text, voice, rate, **kwargs):
        calls.append(text)
        if len(calls) < 3:
            raise RuntimeError(CONNECTION_RESET)
        return b"ID3-fake-mp3"

    monkeypatch.setattr(edge_module, "_synth_async", flaky)
    engine = EdgeEngine()
    engine._sleep = sleeps.append
    engine._random = lambda low, high: 0.0  # 去掉抖动，断言确定性

    result = engine.synthesize("甲 & 乙 <丙> 丁", "zh-CN-XiaoxiaoNeural", "+0%")

    assert result == b"ID3-fake-mp3", "前两次连接错误后第三次必须成功"
    assert len(calls) == 3, "成功前正好尝试 3 次"
    assert sleeps == [1.0, 2.0], "退避必须为 1s、2s（抖动置 0）"
    assert calls[0] == "甲 &amp; 乙 &lt;丙&gt; 丁", "SSML 文本必须做 XML 转义"


def test_exhaustion_raises_tts_error_with_last_error(monkeypatch):
    calls: list[str] = []
    sleeps: list[float] = []

    async def always_fails(text, voice, rate, **kwargs):
        calls.append(text)
        raise RuntimeError(CONNECTION_RESET)

    monkeypatch.setattr(edge_module, "_synth_async", always_fails)
    engine = EdgeEngine()  # 默认 5 次尝试
    engine._sleep = sleeps.append
    engine._random = lambda low, high: 0.0

    with pytest.raises(TTSError) as excinfo:
        engine.synthesize("你好", "zh-CN-XiaoxiaoNeural", "+0%")

    message = str(excinfo.value)
    assert "Edge 语音合成失败" in message
    assert "连接被重置" in message, "TTSError 必须带错误分类"
    assert "Connection was reset" in message, "TTSError 必须带最后一次的原始错误"
    assert "5 次" in message
    assert len(calls) == edge_module.DEFAULT_ATTEMPTS == 5, "最多尝试 5 次，不得无上限重试"
    assert sleeps == [1.0, 2.0, 3.0, 5.0]


def test_timeouts_attempts_and_jitter_are_overridable(monkeypatch):
    seen: dict = {}

    async def capture(text, voice, rate, *, recv_timeout=None, connect_timeout=None):
        seen["recv_timeout"] = recv_timeout
        seen["connect_timeout"] = connect_timeout
        return b"ID3-fake-mp3"

    monkeypatch.setattr(edge_module, "_synth_async", capture)
    engine = EdgeEngine(recv_timeout=12.5, connect_timeout=8.5, attempts=3, jitter=0.0)
    assert engine.recv_timeout == 12.5
    assert engine.connect_timeout == 8.5
    assert engine.attempts == 3
    engine.synthesize("你好", "zh-CN-XiaoxiaoNeural", "+0%")
    assert seen == {"recv_timeout": 12.5, "connect_timeout": 8.5}


@pytest.mark.parametrize(
    "error, expected_kind",
    [
        (TimeoutError("timed out"), "连接超时"),
        (
            RuntimeError(
                "curl: (28) Failed to connect to speech.platform.bing.com port 443 "
                "after 21657 ms: Couldn't connect to server"
            ),
            "连接超时",
        ),
        (RuntimeError(CONNECTION_RESET), "连接被重置"),
        (RuntimeError('{"error":"Invalid voice"}'), "服务端返回 error"),
        (
            RuntimeError("语音服务没有返回音频数据（no audio received）"),
            "无音频数据",
        ),
    ],
)
def test_error_classification_in_tts_error(monkeypatch, error, expected_kind):
    async def fails(text, voice, rate, **kwargs):
        raise error

    monkeypatch.setattr(edge_module, "_synth_async", fails)
    engine = EdgeEngine(attempts=1)
    with pytest.raises(TTSError) as excinfo:
        engine.synthesize("你好", "zh-CN-XiaoxiaoNeural", "+0%")
    message = str(excinfo.value)
    assert "Edge 语音合成失败" in message
    assert expected_kind in message


def test_synthesize_runs_concurrently_in_several_threads(monkeypatch):
    """The sync wrapper must be callable from a thread pool (one loop per call)."""

    async def slow(text, voice, rate, **kwargs):
        await asyncio.sleep(0.02)
        return b"ID3" + text.encode("utf-8")

    monkeypatch.setattr(edge_module, "_synth_async", slow)
    engine = EdgeEngine()
    texts = ["第一段", "第一段", "第三段"]  # duplicate text keeps the cache semantics real
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda text: engine.synthesize(text, "zh-CN-XiaoxiaoNeural", "+0%"), texts
            )
        )
    elapsed = time.perf_counter() - started
    assert results == [b"ID3" + text.encode("utf-8") for text in texts]
    assert elapsed < 1.0, "三个合成应并行完成，而不是串行累加"


# --------------------------------------------------------------------------- #
# real network synthesis (the acceptance-critical check)
# --------------------------------------------------------------------------- #
def test_real_edge_synthesis(tmp_path):
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
