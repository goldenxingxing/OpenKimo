#!/usr/bin/env python3
"""Mirror one GitHub Release to Gitee: metadata plus download links.

Deliberately does NOT upload the binaries. Gitee's community tier caps a
single attachment at 100 MB and the installers are well past that, so
``Release.body_with_downloads()`` appends a download section pointing at
GitHub's own URLs instead (see f34f641 and
docs/superpowers/specs/2026-07-23-gitee-mirror-and-release-sync-design.md,
which states the rule and the reason).

``ApiClient.download`` / ``GitHubReleaseSource.download_asset`` are therefore
not on the sync path. They are kept for the attachment flow that
``GiteeReleaseTarget.replace_asset`` still implements, for assets small enough
to host — nothing calls that from ``sync()`` today.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    download_url: str = ""
    size: int = 0


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    body: str
    assets: list[Asset]
    prerelease: bool = False

    def body_with_downloads(self) -> str:
        if not self.assets:
            return self.body
        lines = ["## 安装包下载", ""]
        for asset in self.assets:
            download_url = asset.download_url or asset.url
            size = f"（{asset.size / 1024 / 1024:.1f} MiB）" if asset.size else ""
            lines.append(f"- [{asset.name}]({download_url}) {size}".rstrip())
        sections = [self.body.rstrip(), "\n".join(lines)]
        return "\n\n".join(section for section in sections if section)


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


#: Longest a Retry-After is allowed to hold up a release sync.
MAX_RETRY_AFTER_SECONDS = 60


def _is_rate_limited(headers) -> bool:
    """Whether a 403 is a rate limit rather than a refusal.

    GitHub uses 403 for its secondary rate limits and marks them, either with
    Retry-After or with an exhausted x-ratelimit-remaining. Everything else
    that comes back 403 means the token may not do this, and retrying it three
    times only makes the failure slower.
    """
    if headers is None:
        return False
    try:
        if headers.get("Retry-After"):
            return True
        remaining = headers.get("X-RateLimit-Remaining")
    except AttributeError:
        return False
    return str(remaining).strip() == "0"


def _retry_after_seconds(headers) -> int:
    """Seconds the server asked us to wait, 0 when it did not say.

    Only the delta-seconds form is read. The HTTP-date form is legal and
    neither Gitee nor GitHub sends it for rate limits; a CI retry is not worth
    a date parser that could itself throw.
    """
    raw = ""
    if headers is not None:
        try:
            raw = headers.get("Retry-After") or ""
        except AttributeError:
            raw = ""
    try:
        # OverflowError as well: float("inf") parses, and int() of it raises —
        # out of a helper whose whole job is to survive whatever a rate limiter
        # puts in the header.
        seconds = int(float(str(raw).strip()))
    except (ValueError, OverflowError):
        return 0
    return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        token_style: str,
        opener: Callable[[urllib.request.Request], Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if token_style not in {"bearer", "query"}:
            raise ValueError("token_style must be 'bearer' or 'query'")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.token_style = token_style
        self.opener = opener
        self.sleeper = sleeper

    def _url(self, path: str) -> str:
        url = path if path.startswith(("http://", "https://")) else self.base_url + path
        if self.token_style == "query":
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urllib.parse.urlencode({'access_token': self.token})}"
        return url

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "OpenKimo-Gitee-Release-Sync",
        }
        if self.token_style == "bearer":
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        return headers

    def _open(self, request: urllib.request.Request):
        for attempt in range(3):
            delay = attempt + 1
            try:
                return self.opener(request)
            except urllib.error.HTTPError as error:
                # GitHub answers a secondary rate limit with 403, not 429, and
                # says so in the headers. A plain 403 is still "you may not do
                # that" and must not be retried — only one carrying a
                # rate-limit signal is worth waiting out.
                rate_limited = error.code == 429 or (
                    error.code == 403 and _is_rate_limited(error.headers)
                )
                if not rate_limited and error.code < 500:
                    raise ApiError(
                        error.code, f"request failed with HTTP {error.code}"
                    ) from None
                if attempt == 2:
                    raise ApiError(
                        error.code,
                        f"request failed after retries with HTTP {error.code}",
                    ) from None
                if rate_limited:
                    # A rate limit says how long it wants; 1, 2, 3 seconds is
                    # almost never it. Ignoring the header spends all three
                    # attempts inside the same window and fails the sync with
                    # a rate limit that had already told us how to avoid it.
                    delay = max(delay, _retry_after_seconds(error.headers))
            except urllib.error.URLError:
                if attempt == 2:
                    raise ApiError(0, "request failed after retries") from None
            self.sleeper(delay)
        raise AssertionError("unreachable")

    def request_json(
        self,
        method: str,
        path: str,
        *,
        fields: dict[str, Any] | None = None,
    ) -> Any:
        headers = self._headers()
        data = None
        if fields is not None:
            data = urllib.parse.urlencode(fields).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            self._url(path), data=data, headers=headers, method=method
        )
        response = self._open(request)
        payload = response.read()
        if not payload:
            return None
        return json.loads(payload)

    def download(self, path: str, destination: Path) -> None:
        headers = self._headers()
        headers["Accept"] = "application/octet-stream"
        request = urllib.request.Request(
            self._url(path), headers=headers, method="GET"
        )
        response = self._open(request)
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)

    def upload(
        self,
        path: str,
        *,
        fields: dict[str, Any],
        file_path: Path,
    ) -> Any:
        boundary = f"OpenKimo-{uuid.uuid4().hex}"
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            body.extend(str(value).encode())
            body.extend(b"\r\n")
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{file_path.name}"\r\n'
            ).encode()
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(file_path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode())

        headers = self._headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(
            self._url(path), data=bytes(body), headers=headers, method="POST"
        )
        response = self._open(request)
        payload = response.read()
        return json.loads(payload) if payload else None


class GitHubReleaseSource:
    def __init__(self, api: ApiClient, owner: str, repository: str):
        self.api = api
        self.owner = owner
        self.repository = repository

    def get_release(self, tag: str) -> Release:
        encoded_tag = urllib.parse.quote(tag, safe="")
        payload = self.api.request_json(
            "GET",
            f"/repos/{self.owner}/{self.repository}/releases/tags/{encoded_tag}",
        )
        return Release(
            tag=payload["tag_name"],
            name=payload.get("name") or payload["tag_name"],
            body=payload.get("body") or "",
            prerelease=bool(payload.get("prerelease")),
            assets=[
                Asset(
                    asset["name"],
                    asset["url"],
                    asset.get("browser_download_url") or asset["url"],
                    int(asset.get("size") or 0),
                )
                for asset in payload.get("assets", [])
            ],
        )

    def download_asset(self, asset: Asset, destination: Path) -> None:
        self.api.download(asset.url, destination)


class GiteeReleaseTarget:
    def __init__(self, api: ApiClient, owner: str, repository: str):
        self.api = api
        self.owner = owner
        self.repository = repository

    @property
    def releases_path(self) -> str:
        return f"/repos/{self.owner}/{self.repository}/releases"

    def default_branch(self) -> str:
        # The GitHub release's target_commitish is meaningless on Gitee (the
        # release repo has its own, unrelated history), so releases are
        # anchored to whatever branch the Gitee repo considers default.
        repo = self.api.request_json("GET", f"/repos/{self.owner}/{self.repository}")
        branch = repo.get("default_branch")
        if not branch:
            raise ApiError(
                400,
                f"Gitee repo {self.owner}/{self.repository} has no default "
                "branch (empty repository?) — push an initial commit first",
            )
        return str(branch)

    def sync_metadata(self, release: Release) -> int:
        encoded_tag = urllib.parse.quote(release.tag, safe="")
        try:
            existing = self.api.request_json(
                "GET", f"{self.releases_path}/tags/{encoded_tag}"
            )
        except ApiError as error:
            if error.status != 404:
                raise
            existing = None

        fields = {
            "tag_name": release.tag,
            "name": release.name,
            "body": release.body_with_downloads(),
            # Carried from the source release, not hardcoded. A prerelease tag
            # (v1.2.0-rc1) matches the workflow's `v*` filter and its
            # startsWith(…, 'v') gate, so it reaches here — and was then
            # republished on Gitee as a stable release.
            "prerelease": "true" if release.prerelease else "false",
        }
        if existing is None:
            fields["target_commitish"] = self.default_branch()
            synchronized = self.api.request_json(
                "POST", self.releases_path, fields=fields
            )
        else:
            synchronized = self.api.request_json(
                "PATCH",
                f"{self.releases_path}/{existing['id']}",
                fields=fields,
            )
        return int(synchronized["id"])

    def replace_asset(
        self, release_id: int, asset: Asset, file_path: Path
    ) -> None:
        attachments_path = f"{self.releases_path}/{release_id}/attach_files"
        attachments = self.api.request_json("GET", attachments_path)
        for attachment in attachments:
            if attachment.get("name") == asset.name:
                self.api.request_json(
                    "DELETE", f"{attachments_path}/{attachment['id']}"
                )
        self.api.upload(attachments_path, fields={}, file_path=file_path)

    def sync(self, release: Release) -> None:
        self.sync_metadata(release)


def parse_repository(value: str, variable_name: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"{variable_name} must use owner/repository format")
    return parts[0], parts[1]


def require_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    arguments = parser.parse_args()
    if not arguments.tag.startswith("v"):
        parser.error("--tag must start with 'v'")

    github_owner, github_repository = parse_repository(
        require_environment("RELEASE_SOURCE_REPOSITORY"), "RELEASE_SOURCE_REPOSITORY"
    )
    gitee_owner, gitee_repository = parse_repository(
        require_environment("GITEE_REPOSITORY"), "GITEE_REPOSITORY"
    )
    github_api = ApiClient(
        "https://api.github.com",
        token=require_environment("GITHUB_TOKEN"),
        token_style="bearer",
    )
    gitee_api = ApiClient(
        "https://gitee.com/api/v5",
        token=require_environment("GITEE_ACCESS_TOKEN"),
        token_style="query",
    )
    source = GitHubReleaseSource(
        github_api, github_owner, github_repository
    )
    target = GiteeReleaseTarget(gitee_api, gitee_owner, gitee_repository)
    release = source.get_release(arguments.tag)

    target.sync(release)

    print(
        f"Synchronized {release.tag} with {len(release.assets)} download link(s) to Gitee"
    )


if __name__ == "__main__":
    main()
