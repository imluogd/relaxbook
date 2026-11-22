"""Tests for the Qwen-TTS engine plumbing (no network required)."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from relaxbook import tts
from relaxbook.config import load_settings
from relaxbook.tts.base import TTSError
from relaxbook.tts.qwen import (
    MODEL_SPECS,
    MULTIMODAL_PATH,
    QWEN3_TTS_VOICES,
    QWEN_AUDIO_FLASH_VOICES,
    QWEN_AUDIO_PLUS_VOICES,
    SPEECH_SYNTH_PATH,
    QwenEngine,
    default_voice_for_model,
    instruction_for_rate,
    parse_rate,
    voices_for_model,
)

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | bytes | None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = payload if isinstance(payload, bytes) else b""

    def json(self):
        if isinstance(self._payload, (dict, list)):
            return self._payload
        raise ValueError("not json")


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict | None, dict | None]] = []
        self.gets: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, *, json=None, headers=None, **kwargs):  # noqa: A002 - httpx signature
        self.requests.append((url, json, headers))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.gets.append(url)
        return self.responses.pop(0)


def _engine_with(responses: list[_FakeResponse], **kwargs) -> tuple[QwenEngine, _FakeClient]:
    engine = QwenEngine("sk-test", **kwargs)
    client = _FakeClient(responses)
    engine._client_factory = lambda: client  # type: ignore[attr-defined]
    return engine, client


# --------------------------------------------------------------------------- #
# configuration / catalogue
# --------------------------------------------------------------------------- #
def test_qwen_without_key_is_explicitly_unavailable(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "data")
    settings.qwen_api_key = ""
    os.environ.pop("DASHSCOPE_API_KEY", None)
    engine = QwenEngine("")
    assert engine.available is False
    with pytest.raises(TTSError) as info:
        engine.synthesize("测试", "longanlingxin_v3.1", "+0%")
    assert "未配置" in str(info.value) and "DASHSCOPE_API_KEY" in str(info.value)
    assert tts.resolve_engine_name("auto", settings) == "edge"


def test_voice_catalogues() -> None:
    plus = voices_for_model("qwen-audio-3.1-tts-plus")
    assert {v.id for v in plus} == {
        "longanlingxin_v3.1",
        "longanhuan_v3.1",
        "longanfengyue_v3.1",
        "xunanchuan_v3.1",
    }
    assert all(v.engine == "qwen" for v in plus)
    assert default_voice_for_model("qwen-audio-3.1-tts-plus") == "longanlingxin_v3.1"
    assert len(voices_for_model("qwen3-tts-flash")) == len(QWEN3_TTS_VOICES) == 14
    assert len(voices_for_model("qwen-audio-3.1-tts-flash")) == len(QWEN_AUDIO_FLASH_VOICES)
    assert len(QWEN_AUDIO_FLASH_VOICES) > len(QWEN_AUDIO_PLUS_VOICES)
    # unknown revision falls back to its family catalogue instead of crashing
    assert voices_for_model("qwen-audio-9.9-tts-plus")
    assert voices_for_model("something-else") == QWEN3_TTS_VOICES


def test_model_endpoints() -> None:
    assert MODEL_SPECS["qwen-audio-3.1-tts-plus"].endpoint == SPEECH_SYNTH_PATH
    assert MODEL_SPECS["qwen-audio-3.1-tts-plus"].instruction is True
    assert MODEL_SPECS["qwen3-tts-flash"].endpoint == MULTIMODAL_PATH
    assert MODEL_SPECS["qwen3-tts-flash"].instruction is False


# --------------------------------------------------------------------------- #
# rate handling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("rate", "expected"),
    [("+20%", 1.2), ("-10%", 0.9), ("+0%", 1.0), ("0%", 1.0), ("", None), ("fast", None)],
)
def test_parse_rate(rate: str, expected: float | None) -> None:
    assert parse_rate(rate) == expected


def test_instruction_for_rate() -> None:
    fast = instruction_for_rate("+20%")
    slow = instruction_for_rate("-10%")
    assert "快约 20%" in fast
    assert "慢约 10%" in slow
    assert "有声书" in fast
    assert "吼" not in fast  # 不要喊读
    neutral = instruction_for_rate("+0%")
    assert "快约" not in neutral and "慢约" not in neutral
    assert "有声书" in neutral


def test_plus_model_sends_instruction_and_uses_speech_endpoint(monkeypatch) -> None:
    payload = {"output": {"audio": {"data": base64.b64encode(WAV_BYTES).decode()}}}
    engine, client = _engine_with([_FakeResponse(200, payload)])
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    audio = engine.synthesize("清晨的列车", "longanlingxin_v3.1", "+20%")
    assert audio == WAV_BYTES
    url, body, headers = client.requests[0]
    assert url.endswith(SPEECH_SYNTH_PATH)
    assert body["model"] == "qwen-audio-3.1-tts-plus"
    assert body["input"]["voice"] == "longanlingxin_v3.1"
    assert "快约 20%" in body["input"]["instruction"]
    assert headers["Authorization"] == "Bearer sk-test"
    assert engine.rate_supported is True


def test_qwen3_flash_rejects_rate_instead_of_ignoring_it() -> None:
    engine = QwenEngine("sk-test", model="qwen3-tts-flash")
    assert engine.rate_supported is False
    with pytest.raises(TTSError) as info:
        engine.synthesize("测试", "Cherry", "+20%")
    assert "不支持语速" in str(info.value)
    assert "qwen-audio-3.1-tts-plus" in str(info.value)


def test_qwen3_flash_uses_multimodal_endpoint(monkeypatch) -> None:
    payload = {"output": {"audio": {"data": base64.b64encode(WAV_BYTES).decode()}}}
    engine, client = _engine_with([_FakeResponse(200, payload)], model="qwen3-tts-flash")
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    assert engine.synthesize("测试", "Cherry", "+0%") == WAV_BYTES
    url, body, _headers = client.requests[0]
    assert url.endswith(MULTIMODAL_PATH)
    assert body["model"] == "qwen3-tts-flash"
    assert "instruction" not in body["input"]


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def test_url_payload_is_downloaded(monkeypatch) -> None:
    payload = {"output": {"audio": {"url": "https://example.invalid/tts/1.wav"}}}
    engine, client = _engine_with(
        [_FakeResponse(200, payload), _FakeResponse(200, b"WAVDATA")]
    )
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    assert engine.synthesize("测试", "longanlingxin_v3.1", "+0%") == b"WAVDATA"
    assert client.gets == ["https://example.invalid/tts/1.wav"]


def test_http_error_becomes_chinese_tts_error(monkeypatch) -> None:
    engine, client = _engine_with(
        [_FakeResponse(401, {"code": "InvalidApiKey", "message": "bad key"})], attempts=1
    )
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    with pytest.raises(TTSError) as info:
        engine.synthesize("测试", "longanlingxin_v3.1", "+0%")
    message = str(info.value)
    assert "Qwen" in message
    assert "认证失败" in message or "InvalidApiKey" in message


def test_throttling_message_mentions_concurrency(monkeypatch) -> None:
    engine, client = _engine_with(
        [_FakeResponse(429, {"code": "Throttling.RateQuota", "message": "rate limit"})],
        attempts=1,
    )
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    with pytest.raises(TTSError) as info:
        engine.synthesize("测试", "longanlingxin_v3.1", "+0%")
    assert "限流" in str(info.value)
    assert "concurrency" in str(info.value)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_auto_engine_uses_qwen_when_key_present(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "data")
    settings.engine = "auto"
    settings.qwen_api_key = "sk-test"
    assert settings.resolved_engine == "qwen"
    assert tts.resolve_engine_name("auto", settings) == "qwen"
    engine = tts.get_engine("auto", settings)
    assert isinstance(engine, QwenEngine)
    assert engine.model == "qwen-audio-3.1-tts-plus"
    status = {row["name"]: row for row in tts.engine_status(settings)}
    assert status["qwen"]["available"] is True
    assert status["qwen"]["selected"] is True
    assert status["edge"]["available"] is True
    # the API lists both engines' voices and the engine follows the voice
    listed = {voice.id: voice.engine for voice in tts.list_voices(settings)}
    assert listed["longanlingxin_v3.1"] == "qwen"
    assert listed["zh-CN-XiaoxiaoNeural"] == "edge"
    assert tts.voice_engine("longanlingxin_v3.1", settings) == "qwen"
    assert tts.voice_engine("zh-CN-XiaoxiaoNeural", settings) == "edge"


def test_engine_status_without_key(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "data")
    settings.engine = "auto"
    settings.qwen_api_key = ""
    os.environ.pop("DASHSCOPE_API_KEY", None)
    status = {row["name"]: row for row in tts.engine_status(settings)}
    assert status["qwen"]["available"] is False
    assert "DASHSCOPE_API_KEY" in status["qwen"]["reason"]
    assert status["edge"]["selected"] is True
