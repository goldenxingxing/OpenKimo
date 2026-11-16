import base64
from io import BytesIO
from pathlib import Path
from typing import override

from kaos.path import KaosPath
from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimo_cli.soul.agent import Runtime
from kimo_cli.tools import SkipThisTool
from kimo_cli.tools.file.utils import MEDIA_SNIFF_BYTES, FileType, detect_file_type
from kimo_cli.tools.utils import load_desc
from kimo_cli.utils.logging import logger
from kimo_cli.utils.media_tags import wrap_media_part
from kimo_cli.utils.path import is_within_workspace, kaos_path_from_user_input
from kimo_cli.wire.types import ImageURLPart, VideoURLPart

# Register HEIF/HEIC/AVIF opener with Pillow at import time so HEIC images
# can be transcoded for vision LLMs that don't accept image/heic.
try:
    from pillow_heif import register_heif_opener  # type: ignore[import-not-found]

    register_heif_opener()
except Exception:  # pragma: no cover - degraded mode if pillow-heif missing
    logger.debug("pillow-heif not available; HEIC/HEIF/AVIF will not be transcoded")

MAX_MEDIA_MEGABYTES = 100

_BASE64_CHUNK_BYTES = 3 * 1024 * 1024
"""Chunk size for base64 encoding: a multiple of three keeps chunks
independently encodable, so the concatenation is a valid base64 stream."""

# Image MIMEs that vision LLMs commonly reject; transcode to JPEG before sending.
_LLM_UNSAFE_IMAGE_MIMES = frozenset({"image/heic", "image/heif", "image/avif"})


