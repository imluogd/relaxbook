"""Tests for the Qwen-TTS engine plumbing (no network required)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from relaxbook import tts
from relaxbook.config import Settings, load_settings
from relaxbook.tts.base import TTSError
from relaxbook.tts.qwen import QWEN3_TTS_VOICES, QWEN_TTS_VOICES, QwenEngine, voices_for_model

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
    """Records requests and returns scripted responses."""

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


def test_qwen_without_key_is_explicitly_unavailable(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "data")
    settings.qwen_api_key = ""
    import os

    os.environ.pop("DASHSCOPE_API_KEY", None)
    engine = QwenEngine("")
    assert engine.available is False
    with pytest.raises(TTSError) as info:
        engine.synthesize("测试", "Cherry", "+0%")
    assert "未配置" in str(info.value) and "DASHSCOPE_API_KEY" in str(info.value)
    # auto resolves to a working engine when no key is configured
    assert tts.resolve_engine_name("auto", settings) == "edge"


def test_voice_catalogues() -> None:
    assert {v.id for v in QWEN_TTS_VOICES} == {"Cherry", "Serena", "Ethan", "Chelsie"}
    assert len(QWEN3_TTS_VOICES) > len(QWEN_TTS_VOICES)
    assert len(voices_for_model("qwen-tts")) == 4
    assert len(voices_for_model("qwen3-tts-flash")) == len(QWEN3_TTS_VOICES)
    assert all(v.engine == "qwen" for v in QWEN_TTS_VOICES)


def test_rate_is_rejected_instead_of_silently_ignored() -> None:
    engine = QwenEngine("sk-test")
    with pytest.raises(TTSError) as info:
        engine.synthesize("测试", "Cherry", "+20%")
    assert "语速" in str(info.value)


def test_base64_audio_payload(monkeypatch) -> None:
    payload = {"output": {"audio": {"data": base64.b64encode(WAV_BYTES).decode()}}}
    engine, client = _engine_with([_FakeResponse(200, payload)])
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    audio = engine.synthesize("清晨的列车", "Cherry", "+0%")
    assert audio == WAV_BYTES
    url, body, headers = client.requests[0]
    assert url.endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert body["model"] == "qwen-tts"
    assert body["input"] == {"text": "清晨的列车", "voice": "Cherry"}
    assert headers["Authorization"] == "Bearer sk-test"


def test_url_payload_is_downloaded(monkeypatch) -> None:
    payload = {"output": {"audio": {"url": "https://example.invalid/tts/1.wav"}}}
    engine, client = _engine_with(
        [_FakeResponse(200, payload), _FakeResponse(200, b"WAVDATA")]
    )
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    audio = engine.synthesize("测试", "Cherry", "+0%")
    assert audio == b"WAVDATA"
    assert client.gets == ["https://example.invalid/tts/1.wav"]


def test_http_error_becomes_chinese_tts_error(monkeypatch) -> None:
    engine, client = _engine_with(
        [_FakeResponse(401, {"code": "InvalidApiKey", "message": "bad key"})], attempts=1
    )
    monkeypatch.setattr(engine, "_client_factory", lambda: client)
    with pytest.raises(TTSError) as info:
        engine.synthesize("测试", "Cherry", "+0%")
    message = str(info.value)
    assert "Qwen" in message
    assert "认证失败" in message or "InvalidApiKey" in message


def test_auto_engine_uses_qwen_when_key_present(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "data")
    settings.engine = "auto"
    settings.qwen_api_key = "sk-test"
    assert settings.resolved_engine == "qwen"
    assert tts.resolve_engine_name("auto", settings) == "qwen"
    assert isinstance(tts.get_engine("auto", settings), QwenEngine)
    status = {row["name"]: row for row in tts.engine_status(settings)}
    assert status["qwen"]["available"] is True
    assert status["qwen"]["selected"] is True
    assert status["edge"]["available"] is True


def test_engine_status_without_key(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "data")
    settings.engine = "auto"
    settings.qwen_api_key = ""
    status = {row["name"]: row for row in tts.engine_status(settings)}
    assert status["qwen"]["available"] is False
    assert "DASHSCOPE_API_KEY" in status["qwen"]["reason"]
    assert status["edge"]["selected"] is True
