"""Strict data models for authoritative global Wiki Markdown."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PositiveInt,
    field_validator,
    model_validator,
)

from kimo_cli.utils.sensitive import is_sensitive_file

_SENSITIVE_URL_PARAMETERS = frozenset(
    {
        "apikey",
        "accesstoken",
        "auth",
        "authorization",
        "authtoken",
        "clientsecret",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "key",
        "password",
        "idtoken",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "session",
        "sessionid",
        "sessiontoken",
        "signature",
        "sig",
        "token",
        "usertoken",
        "userpassword",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
        "xgoogsignature",
    }
)


class UnsafeWikiPath(ValueError):
    """Raised when a Wiki logical or resolved path escapes its managed root."""


class UnsafeWikiPathEscape(UnsafeWikiPath):
    """Raised when a resolved Wiki path lands outside the managed Wiki root."""


class UnsafeWikiPage(ValueError):
    """Raised when Wiki content cannot be stored safely."""


def validate_relative_source_path(value: str) -> str:
    """Validate portable provenance without allowing host-specific paths."""
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or windows_path.drive
        or windows_path.is_absolute()
        or value.startswith(("//", "\\\\"))
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
        or is_sensitive_file(value)
    ):
        raise ValueError("source paths must be safe relative POSIX paths")
    return value


def _normalized_query_name(value: str) -> str:
    """Canonicalize decoded query parameter names before secret-family matching."""
    return "".join(character for character in value.casefold() if character.isalnum())


def is_sensitive_url_parameter(value: str) -> bool:
    """Return whether a decoded URL parameter name is a credential-bearing alias."""
    normalized = _normalized_query_name(value)
    if normalized in _SENSITIVE_URL_PARAMETERS:
        return True
    camel_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    components = tuple(part.casefold() for part in re.findall(r"[A-Za-z0-9]+", camel_boundaries))
    if components[:2] not in {("x", "amz"), ("x", "goog")}:
        return False
    return "".join(components[2:]) in {"credential", "signature", "securitytoken"}


def has_sensitive_url_parameters(url: str) -> bool:
    """Inspect both query and fragment parameters using the shared alias rules."""
    parts = urlsplit(url)
    return any(
        is_sensitive_url_parameter(name)
        for component in (parts.query, parts.fragment)
        for name, _ in parse_qsl(component, keep_blank_values=True)
    )


def has_url_credentials(url: str) -> bool:
    """Return whether a URL has userinfo or sensitive query/fragment parameters."""
    parts = urlsplit(url)
    return (
        parts.username is not None
        or parts.password is not None
        or has_sensitive_url_parameters(url)
    )


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _coerce_json_object(value: object) -> object:
    """Accept a JSON-encoded object where a structured payload was expected.

    A payload nested this deep is easy to hand over already serialized: the
    caller sends ``candidate`` as a string containing JSON instead of as the
    object. Uncoerced, that earns ``model_type`` -- "input should be a valid
    dictionary" -- which describes the wrapper it arrived in rather than the
    one step of work that would fix it.

    Only strings that parse to an object are coerced. A string that parses to
    a scalar, or does not parse at all, is handed back untouched so the
    ordinary validator reports it.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        parsed = json.loads(text)
    except ValueError:
        return value
    return parsed if isinstance(parsed, dict) else value


