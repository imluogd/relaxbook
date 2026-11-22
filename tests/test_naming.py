"""Tests for :mod:`relaxbook.naming` (CONTRACTS section 7)."""

from __future__ import annotations

from pathlib import Path

from relaxbook import naming

WINDOWS_ILLEGAL = '\\/:*?"<>|'


def test_sanitize_replaces_windows_illegal_characters() -> None:
    cleaned = naming.sanitize_title('a\\b/c:d*e?f"g<h>i|j')
    assert cleaned == "a＼b／c：d＊e？f＂g＜h＞i｜j"
    for char in WINDOWS_ILLEGAL:
        assert char not in cleaned


def test_sanitize_removes_control_characters_and_trims_tail() -> None:
    assert naming.sanitize_title("第一章\x00\x07\x1f  ") == "第一章"
    assert naming.sanitize_title("  序言 ...  ") == "序言"
    assert naming.sanitize_title("章节...") == "章节"


def test_sanitize_empty_falls_back() -> None:
    assert naming.sanitize_title("") == "未命名章节"
    assert naming.sanitize_title("   ") == "未命名章节"
    assert naming.sanitize_title("...") == "未命名章节"
    assert naming.sanitize_title("\x00\x01") == "未命名章节"
    assert naming.sanitize_title("", fallback="无题") == "无题"


def test_sanitize_truncates_and_keeps_tail_clean() -> None:
    assert len(naming.sanitize_title("长" * 200)) == 80
    assert naming.sanitize_title("a" * 100, max_len=10) == "a" * 10
    assert naming.sanitize_title("abcdefghij.", max_len=11) == "abcdefghij"
    assert len(naming.sanitize_title("长" * 200, max_len=12)) == 12


def test_sanitize_windows_reserved_device_name() -> None:
    assert naming.sanitize_title("CON") == "CON_"
    assert naming.sanitize_title("nul") == "nul_"


def test_seq_width() -> None:
    assert naming.seq_width(1) == 3
    assert naming.seq_width(3) == 3
    assert naming.seq_width(12) == 3
    assert naming.seq_width(99) == 3
    assert naming.seq_width(100) == 3
    assert naming.seq_width(999) == 3
    assert naming.seq_width(1000) == 4
    assert naming.seq_width(1200) == 4


def test_mp3_filename_basic_rule() -> None:
    assert naming.mp3_filename(1, "序言", 3) == "001 - 序言.mp3"
    assert naming.mp3_filename(2, "出发", 3) == "002 - 出发.mp3"
    assert naming.mp3_filename(3, "出发", 3) == "003 - 出发.mp3"


def test_mp3_filename_sanitizes_slash_and_falls_back() -> None:
    assert naming.mp3_filename(1, "出发/返程", 3) == "001 - 出发／返程.mp3"
    assert naming.mp3_filename(2, "", 3) == "002 - 未命名章节.mp3"
    assert "/" not in naming.mp3_filename(1, "A/B", 3)


def test_mp3_filename_width_tracks_two_digit_and_1200_totals() -> None:
    # Two-digit totals still pad to width 3.
    assert naming.mp3_filename(1, "序言", 12).startswith("001 - ")
    assert naming.mp3_filename(12, "终章", 12).startswith("012 - ")
    # 1200 chapters need width 4.
    assert naming.mp3_filename(1, "序言", 1200) == "0001 - 序言.mp3"
    assert naming.mp3_filename(1200, "终章", 1200).startswith("1200 - ")


def test_same_titled_chapters_have_unique_filenames() -> None:
    names = [naming.mp3_filename(index, "出发", 3) for index in (1, 2, 3)]
    assert len(set(names)) == 3
    assert sorted(names) == names  # sorting by file name keeps reading order


def test_filenames_are_stable_and_legal() -> None:
    first = naming.mp3_filename(2, "含:冒号?问号", 1200)
    second = naming.mp3_filename(2, "含:冒号?问号", 1200)
    assert first == second
    for char in WINDOWS_ILLEGAL:
        assert char not in first
    assert not first.endswith(".")
    assert not first.endswith(" ")


def test_m4b_filename() -> None:
    assert naming.m4b_filename("示例书", "晓晓", 1) == "示例书 - 晓晓 - v01.m4b"
    assert (
        naming.m4b_filename("示例书", "zh-CN-XiaoxiaoNeural", 12)
        == "示例书 - zh-CN-XiaoxiaoNeural - v12.m4b"
    )
    assert naming.m4b_filename("示例/书", "晓晓", 1) == "示例／书 - 晓晓 - v01.m4b"
    assert naming.m4b_filename("", "", 1) == "未命名书籍 - 未知音色 - v01.m4b"


def test_allocate_version_dir_returns_lowest_free(tmp_path: Path) -> None:
    version, path = naming.allocate_version_dir(tmp_path / "示例书")
    assert (version, path.name) == (1, "v01")
    assert path.is_dir()

    version, path = naming.allocate_version_dir(tmp_path / "示例书")
    assert (version, path.name) == (2, "v02")
    assert path.is_dir()

    assert naming.allocate_version_dir(tmp_path / "示例书")[0] == 3


def test_allocate_version_dir_fills_gap(tmp_path: Path) -> None:
    parent = tmp_path / "book"
    (parent / "v01").mkdir(parents=True)
    (parent / "v02").mkdir()

    version, path = naming.allocate_version_dir(parent)
    assert (version, path.name) == (3, "v03")

    (parent / "v02").rmdir()
    version, path = naming.allocate_version_dir(parent)
    assert (version, path) == (2, parent / "v02")
