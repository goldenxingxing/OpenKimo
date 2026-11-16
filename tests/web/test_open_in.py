from __future__ import annotations

import shutil
import subprocess
import urllib.parse

import pytest
from fastapi import HTTPException

from kimo_cli.web.api import open_in as open_in_api


@pytest.mark.anyio
async def test_open_in_supports_windows_directory(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(open_in_api.sys, "platform", "win32")
    monkeypatch.setattr(open_in_api, "_spawn_process", lambda args: calls.append(args))

    response = await open_in_api.open_in(
        open_in_api.OpenInRequest(app="finder", path=str(tmp_path))
    )

    assert response.ok is True
    assert calls == [["explorer", str(tmp_path)]]


@pytest.mark.anyio
async def test_open_in_supports_windows_file_selection(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(open_in_api.sys, "platform", "win32")
    monkeypatch.setattr(open_in_api, "_spawn_process", lambda args: calls.append(args))

    response = await open_in_api.open_in(
        open_in_api.OpenInRequest(app="finder", path=str(file_path))
    )

    assert response.ok is True
    assert calls == [["explorer", f"/select,{file_path}"]]


@pytest.mark.anyio
async def test_open_in_offloads_sync_work_to_thread(monkeypatch, tmp_path) -> None:
    offloaded: dict[str, object] = {}

    def fake_open_in_sync(request, path, *, is_file: bool) -> None:
        offloaded["request"] = request
        offloaded["path"] = path
        offloaded["is_file"] = is_file

    async def fake_to_thread(func, *args, **kwargs):
        offloaded["func"] = func
        return func(*args, **kwargs)

    monkeypatch.setattr(open_in_api.sys, "platform", "win32")
    monkeypatch.setattr(open_in_api, "_open_in_sync", fake_open_in_sync)
    monkeypatch.setattr(open_in_api.asyncio, "to_thread", fake_to_thread)

    response = await open_in_api.open_in(
        open_in_api.OpenInRequest(app="finder", path=str(tmp_path))
    )

    assert response.ok is True
    assert offloaded["func"] is fake_open_in_sync
    assert offloaded["path"] == tmp_path.resolve()
    assert offloaded["is_file"] is False


def test_a_quote_in_the_path_stays_data_in_the_applescript(monkeypatch, tmp_path) -> None:
    """A double quote is legal in a macOS filename, and this is osascript.

    Interpolated raw, a directory named `x" & (do shell script "…") & "` closed
    the AppleScript string literal and the rest of its own name ran as code —
    with the path coming from a POST body.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(open_in_api, "_run_command", lambda args: calls.append(args))

    hostile = tmp_path / 'x" & (do shell script "echo pwned") & "'
    hostile.mkdir()
    open_in_api._open_terminal(hostile)

    script = calls[0][-1]
    # The payload survives as a quoted literal: every quote it contains is
    # escaped, so none of it can terminate the string it sits in.
    assert '\\"' in script
    assert 'do shell script \\"echo pwned\\"' in script


def test_applescript_literals_round_trip_through_osascript(tmp_path) -> None:
    """The escaping is only right if osascript itself gives the path back."""
    osascript = shutil.which("osascript")
    if osascript is None:
        pytest.skip("osascript is macOS-only")

    for name in [
        "plain",
        'a"b',
        "a\\b",
        # A backslash immediately before a quote is where a wrong replacement
        # order silently corrupts the literal — and re-opens the injection.
        'a\\"b',
        "a\nb",
        "a\rb",
        'x" & (do shell script "echo pwned") & "',
    ]:
        literal = open_in_api._applescript_string(str(tmp_path / name))
        result = subprocess.run(
            [osascript, "-e", f"return {literal}"],
            capture_output=True,
            text=True,
            check=True,
        )
        # CR and LF compare equal here: osascript hands a carriage return back
        # as a newline. What matters is that the payload came back as text at
        # all rather than being executed, and that nothing was lost.
        expected = str(tmp_path / name).replace("\r", "\n")
        assert result.stdout.strip().replace("\r", "\n") == expected


def test_a_windows_shell_metacharacter_in_the_path_is_refused(monkeypatch, tmp_path) -> None:
    """cmd.exe re-parses what `cmd /c start` hands it.

    The AppleScript side was quoted; this one cannot be — cmd's quoting does
    not survive being nested inside `start` — so a path it would read as
    syntax is refused instead. `x & calc` is a legal directory name, and the
    agent can create it.
    """
    monkeypatch.setattr(open_in_api, "_run_command", lambda args: None)

    with pytest.raises(HTTPException) as raised:
        open_in_api._open_windows_app("cursor", tmp_path / "x & calc")

    assert raised.value.status_code == 400

    with pytest.raises(HTTPException):
        open_in_api._open_windows_terminal(tmp_path / 'x" & calc')


def test_an_ordinary_windows_path_still_opens(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(open_in_api, "_run_command", lambda args: calls.append(args))

    open_in_api._open_windows_app("cursor", tmp_path / "project")

    assert calls == [["cmd", "/c", "start", "", "cursor", str(tmp_path / "project")]]


@pytest.mark.anyio
async def test_default_app_opens_file_with_its_association_on_macos(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []
    file_path = tmp_path / "report.md"
    file_path.write_text("# hi", encoding="utf-8")
    monkeypatch.setattr(open_in_api.sys, "platform", "darwin")
    monkeypatch.setattr(open_in_api, "_run_command", lambda args: calls.append(args))

    response = await open_in_api.open_in(
        open_in_api.OpenInRequest(app="default", path=str(file_path))
    )

    assert response.ok is True
    assert calls == [["open", str(file_path.resolve())]]


@pytest.mark.anyio
async def test_default_app_opens_directory_on_macos(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(open_in_api.sys, "platform", "darwin")
    monkeypatch.setattr(open_in_api, "_run_command", lambda args: calls.append(args))

    response = await open_in_api.open_in(
        open_in_api.OpenInRequest(app="default", path=str(tmp_path))
    )

    assert response.ok is True
    assert calls == [["open", str(tmp_path.resolve())]]


@pytest.mark.anyio
async def test_default_app_opens_path_via_start_on_windows(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []
    file_path = tmp_path / "report.md"
    file_path.write_text("# hi", encoding="utf-8")
    monkeypatch.setattr(open_in_api.sys, "platform", "win32")
    monkeypatch.setattr(open_in_api, "_spawn_process", lambda args: calls.append(args))

    response = await open_in_api.open_in(
        open_in_api.OpenInRequest(app="default", path=str(file_path))
    )

    assert response.ok is True
    assert calls == [["cmd", "/c", "start", "", str(file_path.resolve())]]


def test_default_app_refuses_windows_metacharacters(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(open_in_api, "_run_command", lambda args: None)

    with pytest.raises(HTTPException) as raised:
        open_in_api._open_in_windows(
            open_in_api.OpenInRequest(app="default", path="unused"),
            tmp_path / "x & calc",
            is_file=False,
        )

    assert raised.value.status_code == 400


@pytest.mark.anyio
async def test_resolve_returns_the_literal_path_when_it_exists(tmp_path) -> None:
    file_path = tmp_path / "report.md"
    file_path.write_text("# hi", encoding="utf-8")

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=str(file_path), work_dir=str(tmp_path))
    )

    assert response.found is True
    assert response.resolved == str(file_path.resolve())


@pytest.mark.anyio
async def test_resolve_decodes_a_percent_encoded_literal_path(tmp_path) -> None:
    """A markdown link's non-ASCII destination can arrive percent-encoded
    (``%E7%A1%AC...``) — the decoder must still find the real file."""
    real = tmp_path / "SEDW00001271_硬件机械复用补充_修订澄清版.docx"
    real.write_bytes(b"docx")

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=urllib.parse.quote(str(real)), work_dir=str(tmp_path))
    )

    assert response.found is True
    assert response.resolved == str(real.resolve())


@pytest.mark.anyio
async def test_resolve_decodes_a_percent_encoded_basename_search(tmp_path, monkeypatch) -> None:
    """A bare encoded basename still finds the real file by its decoded name."""
    monkeypatch.setattr(open_in_api, "_mdfind_matches", lambda query: [])
    nested = tmp_path / "output" / "reviews"
    nested.mkdir(parents=True)
    real = nested / "SEDW00001271_硬件机械复用补充_修订澄清版.docx"
    real.write_bytes(b"docx")

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=urllib.parse.quote(real.name), work_dir=str(tmp_path))
    )

    assert response.found is True
    assert response.resolved == str(real.resolve())


@pytest.mark.anyio
async def test_resolve_finds_a_sibling_workspace_relative_to_work_dir_parent(tmp_path) -> None:
    documents = tmp_path / "Documents"
    work_dir = documents / "OpenKimo"
    work_dir.mkdir(parents=True)
    real = (
        documents
        / "local_agent_work"
        / "output"
        / "insulin-agent-clone"
        / "rl-supervisor-improvement-directions_20261103.md"
    )
    real.parent.mkdir(parents=True)
    real.write_text("# directions", encoding="utf-8")
    cited = work_dir / "local_agent_work" / "output" / "insulin-agent-clone" / real.name

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=str(cited), work_dir=str(work_dir))
    )

    assert response.found is True
    assert response.resolved == str(real.resolve())


@pytest.mark.anyio
async def test_resolve_finds_a_bare_filename_in_a_subdirectory(tmp_path, monkeypatch) -> None:
    """The reported failure: the agent cites `report.docx` while the file
    actually lives in a nested project folder under the work directory."""
    # Pin the tree walk: Spotlight results for tmp files are nondeterministic.
    monkeypatch.setattr(open_in_api, "_mdfind_matches", lambda query: [])
    nested = tmp_path / "output" / "projects" / "insulin_agent" / "HCP-021_SRD"
    nested.mkdir(parents=True)
    real = nested / "HCP-021_闭环算法软件需求_2026-10-15.docx"
    real.write_bytes(b"docx")

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(
            path=str(tmp_path / "HCP-021_闭环算法软件需求_2026-10-15.docx"),
            work_dir=str(tmp_path),
        )
    )

    assert response.found is True
    assert response.resolved == str(real.resolve())


@pytest.mark.anyio
async def test_resolve_prefers_the_shallowest_match(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(open_in_api, "_mdfind_matches", lambda query: [])
    shallow = tmp_path / "notes.md"
    shallow.write_text("shallow", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "notes.md"
    deep.parent.mkdir(parents=True)
    deep.write_text("deep", encoding="utf-8")

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=str(tmp_path / "notes.md"), work_dir=str(tmp_path))
    )

    assert response.resolved == str(shallow.resolve())


@pytest.mark.anyio
async def test_resolve_does_not_descend_into_junk_directories(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(open_in_api, "_mdfind_matches", lambda query: [])
    junk = tmp_path / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "only_here.md").write_text("x", encoding="utf-8")

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=str(tmp_path / "only_here.md"), work_dir=str(tmp_path))
    )

    assert response.found is False


def test_mdfind_queries_the_stem_not_the_extension(monkeypatch, tmp_path) -> None:
    """`mdfind -name` with the extension in the query returns nothing on
    recent macOS; querying the stem is what actually hits the file."""
    queries: list[str] = []

    def fake_matches(query: str) -> list[str]:
        queries.append(query)
        return []

    monkeypatch.setattr(open_in_api.sys, "platform", "darwin")
    monkeypatch.setattr(open_in_api, "_mdfind_matches", fake_matches)

    assert open_in_api._mdfind_by_basename(tmp_path, "report.docx") is None
    assert open_in_api._mdfind_by_basename(tmp_path, "notes") is None
    assert queries == ["report", "notes"]


def test_mdfind_hits_are_filtered_and_shallowest_wins(monkeypatch, tmp_path) -> None:
    """Spotlight matches loosely and searches the whole disk: keep only exact
    basenames under the work directory, then take the shallowest."""
    deep = tmp_path / "a" / "b" / "report.docx"
    shallow = tmp_path / "out" / "report.docx"
    for path in (deep, shallow):
        path.parent.mkdir(parents=True)
        path.write_text("x", encoding="utf-8")
    # Wrong-basename, outside-root, and not-on-disk candidates must be dropped.
    decoys = [
        str(tmp_path / "elsewhere" / "report.docx.bak"),
        str(tmp_path.parent / "report.docx"),
        str(tmp_path / "ghost" / "report.docx"),
    ]

    monkeypatch.setattr(open_in_api.sys, "platform", "darwin")
    monkeypatch.setattr(
        open_in_api,
        "_mdfind_matches",
        lambda query: [str(deep), str(shallow), *decoys],
    )

    hit = open_in_api._mdfind_by_basename(tmp_path, "report.docx")
    assert hit == shallow


@pytest.mark.anyio
async def test_resolve_falls_back_to_the_tree_walk_when_mdfind_is_empty(
    tmp_path, monkeypatch
) -> None:
    """Spotlight off or index stale: the click must still resolve."""
    nested = tmp_path / "deep" / "project"
    nested.mkdir(parents=True)
    real = nested / "spec.md"
    real.write_text("spec", encoding="utf-8")

    monkeypatch.setattr(open_in_api, "_mdfind_matches", lambda query: [])

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=str(tmp_path / "spec.md"), work_dir=str(tmp_path))
    )

    assert response.found is True
    assert response.resolved == str(real.resolve())


def test_elided_frags_split_on_dots_and_unicode_ellipsis() -> None:
    assert open_in_api._elided_frags("PDD000218_B002_..._初始危害分析_ba4404.docx") == [
        "PDD000218_B002_",
        "_初始危害分析_ba4404.docx",
    ]
    assert open_in_api._elided_frags("report…v2.docx") == ["report", "v2.docx"]
    assert open_in_api._elided_frags("plain-name.docx") is None
    assert open_in_api._elided_frags("only...") is None


def test_fragments_must_appear_in_order() -> None:
    base = "PDD000218_B002_HCP-021_PreliminaryHazardAnalysis_初始危害分析_ba4404.docx"
    assert open_in_api._fragments_in_order(base, ["PDD000218_B002_", "_初始危害分析_ba4404.docx"])
    # Reversed fragments do not occur in order — no match.
    assert not open_in_api._fragments_in_order(
        base, ["_初始危害分析_ba4404.docx", "PDD000218_B002_"]
    )


@pytest.mark.anyio
async def test_resolve_expands_an_ellipsis_abbreviated_filename(tmp_path, monkeypatch) -> None:
    """The reported failure: the agent wrote
    `PDD000218_B002_..._初始危害分析_ba4404.docx` while the file on disk spells
    out the elided middle. Exact basename search can never hit it; fragment
    matching must."""
    nested = tmp_path / "review"
    nested.mkdir()
    real = nested / ("PDD000218_B002_HCP-021_PreliminaryHazardAnalysis_初始危害分析_ba4404.docx")
    real.write_bytes(b"docx")
    elided = str(tmp_path / "PDD000218_B002_..._初始危害分析_ba4404.docx")

    real_str = str(real)

    def fake_mdfind(query: str) -> list[str]:
        # Spotlight only answers when queried with a fragment, never with the
        # full elided name.
        if "PDD000218" in query or "初始危害分析" in query:
            return [real_str]
        return []

    monkeypatch.setattr(open_in_api.sys, "platform", "darwin")
    monkeypatch.setattr(open_in_api, "_mdfind_matches", fake_mdfind)

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=elided, work_dir=str(tmp_path))
    )

    assert response.found is True
    assert response.resolved == real_str


@pytest.mark.anyio
async def test_resolve_does_not_fuzzy_match_when_the_name_has_no_ellipsis(
    tmp_path, monkeypatch
) -> None:
    """Fragment matching is only for explicitly abbreviated names — a plain
    missing file must stay missing."""
    (tmp_path / "review").mkdir()
    real = tmp_path / "review" / "PDD000218_full_report.docx"
    real.write_bytes(b"docx")

    monkeypatch.setattr(open_in_api.sys, "platform", "darwin")
    monkeypatch.setattr(
        open_in_api,
        "_mdfind_matches",
        lambda query: [str(real)] if "PDD" in query else [],
    )

    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(
            path=str(tmp_path / "PDD000218_report.docx"), work_dir=str(tmp_path)
        )
    )

    assert response.found is False


@pytest.mark.anyio
async def test_resolve_reports_missing_without_work_dir(tmp_path) -> None:
    response = await open_in_api.resolve_path(
        open_in_api.ResolvePathRequest(path=str(tmp_path / "gone.md"))
    )

    assert response.found is False
    assert response.resolved is None
