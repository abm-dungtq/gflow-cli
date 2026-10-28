"""Post-download 1080p export helper shared by CLI, video batch, and worker."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from gflow_cli.api.transports.migrated_composer import MIGRATED_PROJECT_URL
from gflow_cli.storage import is_cloud_path

if TYPE_CHECKING:
    from gflow_cli.api.video import VideoResult

logger = structlog.get_logger(__name__)


async def upscale_downloaded_video(
    client: Any, result: VideoResult, *, project_id: str | None
) -> tuple[VideoResult, str | None]:
    """Replace *result*'s downloaded 720p clip with Flow's free 1080p export."""
    pid, media_id, local_path = (
        result.project_id or project_id,
        result.status.media_id,
        result.local_path,
    )
    if (
        not pid
        or not media_id
        or not isinstance(local_path, Path)
        or is_cloud_path(local_path)
        or not local_path.is_file()
    ):
        return (
            result,
            "upscale to 1080p skipped: missing media_id, project_id, or local video file; "
            "the 720p clip was kept.",
        )

    tmp_path = local_path.with_name(f"{local_path.stem}.1080p{local_path.suffix or '.mp4'}")
    try:
        exported = await client.upsample_video(
            media_id=media_id, project_id=pid, scale="1080p", out_path=tmp_path
        )
        os.replace(exported if isinstance(exported, Path) else tmp_path, local_path)
        logger.info(
            "video.upscaled_after_download", media_id=media_id, bytes=local_path.stat().st_size
        )
        return result, None
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        logger.warning(
            "video.upscale_after_download_failed", media_id=media_id, project_id=pid, error=str(exc)
        )
        return (
            result,
            f"upscale to 1080p failed ({type(exc).__name__}: {exc}); the 720p clip was kept. "
            f"Retry with: gflow video upscale {media_id} --scale 1080p --project {pid}",
        )
    finally:
        if (page := getattr(client, "page", None)) is not None:
            try:
                url = MIGRATED_PROJECT_URL.format(project_id=pid)
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as nav_exc:  # noqa: BLE001
                logger.warning(
                    "video.upscale_return_to_project_failed", project_id=pid, error=str(nav_exc)
                )
