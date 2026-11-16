"""User-approved installation of a skill into OpenKimo's managed layer."""

import asyncio
import http.client
import ipaddress
import json
import tempfile
import urllib.request
from pathlib import Path
from typing import IO
from urllib.parse import urlparse

from kosong.tooling import BriefDisplayBlock, CallableTool2, ToolError, ToolReturnValue
from pydantic import BaseModel, Field

from kimo_cli.skill.archive import ArchiveLimits, extract_skill_archive
from kimo_cli.skill.manager import SkillManager
from kimo_cli.soul.agent import Runtime


class Params(BaseModel):
    source_url: str = Field(description="HTTPS URL of a ZIP containing one skill.")
    replace: bool = Field(
        default=False,
        description="Replace an existing managed skill with the same name.",
    )


def validate_skill_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Skill source must be an HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("Skill source URL must not contain credentials")
    _reject_internal_host(parsed.hostname)
    return url


_BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".localhost")


def _reject_internal_host(hostname: str) -> None:
    """Refuse hosts that point back into the machine or the local network.

    The installer is agent-driven, and the download is the part that can reach
    hosts the user never sees — the cloud metadata address, a private service
    on the LAN. Those are not skill archives.
    """
    host = hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ValueError(f"Skill source host is not allowed: {hostname}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError(f"Skill source address is not allowed: {hostname}")


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_skill_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_skill_archive(url: str) -> bytes:
    validate_skill_url(url)
    opener = urllib.request.build_opener(_HTTPSRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "OpenKimo/skill-installer"})
    maximum = ArchiveLimits().max_archive_bytes
    with opener.open(request, timeout=20) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Skill archive exceeds size limit")
    return data


def inspect_archive_name(data: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="openkimo-skill-inspect-") as directory:
        return extract_skill_archive(data, Path(directory)).name


class InstallSkill(CallableTool2[Params]):
    name: str = "InstallSkill"
    description: str = (
        "Install one skill from an HTTPS ZIP URL into OpenKimo's shared managed "
        "skill library. Always requires user approval before changing the library."
    )
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            validate_skill_url(params.source_url)
        except Exception as exc:
            return ToolError(message=str(exc), brief="Skill validation failed")

        # Approve before any network access. The download is the step that can
        # reach hosts the user never sees — the cloud metadata address, a
        # service on the LAN — so it must not happen on the agent's say-so.
        result = await self._runtime.approval.request(
            self.name,
            "skill.install",
            (
                f"Download and install a skill from {params.source_url} into "
                "OpenKimo's managed skill library"
            ),
        )
        if not result:
            return result.rejection_error()

        try:
            data = await asyncio.to_thread(download_skill_archive, params.source_url)
            skill_name = await asyncio.to_thread(inspect_archive_name, data)
        except Exception as exc:
            return ToolError(message=str(exc), brief="Skill download failed")

        try:
            installed = await asyncio.to_thread(
                SkillManager().install_archive,
                data,
                replace=params.replace,
            )
        except FileExistsError:
            return ToolError(
                message=f"Skill '{skill_name}' already exists. Retry with replace=true.",
                brief="Skill already exists",
            )
        except Exception as exc:
            return ToolError(message=str(exc), brief="Skill installation failed")

        return ToolReturnValue(
            is_error=False,
            output=json.dumps({"name": installed.name, "installed": True}),
            message="",
            display=[BriefDisplayBlock(text=f"Installed skill: {installed.name}")],
        )
