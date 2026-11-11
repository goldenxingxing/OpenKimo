from types import SimpleNamespace

import pytest

from kimo_cli.tools.skill_install import InstallSkill, Params, validate_skill_url


class _Approved:
    """Truthy approval result."""

    def __bool__(self) -> bool:
        return True

    def rejection_error(self):
        return SimpleNamespace(is_error=True, message="rejected", brief="Rejected")


class _Rejected:
    """Falsy approval result — the tool must stop here."""

    def __bool__(self) -> bool:
        return False

    def rejection_error(self):
        return SimpleNamespace(is_error=True, message="rejected by user", brief="Rejected")


@pytest.mark.parametrize(
    "url",
    ["http://example.com/skill.zip", "file:///tmp/skill.zip", "ftp://example.com/a"],
)
def test_validate_skill_url_requires_https(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        validate_skill_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/skill.zip",
        "https://192.168.1.10/skill.zip",
        "https://127.0.0.1/skill.zip",
        "https://localhost/skill.zip",
        "https://build.internal/skill.zip",
        "https://printer.local/skill.zip",
    ],
)
def test_validate_skill_url_rejects_internal_targets(url: str) -> None:
    """The installer must not be usable to reach the host's own network."""
    with pytest.raises(ValueError, match="not allowed"):
        validate_skill_url(url)


@pytest.mark.anyio
async def test_install_skill_requests_approval_before_install(monkeypatch) -> None:
    calls: list[str] = []

    class Approval:
        async def request(self, tool, action, description):
            calls.append(f"approve:{tool}:{action}")
            return _Approved()

    class Manager:
        def install_archive(self, data, replace=False):
            calls.append("install")
            return SimpleNamespace(name="demo", description="Demo")

    runtime = SimpleNamespace(approval=Approval())

    def download(url):
        calls.append("download")
        return b"archive"

    monkeypatch.setattr(
        "kimo_cli.tools.skill_install.download_skill_archive",
        download,
    )
    monkeypatch.setattr(
        "kimo_cli.tools.skill_install.inspect_archive_name",
        lambda data: "demo",
    )
    monkeypatch.setattr(
        "kimo_cli.tools.skill_install.SkillManager",
        lambda: Manager(),
    )

    result = await InstallSkill(runtime)(Params(source_url="https://example.com/demo.zip"))

    # The download is the network access, so it must come after the approval.
    assert calls == ["approve:InstallSkill:skill.install", "download", "install"]
    assert result.is_error is False


@pytest.mark.anyio
async def test_install_skill_does_not_download_when_rejected(monkeypatch) -> None:
    """A rejected approval must stop the tool before any network access."""
    calls: list[str] = []

    class Approval:
        async def request(self, tool, action, description):
            calls.append("approve")
            return _Rejected()

    runtime = SimpleNamespace(approval=Approval())

    def download(url):
        calls.append("download")
        return b"archive"

    monkeypatch.setattr(
        "kimo_cli.tools.skill_install.download_skill_archive",
        download,
    )

    result = await InstallSkill(runtime)(Params(source_url="https://example.com/demo.zip"))

    assert calls == ["approve"]
    assert result.is_error is True
