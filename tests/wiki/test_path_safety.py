from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from kimo_cli.wiki.models import (
    CurrentSource,
    SourceRef,
    UnsafeWikiPath,
    UnsafeWikiPathEscape,
    WikiPage,
)
from kimo_cli.wiki.schema import (
    UnsafeWikiPage,
    _validate_body,
    resolve_page_path,
    validate_logical_page,
)

_NOW = datetime(2026, 7, 24, tzinfo=UTC)


@pytest.mark.parametrize(
    "bad",
    ["../secret.md", "/tmp/x.md", "entities/../../x.md", ".openkimo/revision", "schema.md"],
)
def test_logical_page_rejects_escape(bad: str) -> None:
    with pytest.raises(UnsafeWikiPath):
        validate_logical_page(bad)


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ("entities/openkimo.md", PurePosixPath("entities/openkimo.md")),
        ("concepts/中文.md", PurePosixPath("concepts/中文.md")),
        ("comparisons/file-locks.md", PurePosixPath("comparisons/file-locks.md")),
    ],
)
def test_logical_page_accepts_declared_category_and_slug(
    page: str, expected: PurePosixPath
) -> None:
    assert validate_logical_page(page) == expected


def test_resolved_page_path_stays_below_wiki_root(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    (root / "concepts").mkdir(parents=True)

    assert (
        resolve_page_path(root, "concepts/atomic-writes.md") == root / "concepts/atomic-writes.md"
    )


def test_resolved_page_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "concepts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeWikiPathEscape, match="outside the managed Wiki root"):
        resolve_page_path(root, "concepts/atomic-writes.md")


def test_invalid_page_path_error_names_category_list_and_slug_rule() -> None:
    with pytest.raises(UnsafeWikiPath) as exc_info:
        validate_logical_page("references/x.md")

    message = str(exc_info.value)
    for category in ("entities", "concepts", "comparisons", "sources", "queries", "lint"):
        assert category in message
    assert "[\\w][\\w-]*" in message


def _wiki_page(logical_path: str) -> WikiPage:
    return WikiPage(
        logical_path=logical_path,
        title="Title",
        created=_NOW,
        updated=_NOW,
        revision=1,
        body="Body text.\n",
    )


def test_wiki_page_completes_missing_md_suffix() -> None:
    assert _wiki_page("concepts/foo").logical_path == "concepts/foo.md"


def test_wiki_page_accepts_uppercase_slug() -> None:
    assert _wiki_page("concepts/Silicon-CGM-GS3-registration.md").logical_path == (
        "concepts/Silicon-CGM-GS3-registration.md"
    )


def test_wiki_page_rejects_undeclared_category_with_rule_message() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _wiki_page("references/x.md")

    message = str(exc_info.value)
    assert "entities" in message and "lint" in message
    assert "[\\w][\\w-]*" in message


@pytest.mark.parametrize(
    "unsafe_path",
    [
        r"C:\Users\person\source.md",
        "C:/Users/person/source.md",
        r"\\server\share\source.md",
        "//server/share/source.md",
    ],
)
def test_source_models_reject_windows_drive_and_unc_paths(unsafe_path: str) -> None:
    with pytest.raises(ValueError):
        SourceRef(
            kind="workspace-file",
            workspace_id="123e4567-e89b-12d3-a456-426614174000",
            path=unsafe_path,
            content_hash="sha256:" + "a" * 64,
        )
    with pytest.raises(ValueError):
        CurrentSource(
            kind="workspace-file",
            workspace_id="123e4567-e89b-12d3-a456-426614174000",
            relative_path=unsafe_path,
        )


@pytest.mark.parametrize(
    "text",
    [
        "趋势 0.99%/0.66%（FDA ≤1% 达标）",  # two percentages, not a root
        "采样 1/3 的窗口",
        "误差 ±0.1/0.2 mg/dL",
    ],
)
def test_body_accepts_a_ratio_between_numbers(text: str) -> None:
    """A segment with no letter in it is a ratio or a unit, not a path.

    A page reporting metrics is otherwise unwritable: the first version of the
    rule read `/0.66%` as a root directory and refused the page.
    """
    _validate_body(f"# Metrics\n\n{text}\n")


@pytest.mark.parametrize(
    "text",
    ["输入 /afk 或 /yolo 切换模式", "运行 /loop 5m 轮询", "见 /compact 的说明"],
)
def test_body_accepts_a_slash_command(text: str) -> None:
    """`/afk` names a command, not a root. This product's own pages say so."""
    _validate_body(f"# Commands\n\n{text}\n")


@pytest.mark.parametrize(
    "text",
    ["读取 /etc/passwd 的内容", "装在 /Applications/OpenKimo.app 下", "见 /usr 的说明"],
)
def test_body_still_rejects_a_real_absolute_path(text: str) -> None:
    with pytest.raises(UnsafeWikiPage):
        _validate_body(f"# Paths\n\n{text}\n")


@pytest.mark.parametrize(
    "text",
    [
        "结构：位置（章节号+照抄引文）/问题（前版→本版→剩余缺口）/为什么重要/建议",
        "字段 (a)/(b)/(c) 依次填写",
        "「已修改」/「不修改」/「有疑问」三选一",
    ],
)
def test_body_accepts_a_slash_after_a_closing_bracket_or_quote(text: str) -> None:
    """A slash after "）" separates fields; a path does not start there.

    Session b8e48d05 was refused twice for the first line, which describes a
    review ledger's columns, and had to guess which sentence to rewrite.
    """
    _validate_body(f"# Ledger\n\n{text}\n")


@pytest.mark.parametrize(
    "text",
    ["路径（/Users/me/work/a.md）", "见“/home/me/notes/x.md”", "在：/Users/me/a/b 下"],
)
def test_body_still_rejects_a_path_after_an_opening_bracket_or_colon(text: str) -> None:
    with pytest.raises(UnsafeWikiPage):
        _validate_body(f"# Paths\n\n{text}\n")


def test_the_refusal_names_the_text_it_refused() -> None:
    with pytest.raises(UnsafeWikiPage, match="/Users/me/work"):
        _validate_body("# Paths\n\n文件在 /Users/me/work/a.md 里\n")
