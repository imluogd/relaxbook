"""Qwen-TTS engine (Alibaba Cloud DashScope / 百炼).

Configuration
-------------
Set the API key either in ``config.json`` (``qwen_api_key``) or, preferably, in
the ``DASHSCOPE_API_KEY`` environment variable.  ``config.engine`` may be
``auto`` (use Qwen when a key is present, otherwise Edge), ``qwen`` or ``edge``.

API shape (documented DashScope "multimodal generation" endpoint)::

    POST {base_url}/api/v1/services/aigc/multimodal-generation/generation
    Authorization: Bearer <DASHSCOPE_API_KEY>
    {"model": "qwen-tts", "input": {"text": "...", "voice": "Cherry"}}

The response carries either base64 audio (``output.audio.data``) or a short
lived download URL (``output.audio.url``).  Both are handled here so the engine
keeps working if Alibaba switches the delivery style of a model revision.

The engine returns **WAV** bytes (24 kHz mono).  ``audio_format`` tells the
worker which suffix to use for the temporary file; FFmpeg decodes either way.
"""

from __future__ import annotations

import base64
import time

import httpx

from .base import TTSError, Voice

#: Voices per model family.  ``qwen3-tts-flash`` is the current model and offers
#: expressive Mandarin voices plus dialect voices; the older ``qwen-tts`` model
#: only exposes the four bilingual ones.  Descriptions follow the official voice
#: list (help.aliyun.com/zh/model-studio/qwen-tts-voice-list).
QWEN_TTS_VOICES: tuple[Voice, ...] = (
    Voice("Cherry", "芊悦（女·阳光自然）", "zh-CN", "Female", "qwen"),
    Voice("Serena", "苏瑶（女·温柔）", "zh-CN", "Female", "qwen"),
    Voice("Ethan", "晨煦（男·阳光，北方口音）", "zh-CN", "Male", "qwen"),
    Voice("Chelsie", "千雪（女·二次元）", "zh-CN", "Female", "qwen"),
)
QWEN3_TTS_VOICES: tuple[Voice, ...] = QWEN_TTS_VOICES + (
    Voice("Moon", "月白（男·率性帅气）", "zh-CN", "Male", "qwen"),
    Voice("Maia", "四月（女·知性温柔）", "zh-CN", "Female", "qwen"),
    Voice("Kai", "凯（男·低沉舒适）", "zh-CN", "Male", "qwen"),
    Voice("Nofish", "不吃鱼（男·卷舌音偏弱的设计师）", "zh-CN", "Male", "qwen"),
    Voice("Bella", "萌宝（女·小萝莉）", "zh-CN", "Female", "qwen"),
    Voice("Momo", "茉兔（女·撒娇搞怪）", "zh-CN", "Female", "qwen"),
    Voice("Vivian", "十三（女·小暴躁）", "zh-CN", "Female", "qwen"),
    Voice("Jada", "Jada（女·上海话）", "zh-CN", "Female", "qwen"),
    Voice("Dylan", "Dylan（男·北京话）", "zh-CN", "Male", "qwen"),
    Voice("Peter", "Peter（男·天津话）", "zh-CN", "Male", "qwen"),
)

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
DEFAULT_MODEL = "qwen3-tts-flash"
GENERATION_PATH = "/api/v1/services/aigc/multimodal-generation/generation"


def voices_for_model(model: str) -> tuple[Voice, ...]:
    if model.startswith("qwen3-tts"):
        return QWEN3_TTS_VOICES
    return QWEN_TTS_VOICES


class QwenEngine:
    """Synchronous Qwen-TTS client used from the worker's thread pool."""

    name = "qwen"
    audio_format = "wav"
    #: Qwen-TTS has no speaking-rate parameter; the pipeline must not fake one.
    rate_supported = False

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        attempts: int = 3,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        # Injectable for tests: returns an httpx.Client-compatible object.
        self._client_factory = lambda: httpx.Client(timeout=self.timeout)

    # -- engine protocol ---------------------------------------------------
    def voices(self) -> list[Voice]:
        return list(voices_for_model(self.model))

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def synthesize(self, text: str, voice: str, rate: str) -> bytes:
        if not self.available:
            raise TTSError(
                "Qwen-TTS 未配置 API Key：请在 config.json 填写 qwen_api_key，"
                "或设置环境变量 DASHSCOPE_API_KEY 后重试。"
            )
        text = (text or "").strip()
        if not text:
            raise TTSError("Qwen-TTS 收到空文本。")
        rate = (rate or "+0%").strip()
        if rate not in ("+0%", "0%", ""):
            raise TTSError(
                f"Qwen-TTS 不支持语速参数（当前 {rate}）。"
                "请把语速设为 +0%，或改用支持语速的 Edge 引擎。"
            )
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                payload = self._request_audio(text, voice)
                if payload:
                    return payload
                last_error = TTSError("服务端未返回音频数据。")
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
            if attempt < self.attempts:
                time.sleep(min(2.0 * attempt, 5.0))
        detail = str(last_error) if last_error else "未知错误"
        raise TTSError(f"Qwen 语音合成失败（共尝试 {self.attempts} 次）：{detail}")

    # -- internals ---------------------------------------------------------
    def _request_audio(self, text: str, voice: str) -> bytes:
        url = f"{self.base_url}{GENERATION_PATH}"
        body = {"model": self.model, "input": {"text": text, "voice": voice}}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with self._client_factory() as client:
            response = client.post(url, json=body, headers=headers)
            if response.status_code >= 400:
                raise TTSError(self._error_message(response))
            try:
                data = response.json()
            except ValueError as exc:
                raise TTSError(f"Qwen 返回了非 JSON 响应（HTTP {response.status_code}）。") from exc
            audio = self._audio_from_payload(data)
            if audio is not None:
                return audio
            download_url = self._url_from_payload(data)
            if download_url:
                download = client.get(download_url, timeout=self.timeout)
                if download.status_code >= 400:
                    raise TTSError(f"下载 Qwen 合成音频失败（HTTP {download.status_code}）。")
                return download.content
        raise TTSError("Qwen 响应里既没有音频数据也没有下载地址。")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return f"Qwen 请求失败（HTTP {response.status_code}）：{response.text[:200]}"
        code = data.get("code") or data.get("error", {}).get("code") or ""
        message = data.get("message") or data.get("error", {}).get("message") or ""
        if str(code).lower() in {"invalidapikey", "invalid_api_key", "authenticationerror"}:
            return f"Qwen 认证失败（{code}）：请检查 DASHSCOPE_API_KEY 是否正确。"
        return f"Qwen 请求失败（HTTP {response.status_code}，{code}）：{message or response.text[:200]}"

    @staticmethod
    def _audio_from_payload(data: dict) -> bytes | None:
        output = data.get("output") or {}
        audio = output.get("audio")
        if isinstance(audio, dict) and audio.get("data"):
            try:
                return base64.b64decode(audio["data"])
            except (ValueError, TypeError):
                return None
        # Some model revisions put base64 directly on the output node.
        if isinstance(output.get("audio"), str):
            try:
                return base64.b64decode(output["audio"])
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _url_from_payload(data: dict) -> str | None:
        output = data.get("output") or {}
        audio = output.get("audio")
        if isinstance(audio, dict):
            for key in ("url", "audio_url", "download_url"):
                value = audio.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
        for key in ("url", "audio_url"):
            value = output.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return None


__all__ = ["QwenEngine", "QWEN_TTS_VOICES", "QWEN3_TTS_VOICES", "voices_for_model", "DEFAULT_MODEL"]
