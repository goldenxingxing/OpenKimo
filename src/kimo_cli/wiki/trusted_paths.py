"""Paths the user named in the conversation, trusted as Wiki evidence roots.

Knowledge reaches the Wiki only when it rests on a file the runtime read inside a
trusted workspace, and the session's work directory used to be the only one. A
repository the user pointed the agent at elsewhere — "/Users/me/repo, look at
the loss here" — therefore produced nothing but unstable shell output, and every
checkpoint grounded in it was discarded. A folder or file the user names in
their own message (an upload's listed path included) is trusted for the rest of
the session: a folder with everything under it, a file on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from kaos.local import local_kaos

from kimo_cli.utils.logging import logger
from kimo_cli.utils.sensitive import is_sensitive_file

if TYPE_CHECKING:
    from kimo_cli.soul.agent import Runtime

_PATH_CANDIDATE = re.compile(r"(?<![\w./~-])((?:~?/|[A-Za-z]:\\)[^\s`'\"<>()\[\]{}，。；：、！？（）《》“”‘’]+)")  # noqa: E501
_TRAILING_PUNCTUATION = ".,;:!?"
_MAX_TRUSTED_ROOTS = 64


@dataclass(frozen=True, slots=True)
class TrustedRoot:
    workspace_id: UUID
    root: Path
    only_file: Path | None = None
    """Set when the user named a single file: that file is trusted, its siblings are not."""


@dataclass(slots=True)
class UserTrustedPaths:
    roots: list[TrustedRoot] = field(default_factory=list[TrustedRoot])

    def root_for_file(self, candidate: Path) -> TrustedRoot | None:
        """The trusted root a resolved file falls under, if any."""
        for trusted in self.roots:
            if trusted.only_file is not None:
                if candidate == trusted.only_file:
                    return trusted
            elif candidate.is_relative_to(trusted.root):
                return trusted
        return None

    def root_for_directory(self, directory: Path) -> TrustedRoot | None:
        """The trusted folder a resolved directory falls under; named files never qualify."""
        for trusted in self.roots:
            if trusted.only_file is None and directory.is_relative_to(trusted.root):
                return trusted
        return None


def mentioned_paths(text: str) -> list[Path]:
    """Existing absolute or ``~`` paths in *text*, resolved, in order of appearance.

    The filesystem root, the home directory and its parents are skipped: naming
    one of those is not pointing at something, and trusting it would trust
    everything. Sensitive files are skipped for the same reason they are never
    recorded as evidence.
    """
    home = Path.home().resolve()
    too_broad = {Path("/"), home, *home.parents}
    found: list[Path] = []
    for match in _PATH_CANDIDATE.finditer(text):
        raw = match.group(1).rstrip(_TRAILING_PUNCTUATION)
        if raw in ("", "/", "~", "~/"):
            continue
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if path in too_broad or is_sensitive_file(str(path)):
            continue
        if not (path.is_dir() or path.is_file()) or path in found:
            continue
        found.append(path)
    return found


def trust_user_mentioned_paths(runtime: Runtime, text: str) -> None:
    """Register the folders and files the user named as trusted evidence roots.

    Only the root runtime of a local session holds this, and only text the user
    wrote reaches it. Paths inside the work directory are already trusted. A
    folder is registered without an identity marker: it may be someone else's
    repository, and naming it should not leave an untracked file behind.
    """
    if (
        runtime.role != "root"
        or runtime.wiki is None
        or runtime.workspace_id is None
        or runtime.session.work_dir_meta.kaos != local_kaos.name
    ):
        return
    try:
        work_dir = Path(str(runtime.session.work_dir)).expanduser().resolve(strict=True)
        paths = mentioned_paths(text)
    except OSError:
        return
    trusted = getattr(runtime, "wiki_trusted_paths", None)
    if trusted is None:
        trusted = runtime.wiki_trusted_paths = UserTrustedPaths()
    for path in paths:
        if len(trusted.roots) >= _MAX_TRUSTED_ROOTS:
            break
        if path.is_relative_to(work_dir):
            continue
        if path.is_dir():
            if trusted.root_for_directory(path) is not None:
                continue
            root, only_file = path, None
        else:
            if trusted.root_for_file(path) is not None:
                continue
            root, only_file = path.parent, path
        try:
            workspace_id = runtime.wiki.registry.register(root, write_marker=False)
        except (OSError, ValueError) as exc:
            logger.warning("Could not trust a user-named path: {error}", error=exc)
            continue
        trusted.roots.append(TrustedRoot(workspace_id=workspace_id, root=root, only_file=only_file))
        if runtime.wiki_coordinator is not None:
            runtime.wiki_coordinator.trust_workspace(workspace_id)
