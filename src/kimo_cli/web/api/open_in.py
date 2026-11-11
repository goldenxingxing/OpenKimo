"""Open local apps for a path on the host machine."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from kimo_cli import logger

router = APIRouter(prefix="/api/open-in", tags=["open-in"])


class OpenInRequest(BaseModel):
    """Open path in a local app."""

    app: Literal["default", "finder", "cursor", "vscode", "iterm", "terminal", "antigravity"]
    path: str


class OpenInResponse(BaseModel):
    """Open path response."""

    ok: bool
    detail: str | None = None


class ResolvePathRequest(BaseModel):
    """Resolve a mentioned path to an existing file on disk."""

    path: str
    work_dir: str | None = None


class ResolvePathResponse(BaseModel):
    """Resolve path response."""

    found: bool
    resolved: str | None = None


#: Directories that never hold the file a reply mentions, but massively slow
#: down a recursive search.
_SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".venvstacks",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "out",
    "target",
    ".next",
    "coverage",
    ".cache",
}

#: Bounds so a huge work directory cannot hang the click handler.
_MAX_SEARCH_DEPTH = 8
_MAX_VISITED_ENTRIES = 100_000
#: Wall-clock cap on the walk, on top of the entry count — machine speed
#: varies far more than tree shape, and the click must stay snappy.
_BFS_TIME_BUDGET_SECONDS = 2.5

#: Directories the agent actually writes deliverables into. When a just-created
#: file is not in Spotlight yet, these are explored before everything else.
_PREFERRED_DIR_NAMES = {
    "output",
    "session-data",
    "docs",
    "documents",
    "projects",
    "reports",
    "deliverables",
    "results",
    "uploads",
}

#: Spotlight answers in well under a second; give up before the click feels stuck.
_MDFIND_TIMEOUT_SECONDS = 5.0


def _mdfind_matches(query_name: str) -> list[str]:
    """Run Spotlight for ``query_name``; return candidate paths (possibly empty).

    Any failure — mdfind missing, Spotlight disabled, timeout — degrades to an
    empty list so the caller falls back to walking the tree.
    """
    try:
        proc = subprocess.run(
            ["mdfind", "-name", query_name],
            capture_output=True,
            text=True,
            timeout=_MDFIND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def _mdfind_by_basename(root: Path, name: str) -> Path | None:
    """Find ``name`` under ``root`` via Spotlight; None when it has no answer.

    Only paths under ``root`` with exactly ``name`` count — mdfind matches
    loosely, and a hit from elsewhere on the disk is not what the reply meant.
    """
    if sys.platform != "darwin":
        return None
    # `mdfind -name` with the extension in the query returns nothing on
    # recent macOS; querying the stem does hit the file. The exact-basename
    # filter below keeps a stem query from matching near-misses.
    query = Path(name).stem or name
    prefix = str(root) + os.sep
    best: Path | None = None
    best_depth = -1
    for candidate in _mdfind_matches(query):
        path = Path(candidate)
        if not str(path).startswith(prefix):
            continue
        if path.name != name or not path.exists():
            continue
        depth = len(path.relative_to(root).parts)
        if best is None or depth < best_depth:
            best, best_depth = path, depth
    return best


def _bfs_by_basename(root: Path, name: str) -> Path | None:
    """Depth-limited BFS under ``root`` for an entry named ``name``.

    Returns the shallowest match — when several directories hold a file with
    the same name, the one closest to the work directory is the likeliest
    referent. Bounded by depth, visited-entry count and wall-clock time so a
    pathological tree still answers quickly. Output-style directories are
    seeded ahead of everything else: the fallback only runs for files
    Spotlight has not indexed yet, and those are overwhelmingly things the
    agent just wrote into its output area.
    """
    import time
    from collections import deque

    deadline = time.monotonic() + _BFS_TIME_BUDGET_SECONDS
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    visited = 0
    while queue:
        current, depth = queue.popleft()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        subdirs: list[Path] = []
        for entry in entries:
            visited += 1
            if visited > _MAX_VISITED_ENTRIES:
                return None
            if entry.name == name:
                return Path(entry.path)
            if (
                entry.is_dir(follow_symlinks=False)
                and depth < _MAX_SEARCH_DEPTH
                and entry.name not in _SKIP_DIR_NAMES
            ):
                subdirs.append(Path(entry.path))
        # Preferred dirs first (then alphabetical), so the agent's own output
        # tree is exhausted before generic branches of the workspace.
        subdirs.sort(key=lambda p: (p.name not in _PREFERRED_DIR_NAMES, p.name))
        queue.extend((d, depth + 1) for d in subdirs)
        if time.monotonic() > deadline:
            return None
    return None


_ELLIPSIS_PATTERN = re.compile(r"\.{3}|…")


def _elided_frags(name: str) -> list[str] | None:
    """Split an agent-abbreviated filename into its literal fragments.

    Replies routinely shorten long document names —
    ``PDD000218_B002_..._初始危害分析_ba4404.docx`` for a file actually called
    ``PDD000218_B002_HCP-021_PreliminaryHazardAnalysis_初始危害分析_ba4404.docx``.
    No basename search can hit an elided name; only fragment matching can.
    Returns None when the name carries no ellipsis (nothing to expand).
    """
    frags = [f for f in _ELLIPSIS_PATTERN.split(name) if f]
    return frags if len(frags) >= 2 else None


def _fragments_in_order(basename: str, frags: list[str]) -> bool:
    """True when every fragment appears in ``basename`` in order."""
    pos = 0
    for frag in frags:
        idx = basename.find(frag, pos)
        if idx < 0:
            return False
        pos = idx + len(frag)
    return True


def _mdfind_by_fragments(root: Path, frags: list[str]) -> Path | None:
    """Find a file whose basename contains every fragment, in order.

    Queries Spotlight with each fragment (the stem-only rule applies the same
    way) and keeps ordered superstring matches under ``root``. Elided names
    only ever come from the agent quoting a real document, so this is the one
    case where a non-exact basename match is trustworthy.
    """
    if sys.platform != "darwin":
        return None
    prefix = str(root) + os.sep
    best: Path | None = None
    best_depth = -1
    queries = {Path(f).stem or f for f in frags}
    for query in queries:
        for candidate in _mdfind_matches(query):
            path = Path(candidate)
            if not str(path).startswith(prefix):
                continue
            if not _fragments_in_order(path.name, frags) or not path.exists():
                continue
            depth = len(path.relative_to(root).parts)
            if best is None or depth < best_depth:
                best, best_depth = path, depth
    return best


def _find_by_basename(root: Path, name: str) -> Path | None:
    """Locate ``name`` under ``root``: Spotlight first, directory walk second,
    then fragment matching for ellipsis-abbreviated names.

    A work directory holding build artifacts can hold hundreds of thousands of
    entries — the BFS alone burned its visited budget in a `.refsrc` checkout
    without reaching the file four levels down. mdfind answers that instantly;
    the walk remains for hosts where Spotlight is off or the index is stale.
    """
    hit = _mdfind_by_basename(root, name)
    if hit is not None:
        return hit
    hit = _bfs_by_basename(root, name)
    if hit is not None:
        return hit
    frags = _elided_frags(name)
    if frags is not None:
        return _mdfind_by_fragments(root, frags)
    return None


def _resolve_path(path: str) -> Path:
    """Resolve and validate a path (file or directory)."""
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path does not exist: {path}",
        ) from None

    if not resolved.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path does not exist: {path}",
        )
    return resolved


def _run_command(args: list[str]) -> None:
    subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
    )


def _spawn_process(args: list[str]) -> None:
    subprocess.Popen(args, close_fds=True)


def _open_app(app_name: str, path: Path, fallback: str | None = None) -> None:
    try:
        _run_command(["open", "-a", app_name, str(path)])
        return
    except subprocess.CalledProcessError as exc:
        if fallback is None:
            raise
        logger.warning("Open with {} failed: {}", app_name, exc)
    _run_command(["open", "-a", fallback, str(path)])


def _applescript_string(value: str) -> str:
    """Render `value` as an AppleScript string literal.

    The path is interpolated into AppleScript *source*, so it has to be quoted
    for AppleScript before `quoted form of` quotes it for the shell. A double
    quote is a legal character in a macOS filename, and without this a
    directory named `x" & (do shell script "…") & "` closed the literal and ran
    the rest of its own name — a POST body reaching osascript as code.
    """
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _open_terminal(path: Path) -> None:
    literal = _applescript_string(str(path))
    script = f'tell application "Terminal" to do script "cd " & quoted form of {literal}'
    _run_command(["osascript", "-e", script])


def _open_iterm(path: Path) -> None:
    script = "\n".join(
        [
            'tell application "iTerm"',
            "  create window with default profile",
            "  tell current session of current window",
            f'    write text "cd " & quoted form of {_applescript_string(str(path))}',
            "  end tell",
            "end tell",
        ]
    )
    try:
        _run_command(["osascript", "-e", script])
    except subprocess.CalledProcessError:
        script = script.replace('"iTerm"', '"iTerm2"')
        _run_command(["osascript", "-e", script])


#: Characters cmd.exe re-parses out of a command line it is handed. `%` is
#: here because %VAR% is expanded at cmd's initial parse whatever the quoting,
#: and `!` because it is, too, on a host with DelayedExpansion enabled.
_CMD_METACHARACTERS = '&|^<>"%!'


def _reject_cmd_metacharacters(path: Path) -> None:
    """Refuse a path cmd.exe would read as syntax rather than as a path.

    These helpers hand their argument to `cmd /c start ...`, and cmd re-parses
    what it receives: a directory named `x & calc` runs calc. The path comes
    from a POST body, and the agent is free to create such a directory, so
    this is the same hole the AppleScript quoting closed on macOS. Refusing is
    the honest fix — cmd's quoting rules do not survive being nested inside
    `start`, and there is no path worth opening that needs these characters.
    """
    if any(ch in str(path) for ch in _CMD_METACHARACTERS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Path contains a character the Windows shell would interpret "
                f"({_CMD_METACHARACTERS}); cannot open it."
            ),
        )


def _open_windows_app(command: str, path: Path) -> None:
    _reject_cmd_metacharacters(path)
    _run_command(["cmd", "/c", "start", "", command, str(path)])


def _open_windows_explorer(path: Path, *, is_file: bool) -> None:
    if is_file:
        _spawn_process(["explorer", f"/select,{path}"])
    else:
        _spawn_process(["explorer", str(path)])


def _open_windows_terminal(path: Path) -> None:
    _reject_cmd_metacharacters(path)
    try:
        _run_command(["cmd", "/c", "start", "", "wt.exe", "-d", str(path)])
    except subprocess.CalledProcessError as exc:
        logger.warning("Open with Windows Terminal failed: {}", exc)
        _run_command(["cmd", "/c", "start", "", "cmd.exe", "/K", f'cd /d "{path}"'])


def _open_in_macos(app: OpenInRequest, path: Path, *, is_file: bool) -> None:
    match app.app:
        case "default":
            # File → the app macOS associates with it; directory → Finder window.
            _run_command(["open", str(path)])
        case "finder":
            if is_file:
                # Reveal file in Finder
                _run_command(["open", "-R", str(path)])
            else:
                _run_command(["open", str(path)])
        case "cursor":
            _open_app("Cursor", path)
        case "vscode":
            _open_app("Visual Studio Code", path, fallback="Code")
        case "antigravity":
            _open_app("Antigravity", path)
        case "iterm":
            # Terminal apps need directory
            directory = path.parent if is_file else path
            _open_iterm(directory)
        case "terminal":
            directory = path.parent if is_file else path
            _open_terminal(directory)
        case _:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported app: {app.app}",
            )


def _open_in_windows(app: OpenInRequest, path: Path, *, is_file: bool) -> None:
    match app.app:
        case "default":
            # File → default association; directory → Explorer window.
            _reject_cmd_metacharacters(path)
            _spawn_process(["cmd", "/c", "start", "", str(path)])
        case "finder":
            _open_windows_explorer(path, is_file=is_file)
        case "cursor":
            _open_windows_app("cursor", path)
        case "vscode":
            _open_windows_app("code", path)
        case "terminal":
            directory = path.parent if is_file else path
            _open_windows_terminal(directory)
        case "iterm" | "antigravity":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{app.app} is not supported on Windows.",
            )
        case _:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported app: {app.app}",
            )


def _open_in_sync(request: OpenInRequest, path: Path, *, is_file: bool) -> None:
    if sys.platform == "darwin":
        _open_in_macos(request, path, is_file=is_file)
    else:
        _open_in_windows(request, path, is_file=is_file)


def _path_variants(raw: str) -> list[str]:
    """The literal forms a frontend path may arrive in.

    A markdown link with a non-ASCII destination (``SEDW..._硬件机械复用补充.docx``)
    can reach this API percent-encoded (``SEDW..._%E7%A1%AC...docx``) — the
    encoder is upstream in the renderer, not here. ``urllib.parse.unquote`` is
    a no-op for a plain path (no ``%`` escapes), so trying both forms is free;
    a literal ``%`` that is not a valid escape stays untouched by unquote.
    """
    variants = [raw]
    decoded = urllib.parse.unquote(raw)
    if decoded != raw:
        variants.append(decoded)
    return variants


@router.post("/resolve", summary="Resolve a mentioned path to an existing file")
async def resolve_path(request: ResolvePathRequest) -> ResolvePathResponse:
    """Map a path mentioned in a reply onto a real file on disk.

    An agent often cites just the file name (``report.docx``) while the file
    actually lives somewhere under the session's work directory. When the
    literal path does not exist, search that directory tree by basename and
    hand back the shallowest match.
    """
    variants = _path_variants(request.path)

    # Literal form first: ``/a/b/name`` as sent, then the percent-decoded
    # variant — the renderer may have encoded a non-ASCII path upstream.
    for variant in variants:
        raw = Path(variant).expanduser()
        try:
            resolved = raw.resolve()
        except OSError:
            resolved = raw
        if resolved.exists():
            return ResolvePathResponse(found=True, resolved=str(resolved))

    if not request.work_dir:
        return ResolvePathResponse(found=False)
    work_root = Path(request.work_dir).expanduser().resolve()
    if not work_root.is_dir():
        return ResolvePathResponse(found=False)

    # The frontend resolves relative mentions against the session work
    # directory before calling this API. Agents also cite sibling workspaces
    # from their common parent (for example ``local_agent_work/output/...``
    # from an ``OpenKimo`` work directory), so retry the complete relative
    # path against each ancestor before degrading to a basename search.
    for variant in variants:
        try:
            work_relative = Path(variant).expanduser().resolve().relative_to(work_root)
        except (OSError, ValueError):
            continue
        for ancestor in work_root.parents:
            candidate = (ancestor / work_relative).resolve()
            if candidate.exists():
                return ResolvePathResponse(found=True, resolved=str(candidate))

    # Basename search: try each variant's basename (raw first, then decoded), so
    # an encoded CJK name still finds the real file by its decoded basename.
    for variant in variants:
        match = await asyncio.to_thread(
            _find_by_basename,
            work_root,
            Path(variant).name,
        )
        if match is not None:
            return ResolvePathResponse(found=True, resolved=str(match.resolve()))
    return ResolvePathResponse(found=False)


@router.post("", summary="Open a path in a local application")
async def open_in(request: OpenInRequest) -> OpenInResponse:
    if sys.platform not in {"darwin", "win32"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Open-in is only supported on macOS and Windows.",
        )

    path = _resolve_path(request.path)
    is_file = path.is_file()

    try:
        await asyncio.to_thread(_open_in_sync, request, path, is_file=is_file)
    except subprocess.CalledProcessError as exc:
        logger.warning("Open-in failed ({}): {}", request.app, exc)
        detail = exc.stderr.strip() if exc.stderr else "Failed to open application."
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        ) from exc
    except OSError as exc:
        # `open`, `osascript`, `explorer`, `wt.exe` — any of them can be
        # missing or refuse to spawn, which is not a CalledProcessError. That
        # went out as a bare 500 with a stack trace instead of a sentence
        # saying which app could not be launched.
        logger.warning("Open-in could not launch ({}): {}", request.app, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not launch {request.app}: {exc}",
        ) from exc

    return OpenInResponse(ok=True)
