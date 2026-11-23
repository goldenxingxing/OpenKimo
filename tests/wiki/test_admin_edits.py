from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import kimo_cli.wiki.transaction as transaction_module
from kimo_cli.wiki.initialize import ensure_wiki
from kimo_cli.wiki.models import PageDeletion, WikiPage
from kimo_cli.wiki.schema import render_page
from kimo_cli.wiki.transaction import (
    WikiConflictError,
    WikiTransaction,
    recover_transactions,
)


def _page(path: str, *, revision: int = 1, body: str = "Before.\n") -> WikiPage:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    return WikiPage(
        logical_path=path,
        title="Atomic writes",
        created=now,
        updated=now,
        tags=["wiki"],
        sources=[],
        revision=revision,
        body=body,
    )


def _prepare_delete(root: Path) -> tuple[WikiTransaction, Path]:
    layout = ensure_wiki(root)
    target = layout.root / "concepts" / "atomic-writes.md"
    target.write_bytes(render_page(_page("concepts/atomic-writes.md")).encode("utf-8"))
    transaction = WikiTransaction.prepare(
        layout=layout,
        changes=[],
        deletions=[PageDeletion(logical_path="concepts/atomic-writes.md", expected_revision=1)],
        expected_global_revision=0,
        index_bytes=b"# Wiki Index\n",
        log_bytes=b"# Wiki Log\n\nadmin-delete\n",
    )
    return transaction, target


def _state(transaction: WikiTransaction, target: Path) -> tuple[bytes | None, ...]:
    return (
        target.read_bytes() if target.exists() else None,
        transaction.layout.index.read_bytes(),
        transaction.layout.log.read_bytes(),
        transaction.layout.revision.read_bytes(),
    )


def test_transaction_removes_a_page_with_index_log_and_revision(tmp_path: Path) -> None:
    transaction, target = _prepare_delete(tmp_path / "wiki")

    assert transaction.commit() == 1

    assert not target.exists()
    assert transaction.layout.index.read_bytes() == b"# Wiki Index\n"
    assert transaction.layout.revision.read_bytes() == b"1\n"
    assert not any((transaction.layout.metadata / "journal").iterdir())