class SourceRef(BaseModel):
    """Portable provenance for information recorded in a Wiki page."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["workspace-file", "conversation", "web"]
    workspace_id: UUID | None = None
    path: str | None = None
    session_id: UUID | None = None
    url: HttpUrl | None = None
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("content_hash", mode="before")
    @classmethod
    def refuse_a_stand_in_hash(cls, value: object) -> object:
        """Say what to do about a hash that was not computed from bytes.

        A model with no hash to hand writes `sha256:placeholder` (or `...`,
        or `TODO`), and the pattern error it gets back describes the regex it
        missed rather than the fact that provenance is the runtime's to fill.
        """
        if isinstance(value, str) and not _SHA256.match(value):
            raise ValueError(
                f"content_hash must be sha256:<64 hex digits> computed from the source bytes, "
                f"not {value!r}. When resolving a checkpoint, leave `sources` out entirely: "
                "the runtime attaches the provenance it actually observed."
            )
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return validate_relative_source_path(value) if value is not None else None

    @model_validator(mode="after")
    def require_kind_specific_provenance(self) -> SourceRef:
        workspace = self.workspace_id is not None and self.path is not None
        if self.kind == "workspace-file":
            if not workspace or self.session_id is not None or self.url is not None:
                raise ValueError("workspace-file sources require only workspace_id and path")
        elif self.kind == "conversation":
            if self.session_id is None or any(
                value is not None for value in (self.workspace_id, self.path, self.url)
            ):
                raise ValueError("conversation sources require only session_id")
        else:
            if self.url is None or any(
                value is not None for value in (self.workspace_id, self.path, self.session_id)
            ):
                raise ValueError("web sources require only url")
            if has_url_credentials(str(self.url)):
                raise ValueError("web source URLs cannot contain credentials")
        return self


class CurrentSource(BaseModel):
    """Content supplied in the active session and eligible for controlled ingest."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def normalize_shape(cls, value: object) -> object:
        """Accept a JSON-encoded source.

        The same pre-serialization slip as a candidate: without this the caller
        earns ``model_type`` -- "input should be a valid dictionary" -- which
        names the wrapper rather than the fix.
        """
        return _coerce_json_object(value)

    kind: Literal["inline", "workspace-file"]
    content: str | None = None
    workspace_id: UUID | None = None
    relative_path: str | None = None

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        return validate_relative_source_path(value) if value is not None else None

    @model_validator(mode="after")
    def require_current_source_content(self) -> CurrentSource:
        if self.kind == "inline":
            if not self.content or self.workspace_id is not None or self.relative_path is not None:
                raise ValueError("inline sources require non-empty content only")
        elif self.content is not None or self.workspace_id is None or self.relative_path is None:
            raise ValueError("workspace-file sources require workspace_id and relative_path only")
        return self


class WikiPage(BaseModel):
    """A parsed content page, excluding special generated Wiki files."""

    model_config = ConfigDict(extra="forbid")

    logical_path: str = Field(
        description=(
            "Logical page path in `category/slug.md` form: category is one of "
            "entities|concepts|comparisons|sources|queries|lint, and the slug matches "
            "`[\\w][\\w-]*` (Unicode word characters — uppercase and CJK are allowed). "
            "A two-part path without the `.md` suffix is accepted and completed."
        )
    )
    title: str = Field(min_length=1, max_length=500)
    created: datetime
    updated: datetime
    tags: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(
        default_factory=list[SourceRef],
        description=(
            "Leave this out when resolving a checkpoint: the runtime attaches the "
            "provenance it actually observed. A source you write yourself has to "
            "match those bytes exactly, which is not something you can reconstruct."
        ),
    )
    # Strict on purpose: a stored page always carries a revision, so 0 here
    # means the frontmatter on disk is corrupt. Candidates are the exception --
    # ``normalize_shape`` promotes a 0 to 1 before this model sees it, because
    # 0 is what a caller naturally writes for a page it is about to create.
    revision: PositiveInt
    body: str = Field(min_length=1)

    @field_validator("logical_path", mode="before")
    @classmethod
    def complete_missing_suffix(cls, value: object) -> object:
        """Accept `category/slug` and complete it to `category/slug.md`.

        Only a two-part path with no suffix at all is completed; everything
        else — including a bare category such as `concepts` — is passed through
        unchanged for the regular validator to judge.
        """
        if isinstance(value, str):
            path = PurePosixPath(value)
            if len(path.parts) == 2 and not path.suffix:
                return f"{value}.md"
        return value

    @field_validator("logical_path")
    @classmethod
    def validate_logical_path(cls, value: str) -> str:
        # The public validator lives in schema.py; importing it lazily avoids a module cycle.
        from kimo_cli.wiki.schema import validate_logical_page

        return validate_logical_page(value).as_posix()

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title cannot be blank")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        if any(not tag or tag != tag.strip() for tag in value):
            raise ValueError("tags cannot be blank or padded")
        if len(set(value)) != len(value):
            raise ValueError("tags must be unique")
        return value

    @model_validator(mode="after")
    def validate_timestamps(self) -> WikiPage:
        if self.created.tzinfo is None or self.updated.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        if self.updated < self.created:
            raise ValueError("updated timestamp cannot precede created timestamp")
        return self


