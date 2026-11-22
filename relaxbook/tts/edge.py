"""Microsoft Edge online neural TTS (CONTRACTS section 10).

Why this file does not just use ``edge_tts.Communicate``: connecting to
``speech.platform.bing.com`` from this machine with OpenSSL / aiohttp is cut off
during the TLS handshake (``SSL: UNEXPECTED_EOF``).  A browser TLS fingerprint is
required, so the WebSocket is opened with :mod:`curl_cffi`'s ``AsyncSession`` and
``impersonate="chrome"``.  The protocol framing (speech.config + ssml messages,
``Path:turn.end`` terminator, 2-byte big-endian header length before audio
frames) is the same one ``edge-tts`` uses, and its helpers are reused for the
DRM token, headers and timestamps.

``synthesize`` is synchronous by design: the worker runs several of them in a
thread pool, and each call wraps its own ``asyncio.run`` around a fresh
``AsyncSession``, so there is no shared event loop or mutable session state.

Robustness (the public endpoint is flaky in practice): each attempt has a short
handshake timeout (20 s) and a short per-receive timeout (25 s), so a reset
connection costs seconds instead of a minute; a failed attempt is retried with
an exponential-ish back-off plus random jitter (to de-phase concurrent worker
threads) and always re-opens a fresh session.  There is a hard cap of 5 attempts
(``EdgeEngine(attempts=...)``) so a broken network cannot park a job forever;
the final :class:`TTSError` carries the classified cause of the **last** error.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from typing import Iterable
from xml.sax.saxutils import escape

from curl_cffi.const import CurlWsFlag
from curl_cffi.requests import AsyncSession
from edge_tts.communicate import date_to_string
from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
from edge_tts.drm import DRM

from .base import TTSError, Voice

logger = logging.getLogger(__name__)

#: Output format requested from the service: 24 kHz, 48 kbps, mono MP3.
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"

#: Per-attempt WebSocket receive timeout (seconds).  A reset used to cost 60 s.
RECV_TIMEOUT = 25.0

#: WebSocket handshake / TCP+TLS connect timeout (seconds).
CONNECT_TIMEOUT = 20.0

#: Hard cap on total attempts (initial try + retries); overridable per engine.
DEFAULT_ATTEMPTS = 5

#: Back-off schedule between attempts (seconds, before jitter).  With the
#: default 5 total attempts the first four entries are used (1/2/3/5 s); the
#: 8 s entry is there for a deliberately more patient ``attempts=6`` engine.
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0)

#: Random extra delay (0..JITTER seconds) added to each back-off.
JITTER = 1.0

#: Curated built-in catalogue (no network call needed to render the UI).
BUILTIN_VOICES: tuple[Voice, ...] = (
    Voice("zh-CN-XiaoxiaoNeural", "晓晓（女·温暖）", "zh-CN", "Female", "edge"),
    Voice("zh-CN-XiaoyiNeural", "晓伊（女·活泼）", "zh-CN", "Female", "edge"),
    Voice("zh-CN-YunxiNeural", "云希（男·年轻）", "zh-CN", "Male", "edge"),
    Voice("zh-CN-YunjianNeural", "云健（男·沉稳）", "zh-CN", "Male", "edge"),
    Voice("zh-CN-YunyangNeural", "云扬（男·新闻）", "zh-CN", "Male", "edge"),
    Voice("en-US-AriaNeural", "Aria（女·美式）", "en-US", "Female", "edge"),
    Voice("en-US-GuyNeural", "Guy（男·美式）", "en-US", "Male", "edge"),
)

#: ``zh-CN-XiaoxiaoNeural`` style identifiers only; keeps SSML attribute safe.
_VOICE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
#: ``+0%`` / ``-10%`` / ``+25%``.
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")

#: Error-classification markers (curl_cffi messages are English, ours are zh).
_TIMEOUT_MARKERS = (
    "timed out",
    "timeout",
    "operation was timed out",
    "curl: (28)",
    "(28)",
)
_RESET_MARKERS = (
    "connection was reset",
    "connection reset",
    "recv failure",
    "connection aborted",
    "unexpected_eof",
    "unexpected eof",
    "curl: (35)",
    "(35)",
)
_NO_AUDIO_MARKERS = ("没有返回音频", "no audio", "空音频")
_SERVICE_ERROR_MARKERS = ('"error"', "服务端返回", "path:response")


def _describe_error(exc: BaseException) -> str:
    """Return ``"<中文类别>：<原始错误>"`` for logs and the final TTSError."""
    detail = str(exc).strip() or exc.__class__.__name__
    lowered = detail.lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or any(
        marker in lowered for marker in _TIMEOUT_MARKERS
    ):
        kind = "连接超时"
    elif any(marker in lowered for marker in _RESET_MARKERS):
        kind = "连接被重置"
    elif any(marker in lowered for marker in _NO_AUDIO_MARKERS):
        kind = "无音频数据"
    elif any(marker in lowered for marker in _SERVICE_ERROR_MARKERS):
        kind = "服务端返回 error"
    else:
        kind = exc.__class__.__name__
    return f"{kind}：{detail}"


def _check_voice(voice: str) -> str:
    value = (voice or "").strip()
    if not value:
        raise TTSError("未指定音色。")
    if len(value) > 64 or any(ch not in _VOICE_ID_CHARS for ch in value):
        raise TTSError(f"不支持的音色标识：{voice}")
    return value


def _check_rate(rate: str | None) -> str:
    value = (rate or "+0%").strip() or "+0%"
    if not _RATE_RE.match(value):
        raise TTSError(f"不支持的语速设置：{rate}")
    return value


def _ssml(text: str, voice: str, rate: str) -> str:
    """Build the SSML request body; *text* must already be XML-escaped."""
    lang = "zh-CN" if voice.startswith("zh") else "en-US"
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xml:lang='{lang}'>"
        f"<voice name='{voice}'><prosody pitch='+0Hz' rate='{rate}' volume='+0%'>"
        f"{text}</prosody></voice></speak>"
    )


async def _synth_async(
    text: str,
    voice: str,
    rate: str,
    *,
    recv_timeout: float = RECV_TIMEOUT,
    connect_timeout: float = CONNECT_TIMEOUT,
) -> bytes:
    """One fresh WebSocket session: send speech.config + SSML, collect audio.

    ``text`` is expected to be already XML-escaped by :meth:`EdgeEngine.synthesize`.
    """
    url = (
        f"{WSS_URL}&ConnectionId={uuid.uuid4().hex}"
        f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
        f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
    )
    audio = bytearray()
    async with AsyncSession() as session:
        async with session.ws_connect(
            url,
            impersonate="chrome",
            headers=DRM.headers_with_muid(WSS_HEADERS),
            timeout=connect_timeout,
        ) as ws:
            await ws.send_str(
                f"X-Timestamp:{date_to_string()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\n"
                "Path:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":'
                '{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
                f'"outputFormat":"{OUTPUT_FORMAT}"}}}}}}}}'  # 4 literal closing braces
                "\r\n"
            )
            await ws.send_str(
                f"X-RequestId:{uuid.uuid4().hex}\r\n"
                "Content-Type:application/ssml+xml\r\n"
                f"X-Timestamp:{date_to_string()}Z\r\n"
                "Path:ssml\r\n\r\n" + _ssml(text, voice, rate)
            )
            while True:
                data, flags = await ws.recv(timeout=recv_timeout)
                if flags & CurlWsFlag.TEXT:
                    raw = data.encode("utf-8") if isinstance(data, str) else data
                    separator = raw.find(b"\r\n\r\n")
                    head = raw[:separator] if separator >= 0 else raw
                    if b"Path:turn.end" in head:
                        break
                    body = raw[separator + 4 :] if separator >= 0 else b""
                    if b"Path:response" in head and body and b'"error"' in body.lower():
                        raise RuntimeError(body.decode("utf-8", "replace"))
                elif flags & CurlWsFlag.BINARY:
                    if isinstance(data, str):
                        data = data.encode("utf-8", "surrogatepass")
                    if len(data) < 2:
                        continue
                    header_len = int.from_bytes(data[0:2], "big")
                    if b"Path:audio\r\n" in data[2 : 2 + header_len]:
                        audio += data[2 + header_len :]
    if not audio:
        raise RuntimeError("语音服务没有返回音频数据（no audio received）")
    return bytes(audio)


class EdgeEngine:
    """Online Edge neural voices, browser-fingerprinted WebSocket transport."""

    name = "edge"
    model = "edge-neural"

    def __init__(
        self,
        voices: Iterable[Voice] | None = None,
        *,
        recv_timeout: float = RECV_TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
        attempts: int = DEFAULT_ATTEMPTS,
        jitter: float = JITTER,
    ) -> None:
        self._voices: list[Voice] = list(voices) if voices is not None else list(BUILTIN_VOICES)
        self.recv_timeout = float(recv_timeout)
        self.connect_timeout = float(connect_timeout)
        self.attempts = max(1, int(attempts))
        self.jitter = max(0.0, float(jitter))
        #: Injectable for tests so retry back-off does not really sleep/randomise.
        self._sleep = time.sleep
        self._random = random.uniform

    def voices(self) -> list[Voice]:
        return list(self._voices)

    def _retry_delay(self, failure_index: int) -> float:
        """Back-off for *failure_index* (0-based) plus 0..jitter seconds."""
        schedule = RETRY_DELAYS
        base = float(schedule[min(failure_index, len(schedule) - 1)])
        return base + float(self._random(0.0, self.jitter))

    def synthesize(self, text: str, voice: str, rate: str = "+0%") -> bytes:
        """Return MP3 bytes; retries up to :attr:`attempts` times, then raises."""
        plain = (text or "").strip()
        if not plain:
            raise TTSError("合成文本为空。")
        checked_voice = _check_voice(voice)
        checked_rate = _check_rate(rate)
        # The service parses SSML, so text must be escaped; voice/rate are
        # validated above instead of escaped because they live in attributes.
        escaped = escape(plain)

        last_error: BaseException | None = None
        last_description = "未知错误"
        for attempt in range(1, self.attempts + 1):
            started = time.perf_counter()
            try:
                audio = asyncio.run(
                    _synth_async(
                        escaped,
                        checked_voice,
                        checked_rate,
                        recv_timeout=self.recv_timeout,
                        connect_timeout=self.connect_timeout,
                    )
                )
                if not audio:
                    raise RuntimeError("语音服务没有返回音频数据（no audio received）")
                return audio
            except Exception as exc:  # noqa: BLE001 - wrapped into TTSError below
                last_error = exc
                last_description = _describe_error(exc)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                if attempt < self.attempts:
                    delay = self._retry_delay(attempt - 1)
                    logger.warning(
                        "Edge 合成失败（第 %d/%d 次尝试，耗时 %d ms，%.1f 秒后重试）：%s",
                        attempt,
                        self.attempts,
                        elapsed_ms,
                        delay,
                        last_description,
                    )
                    self._sleep(delay)
                else:
                    logger.warning(
                        "Edge 合成失败（第 %d/%d 次尝试，耗时 %d ms，放弃）：%s",
                        attempt,
                        self.attempts,
                        elapsed_ms,
                        last_description,
                    )
        raise TTSError(
            f"Edge 语音合成失败（{self.attempts} 次尝试后仍失败）：{last_description}"
        ) from last_error


__all__ = [
    "BUILTIN_VOICES",
    "CONNECT_TIMEOUT",
    "DEFAULT_ATTEMPTS",
    "EdgeEngine",
    "JITTER",
    "OUTPUT_FORMAT",
    "RECV_TIMEOUT",
    "RETRY_DELAYS",
    "_describe_error",
    "_synth_async",
]
