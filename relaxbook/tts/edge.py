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
"""

from __future__ import annotations

import asyncio
import logging
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

#: Per-attempt WebSocket receive timeout (seconds).
RECV_TIMEOUT = 60.0

#: Back-off delays between retries; index 0 is after the first failure.
RETRY_DELAYS: tuple[float, float] = (0.5, 1.5)

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


async def _synth_async(text: str, voice: str, rate: str) -> bytes:
    """One WebSocket session: send speech.config + SSML, collect audio frames.

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
                data, flags = await ws.recv(timeout=RECV_TIMEOUT)
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

    def __init__(self, voices: Iterable[Voice] | None = None) -> None:
        self._voices: list[Voice] = list(voices) if voices is not None else list(BUILTIN_VOICES)
        #: Injectable for tests so retry back-off does not really sleep.
        self._sleep = time.sleep

    def voices(self) -> list[Voice]:
        return list(self._voices)

    def synthesize(self, text: str, voice: str, rate: str = "+0%") -> bytes:
        """Return MP3 bytes for *text*; retries twice (0.5 s, 1.5 s) then raises."""
        plain = (text or "").strip()
        if not plain:
            raise TTSError("合成文本为空。")
        checked_voice = _check_voice(voice)
        checked_rate = _check_rate(rate)
        # The service parses SSML, so text must be escaped; voice/rate are
        # validated above instead of escaped because they live in attributes.
        escaped = escape(plain)

        last_error: Exception | None = None
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                audio = asyncio.run(_synth_async(escaped, checked_voice, checked_rate))
                if not audio:
                    raise RuntimeError("语音服务返回了空音频")
                return audio
            except Exception as exc:  # noqa: BLE001 - wrapped into TTSError below
                last_error = exc
                if attempt < attempts:
                    delay = RETRY_DELAYS[attempt - 1]
                    logger.warning(
                        "Edge 合成失败（第 %d/%d 次尝试），%.1f 秒后重试：%s",
                        attempt,
                        attempts,
                        delay,
                        exc,
                    )
                    self._sleep(delay)
        raise TTSError(f"Edge 语音合成失败：{last_error}") from last_error


__all__ = [
    "BUILTIN_VOICES",
    "EdgeEngine",
    "OUTPUT_FORMAT",
    "RETRY_DELAYS",
    "_synth_async",
]
