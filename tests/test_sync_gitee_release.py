from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "sync_gitee_release.py"
SPEC = importlib.util.spec_from_file_location("sync_gitee_release", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


@dataclass
class Call:
    method: str
    path: str
    fields: dict | None = None
    file_path: Path | None = None


class FakeApi:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request_json(self, method, path, *, fields=None):
        self.calls.append(Call(method, path, fields))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def upload(self, path, *, fields, file_path):
        self.calls.append(Call("POST", path, fields, file_path))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class GiteeReleaseTargetTests(unittest.TestCase):
    def test_release_body_contains_direct_download_links(self):
        release = sync.Release(
            tag="v0.1.19",
            name="v0.1.19",
            body="release notes",
            assets=[
                sync.Asset(
                    "OpenKimo.dmg",
                    "https://api.github.invalid/asset/1",
                    "https://github.com/example/OpenKimo.dmg",
                    257_396_137,
                )
            ],
        )

        body = release.body_with_downloads()

        self.assertIn("release notes", body)
        self.assertIn("## 安装包下载", body)
        self.assertIn("[OpenKimo.dmg](https://github.com/example/OpenKimo.dmg)", body)
        self.assertIn("245.5 MiB", body)

    def test_creates_release_when_tag_is_absent(self):
        api = FakeApi(
            [
                sync.ApiError(404, "request failed with HTTP 404"),
                {"default_branch": "master"},
                {"id": 7},
            ]
        )
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")

        release_id = target.sync_metadata(
            sync.Release(tag="v0.1.19", name="v0.1.19", body="notes", assets=[])
        )

        self.assertEqual(release_id, 7)
        self.assertEqual(api.calls[-1].method, "POST")
        self.assertEqual(api.calls[-1].path, "/repos/qunwei/OpenKimo/releases")
        self.assertEqual(api.calls[-1].fields["tag_name"], "v0.1.19")
        # Anchored to the Gitee repo's own default branch, not GitHub's.
        self.assertEqual(api.calls[-1].fields["target_commitish"], "master")

    def test_create_fails_clearly_when_gitee_repo_is_empty(self):
        api = FakeApi(
            [
                sync.ApiError(404, "request failed with HTTP 404"),
                {"default_branch": None},
            ]
        )
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")

        with self.assertRaises(sync.ApiError) as ctx:
            target.sync_metadata(
                sync.Release(tag="v0.1.19", name="v0.1.19", body="notes", assets=[])
            )
        self.assertIn("no default branch", str(ctx.exception))

    def test_updates_release_when_tag_exists(self):
        api = FakeApi([{"id": 7}, {"id": 7}])
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")

        release_id = target.sync_metadata(
            sync.Release(
                tag="v0.1.19",
                name="OpenKimo 0.1.19",
                body="notes",
                assets=[],
            )
        )

        self.assertEqual(release_id, 7)
        self.assertEqual(api.calls[-1].method, "PATCH")
        self.assertEqual(api.calls[-1].path, "/repos/qunwei/OpenKimo/releases/7")

    def test_sync_does_not_upload_release_assets(self):
        api = FakeApi([{"id": 7}, {"id": 7}])
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")
        release = sync.Release(
            tag="v0.1.19",
            name="v0.1.19",
            body="notes",
            assets=[
                sync.Asset(
                    "OpenKimo.dmg",
                    "https://api.github.invalid/asset/1",
                    "https://github.com/example/OpenKimo.dmg",
                    257_396_137,
                )
            ],
        )

        target.sync(release)

        self.assertEqual([call.method for call in api.calls], ["GET", "PATCH"])

    def test_prerelease_is_carried_from_the_source_release(self):
        """An -rc tag must not be republished on Gitee as stable.

        `prerelease` was hardcoded to "false". A prerelease tag matches the
        workflow's `v*` filter and its startsWith(..., 'v') gate, so it reaches
        the sync and was announced to mainland users as a stable release.
        """
        api = FakeApi([{"id": 7}, {"id": 7}])
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")
        release = sync.Release(
            tag="v1.2.0-rc1",
            name="v1.2.0-rc1",
            body="notes",
            assets=[],
            prerelease=True,
        )

        target.sync(release)

        self.assertEqual(api.calls[1].fields["prerelease"], "true")

    def test_a_stable_release_is_still_stable(self):
        api = FakeApi([{"id": 7}, {"id": 7}])
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")
        release = sync.Release(tag="v1.2.0", name="v1.2.0", body="notes", assets=[])

        target.sync(release)

        self.assertEqual(api.calls[1].fields["prerelease"], "false")

    def test_replaces_same_named_asset_and_keeps_other_assets(self):
        existing = [
            {"id": 3, "name": "OpenKimo.dmg"},
            {"id": 4, "name": "notes.txt"},
        ]
        api = FakeApi([existing, None, {"id": 5}])
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")

        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "OpenKimo.dmg"
            file_path.write_bytes(b"dmg")
            target.replace_asset(
                7,
                sync.Asset("OpenKimo.dmg", "https://example.invalid/app"),
                file_path,
            )

        self.assertEqual([call.method for call in api.calls], ["GET", "DELETE", "POST"])
        self.assertIn("/attach_files/3", api.calls[1].path)
        self.assertNotIn("/attach_files/4", api.calls[1].path)
        self.assertEqual(api.calls[2].file_path.name, "OpenKimo.dmg")


class ApiClientTests(unittest.TestCase):
    def test_http_error_does_not_expose_tokens(self):
        client = sync.ApiClient(
            "https://example.invalid",
            token="gitee-secret",
            token_style="query",
            opener=lambda _request: (_ for _ in ()).throw(
                sync.urllib.error.HTTPError(
                    "https://example.invalid",
                    403,
                    "github-secret",
                    {},
                    None,
                )
            ),
            sleeper=lambda _delay: None,
        )

        with self.assertRaises(sync.ApiError) as raised:
            client.request_json("GET", "/release")

        message = str(raised.exception)
        self.assertIn("HTTP 403", message)
        self.assertNotIn("gitee-secret", message)
        self.assertNotIn("github-secret", message)

    def test_rate_limit_waits_as_long_as_the_server_asked(self):
        """1, 2, 3 seconds is not a rate-limit window; the header says what is."""
        delays: list[float] = []
        attempts = {"n": 0}

        def opener(_request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sync.urllib.error.HTTPError(
                    "https://example.invalid",
                    429,
                    "slow down",
                    {"Retry-After": "42"},
                    None,
                )
            return io.BytesIO(b"{}")

        client = sync.ApiClient(
            "https://example.invalid",
            token="t",
            token_style="query",
            opener=opener,
            sleeper=delays.append,
        )

        client.request_json("GET", "/release")

        self.assertEqual(delays, [42])

    def test_an_absurd_retry_after_is_capped(self):
        delays: list[float] = []
        attempts = {"n": 0}

        def opener(_request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sync.urllib.error.HTTPError(
                    "https://example.invalid",
                    429,
                    "slow down",
                    {"Retry-After": "86400"},
                    None,
                )
            return io.BytesIO(b"{}")

        client = sync.ApiClient(
            "https://example.invalid",
            token="t",
            token_style="query",
            opener=opener,
            sleeper=delays.append,
        )

        client.request_json("GET", "/release")

        self.assertEqual(delays, [sync.MAX_RETRY_AFTER_SECONDS])

    def test_a_rate_limited_403_is_retried_but_a_plain_403_is_not(self):
        """GitHub answers a secondary rate limit with 403, and says so."""
        for headers, expect_retry in (
            ({"Retry-After": "5"}, True),
            ({"X-RateLimit-Remaining": "0"}, True),
            ({}, False),
        ):
            attempts = {"n": 0}

            def opener(_request, headers=headers, attempts=attempts):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise sync.urllib.error.HTTPError(
                        "https://example.invalid", 403, "no", headers, None
                    )
                return io.BytesIO(b"{}")

            client = sync.ApiClient(
                "https://example.invalid",
                token="t",
                token_style="query",
                opener=opener,
                sleeper=lambda _d: None,
            )

            if expect_retry:
                client.request_json("GET", "/release")
                self.assertEqual(attempts["n"], 2, headers)
            else:
                with self.assertRaises(sync.ApiError):
                    client.request_json("GET", "/release")
                self.assertEqual(attempts["n"], 1, headers)


if __name__ == "__main__":
    unittest.main()