@pytest.mark.parametrize(
    "failpoint",
    ["journal_fsync", "page_remove", "index_replace", "log_replace", "revision_replace"],
)
def test_interrupted_removal_recovers_to_one_whole_revision(
    tmp_path: Path,
    failpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction, target = _prepare_delete(tmp_path / failpoint)
    before = _state(transaction, target)

    def inject(name: str) -> None:
        if name == failpoint:
            raise OSError(f"injected {name}")

    monkeypatch.setattr(transaction_module, "_hit_failpoint", inject)
    with pytest.raises(OSError, match="injected"):
        transaction.commit()
    monkeypatch.setattr(transaction_module, "_hit_failpoint", lambda _name: None)

    result = recover_transactions(transaction.layout)
    after = _state(transaction, target)

    assert result.writes_quarantined is False
    if after != before:
        assert after == (None, b"# Wiki Index\n", b"# Wiki Log\n\nadmin-delete\n", b"1\n")


def test_interrupted_removal_restores_the_page_when_it_cannot_roll_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction, target = _prepare_delete(tmp_path / "wiki")
    before = _state(transaction, target)

    def inject(name: str) -> None:
        if name == "page_remove":
            raise OSError("injected")

    monkeypatch.setattr(transaction_module, "_hit_failpoint", inject)
    with pytest.raises(OSError):
        transaction.commit()
    monkeypatch.setattr(transaction_module, "_hit_failpoint", lambda _name: None)
    assert not target.exists()
    # The index artifact is the second target; without it the change cannot finish.
    next(transaction.layout.metadata.joinpath("journal").glob("*/new/0001")).unlink()

    result = recover_transactions(transaction.layout)

    assert _state(transaction, target) == before
    assert result.rolled_back_transactions == 1


def test_removal_of_a_page_that_moved_on_is_refused(tmp_path: Path) -> None:
    layout = ensure_wiki(tmp_path / "wiki")
    target = layout.root / "concepts" / "atomic-writes.md"
    target.write_text(render_page(_page("concepts/atomic-writes.md", revision=2)), "utf-8")

    with pytest.raises(WikiConflictError, match="page revision changed"):
        WikiTransaction.prepare(
            layout=layout,
            changes=[],
            deletions=[PageDeletion(logical_path="concepts/atomic-writes.md", expected_revision=1)],
            expected_global_revision=0,
            index_bytes=b"# Wiki Index\n",
            log_bytes=b"# Wiki Log\n",
        )


@pytest.fixture
def manager(tmp_path: Path):
    from kimo_cli.wiki.manager import WikiManager

    instance = WikiManager(tmp_path / "wiki", wal=False)
    yield instance
    instance.close()


def test_person_can_create_edit_and_delete_a_page(manager) -> None:
    created = manager.save_page(
        logical_path="concepts/release-train.md",
        title="Release train",
        tags=["process"],
        body="Releases ship every second Tuesday.\n",
        expected_revision=None,
    )
    assert created.global_revision == 1
    assert manager.read("concepts/release-train.md").page.revision == 1
    assert "[[concepts/release-train]] — Releases ship" in manager.layout.index.read_text("utf-8")
    assert manager.search("Tuesday", 5)[0].logical_path == "concepts/release-train.md"

    edited = manager.save_page(
        logical_path="concepts/release-train.md",
        title="Release train",
        tags=["process", "release"],
        body="Releases ship every second Wednesday.\n",
        expected_revision=1,
    )
    assert edited.global_revision == 2
    page = manager.read("concepts/release-train.md").page
    assert page.revision == 2
    assert page.tags == ["process", "release"]
    assert page.created <= page.updated

    manager.delete_page("concepts/release-train.md", expected_revision=2)
    assert not (manager.layout.root / "concepts" / "release-train.md").exists()
    assert "release-train" not in manager.layout.index.read_text("utf-8")
    assert manager.search("Wednesday", 5) == []
    assert manager.list_pages().global_revision == 3

    log = manager.read_log()
    assert "operation=admin-create" in log
    assert "operation=admin-edit" in log
    assert "operation=admin-delete" in log


def test_edit_keeps_the_provenance_a_page_already_had(manager) -> None:
    from tests.wiki.test_manager import _candidate, _context

    manager.commit(manager.prepare(_candidate(), _context()))
    original = manager.read("concepts/cache-mode.md").page

    manager.save_page(
        logical_path="concepts/cache-mode.md",
        title="Cache mode",
        tags=[],
        body="Corrected by hand.\n",
        expected_revision=1,
    )

    page = manager.read("concepts/cache-mode.md").page
    assert page.sources == original.sources
    assert page.created == original.created


def test_stale_edit_and_duplicate_create_are_conflicts(manager) -> None:
    manager.save_page(
        logical_path="concepts/a.md",
        title="A",
        tags=[],
        body="First.\n",
        expected_revision=None,
    )

    with pytest.raises(WikiConflictError, match="already exists"):
        manager.save_page(
            logical_path="concepts/a.md",
            title="A",
            tags=[],
            body="Again.\n",
            expected_revision=None,
        )
    manager.save_page(
        logical_path="concepts/a.md", title="A", tags=[], body="Second.\n", expected_revision=1
    )
    with pytest.raises(WikiConflictError, match="revision changed"):
        manager.save_page(
            logical_path="concepts/a.md", title="A", tags=[], body="Lost.\n", expected_revision=1
        )
    with pytest.raises(WikiConflictError, match="revision changed"):
        manager.delete_page("concepts/a.md", expected_revision=1)
    assert manager.read("concepts/a.md").page.body == "Second.\n"


@pytest.mark.parametrize(
    "body",
    [
        "token: sk-abcdefghijklmnopqrstuvwxyz\n",
        "See /Users/someone/notes.md for details.\n",
        "Broken [[not a link]] here.\n",
    ],
)
def test_person_edits_still_obey_the_page_rules(manager, body: str) -> None:
    with pytest.raises(ValueError):
        manager.save_page(
            logical_path="concepts/unsafe.md",
            title="Unsafe",
            tags=[],
            body=body,
            expected_revision=None,
        )
    assert not (manager.layout.root / "concepts" / "unsafe.md").exists()
    assert manager.layout.revision.read_text("ascii") == "0\n"


def test_listing_shows_malformed_pages_with_the_reason(manager) -> None:
    manager.save_page(
        logical_path="entities/sqlite.md",
        title="SQLite",
        tags=["db"],
        body="An embedded database.\n",
        expected_revision=None,
    )
    (manager.layout.root / "concepts" / "broken.md").write_text("no frontmatter\n", "utf-8")

    listing = manager.list_pages()

    by_path = {page.logical_path: page for page in listing.pages}
    assert by_path["entities/sqlite.md"].title == "SQLite"
    assert by_path["entities/sqlite.md"].revision == 1
    assert by_path["entities/sqlite.md"].error is None
    assert by_path["concepts/broken.md"].title is None
    assert "frontmatter" in (by_path["concepts/broken.md"].error or "")


def test_hand_written_page_is_not_missing_provenance(manager) -> None:
    manager.save_page(
        logical_path="concepts/by-hand.md",
        title="By hand",
        tags=[],
        body="Written by a person.\n",
        expected_revision=None,
    )
    # Same shape, but written straight to disk: nothing vouches for it.
    (manager.layout.root / "concepts" / "off-books.md").write_text(
        render_page(_page("concepts/off-books.md", body="Nobody wrote this through the Wiki.\n")),
        "utf-8",
    )

    missing = {
        issue.logical_path
        for issue in manager.lint(None).issues
        if issue.code == "missing_provenance"
    }

    assert missing == {"concepts/off-books.md"}


def test_hand_written_attribution_follows_the_latest_write() -> None:
    from kimo_cli.wiki.manager import hand_written_pages, parse_log

    log = "\n".join(
        [
            "# Wiki Log",
            "",
            "2026-07-24T12:00:00+00:00 | operation=admin-create | revision=1 | "
            "pages=concepts/a.md | sources= | summary=a",
            "2026-07-24T12:01:00+00:00 | operation=admin-create | revision=2 | "
            "pages=concepts/b.md | sources= | summary=b%7Cc",
            "2026-07-24T12:02:00+00:00 | operation=remember | revision=3 | "
            "pages=concepts/a.md | sources=web@x | summary=model",
            "2026-07-24T12:03:00+00:00 | operation=admin-delete | revision=4 | "
            "pages=concepts/b.md | sources= | summary=gone",
            "2026-07-24T12:04:00+00:00 | operation=admin-edit | revision=5 | "
            "pages=concepts/c.md | sources= | summary=c",
        ]
    )

    entries = parse_log(log)

    assert [entry.revision for entry in entries] == [1, 2, 3, 4, 5]
    assert entries[1].summary == "b|c"
    assert hand_written_pages(entries) == frozenset({"concepts/c.md"})
