"""Regression guards for the 2026-11-22 incident (package directory deleted).

Root cause: on Windows ``RelaxBook`` and ``relaxbook`` are the same path.  A
data directory that fell back to ``<temp>/RelaxBook`` resolved to the package
directory, and cleaning it up with a recursive delete removed the whole package.

These tests keep that class of mistake from coming back:
* the data directory must never live inside the Git work tree;
* ``.gitignore`` must not accidentally ignore the package (case-insensitive FS);
* no runtime data directory may be created at the repository root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from relaxbook.config import Settings, default_data_dir, load_settings

ROOT = Path(__file__).resolve().parent.parent


def _inside_repo(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == ROOT or ROOT in resolved.parents


def test_default_data_dir_is_outside_repo() -> None:
    assert not _inside_repo(default_data_dir()), (
        "默认数据目录不能位于 Git 工作区内；否则 `RelaxBook` 会与包名 `relaxbook` "
        "在同一路径上冲突"
    )


def test_settings_data_dir_is_outside_repo(tmp_path: Path) -> None:
    # An explicit --data-dir wins, but the default must stay outside the tree.
    settings = Settings(data_dir=str(tmp_path))
    settings.ensure_dirs()
    assert not Path(settings.data_dir) == ROOT


def test_gitignore_does_not_hide_the_package() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        ["git", "check-ignore", "-v", "relaxbook/__init__.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode != 0, f"包被 .gitignore 忽略了（大小写不敏感）：{result.stdout.strip()}"


def test_no_case_collision_data_dir_at_repo_root() -> None:
    """No directory at the repo root may resolve to the package path."""
    package = (ROOT / "relaxbook").resolve()
    for item in ROOT.iterdir():
        if not item.is_dir() or item.name == "relaxbook":
            continue
        if item.name.lower() == "relaxbook":
            pytest.fail(f"大小写冲突目录：{item}")
        if item.resolve() == package:
            pytest.fail(f"目录 {item} 解析到包目录 {package}")