def _transcode_image_to_jpeg(data: bytes) -> bytes | None:
    """Decode an image and re-encode as JPEG. Returns None on failure."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buf = BytesIO()
            image.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
    except Exception as e:
        logger.warning("Image transcode to JPEG failed: {error}", error=e)
        return None


def _to_data_url(mime_type: str, data: bytes) -> str:
    """Build a base64 data URL for *data*.

    Encoded in chunks and joined once: ``base64.b64encode(data).decode()`` plus
    the f-string made two extra copies of the 133%-sized payload, so a file at
    the 100MB cap peaked at several times its own size. A chunk size that is a
    multiple of three keeps every chunk independently encodable.
    """
    parts = [f"data:{mime_type};base64,"]
    for start in range(0, len(data), _BASE64_CHUNK_BYTES):
        parts.append(base64.b64encode(data[start : start + _BASE64_CHUNK_BYTES]).decode("ascii"))
    return "".join(parts)


def _extract_image_size(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            return image.size
    except Exception:
        return None


class Params(BaseModel):
    path: str = Field(
        description=(
            "The path to the file to read. Absolute paths are required when reading files "
            "outside the working directory."
        )
    )


class ReadMediaFile(CallableTool2[Params]):
    name: str = "ReadMediaFile"
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        capabilities = runtime.llm.capabilities if runtime.llm else set[str]()
        if "image_in" not in capabilities and "video_in" not in capabilities:
            raise SkipThisTool()

        description = load_desc(
            Path(__file__).parent / "read_media.md",
            {
                "MAX_MEDIA_MEGABYTES": MAX_MEDIA_MEGABYTES,
                "capabilities": capabilities,
            },
        )
        super().__init__(description=description)

        self._runtime = runtime
        self._work_dir = runtime.builtin_args.KIMO_WORK_DIR
        self._additional_dirs = runtime.additional_dirs
        self._capabilities = capabilities

    async def _validate_path(self, path: KaosPath) -> ToolError | None:
        """Validate that the path is safe to read."""
        resolved_path = path.canonical()

        if (
            not is_within_workspace(resolved_path, self._work_dir, self._additional_dirs)
            and not path.is_absolute()
        ):
            # Outside files can only be read with absolute paths
            return ToolError(
                message=(
                    f"`{path}` is not an absolute path. "
                    "You must provide an absolute path to read a file "
                    "outside the working directory."
                ),
                brief="Invalid path",
            )
        return None

    async def _read_media(self, path: KaosPath, file_type: FileType) -> ToolReturnValue:
        if file_type.kind not in ("image", "video"):
            raise ValueError(f"Unsupported file type for ReadMediaFile: {file_type.kind!r}")

        media_path = str(path)
        stat = await path.stat()
        size = stat.st_size
        if size == 0:
            return ToolError(
                message=f"`{path}` is empty.",
                brief="Empty file",
            )
        if size > (MAX_MEDIA_MEGABYTES << 20):
            return ToolError(
                message=(
                    f"`{path}` is {size} bytes, which exceeds the maximum size of "
                    f"{MAX_MEDIA_MEGABYTES}MB for media files."
                ),
                brief="File too large",
            )

        match file_type.kind:
            case "image":
                data = await path.read_bytes()
                effective_mime = file_type.mime_type
                if effective_mime in _LLM_UNSAFE_IMAGE_MIMES:
                    transcoded = _transcode_image_to_jpeg(data)
                    if transcoded is None:
                        return ToolError(
                            message=(
                                f"`{path}` is {effective_mime}, which the model does not "
                                "accept, and on-the-fly transcoding to JPEG failed."
                            ),
                            brief="Unsupported image format",
                        )
                    logger.info(
                        "Transcoded {mime} -> image/jpeg for {path} ({old}B -> {new}B)",
                        mime=effective_mime,
                        path=media_path,
                        old=len(data),
                        new=len(transcoded),
                    )
                    data = transcoded
                    effective_mime = "image/jpeg"
                data_url = _to_data_url(effective_mime, data)
                part = ImageURLPart(image_url=ImageURLPart.ImageURL(url=data_url))
                wrapped = wrap_media_part(part, tag="image", attrs={"path": media_path})
                image_size = _extract_image_size(data)
                file_type = FileType(kind="image", mime_type=effective_mime)
            case "video":
                data = await path.read_bytes()
                data_url = _to_data_url(file_type.mime_type, data)
                part = VideoURLPart(video_url=VideoURLPart.VideoURL(url=data_url))
                wrapped = wrap_media_part(part, tag="video", attrs={"path": media_path})
                image_size = None

        size_hint = ""
        if image_size:
            size_hint = f", original size {image_size[0]}x{image_size[1]}px"
        note = (
            " If you need to output coordinates, output relative coordinates first and "
            "compute absolute coordinates using the original image size; if you generate or "
            "edit images/videos via commands or scripts, read the result back immediately "
            "before continuing."
        )
        return ToolOk(
            output=wrapped,
            message=(
                f"Loaded {file_type.kind} file `{path}` "
                f"({file_type.mime_type}, {size} bytes{size_hint}).{note}"
            ),
        )

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if not params.path:
            return ToolError(
                message="File path cannot be empty.",
                brief="Empty file path",
            )

        try:
            p = kaos_path_from_user_input(params.path)
            if err := await self._validate_path(p):
                return err
            p = p.canonical()

            if not await p.exists():
                return ToolError(
                    message=f"`{params.path}` does not exist.",
                    brief="File not found",
                )
            if not await p.is_file():
                return ToolError(
                    message=f"`{params.path}` is not a file.",
                    brief="Invalid path",
                )

            header = await p.read_bytes(MEDIA_SNIFF_BYTES)
            file_type = detect_file_type(str(p), header=header)
        except Exception as e:
            # Separate stage from the media read below so the message says which
            # half failed (path probing vs. decoding/uploading).
            logger.warning(
                "ReadMediaFile path check failed: {path}: {error}", path=params.path, error=e
            )
            return ToolError(
                message=f"Failed to read {params.path}. Error: {e}",
                brief="Failed to read file",
            )

        if file_type.kind == "text":
            return ToolError(
                message=f"`{params.path}` is a text file. Use ReadFile to read text files.",
                brief="Unsupported file type",
            )
        if file_type.kind == "unknown":
            return ToolError(
                message=(
                    f"`{params.path}` seems not readable as an image or video file. "
                    "You may need to read it with proper shell commands, Python tools "
                    "or MCP tools if available. "
                    "If you read/operate it with Python, you MUST ensure that any "
                    "third-party packages are installed in a virtual environment (venv)."
                ),
                brief="File not readable",
            )

        if file_type.kind == "image" and "image_in" not in self._capabilities:
            return ToolError(
                message=(
                    "The current model does not support image input. "
                    "Tell the user to use a model with image input capability."
                ),
                brief="Unsupported media type",
            )
        if file_type.kind == "video" and "video_in" not in self._capabilities:
            return ToolError(
                message=(
                    "The current model does not support video input. "
                    "Tell the user to use a model with video input capability."
                ),
                brief="Unsupported media type",
            )

        try:
            return await self._read_media(p, file_type)
        except Exception as e:
            logger.warning("ReadMediaFile failed: {path}: {error}", path=params.path, error=e)
            return ToolError(
                message=f"Failed to read {params.path}. Error: {e}",
                brief="Failed to read file",
            )
