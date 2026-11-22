"""Qwen-TTS engines (Alibaba Cloud DashScope / 百炼).

Two model families are supported, each with its own endpoint and voice set:

``qwen-audio-3.1-tts-plus`` / ``qwen-audio-3.1-tts-flash`` (Qwen-Audio-TTS)
    Endpoint ``POST /api/v1/services/audio/tts/SpeechSynthesizer``.
    Highest quality, supports *instruction control* (语气、语速、方言), SSML and
    timestamps.  ``Voice`` ids look like ``longanlingxin_v3.1``.

``qwen3-tts-flash`` (Qwen3-TTS)
    Endpoint ``POST /api/v1/services/aigc/multimodal-generation/generation``.
    Fast and cheap; voices are ``Cherry``/``Serena``/... and it has no
    instruction control (so no generation-side speed control).

Both return a short-lived WAV download URL (24 kHz mono).  ``audio_format`` tells
the worker which suffix to use for the temporary file; FFmpeg decodes either way.

Speaking rate: the Qwen-Audio-TTS models do not take a numeric speed parameter,
but they accept a natural-language ``instruction``.  We translate ``rate``
(``+20%``, ``-10%``, ...) into such an instruction so the UI's rate control keeps
working, e.g. ``+20%`` -> “语速比正常快 20%，保持自然流畅。”.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass

import httpx

from .base import TTSError, Voice

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"

MULTIMODAL_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
SPEECH_SYNTH_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"

DEFAULT_MODEL = "qwen-audio-3.1-tts-plus"

# ---------------------------------------------------------------------------
# voice catalogues
# ---------------------------------------------------------------------------
#: Qwen3-TTS voices (qwen3-tts-flash): expressive Mandarin voices.
QWEN3_TTS_VOICES: tuple[Voice, ...] = (
    Voice("Cherry", "芊悦（女·阳光自然）", "zh-CN", "Female", "qwen"),
    Voice("Serena", "苏瑶（女·温柔）", "zh-CN", "Female", "qwen"),
    Voice("Ethan", "晨煦（男·阳光，北方口音）", "zh-CN", "Male", "qwen"),
    Voice("Chelsie", "千雪（女·二次元）", "zh-CN", "Female", "qwen"),
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

#: Flagship voices of qwen-audio-3.1-tts-plus (Qwen-Audio-TTS 旗舰音色).
QWEN_AUDIO_PLUS_VOICES: tuple[Voice, ...] = (
    Voice("longanlingxin_v3.1", "龙安灵心（女·知心温暖，有声书）", "zh-CN", "Female", "qwen"),
    Voice("longanhuan_v3.1", "龙安欢（女·热情活泼）", "zh-CN", "Female", "qwen"),
    Voice("longanfengyue_v3.1", "龙安风悦（女·自然亲切）", "zh-CN", "Female", "qwen"),
    Voice("xunanchuan_v3.1", "许南川（男·磁性沉稳）", "zh-CN", "Male", "qwen"),
)

#: Audiobook-oriented premium voices of qwen-audio-3.1-tts-flash.
QWEN_AUDIO_FLASH_VOICES: tuple[Voice, ...] = QWEN_AUDIO_PLUS_VOICES + (
    Voice("wenhuaiqing_v3.1", "温怀清（女·清亮柔和）", "zh-CN", "Female", "qwen"),
    Voice("anxiaolan_v3.1", "安小岚（女·清甜纯净）", "zh-CN", "Female", "qwen"),
    Voice("xieshurou_v3.1", "谢舒柔（女·柔和知性）", "zh-CN", "Female", "qwen"),
    Voice("xuyuyuan_v3.1", "许玉远（女·知性成熟）", "zh-CN", "Female", "qwen"),
    Voice("wenhuaizhi_v3.1", "闻怀之（女·稳重成熟）", "zh-CN", "Female", "qwen"),
    Voice("xiaoxingzhi_v3.1", "萧行之（女·端庄贵气）", "zh-CN", "Female", "qwen"),
    Voice("guyunshu_v3.1", "顾云舒（女·成熟稳重）", "zh-CN", "Female", "qwen"),
    Voice("yeqinghe_v3.1", "叶清禾（女·亲切温柔）", "zh-CN", "Female", "qwen"),
    Voice("anmingyuan_v3.1", "安明远（男·清亮自然）", "zh-CN", "Male", "qwen"),
    Voice("huozhuoshi_v3.1", "霍拙石（男·清亮）", "zh-CN", "Male", "qwen"),
    Voice("longyuan_v3.1", "龙媛（女·温暖治愈，有声书）", "zh-CN", "Female", "qwen"),
    Voice("longmiao_v3.1", "龙妙（女·抑扬顿挫，有声书）", "zh-CN", "Female", "qwen"),
    Voice("longsanshu_v3.1", "龙三叔（男·沉稳质感，有声书）", "zh-CN", "Male", "qwen"),
    Voice("longnan_v3.1", "龙楠（男·睿智，有声书）", "zh-CN", "Male", "qwen"),
    Voice("loongbella_v3.1", "Bella（女·干练磁性，中英）", "zh-CN", "Female", "qwen"),
)


@dataclass(frozen=True)
class ModelSpec:
    endpoint: str
    voices: tuple[Voice, ...]
    instruction: bool = False
    default_voice: str = ""
    #: Model family: "qwen-audio-tts" (SpeechSynthesizer) or "qwen3-tts".
    family: str = "qwen3-tts"


MODEL_SPECS: dict[str, ModelSpec] = {
    "qwen-audio-3.1-tts-plus": ModelSpec(
        endpoint=SPEECH_SYNTH_PATH,
        voices=QWEN_AUDIO_PLUS_VOICES,
        instruction=True,
        default_voice="longanlingxin_v3.1",
        family="qwen-audio-tts",
    ),
    "qwen-audio-3.1-tts-flash": ModelSpec(
        endpoint=SPEECH_SYNTH_PATH,
        voices=QWEN_AUDIO_FLASH_VOICES,
        instruction=True,
        default_voice="longanlingxin_v3.1",
        family="qwen-audio-tts",
    ),
    "qwen-audio-3.0-tts-plus": ModelSpec(
        endpoint=SPEECH_SYNTH_PATH,
        voices=(
            Voice("longanlingxin", "龙安灵心（女·知心温暖）", "zh-CN", "Female", "qwen"),
            Voice("longanlufeng", "龙安鲁风（男·明亮开朗）", "zh-CN", "Male", "qwen"),
        ),
        instruction=True,
        default_voice="longanlingxin",
        family="qwen-audio-tts",
    ),
    "qwen3-tts-flash": ModelSpec(
        endpoint=MULTIMODAL_PATH,
        voices=QWEN3_TTS_VOICES,
        instruction=False,
        default_voice="Cherry",
        family="qwen3-tts",
    ),
}


def spec_for_model(model: str) -> ModelSpec:
    name = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if name in MODEL_SPECS:
        return MODEL_SPECS[name]
    # Unknown/renamed revision: guess by family prefix and reuse that catalogue.
    if name.startswith("qwen-audio"):
        return MODEL_SPECS["qwen-audio-3.1-tts-plus"]
    return MODEL_SPECS["qwen3-tts-flash"]


def voices_for_model(model: str) -> tuple[Voice, ...]:
    return spec_for_model(model).voices


def default_voice_for_model(model: str) -> str:
    return spec_for_model(model).default_voice


# ---------------------------------------------------------------------------
# rate -> instruction
# ---------------------------------------------------------------------------
_RATE_RE = re.compile(r"^\s*([+-])?\s*(\d+(?:\.\d+)?)\s*%\s*$")


def parse_rate(rate: str | None) -> float | None:
    """``"+20%"`` -> 1.2, ``"-10%"`` -> 0.9, ``"0%"`` -> 1.0, anything else -> ``None``."""
    match = _RATE_RE.match(rate or "")
    if not match:
        return None
    sign = -1.0 if match.group(1) == "-" else 1.0
    return 1.0 + sign * float(match.group(2)) / 100.0


def instruction_for_rate(rate: str | None, *, style: str = "") -> str:
    """Natural-language instruction carrying the requested speaking rate."""
    parts: list[str] = []
    factor = parse_rate(rate)
    if factor is not None and abs(factor - 1.0) >= 0.01:
        percent = round(abs(factor - 1.0) * 100)
        if factor > 1.0:
            parts.append(f"语速比正常快约 {percent}%，但不要赶，保持吐字清晰")
        else:
            parts.append(f"语速比正常慢约 {percent}%，保持自然，不要拖沓")
    if style:
        parts.append(style)
    parts.append("用适合有声书朗读的语气，自然、连贯、少感叹。")
    return "；".join(parts) + "。"


class QwenEngine:
    """Synchronous DashScope Qwen-TTS client used from the worker's thread pool."""

    name = "qwen"
    audio_format = "wav"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 90.0,
        attempts: int = 3,
        instruction: str = "",
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self.spec = spec_for_model(self.model)
        #: Extra style hint appended to the generated rate instruction.
        self.instruction = instruction or ""
        # Injectable for tests: returns an httpx.Client-compatible object.
        self._client_factory = lambda: httpx.Client(timeout=self.timeout)

    # -- engine protocol ---------------------------------------------------
    def voices(self) -> list[Voice]:
        return list(self.spec.voices)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def rate_supported(self) -> bool:
        """True when the model can honour ``rate`` through instruction control."""
        return bool(self.spec.instruction)

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
        instruction = ""
        if self.spec.instruction:
            instruction = instruction_for_rate(rate, style=self.instruction)
        elif parse_rate(rate) not in (None, 1.0):
            raise TTSError(
                f"当前 Qwen 模型（{self.model}）不支持语速参数（{rate}）。"
                "请改用 qwen-audio-3.1-tts-plus（支持指令控速）或把语速设为 +0%。"
            )
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                payload = self._request_audio(text, voice, instruction)
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
    def _request_body(self, text: str, voice: str, instruction: str) -> dict:
        payload = {"text": text, "voice": voice}
        if instruction:
            payload["instruction"] = instruction
        return {"model": self.model, "input": payload}

    def _request_audio(self, text: str, voice: str, instruction: str) -> bytes:
        url = f"{self.base_url}{self.spec.endpoint}"
        body = self._request_body(text, voice, instruction)
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
        code = data.get("code") or (data.get("error") or {}).get("code") or ""
        message = data.get("message") or (data.get("error") or {}).get("message") or ""
        lowered = str(code).lower()
        if lowered in {"invalidapikey", "invalid_api_key", "authenticationerror"}:
            return f"Qwen 认证失败（{code}）：请检查 DASHSCOPE_API_KEY 是否正确。"
        if "throttling" in lowered or response.status_code == 429:
            return f"Qwen 接口限流（{code}）：请稍后重试，或降低 config.json 的 concurrency。"
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


__all__ = [
    "DEFAULT_MODEL",
    "MODEL_SPECS",
    "ModelSpec",
    "QWEN3_TTS_VOICES",
    "QWEN_AUDIO_FLASH_VOICES",
    "QWEN_AUDIO_PLUS_VOICES",
    "QwenEngine",
    "default_voice_for_model",
    "instruction_for_rate",
    "parse_rate",
    "spec_for_model",
    "voices_for_model",
]
