"""RelaxBook 运维入口：``setup`` / ``doctor`` / ``start``。

在仓库根目录用项目虚拟环境运行::

    python -m relaxbook.cli setup
    python -m relaxbook.cli doctor
    python -m relaxbook.cli start --no-browser --data-dir DIR

``scripts/`` 下的 PowerShell / shell 文件只是本入口的薄包装。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .config import (
    MIN_FFMPEG_MAJOR,
    bundled_ffmpeg,
    default_data_dir,
    ffmpeg_major,
    load_settings,
    locate_tool,
    save_settings,
)

ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 11)
MIN_FREE_BYTES = 1024 ** 3
REQUIRED_IMPORTS = (
    "fastapi",
    "uvicorn",
    "jinja2",
    "lxml",
    "pypdf",
    "pdfplumber",
    "edge_tts",
    "curl_cffi",
    "imageio_ffmpeg",
)
# 本机（以及中国大陆网络）实测参数：时钟超前导致 HTTPS 证书校验失败，必须走 http 镜像；
# pip 默认缓存会挂起，必须 --no-cache-dir。
PIP_MIRROR_ARGS = (
    "--no-cache-dir",
    "-i",
    "http://mirrors.ustc.edu.cn/pypi/simple/",
    "--trusted-host",
    "mirrors.ustc.edu.cn",
    "--timeout",
    "20",
    "--disable-pip-version-check",
)
OK = "通过"
WARN = "警告"
BLOCK = "阻塞"


@dataclass
class Check:
    """doctor 表格中的一行。"""

    item: str
    status: str
    detail: str = ""
    fix: str = ""


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def venv_dir() -> Path:
    return ROOT / ".venv"


def venv_python() -> Path:
    if os.name == "nt":
        return venv_dir() / "Scripts" / "python.exe"
    return venv_dir() / "bin" / "python"


def project_python() -> Path:
    """优先返回项目虚拟环境中的解释器；没有 venv 时退回当前解释器。"""
    python = venv_python()
    return python if python.exists() else Path(sys.executable)


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """返回去掉了本机 PYTHONPATH 污染的环境变量。"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def _data_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    return default_data_dir()


def _display_width(text: str) -> int:
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if _display_width(text) <= limit:
        return text
    out: list[str] = []
    width = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if width + step > max(limit - 3, 1):
            break
        out.append(char)
        width += step
    return "".join(out) + "..."


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _display_width(text))


def _parse_json_line(raw: bytes | str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _tail(path: Path, lines: int = 12) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]


def _write_probe(root: Path) -> tuple[bool, str]:
    """在数据目录写一个临时文件，确认可写。"""
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".relaxbook-write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _command_available(command: Sequence[str]) -> bool:
    head = str(command[0])
    if Path(head).exists():
        return True
    return shutil.which(head) is not None


