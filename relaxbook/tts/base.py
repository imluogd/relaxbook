"""Speech synthesis engine interfaces (CONTRACTS section 10).

The worker and the web API only ever talk to the small :class:`Engine`
protocol defined here, so a new engine can be added without touching the
conversion pipeline.  All engines expose the *synchronous* ``synthesize``
method: the worker calls it from a thread pool, and every engine implementation
is responsible for bridging to its own async SDK internally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Voice:
    """One selectable voice; ``name`` is the Chinese description shown in the UI."""

    id: str
    name: str
    language: str = "zh-CN"
    gender: str = "Female"
    engine: str = "edge"


class TTSError(Exception):
    """User-visible synthesis failure; the message itself is Chinese text."""


@runtime_checkable
class Engine(Protocol):
    """Minimal engine contract used by the worker and the API layer."""

    name: str
    model: str

    def voices(self) -> list[Voice]:
        """Built-in voice catalogue; never requires network access."""

    def synthesize(self, text: str, voice: str, rate: str) -> bytes:
        """Return 24 kHz mono 48 kbps MP3 bytes for *text*."""


__all__ = ["Engine", "TTSError", "Voice"]
