"""TTS engine registry and voice catalogue (CONTRACTS section 10).

Only this module should be imported by the worker and the web API.  Engine
implementations are imported lazily so that a missing optional dependency only
breaks the engine that needs it (``get_engine("kokoro")`` must always work).
"""

from __future__ import annotations

from .base import Engine, TTSError, Voice

#: Stable engine ids accepted by the API and the config file.
ENGINE_NAMES: tuple[str, ...] = ("edge", "kokoro")


def get_engine(name: str | None, settings=None) -> Engine:
    """Return an engine instance; ``name`` falls back to ``settings.engine``.

    Raises :class:`TTSError` for unknown ids and for engines whose third-party
    dependency is missing; it never raises at import time.
    """
    fallback = getattr(settings, "engine", None) if settings is not None else None
    key = (name or fallback or "edge").strip().lower()
    if key == "edge":
        try:
            from .edge import EdgeEngine
        except ImportError as exc:  # pragma: no cover - depends on install
            raise TTSError(
                "Edge 语音引擎不可用：缺少 curl_cffi 或 edge-tts 依赖，请重新安装。"
            ) from exc
        return EdgeEngine()
    if key == "kokoro":
        from .kokoro import KokoroEngine

        return KokoroEngine()
    raise TTSError(f"未知语音引擎：{name}（可用：{', '.join(ENGINE_NAMES)}）")


def list_voices(settings=None) -> list[Voice]:
    """Voices of the engine selected in *settings* (defaults to ``edge``)."""
    key = getattr(settings, "engine", None) if settings is not None else None
    return get_engine(key, settings).voices()


__all__ = ["ENGINE_NAMES", "Engine", "TTSError", "Voice", "get_engine", "list_voices"]
