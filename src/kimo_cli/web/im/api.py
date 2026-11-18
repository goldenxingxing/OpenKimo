"""REST API for IM bridge management (channels, whitelist, bindings).

The app secret is write-only: it is accepted on create/patch and never
serialized back (``has_secret`` tells the UI whether one is set).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from kimo_cli.web.im import db
from kimo_cli.web.im.manager import SUPPORTED_CHANNEL_TYPES, ImBridgeManager

router = APIRouter(prefix="/api/im", tags=["im"])


def get_bridge_manager(req: Request) -> ImBridgeManager:
    manager = getattr(req.app.state, "im_bridge", None)
    if manager is None:  # pragma: no cover - defensive; lifespan always sets it
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IM bridge manager not started",
        )
    return manager


class ImChannelConfigModel(BaseModel):
    show_thinking: bool = True
    group_chat_enabled: bool = False
    timeout_seconds: int = Field(default=600, ge=60, le=3600)


class ChannelCreate(BaseModel):
    type: str = "feishu"
    name: str = Field(min_length=1, max_length=64)
    app_id: str = ""
    app_secret: str = ""
    enabled: bool = False
    config: ImChannelConfigModel | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in SUPPORTED_CHANNEL_TYPES:
            raise ValueError(f"unsupported channel type: {v}")
        return v


class ChannelPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    app_id: str | None = None
    app_secret: str | None = None  # empty string = leave unchanged
    enabled: bool | None = None
    config: ImChannelConfigModel | None = None


class ChannelOut(BaseModel):
    id: int
    type: str
    name: str
    enabled: bool
    app_id: str
    has_secret: bool
    config: ImChannelConfigModel
    status: dict[str, Any]


class WhitelistOut(BaseModel):
    open_ids: list[str]


class BindingOut(BaseModel):
    external_id: str
    display_name: str
    session_id: str


class PendingOut(BaseModel):
    external_id: str
    display_name: str
    last_seen: float
    message_count: int


def _to_out(row: db.ImChannelRow, manager: ImBridgeManager) -> ChannelOut:
    return ChannelOut(
        id=row.id,
        type=row.type,
        name=row.name,
        enabled=row.enabled,
        app_id=row.app_id,
        has_secret=bool(row.app_secret),
        config=ImChannelConfigModel(**row.config),
        status=manager.status(row.id),
    )


# ---------------------------------------------------------------------- #
@router.get("/channels")
async def list_channels(request: Request) -> list[ChannelOut]:
    manager = get_bridge_manager(request)
    return [_to_out(c, manager) for c in db.list_channels()]


@router.post("/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(body: ChannelCreate, request: Request) -> ChannelOut:
    manager = get_bridge_manager(request)
    row = db.create_channel(
        type=body.type,
        name=body.name,
        app_id=body.app_id,
        app_secret=body.app_secret,
        enabled=body.enabled,
        config=body.config.model_dump() if body.config else None,
    )
    await manager.restart_channel(row.id)
    return _to_out(db.get_channel(row.id) or row, manager)


@router.patch("/channels/{channel_id}")
async def patch_channel(channel_id: int, body: ChannelPatch, request: Request) -> ChannelOut:
    manager = get_bridge_manager(request)
    secret = body.app_secret if body.app_secret else None
    row = db.update_channel(
        channel_id,
        name=body.name,
        app_id=body.app_id,
        app_secret=secret,
        enabled=body.enabled,
        config=body.config.model_dump() if body.config else None,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    await manager.restart_channel(channel_id)
    return _to_out(row, manager)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(channel_id: int, request: Request) -> None:
    manager = get_bridge_manager(request)
    await manager.remove_channel(channel_id)
    db.delete_channel(channel_id)


@router.get("/channels/{channel_id}/whitelist")
async def get_whitelist(channel_id: int) -> WhitelistOut:
    if db.get_channel(channel_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return WhitelistOut(open_ids=db.get_whitelist(channel_id))


@router.put("/channels/{channel_id}/whitelist", status_code=status.HTTP_204_NO_CONTENT)
async def put_whitelist(channel_id: int, body: WhitelistOut) -> None:
    if db.get_channel(channel_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    cleaned = [e.strip() for e in body.open_ids if e.strip()]
    db.set_whitelist(channel_id, cleaned)


@router.get("/channels/{channel_id}/bindings")
async def get_bindings(channel_id: int) -> list[BindingOut]:
    if db.get_channel(channel_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return [BindingOut(**b) for b in db.list_bindings(channel_id)]


@router.get("/channels/{channel_id}/pending")
async def get_pending(channel_id: int) -> list[PendingOut]:
    """Strangers who messaged the bot and await one-click authorization."""
    if db.get_channel(channel_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return [PendingOut(**p) for p in db.list_pending(channel_id)]


@router.post(
    "/channels/{channel_id}/pending/{external_id}/approve",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def approve_pending(channel_id: int, external_id: str) -> None:
    """Authorize a pending sender: adds them to the whitelist."""
    if db.get_channel(channel_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    db.add_whitelist(channel_id, external_id)
    db.delete_pending(channel_id, external_id)


@router.delete(
    "/channels/{channel_id}/pending/{external_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_pending(channel_id: int, external_id: str) -> None:
    """Drop a pending entry without authorizing (the sender can re-appear)."""
    if db.get_channel(channel_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    db.delete_pending(channel_id, external_id)
