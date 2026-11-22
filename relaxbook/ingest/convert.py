"""MOBI / AZW3 → 工作副本 EPUB 的格式转换（依赖 calibre 的 ``ebook-convert``）。

契约见 ``docs/CONTRACTS.md`` 第 5 节。转换结果放在调用方提供的临时工作目录里，
原文件始终保留；找不到 calibre 或转换失败时抛中文 ``IngestError``。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from relaxbook.config import locate_tool

from . import IngestError

MISSING_CALIBRE_MESSAGE = (
    "未找到 calibre（ebook-convert），无法转换 MOBI/AZW3。"
    "请安装 calibre 或在 config.json 配置 calibre_convert。"
)

CONVERT_TIMEOUT_SECONDS = 600


def to_epub(path: Path, settings, work_dir: Path) -> Path:
    """用 calibre 把 MOBI/AZW3 转成 EPUB 工作副本，返回副本路径。"""
    source = Path(path)
    if not source.exists():
        raise IngestError(f"待转换文件不存在：{source.name}")

    executable = _find_convert(settings)
    if not executable:
        raise IngestError(MISSING_CALIBRE_MESSAGE)

    target_dir = Path(work_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IngestError(f"无法创建工作目录：{target_dir}（{exc}）") from exc

    destination = target_dir / f"{_safe_stem(source)}.epub"
    command = [
        executable,
        str(source),
        str(destination),
        "--no-images=false",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=CONVERT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestError(
            f"calibre 转换超时（超过 {CONVERT_TIMEOUT_SECONDS} 秒）：{source.name}。"
            "请检查源文件是否受保护或损坏。"
        ) from exc
    except OSError as exc:
        raise IngestError(f"无法启动 calibre（ebook-convert）：{exc}") from exc

    if completed.returncode != 0 or not destination.exists():
        detail = _tail_text(completed.stderr) or _tail_text(completed.stdout)
        if detail:
            raise IngestError(
                f"calibre 转换失败（返回码 {completed.returncode}）：{detail}"
            )
        raise IngestError(
            f"calibre 转换失败（返回码 {completed.returncode}），未生成 EPUB 工作副本。"
        )

    if destination.stat().st_size <= 0:
        raise IngestError("calibre 转换失败：生成的 EPUB 工作副本为空。")
    return destination


def _find_convert(settings) -> str:
    configured = (getattr(settings, "calibre_convert", "") or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(configured)
        if found:
            return found
    found = locate_tool("ebook-convert")
    if found and Path(found).is_file():
        return found
    return ""


def _tail_text(data: bytes | str | None, limit: int = 500) -> str:
    if not data:
        return ""
    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    else:
        text = str(data)
    text = text.strip()
    return text[-limit:].strip()


def _safe_stem(source: Path) -> str:
    stem = source.stem or "book"
    cleaned = "".join(
        "_" if character in '\\/:*?"<>|\r\n\t' else character for character in stem
    )
    return cleaned.strip() or "book"