class PageChange(BaseModel):
    """A revision-checked replacement for one logical Wiki content page."""

    model_config = ConfigDict(extra="forbid")

    page: WikiPage
    expected_revision: PositiveInt | None = Field(
        default=None,
        description=(
            "Leave out to create a new page. To replace a page that already exists, "
            "set this to its current `revision` (as Wiki read or search report it); "
            "the write is refused if the page has changed since."
        ),
    )


class PageDeletion(BaseModel):
    """A revision-checked removal of one logical Wiki content page."""

    model_config = ConfigDict(extra="forbid")

    logical_path: str
    expected_revision: PositiveInt

    @field_validator("logical_path")
    @classmethod
    def validate_logical_path(cls, value: str) -> str:
        from kimo_cli.wiki.schema import validate_logical_page

        return validate_logical_page(value).as_posix()


class WikiCandidate(BaseModel):
    """A proposed, still-uncommitted Knowledge Base update."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    pages: list[PageChange] = Field(min_length=1)
    sources: list[SourceRef] = Field(
        default_factory=list[SourceRef],
        description=(
            "Leave this out when resolving a checkpoint: the runtime attaches the "
            "provenance it actually observed. A source you write yourself has to "
            "match those bytes exactly, which is not something you can reconstruct."
        ),
    )
    value: Literal["high", "medium", "low"]

    @model_validator(mode="before")
    @classmethod
    def normalize_shape(cls, value: object) -> object:
        """Accept a JSON-encoded candidate, and pages written one level shallow.

        Both slips come from the same cause: this payload is nested several
        levels deep, and a caller reproducing it tends to drop a level or
        hand it over pre-serialized. They are handled in one validator because
        Pydantic runs ``mode="before"`` model validators outermost-first, so
        two siblings here would each see the other's input untouched and the
        second slip would surface as an error neither one describes.

        A JSON string earns ``model_type`` -- "input should be a valid
        dictionary" -- which names the wrapper it arrived in rather than the
        one step of work that would fix it. A page written without its
        ``PageChange`` wrapper earns two failures (``page`` missing, ``body``
        extra) that together do not say "you are one level too shallow".
        """
        data = _coerce_json_object(value)
        if not isinstance(data, dict):
            return data
        pages = data.get("pages")
        if not isinstance(pages, list):
            return data
        wrapped: list[Any] = []
        changed = False
        for entry in cast(list[Any], pages):
            if isinstance(entry, dict) and "page" not in entry:
                page = {k: v for k, v in cast(dict[str, Any], entry).items()}
                revision = page.pop("expected_revision", None)
                # 0 is the value a caller reaches for a page it is creating, and
                # the manager would stamp it to 1 anyway. Promoting it here
                # keeps ``WikiPage`` strict for stored pages, where a 0 means
                # corrupt frontmatter rather than "not born yet".
                if page.get("revision") == 0:
                    page["revision"] = 1
                wrapped.append({"page": page, "expected_revision": revision})
                changed = True
            elif isinstance(entry, dict) and isinstance(entry.get("page"), dict):
                inner = cast(dict[str, Any], entry["page"])
                if inner.get("revision") == 0:
                    wrapped.append({**entry, "page": {**inner, "revision": 1}})
                    changed = True
                    continue
                wrapped.append(entry)
            else:
                wrapped.append(entry)
        return {**data, "pages": wrapped} if changed else data

    @model_validator(mode="after")
    def require_unique_pages(self) -> WikiCandidate:
        paths = [change.page.logical_path for change in self.pages]
        if len(set(paths)) != len(paths):
            raise ValueError("candidate pages must have distinct logical paths")
        return self
