#!/usr/bin/env python3
"""Copy one GitHub Release and all of its assets to Gitee."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import tempfile
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


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    body: str
    assets: list[Asset]


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
            try:
                return self.opener(request)
            except urllib.error.HTTPError as error:
                if error.code != 429 and error.code < 500:
                    raise ApiError(
                        error.code, f"request failed with HTTP {error.code}"
                    ) from None
                if attempt == 2:
                    raise ApiError(
                        error.code,
                        f"request failed after retries with HTTP {error.code}",
                    ) from None
            except urllib.error.URLError:
                if attempt == 2:
                    raise ApiError(0, "request failed after retries") from None
            self.sleeper(attempt + 1)
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
            assets=[
                Asset(asset["name"], asset["url"])
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
            "body": release.body,
            "prerelease": "false",
        }
        if existing is None:
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

    def sync(
        self,
        release: Release,
        source: GitHubReleaseSource,
        directory: Path,
    ) -> None:
        release_id = self.sync_metadata(release)
        for asset in release.assets:
            destination = directory / asset.name
            source.download_asset(asset, destination)
            self.replace_asset(release_id, asset, destination)


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
        require_environment("GITHUB_REPOSITORY"), "GITHUB_REPOSITORY"
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

    with tempfile.TemporaryDirectory(prefix="openkimo-gitee-release-") as directory:
        target.sync(release, source, Path(directory))

    print(
        f"Synchronized {release.tag} with {len(release.assets)} attachment(s) to Gitee"
    )


if __name__ == "__main__":
    main()
