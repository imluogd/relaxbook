"""Runtime configuration and data-directory layout.

The data directory lives *outside* the Git work tree.  Default locations:

* Windows: ``%LOCALAPPDATA%\\RelaxBook``
* macOS:   ``~/Library/Application Support/RelaxBook``
* Linux:   ``$XDG_DATA_HOME/relaxbook`` or ``~/.local/share/relaxbook``

``RELAXBOOK_DATA_DIR`` overrides everything; ``load_settings`` accepts an
explicit override as well (used by the command line entry points).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"


def default_data_dir() -> Path:
    env = os.environ.get("RELAXBOOK_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "RelaxBook"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RelaxBook"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "relaxbook"


def _which(name: str) -> str:
    found = shutil.which(name)
    return found or ""


# Common Windows locations that are frequently missing from PATH.
_WINDOWS_HINTS = {
    "ffmpeg": [r"C:\ffmpeg\bin\ffmpeg.exe", r"E:\ffmpeg\bin\ffmpeg.exe", r"D:\ffmpeg\bin\ffmpeg.exe"],
    "ffprobe": [r"C:\ffmpeg\bin\ffprobe.exe", r"E:\ffmpeg\bin\ffprobe.exe", r"D:\ffmpeg\bin\ffprobe.exe"],
    "ebook-convert": [
        r"C:\Program Files\Calibre2\ebook-convert.exe",
        r"C:\Program Files (x86)\Calibre2\ebook-convert.exe",
        r"D:\Program Files\Calibre2\ebook-convert.exe",
        r"F:\calibre\ebook-convert.exe",
        r"E:\calibre\ebook-convert.exe",
        r"D:\calibre\ebook-convert.exe",
    ],
}


def locate_tool(name: str) -> str:
    """Return an absolute path for a required external tool (empty if missing)."""
    found = _which(name)
    if found:
        return found
    if sys.platform.startswith("win"):
        for hint in _WINDOWS_HINTS.get(name, []):
            if Path(hint).exists():
                return hint
    return ""


# The MP4/M4B chapter *text track* (what Apple Books / BookPlayer / Voice read)
# is only written correctly by FFmpeg 7 and newer.  Older builds emit chapter
# timings but drop the chapter titles, so a minimum version is enforced.
MIN_FFMPEG_MAJOR = 7


@lru_cache(maxsize=8)
def tool_version_line(exe: str) -> str:
    """First line of ``<tool> -version`` (empty when the tool cannot run)."""
    if not exe:
        return ""
    try:
        result = subprocess.run(
            [exe, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or result.stderr or "").splitlines()[0] if (result.stdout or result.stderr) else ""


def ffmpeg_major(exe: str) -> int:
    """Major version of an FFmpeg binary (0 when unknown)."""
    line = tool_version_line(exe)
    match = re.search(r"ffmpeg version (?:n)?(\d+)\.(\d+)", line)
    if not match:
        return 0
    # "n6.1.1" / "7.0" style; git snapshot builds report e.g. "N-110363" -> 0
    return int(match.group(1))


def bundled_ffmpeg() -> str:
    """FFmpeg shipped by the ``imageio-ffmpeg`` wheel (version pinned, no OS install)."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - optional dependency
        return ""



@dataclass
class Settings:
    """All per-machine settings.  Stored as ``<data_dir>/config.json``."""

    data_dir: str
    host: str = "127.0.0.1"
    port: int = 8765

    # TTS
    # "auto" = use Qwen-TTS when an API key is configured, otherwise Edge.
    engine: str = "auto"
    model: str = "edge-neural"
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"
    # Number of concurrent synthesis requests inside the worker.  Edge resets
    # connections more often with higher concurrency, so 3 is the default.
    concurrency: int = 3

    # Qwen-TTS (Alibaba Cloud DashScope / 百炼).  Prefer the environment
    # variable DASHSCOPE_API_KEY; a value here wins over the environment.
    qwen_api_key: str = ""
    qwen_model: str = "qwen3-tts-flash"
    qwen_base_url: str = "https://dashscope.aliyuncs.com"
    qwen_timeout: float = 60.0
    qwen_voice: str = "Cherry"

    # External tools (absolute paths; auto-detected when empty)
    ffmpeg: str = ""
    ffprobe: str = ""
    calibre_convert: str = ""

    # Encoding
    mp3_bitrate: str = "64k"
    m4b_bitrate: str = "64k"
    zip_packaging: bool = False

    # Web
    open_browser: bool = True

    @property
    def root(self) -> Path:
        return Path(self.data_dir)

    @property
    def qwen_key(self) -> str:
        """DashScope API key: config value wins, then ``DASHSCOPE_API_KEY``."""
        return (self.qwen_api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()

    @property
    def resolved_engine(self) -> str:
        """Concrete engine id for ``engine="auto"``."""
        name = (self.engine or "auto").strip().lower()
        if name != "auto":
            return name
        return "qwen" if self.qwen_key else "edge"

    @property
    def db_path(self) -> Path:
        return self.root / "relaxbook.db"

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def books_dir(self) -> Path:
        return self.root / "books"

    @property
    def segments_dir(self) -> Path:
        return self.root / "segments"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def previews_dir(self) -> Path:
        return self.root / "previews"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def ensure_dirs(self) -> None:
        for path in (
            self.root,
            self.books_dir,
            self.segments_dir,
            self.jobs_dir,
            self.previews_dir,
            self.models_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def resolve_tools(self) -> None:
        """Fill in empty tool paths from PATH / known install locations.

        ``ffmpeg`` prefers a system install that is new enough to write M4B
        chapter tracks; otherwise the version-pinned binary bundled with the
        ``imageio-ffmpeg`` wheel is used, which keeps chaptered M4B output
        reproducible on any machine.  ``ffprobe`` always comes from the system.
        """
        if not self.ffmpeg:
            system = locate_tool("ffmpeg")
            if system and ffmpeg_major(system) >= MIN_FFMPEG_MAJOR:
                self.ffmpeg = system
            else:
                self.ffmpeg = bundled_ffmpeg() or system
        if not self.ffprobe:
            self.ffprobe = locate_tool("ffprobe")
            if not self.ffprobe and self.ffmpeg:
                sibling = Path(self.ffmpeg).with_name(
                    "ffprobe.exe" if self.ffmpeg.lower().endswith(".exe") else "ffprobe"
                )
                if sibling.exists():
                    self.ffprobe = str(sibling)
        if not self.calibre_convert:
            self.calibre_convert = locate_tool("ebook-convert")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_settings(data_dir: Path | str | None = None) -> Settings:
    """Load settings, creating a default configuration on first use."""
    root = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / CONFIG_NAME
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    known = {f for f in Settings.__dataclass_fields__}
    data = {k: v for k, v in data.items() if k in known}
    data["data_dir"] = str(root)
    settings = Settings(**data)
    settings.ensure_dirs()
    settings.resolve_tools()
    if not settings.config_path.exists():
        save_settings(settings)
    return settings


def save_settings(settings: Settings) -> None:
    settings.ensure_dirs()
    settings.config_path.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