def _python_version_of(command: Sequence[str]) -> tuple[int, int] | None:
    try:
        proc = subprocess.run(
            [*command, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=60,
            env=clean_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        major, minor = proc.stdout.strip().split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return None


def find_base_python() -> list[str] | None:
    """找一个 >=3.11 的解释器用来创建 venv。返回可执行命令行（list）。"""
    candidates: list[list[str]] = []
    if sys.version_info[:2] >= (3, 12):
        candidates.append([sys.executable])
    if os.name == "nt":
        candidates.extend([["py", "-3.12"], ["py", "-3.11"]])
    candidates.extend([["python3.12"], ["python3.11"], ["python3"], ["python"], [sys.executable]])
    for command in candidates:
        if not _command_available(command):
            continue
        version = _python_version_of(command)
        if version is not None and version >= MIN_PYTHON:
            return command
    return None


# ---------------------------------------------------------------------------
# 探测子进程代码（避免依赖当前解释器）
# ---------------------------------------------------------------------------
_INTERPRETER_PROBE = r"""
import json, sys
mods = __MODULES__
missing = []
for name in mods:
    try:
        __import__(name)
    except BaseException:
        missing.append(name)
print(json.dumps({
    "version": "%d.%d.%d" % sys.version_info[:3],
    "executable": sys.executable,
    "in_venv": sys.prefix != sys.base_prefix,
    "missing": missing,
}, ensure_ascii=False))
"""

_REQUIREMENTS_PROBE = r"""
import json, sys
from importlib import metadata
bad = []
total = 0
with open(sys.argv[1], encoding="utf-8-sig") as handle:
    for raw in handle:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        total += 1
        if "==" not in line:
            continue
        name, _, want = line.partition("==")
        name, want = name.strip(), want.strip()
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            bad.append(name + " (missing)")
        except Exception:
            bad.append(name + " (unknown)")
        else:
            if have != want:
                bad.append(name + " " + have + "!=" + want)
print(json.dumps({"total": total, "bad": bad}, ensure_ascii=False))
"""

_TTS_PROBE = r"""
import json, sys, time
out = {"pending": [], "voices": [], "synth": None, "voices_error": None, "fatal": None}
try:
    from relaxbook.config import load_settings
    from relaxbook import tts
    settings = load_settings(sys.argv[1])
except BaseException as exc:
    out["fatal"] = "%s: %s" % (type(exc).__name__, exc)
else:
    try:
        voices = tts.list_voices(settings)
    except NotImplementedError:
        out["pending"].append("list_voices")
    except BaseException as exc:
        out["voices_error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        out["voices"] = [
            {
                "id": getattr(voice, "id", ""),
                "name": getattr(voice, "name", ""),
                "language": getattr(voice, "language", ""),
                "gender": getattr(voice, "gender", ""),
                "engine": getattr(voice, "engine", ""),
            }
            for voice in (voices or [])
        ]
    try:
        engine = tts.get_engine(settings.engine, settings)
    except NotImplementedError:
        out["pending"].append("get_engine")
    except BaseException as exc:
        out["synth"] = {
            "ok": False,
            "error": "get_engine: %s: %s" % (type(exc).__name__, exc),
            "voice": getattr(settings, "voice", ""),
        }
    else:
        started = time.perf_counter()
        try:
            audio = engine.synthesize("测试", settings.voice, settings.rate)
        except NotImplementedError:
            out["pending"].append("synthesize")
        except BaseException as exc:
            out["synth"] = {
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "ms": int((time.perf_counter() - started) * 1000),
                "voice": settings.voice,
            }
        else:
            out["synth"] = {
                "ok": bool(audio),
                "bytes": len(audio or b""),
                "ms": int((time.perf_counter() - started) * 1000),
                "voice": settings.voice,
            }
print(json.dumps(out, ensure_ascii=False))
"""


def probe_interpreter(python: Path) -> dict[str, Any] | None:
    code = _INTERPRETER_PROBE.replace("__MODULES__", repr(list(REQUIRED_IMPORTS)))
    try:
        proc = subprocess.run(
            [str(python), "-c", code],
            cwd=str(ROOT),
            env=clean_env(),
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_json_line(proc.stdout)


def probe_requirements(python: Path, requirements: Path) -> list[str] | None:
    try:
        proc = subprocess.run(
            [str(python), "-c", _REQUIREMENTS_PROBE, str(requirements)],
            cwd=str(ROOT),
            env=clean_env(),
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = _parse_json_line(proc.stdout)
    if data is None:
        return None
    return list(data.get("bad") or [])


def probe_tts(settings, python: Path) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            [str(python), "-c", _TTS_PROBE, settings.data_dir],
            cwd=str(ROOT),
            env=clean_env({"RELAXBOOK_DATA_DIR": settings.data_dir}),
            capture_output=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"fatal": "试合成超时（超过 180 秒）"}
    except OSError as exc:
        return {"fatal": f"{type(exc).__name__}: {exc}"}
    data = _parse_json_line(proc.stdout)
    if data is not None:
        return data
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
    suffix = ("：" + " / ".join(stderr)) if stderr else ""
    return {"fatal": f"TTS 探测无输出（退出码 {proc.returncode}）{suffix}"}


# ---------------------------------------------------------------------------
# 修复命令
# ---------------------------------------------------------------------------
def _pip_fix_command() -> str:
    if os.name == "nt":
        head = r".\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    else:
        head = ".venv/bin/python -m pip install -r requirements.txt"
    return head + " " + " ".join(PIP_MIRROR_ARGS)


def _ffmpeg_fix(settings) -> str:
    return (
        "运行 scripts/setup.ps1（或 scripts/setup.sh）重新安装依赖：setup 会安装 imageio-ffmpeg，"
        "它自带的 FFmpeg 9.0.2（>=7）可写 M4B 内嵌章节标题，不需要手动安装 FFmpeg；"
        f'若在 {settings.config_path} 手动指定了过旧的 "ffmpeg" 路径，请删除该字段后重跑 setup'
    )


def _ffprobe_fix(settings) -> str:
    if os.name == "nt":
        return (
            "安装系统 FFmpeg 以获得 ffprobe：winget install --id Gyan.FFmpeg -e，"
            f'或在 {settings.config_path} 里把 "ffprobe" 填为 ffprobe.exe 的绝对路径'
        )
    if sys.platform == "darwin":
        return "brew install ffmpeg（提供 ffprobe）"
    return "sudo apt install ffmpeg（提供 ffprobe；其它发行版用对应包管理器）"


def _calibre_fix(settings) -> str:
    if os.name == "nt":
        return (
            "winget install --id calibre.calibre -e，"
            f'或在 {settings.config_path} 里把 "calibre_convert" 填为 ebook-convert.exe 的绝对路径'
        )
    if sys.platform == "darwin":
        return "brew install --cask calibre"
    return "sudo apt install calibre"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def _tool_version(path: str, flag: str = "-version") -> tuple[bool, str]:
    if not path:
        return False, "未找到"
    if not Path(path).exists():
        return False, f"路径不存在：{path}"
    try:
        proc = subprocess.run(
            [path, flag], capture_output=True, timeout=30, env=clean_env()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"无法执行：{type(exc).__name__}: {exc}"
    raw = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    lines = [line.strip() for line in raw.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return False, f"没有输出（退出码 {proc.returncode}）"
    return True, lines[0][:120]


def _tts_checks(settings, info: dict[str, Any] | None) -> list[Check]:
    checks: list[Check] = []
    if info is None:
        checks.append(Check("Edge TTS 可用性", WARN, "无法执行 TTS 探测", "确认已在 .venv 内运行 doctor"))
        checks.append(Check("音色数量", WARN, "无法读取音色清单", "确认已在 .venv 内运行 doctor"))
        return checks
    fatal = info.get("fatal")
    pending = [str(item) for item in (info.get("pending") or [])]
    synth = info.get("synth")
    if fatal:
        checks.append(
            Check(
                "Edge TTS 可用性",
                WARN,
                f"无法探测：{fatal}",
                "若 TTS 模块尚未实现，集成后重跑 doctor；否则确认在 .venv 内运行且依赖齐全",
            )
        )
    elif isinstance(synth, dict) and synth.get("ok"):
        checks.append(
            Check(
                "Edge TTS 可用性",
                OK,
                f"试合成「测试」{synth.get('ms', '?')} ms，返回 {synth.get('bytes', 0)} 字节"
                f"（voice={synth.get('voice', '')}）",
            )
        )
    elif isinstance(synth, dict):
        error = synth.get("error") or "返回空音频"
        checks.append(
            Check(
                "Edge TTS 可用性",
                BLOCK,
                f"试合成失败：{error}",
                "Edge 为在线合成，需联网。检查网络后重跑 doctor；"
                "仍失败时用 setup 的 pip 镜像命令重装 edge-tts 与 curl_cffi",
            )
        )
    elif pending:
        checks.append(
            Check(
                "Edge TTS 可用性",
                WARN,
                "待实现：" + "、".join(pending),
                "TTS 模块由并行成员实现，集成后重跑 doctor 复测",
            )
        )
    else:
        checks.append(Check("Edge TTS 可用性", WARN, "未返回合成结果", "重跑 doctor 复测"))

    voices = info.get("voices") or []
    if not voices:
        if fatal:
            detail = f"无法读取音色清单（{fatal}）"
        elif pending:
            detail = "待实现：" + "、".join(pending)
        elif info.get("voices_error"):
            detail = f"读取失败：{info.get('voices_error')}"
        else:
            detail = "音色清单为空"
        checks.append(
            Check("音色数量", WARN, detail, "TTS 模块实现后重跑 doctor；正常应有至少 3 个中文音色")
        )
    else:
        zh = [voice for voice in voices if str(voice.get("language", "")).lower().startswith("zh")]
        en = [voice for voice in voices if str(voice.get("language", "")).lower().startswith("en")]
        ok = len(zh) >= 3 and len(en) >= 1
        checks.append(
            Check(
                "音色数量",
                OK if ok else WARN,
                f"中文 {len(zh)} 个 / 英文 {len(en)} 个（要求中文 >=3、英文 >=1）",
                "" if ok else "检查 relaxbook/tts 的内置音色清单是否完整",
            )
        )
    return checks


def run_doctor(
    settings,
    *,
    python: Path | None = None,
    skip_tts: bool = False,
) -> list[Check]:
    """逐项检查运行环境，返回检查结果列表。"""
    checks: list[Check] = []
    python = python or project_python()
    info = probe_interpreter(python)

    # Python 版本
    if info is None:
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        checks.append(
            Check(
                "Python 版本",
                BLOCK,
                f"无法运行解释器 {python}",
                "重新运行 scripts/setup.ps1（或 scripts/setup.sh）创建虚拟环境",
            )
        )
        version_ok = False
    else:
        version = str(info.get("version", "?"))
        try:
            parts = tuple(int(part) for part in version.split(".")[:2])
        except ValueError:
            parts = (0, 0)
        version_ok = parts >= MIN_PYTHON
        checks.append(
            Check(
                "Python 版本",
                OK if version_ok else BLOCK,
                f"{version}（{python}）",
                "" if version_ok else "安装 Python 3.12，删除 .venv 后重跑 setup",
            )
        )

    # 虚拟环境
    expected_venv = venv_python()
    if expected_venv.exists():
        detail = str(expected_venv)
        if info is not None and not info.get("in_venv"):
            detail += "（探测解释器不在 venv 内）"
        checks.append(Check("虚拟环境 .venv", OK, detail))
    else:
        checks.append(
            Check(
                "虚拟环境 .venv",
                BLOCK,
                f"未找到 {expected_venv}",
                "python -m venv .venv，然后运行 scripts/setup.ps1（或 scripts/setup.sh）",
            )
        )

    # 依赖
    missing = list((info or {}).get("missing") or [])
    if info is None:
        checks.append(Check("依赖包", BLOCK, "无法检查依赖", _pip_fix_command()))
    elif missing:
        checks.append(
            Check("依赖包", BLOCK, "缺少/无法导入：" + "、".join(missing), _pip_fix_command())
        )
    else:
        checks.append(
            Check(
                "依赖包",
                OK,
                "fastapi/uvicorn/jinja2/lxml/pypdf/pdfplumber/edge_tts/curl_cffi/imageio_ffmpeg 均可导入",
            )
        )

    # ffmpeg：M4B 内嵌章节标题需要 FFmpeg 7+；setup 安装的 imageio-ffmpeg 自带 9.0.2
    ffmpeg_path = settings.ffmpeg or locate_tool("ffmpeg") or bundled_ffmpeg()
    if not ffmpeg_path:
        checks.append(Check("ffmpeg", BLOCK, "未找到可用的 ffmpeg", _ffmpeg_fix(settings)))
    else:
        usable, version_line = _tool_version(ffmpeg_path, "-version")
        if not usable:
            checks.append(Check("ffmpeg", BLOCK, version_line, _ffmpeg_fix(settings)))
        else:
            major = ffmpeg_major(ffmpeg_path)
            source = "（imageio-ffmpeg 自带）" if ffmpeg_path == bundled_ffmpeg() else ""
            if major >= MIN_FFMPEG_MAJOR:
                checks.append(Check("ffmpeg", OK, f"{version_line}{source}"))
            elif major > 0:
                checks.append(
                    Check(
                        "ffmpeg",
                        BLOCK,
                        f"FFmpeg {major} 过旧，M4B 内嵌章节标题需要 {MIN_FFMPEG_MAJOR}+：{version_line}",
                        _ffmpeg_fix(settings),
                    )
                )
            else:
                checks.append(
                    Check(
                        "ffmpeg",
                        WARN,
                        f"无法识别版本，M4B 章节标题需要 {MIN_FFMPEG_MAJOR}+：{version_line}",
                        _ffmpeg_fix(settings),
                    )
                )

    # ffprobe：系统提供（imageio-ffmpeg 只带 ffmpeg，不含 ffprobe），用于校验成品
    ffprobe_path = settings.ffprobe or locate_tool("ffprobe")
    if not ffprobe_path:
        checks.append(
            Check(
                "ffprobe",
                BLOCK,
                "未找到系统 ffprobe（用于校验成品）",
                _ffprobe_fix(settings),
            )
        )
    else:
        usable, version_line = _tool_version(ffprobe_path, "-version")
        if usable:
            checks.append(Check("ffprobe", OK, version_line))
        else:
            checks.append(Check("ffprobe", BLOCK, version_line, _ffprobe_fix(settings)))

    # calibre（可选；缺失只影响 MOBI/AZW3）
    calibre = settings.calibre_convert or locate_tool("ebook-convert")
    if calibre:
        usable, detail = _tool_version(calibre, "--version")
    else:
        usable, detail = False, "未找到"
    if usable:
        checks.append(Check("calibre（可选）", OK, detail))
    else:
        checks.append(
            Check(
                "calibre（可选）",
                WARN,
                "未找到 ebook-convert：MOBI/AZW3 不可用，EPUB/PDF 仍可用",
                _calibre_fix(settings),
            )
        )

    # 数据目录可写 + 剩余空间
    writable, error = _write_probe(settings.root)
    if writable:
        checks.append(Check("数据目录可写", OK, str(settings.root)))
        try:
            free = shutil.disk_usage(settings.root).free
        except OSError:
            free = 0
        free_gb = free / float(1024 ** 3)
        if free >= MIN_FREE_BYTES:
            checks.append(Check("剩余空间", OK, f"{free_gb:.1f} GB 可用（要求 >=1 GB）"))
        else:
            checks.append(
                Check(
                    "剩余空间",
                    WARN,
                    f"仅 {free_gb:.2f} GB 可用（建议 >=1 GB）",
                    "清理磁盘，或设置 RELAXBOOK_DATA_DIR 指向空间充足的目录",
                )
            )
    else:
        checks.append(
            Check(
                "数据目录可写",
                BLOCK,
                f"{settings.root} 不可写：{error}",
                "设置 RELAXBOOK_DATA_DIR 或 --data-dir 指向可写目录后重跑 doctor",
            )
        )
        checks.append(Check("剩余空间", WARN, "数据目录不可写，无法检查剩余空间"))

    # TTS 引擎与音色
    if skip_tts:
        checks.append(Check("Edge TTS 可用性", WARN, "已跳过（--skip-tts）"))
        checks.append(Check("音色数量", WARN, "已跳过（--skip-tts）"))
    else:
        checks.extend(_tts_checks(settings, probe_tts(settings, python)))

    return checks


def print_environment(settings, python: Path) -> None:
    print(f"系统：{platform.platform()}")
    print(f"解释器：{python}")
    print(f"数据目录：{settings.root}")
    print(f"配置文件：{settings.config_path}")
    print(f"引擎：{settings.engine} / 音色：{settings.voice} / 并行度：{settings.concurrency}")


def print_checks(checks: Sequence[Check], *, title: str = "RelaxBook 环境检查") -> None:
    if not checks:
        return
    print()
    print(f"===== {title} =====")
    item_width = max(_display_width("检查项"), max(_display_width(check.item) for check in checks))
    detail_limit = 72
    total_width = item_width + 4 + 2 + detail_limit
    status_width = 4
    print("  " + _pad("检查项", item_width) + "  " + _pad("状态", status_width) + "  说明")
    print("  " + "-" * total_width)
    for check in checks:
        print(
            "  "
            + _pad(check.item, item_width)
            + "  "
            + _pad(check.status, status_width)
            + "  "
            + _clip(check.detail, detail_limit)
        )
    fixes = [check for check in checks if check.status != OK and check.fix]
    if fixes:
        print()
        print("修复建议（可直接复制执行）：")
        for check in fixes:
            print(f"- [{check.item}] {check.fix}")


def summarize_checks(checks: Sequence[Check]) -> int:
    passed = sum(1 for check in checks if check.status == OK)
    warned = sum(1 for check in checks if check.status == WARN)
    blocked = sum(1 for check in checks if check.status == BLOCK)
    print()
    if blocked:
        print(f"结果：通过 {passed} 项，警告 {warned} 项，阻塞 {blocked} 项。")
        print("存在阻塞项，请按修复建议处理后重跑 doctor。")
    elif warned:
        print(f"结果：通过 {passed} 项，警告 {warned} 项，阻塞 0 项。")
        print("可以启动，警告项对应功能暂不可用（见上表）。")
    else:
        print(f"结果：全部 {passed} 项通过。")
    return blocked


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
def command_setup(args) -> int:
    data_dir = _data_dir(args.data_dir)
    print(f"===== RelaxBook setup =====")
    print(f"仓库目录：{ROOT}")
    python = venv_python()
    if python.exists():
        print(f"复用已有虚拟环境：{venv_dir()}（{python}）")
    else:
        base = find_base_python()
        if base is None:
            print("阻塞：找不到 Python 3.11 及以上版本的候选解释器。")
            print("修复：安装 Python 3.12（https://www.python.org/downloads/），然后重跑 scripts/setup.ps1。")
            return 1
        print(f"创建虚拟环境：{venv_dir()}（使用 {base[0]}）")
        proc = subprocess.run(
            [*base, "-m", "venv", str(venv_dir())], cwd=str(ROOT), env=clean_env()
        )
        if proc.returncode != 0 or not python.exists():
            print("阻塞：创建虚拟环境失败。")
            print(f"修复：删除 {venv_dir()} 后重试，或手动运行 python -m venv .venv。")
            return 1
        print(f"虚拟环境已创建：{python}")

    requirements = ROOT / "requirements.txt"
    if not requirements.exists():
        print(f"阻塞：未找到依赖清单 {requirements}。")
        return 1
    mismatched = probe_requirements(python, requirements)
    if mismatched is None:
        print("警告：无法读取依赖安装状态，改为直接执行 pip install。")
        mismatched = ["(unchecked)"]
    if mismatched:
        print(f"安装依赖：{len(mismatched)} 项缺失或版本不符，开始 pip install ...")
        command = [str(python), "-m", "pip", "install", "-r", str(requirements), *PIP_MIRROR_ARGS]
        print("命令：" + " ".join(command))
        proc = subprocess.run(command, cwd=str(ROOT), env=clean_env())
        if proc.returncode != 0:
            print("阻塞：依赖安装失败。")
            print("修复：确认网络可用后重试；本机 HTTPS 证书校验失败时必须用上面的 http 镜像参数（脚本已内置）。")
            return 1
    else:
        print("依赖已满足（requirements.txt 中的版本全部一致），跳过 pip install。")

    try:
        settings = load_settings(data_dir)
    except OSError as exc:
        print(f"阻塞：无法创建/读取数据目录 {data_dir}：{type(exc).__name__}: {exc}")
        print("修复：设置 RELAXBOOK_DATA_DIR 或 --data-dir 指向可写目录后重跑 setup。")
        return 1
    settings.resolve_tools()
    try:
        save_settings(settings)
    except OSError as exc:
        print(f"阻塞：写入配置文件失败：{type(exc).__name__}: {exc}")
        return 1
    print(f"数据目录：{settings.root}（位于 Git 工作区之外，书籍与音频只保存在本机）")
    print(f"配置文件：{settings.config_path}")
    print(f"   ffmpeg        = {settings.ffmpeg or '(未找到)'}")
    print(f"   ffprobe       = {settings.ffprobe or '(未找到)'}")
    print(f"   calibre_convert = {settings.calibre_convert or '(未找到，MOBI/AZW3 不可用)'}")
    print(
        "提示：ffmpeg 使用 imageio-ffmpeg 自带的 FFmpeg 9.0.2（>=7，可写 M4B 内嵌章节标题），"
        "无需手动安装；ffprobe 需要系统提供（用于校验成品）。"
    )

    checks = run_doctor(settings, python=python, skip_tts=args.skip_tts)
    print_checks(checks, title="setup 关键检查")
    blocked = summarize_checks(checks)

    print()
    if blocked:
        print("下一步：先按上面的修复建议处理阻塞项，然后运行 scripts/doctor.ps1（或 scripts/doctor.sh）复查。")
    else:
        print("下一步：运行 scripts/start.ps1（或 scripts/start.sh）启动网页；")
        print("       浏览器会打开 http://127.0.0.1:<port>/，端口默认取 config.json 的 port。")
    print("提示：数据目录可用环境变量 RELAXBOOK_DATA_DIR 或 --data-dir 覆盖。")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def command_doctor(args) -> int:
    data_dir = _data_dir(args.data_dir)
    try:
        settings = load_settings(data_dir)
    except OSError as exc:
        print(f"阻塞：无法创建/读取数据目录 {data_dir}：{type(exc).__name__}: {exc}")
        print("修复：设置 RELAXBOOK_DATA_DIR 或 --data-dir 指向可写目录后重跑 doctor。")
        return 1
    python = project_python()
    print_environment(settings, python)
    checks = run_doctor(settings, python=python, skip_tts=args.skip_tts)
    print_checks(checks)
    blocked = summarize_checks(checks)
    return 1 if blocked else 0


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
def find_free_port(host: str, start: int, attempts: int = 21) -> int | None:
    for port in range(start, start + attempts):
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
        return port
    return None


def _stop_worker(worker: subprocess.Popen | None, log_file) -> None:
    if worker is not None and worker.poll() is None:
        print("正在停止工作进程 ...")
        try:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        except OSError as exc:
            print(f"警告：停止工作进程时出错：{type(exc).__name__}: {exc}")
        else:
            print("工作进程已停止。")
    if log_file is not None:
        try:
            log_file.close()
        except OSError:
            pass


def command_start(args) -> int:
    data_dir = _data_dir(args.data_dir)
    try:
        settings = load_settings(data_dir)
    except OSError as exc:
        print(f"阻塞：无法创建/读取数据目录 {data_dir}：{type(exc).__name__}: {exc}")
        print("修复：设置 RELAXBOOK_DATA_DIR 或 --data-dir 指向可写目录后重试。")
        return 1
    host = args.host or settings.host
    base_port = args.port or settings.port

    writable, error = _write_probe(settings.root)
    if not writable:
        print(f"阻塞：数据目录不可写：{settings.root}（{error}）")
        print("修复：设置 RELAXBOOK_DATA_DIR 或 --data-dir 指向可写目录后重试。")
        return 1

    actual_port = find_free_port(host, base_port)
    if actual_port is None:
        print(f"阻塞：端口 {base_port}-{base_port + 20} 均被占用，无法启动网页。")
        print("修复：用 --port 指定其它端口，或修改 config.json 的 port。")
        return 1
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{display_host}:{actual_port}/"
    worker_command = [sys.executable, "-m", "relaxbook.worker", "--data-dir", settings.data_dir]
    worker_log = settings.logs_dir / "worker.out"

    print(f"===== RelaxBook start =====")
    print(f"数据目录：{settings.root}")
    print(f"网页地址：{url}")
    if actual_port != base_port:
        print(f"（端口 {base_port} 已被占用，已自动改用 {actual_port}）")

    if args.dry_run:
        print("[dry-run] 仅检查参数与端口选择，不启动任何进程。")
        print("工作进程：" + " ".join(worker_command))
        print(f"工作日志：{worker_log}")
        print(f"网页服务：uvicorn relaxbook.main:app --host {host} --port {actual_port}")
        return 0

    project = project_python()
    try:
        in_project = Path(sys.executable).resolve() == project.resolve()
    except OSError:
        in_project = False
    if not in_project and project.exists():
        print(f"阻塞：start 需要项目虚拟环境中的解释器（当前：{sys.executable}）。")
        print(f"修复：使用 {project} -m relaxbook.cli start，或运行 scripts/start.ps1 / scripts/start.sh。")
        return 1

    os.environ["RELAXBOOK_DATA_DIR"] = settings.data_dir

    # 先确认网页入口可导入，避免起了工作进程才发现网页端没实现。
    try:
        module = importlib.import_module("relaxbook.main")
        app = getattr(module, "app", None)
        if app is None:
            raise RuntimeError("模块 relaxbook.main 中没有 app 属性，uvicorn 无法加载 relaxbook.main:app")
    except Exception as exc:
        print("阻塞：网页入口 relaxbook.main:app 不可用，未启动任何进程。")
        print(f"真实错误：{type(exc).__name__}: {exc}")
        print("说明：relaxbook.main 由并行成员实现；实现后重跑 start 即可。")
        return 1

    worker: subprocess.Popen | None = None
    log_file = None
    exit_code = 0
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(worker_log, "ab")
        log_file.write(
            f"\n=== {datetime.now().isoformat(timespec='seconds')} start ===\n".encode("utf-8")
        )
        log_file.flush()
        worker_env = clean_env(
            {"RELAXBOOK_DATA_DIR": settings.data_dir, "PYTHONUNBUFFERED": "1"}
        )
        worker = subprocess.Popen(
            worker_command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=worker_env,
        )
        print(f"工作进程：PID {worker.pid}，日志 {worker_log}")
        time.sleep(1.2)
        early_exit = worker.poll()
        if early_exit is not None:
            print(f"警告：工作进程启动后立即退出（退出码 {early_exit}），本次仅运行网页。最近日志：")
            for line in _tail(worker_log, 12):
                print("    " + line)
        else:
            print("工作进程：运行中。")

        if not args.no_browser and settings.open_browser:
            timer = threading.Timer(2.0, webbrowser.open, args=[url])
            timer.daemon = True
            timer.start()
        print("按 Ctrl+C 停止（会同时结束后台工作进程）。")

        try:
            import uvicorn

            uvicorn.run("relaxbook.main:app", host=host, port=actual_port, log_level="info")
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            print(f"网页服务异常退出：{type(exc).__name__}: {exc}")
            exit_code = 1
    finally:
        _stop_worker(worker, log_file)
    return exit_code


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m relaxbook.cli",
        description="RelaxBook 运维入口：setup 安装、doctor 检查、start 启动。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s (RelaxBook 0.1.0)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        default=None,
        metavar="DIR",
        help="数据目录（默认取 RELAXBOOK_DATA_DIR，未设置时用系统默认位置）",
    )

    setup = subparsers.add_parser(
        "setup", parents=[common], help="创建虚拟环境、安装依赖、写默认配置并做关键检查"
    )
    setup.add_argument("--skip-tts", action="store_true", help="跳过 Edge TTS 试合成")

    doctor = subparsers.add_parser("doctor", parents=[common], help="逐项检查运行环境")
    doctor.add_argument("--skip-tts", action="store_true", help="跳过 Edge TTS 试合成（离线时使用）")

    start = subparsers.add_parser(
        "start", parents=[common], help="启动工作进程与本机网页服务"
    )
    start.add_argument("--host", default=None, help="监听地址（默认取 config.json 的 host）")
    start.add_argument(
        "--port",
        type=int,
        default=None,
        help="起始端口（默认取 config.json 的 port，被占用时依次尝试后续端口）",
    )
    start.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    start.add_argument("--dry-run", action="store_true", help="只检查参数与端口选择，不启动进程")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # 输出被重定向到文件/管道时保持逐行刷新，避免启动信息滞留在缓冲区。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            return command_setup(args)
        if args.command == "doctor":
            return command_doctor(args)
        if args.command == "start":
            return command_start(args)
    except KeyboardInterrupt:
        print()
        print("已取消。")
        return 130
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
