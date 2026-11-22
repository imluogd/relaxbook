"""Portable file names for audio artifacts (CONTRACTS section 7).

The rules are intentionally deterministic: the same chapter always maps to the
same file name so a retried job overwrites its own previous output instead of
creating duplicates.  Chinese characters are preserved; Windows-reserved
characters are folded to their full-width lookalikes, control characters are
dropped, and trailing spaces / dots are removed (both are illegal on Windows).
"""

from __future__ import annotations

from pathlib import Path

#: Windows-illegal characters mapped to visually equivalent full-width forms.
_ILLEGAL = {
    "\\": "＼",
    "/": "／",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "｜",
}

#: Device names that are reserved on Windows, even with an extension.
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_DEFAULT_FALLBACK = "未命名章节"


def _clean(raw: str | None, *, max_len: int) -> str:
    """Sanitize one string; returns ``""`` when nothing usable is left."""
    text = str(raw or "")
    limit = max(1, int(max_len))
    chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:  # NUL..US and DEL are not portable
            continue
        chars.append(_ILLEGAL.get(ch, ch))
    cleaned = "".join(chars).strip()
    # Windows strips trailing spaces and dots from names; do the same so that
    # both platforms produce the same, openable file name.
    cleaned = cleaned.rstrip(" .")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(" .")
    return cleaned


def sanitize_title(title: str, *, fallback: str = _DEFAULT_FALLBACK, max_len: int = 80) -> str:
    """Return a file-name safe version of *title*.

    Illegal characters are replaced (never silently dropped mid-title so the
    name stays recognizable), control characters are removed, trailing spaces
    and full stops are stripped, the result is truncated to *max_len* and an
    empty result falls back to *fallback*.
    """
    cleaned = _clean(title, max_len=max_len)
    if not cleaned:
        cleaned = _clean(fallback, max_len=max_len) or _DEFAULT_FALLBACK
    if cleaned.upper() in _RESERVED_NAMES:
        cleaned += "_"
    return cleaned


def seq_width(total: int) -> int:
    """Zero-padding width for chapter numbers: ``max(3, len(str(total)))``."""
    return max(3, len(str(abs(int(total)))))


def mp3_filename(index: int, title: str, total: int) -> str:
    """``"001 - 序言.mp3"`` — unique per chapter because *index* is included."""
    width = seq_width(total)
    return f"{int(index):0{width}d} - {sanitize_title(title)}.mp3"


def m4b_filename(book_title: str, voice: str, version: int) -> str:
    """``"示例书 - 晓晓 - v01.m4b"``."""
    book = sanitize_title(book_title, fallback="未命名书籍")
    voice_name = sanitize_title(voice, fallback="未知音色")
    return f"{book} - {voice_name} - v{int(version):02d}.m4b"


def allocate_version_dir(parent: Path) -> tuple[int, Path]:
    """Create and return the lowest unused ``vNN`` directory under *parent*.

    ``mkdir`` without ``exist_ok`` is used as the atomic probe, so two callers
    racing for the same parent can never end up sharing a version directory.
    """
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    version = 1
    while True:
        candidate = parent / f"v{version:02d}"
        try:
            candidate.mkdir()
        except FileExistsError:
            version += 1
            continue
        return version, candidate
