from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "sync_gitee_release.py"
)
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
    def test_creates_release_when_tag_is_absent(self):
        api = FakeApi([sync.ApiError(404, "request failed with HTTP 404"), {"id": 7}])
        target = sync.GiteeReleaseTarget(api, "qunwei", "OpenKimo")

        release_id = target.sync_metadata(
            sync.Release(tag="v0.1.19", name="v0.1.19", body="notes", assets=[])
        )

        self.assertEqual(release_id, 7)
        self.assertEqual(api.calls[-1].method, "POST")
        self.assertEqual(api.calls[-1].path, "/repos/qunwei/OpenKimo/releases")
        self.assertEqual(api.calls[-1].fields["tag_name"], "v0.1.19")
        self.assertEqual(api.calls[-1].fields["target_commitish"], "main")

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
        self.assertEqual(
            api.calls[-1].path, "/repos/qunwei/OpenKimo/releases/7"
        )

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

        self.assertEqual(
            [call.method for call in api.calls], ["GET", "DELETE", "POST"]
        )
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


if __name__ == "__main__":
    unittest.main()
