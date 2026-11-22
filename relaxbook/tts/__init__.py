"""TTS engine registry and voice catalogue (CONTRACTS section 10).

Only this module should be imported by the worker and the web API.  Engine
implementations are imported lazily so that a missing optional dependency only
breaks the engine that needs it (``get_engine("kokoro")`` must always work).

Engine ids: ``edge`` (Microsoft Edge voices, online, free), ``qwen``
(Alibaba Cloud DashScope Qwen-TTS, online, needs an API key), ``kokoro``
(optional local model, not installed) and ``auto`` which resolves to Qwen when
a DashScope key is configured and to Edge otherwise.
"""

from __future__ import annotations

from .base import Engine, TTSError, Voice

#: Stable engine ids accepted by the API and the config file.
ENGINE_NAMES: tuple[str, ...] = ("auto", "edge", "qwen", "kokoro")


def _edge() -> Engine:
    try:
        from .edge import EdgeEngine
    except ImportError as exc:  # pragma: no cover - depends on install
        raise TTSError(
            "Edge 语音引擎不可用：缺少 curl_cffi 或 edge-tts 依赖，请重新安装。"
        ) from exc
    return EdgeEngine()


def _qwen(settings) -> Engine:
    from .qwen import DEFAULT_MODEL, QwenEngine

    return QwenEngine(
        getattr(settings, "qwen_key", "") if settings is not None else "",
        model=getattr(settings, "qwen_model", DEFAULT_MODEL) or DEFAULT_MODEL,
        base_url=getattr(settings, "qwen_base_url", "") or "https://dashscope.aliyuncs.com",
        timeout=float(getattr(settings, "qwen_timeout", 60.0) or 60.0),
    )


def resolve_engine_name(name: str | None, settings=None) -> str:
    """Map ``auto`` to a concrete id (``qwen`` when a DashScope key exists)."""
    key = (name or "").strip().lower()
    if not key or key == "auto":
        key = (getattr(settings, "engine", "") or "auto").strip().lower() if settings is not None else "auto"
    if key == "auto" or not key:
        return getattr(settings, "resolved_engine", "edge") if settings is not None else "edge"
    return key


def get_engine(name: str | None, settings=None) -> Engine:
    """Return an engine instance; ``name`` falls back to ``settings.engine``.

    Raises :class:`TTSError` for unknown ids and for engines whose third-party
    dependency is missing; it never raises at import time.
    """
    key = resolve_engine_name(name, settings)
    if key == "edge":
        return _edge()
    if key == "qwen":
        return _qwen(settings)
    if key == "kokoro":
        from .kokoro import KokoroEngine

        return KokoroEngine()
    raise TTSError(f"未知语音引擎：{name}（可用：{', '.join(ENGINE_NAMES)}）")


def list_voices(settings=None) -> list[Voice]:
    """Voices the user may actually pick with the configured engine.

    A configured Qwen key adds the Qwen catalogue on top of the always available
    Edge voices, so switching engines in the UI never loses the working option.
    """
    voices = list(_edge().voices())
    if settings is not None and getattr(settings, "qwen_key", ""):
        try:
            voices.extend(_qwen(settings).voices())
        except Exception:  # noqa: BLE001 - a broken optional engine must not break the list
            pass
    return voices


def voice_engine(voice_id: str, settings=None) -> str | None:
    """Which engine owns *voice_id* (``None`` when the voice is unknown).

    The API lists Edge and Qwen voices together so the user can compare them;
    the engine must follow the chosen voice, otherwise a Qwen job would be
    created with an Edge voice id (or the other way round).
    """
    wanted = (voice_id or "").strip()
    if not wanted:
        return None
    try:
        if any(voice.id == wanted for voice in _edge().voices()):
            return "edge"
    except TTSError:
        pass
    if settings is not None and getattr(settings, "qwen_key", ""):
        try:
            if any(voice.id == wanted for voice in _qwen(settings).voices()):
                return "qwen"
        except Exception:  # noqa: BLE001 - optional engine
            pass
    return None


def voices_for_engine(engine_name: str, settings=None) -> list[Voice]:
    """Voices of one concrete engine (``auto`` is resolved first)."""
    key = resolve_engine_name(engine_name, settings)
    try:
        return list(get_engine(key, settings).voices())
    except Exception:  # noqa: BLE001 - unavailable optional engine
        return []


def engine_status(settings=None) -> list[dict]:
    """Availability report used by ``/api/voices`` and ``doctor``."""
    qwen_ready = bool(getattr(settings, "qwen_key", "")) if settings is not None else False
    return [
        {"name": "edge", "model": "edge-neural", "available": True,
         "reason": "", "selected": resolve_engine_name(None, settings) == "edge"},
        {"name": "qwen", "model": getattr(settings, "qwen_model", "qwen-tts"),
         "available": qwen_ready,
         "reason": "" if qwen_ready else "未配置 DASHSCOPE_API_KEY 或 config.json 的 qwen_api_key",
         "selected": resolve_engine_name(None, settings) == "qwen"},
        {"name": "kokoro", "model": "kokoro-multi-lang",
         "available": False, "reason": "本地离线引擎尚未安装（可选方案）", "selected": False},
    ]


__all__ = [
    "ENGINE_NAMES",
    "Engine",
    "TTSError",
    "Voice",
    "engine_status",
    "get_engine",
    "list_voices",
    "resolve_engine_name",
    "voice_engine",
    "voices_for_engine",
]
