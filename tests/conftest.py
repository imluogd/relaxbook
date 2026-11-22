"""Shared pytest fixtures for the RelaxBook test suite (lead-owned)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from relaxbook import db
from relaxbook.config import Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample"


def _is_outside_repo(path: Path) -> bool:
    repo = ROOT.resolve()
    resolved = path.resolve()
    return resolved != repo and repo not in resolved.parents


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def safe_temp_root() -> Path:
    """A temp directory that is guaranteed to live *outside* the repository.

    Some sandboxes (and the DSH one this project is developed in) point TMP at
    the checkout itself.  Creating test directories there once deleted the
    ``relaxbook/`` package because ``RelaxBook`` and ``relaxbook`` are the same
    path on case-insensitive file systems.  Never let that happen again.
    """
    candidates = [
        Path(tempfile.gettempdir()),
        Path.home() / ".cache" / "relaxbook-tests",
        ROOT.resolve().parent / "relaxbook-test-tmp",
    ]
    for candidate in candidates:
        if _is_outside_repo(candidate) and _writable(candidate):
            return candidate.resolve()
    # Last resort: still safer than the checkout, even if not ideal.
    fallback = ROOT.resolve().parent / "relaxbook-test-tmp"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback.resolve()


SAFE_TEMP = safe_temp_root()


def pytest_configure(config: pytest.Config) -> None:
    tempfile.tempdir = str(SAFE_TEMP)
    basetemp = config.option.basetemp
    if not basetemp:
        config.option.basetemp = str(SAFE_TEMP / "pytest-tmp")


@pytest.fixture(scope="session", autouse=True)
def _isolated_tempdir():
    original = tempfile.tempdir
    tempfile.tempdir = str(SAFE_TEMP)
    try:
        yield SAFE_TEMP
    finally:
        tempfile.tempdir = original



@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def sample_dir() -> Path:
    if not (SAMPLE_DIR / "示例书.epub").exists():
        pytest.skip("sample books missing; run: python tools/make_samples.py")
    return SAMPLE_DIR


@pytest.fixture(scope="session")
def sample_epub(sample_dir: Path) -> Path:
    return sample_dir / "示例书.epub"


@pytest.fixture(scope="session")
def sample_pdf(sample_dir: Path) -> Path:
    return sample_dir / "示例书.pdf"


@pytest.fixture(scope="session")
def scanned_pdf(sample_dir: Path) -> Path:
    return sample_dir / "扫描示例.pdf"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """A pristine per-test data directory with an initialised database."""
    data_dir = tmp_path / "data"
    cfg = load_settings(data_dir)
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture()
def conn(settings: Settings):
    connection = db.connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def work_copy(tmp_path: Path) -> Path:
    """Writable copy of the sample directory, for tests that add files."""
    target = tmp_path / "samples"
    target.mkdir()
    for item in SAMPLE_DIR.glob("*"):
        if item.is_file():
            shutil.copy2(item, target / item.name)
    return target
