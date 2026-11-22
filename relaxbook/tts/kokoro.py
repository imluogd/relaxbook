"""Local Kokoro engine placeholder (CONTRACTS section 10).

The first release ships only the Edge online engine.  Kokoro (sherpa-onnx) is a
planned optional offline engine; importing this module and resolving
``get_engine("kokoro")`` must never fail, they simply report that the local
model is not installed.  The voice catalogue is empty and synthesis raises a
user-visible :class:`TTSError` so the UI can keep the remaining sample voices.
"""

from __future__ import annotations

from .base import TTSError, Voice

#: Chinese error shown when the optional offline model is not installed.
NOT_INSTALLED_MESSAGE = "本地 Kokoro 引擎未安装（可选的离线方案）"


class KokoroEngine:
    """Placeholder that keeps the engine registry / UI working without models."""

    name = "kokoro"
    model = "kokoro-multi-lang-v1_1"

    def voices(self) -> list[Voice]:
        return []

    def synthesize(self, text: str, voice: str, rate: str = "+0%") -> bytes:
        raise TTSError(NOT_INSTALLED_MESSAGE)


__all__ = ["KokoroEngine", "NOT_INSTALLED_MESSAGE"]
