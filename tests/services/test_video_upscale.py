"""Unit tests for the shared post-download 1080p export helper."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gflow_cli.api.video import VideoResult, VideoStatus
from gflow_cli.errors import MediaDownloadError, UpscaleUnavailableError
from gflow_cli.services.video_upscale import upscale_downloaded_video

if TYPE_CHECKING:
    from pathlib import Path

_MP4_720P = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"7" * 64
_MP4_1080P = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"H" * 128


def _make_video_result(
    local_path: Path | None,
    *,
    media_id: str | None = "11111111-2222-3333-4444-555555555555",
    project_id: str | None = "proj-1",
) -> VideoResult:
    return VideoResult(
        status=VideoStatus(
            media_id=media_id or "",
            status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
        ),
        local_path=local_path,
        project_id=project_id,
    )


@pytest.mark.asyncio
async def test_upscale_downloaded_video_happy_path_replaces_720p_and_returns_to_project(
    tmp_path: Path,
) -> None:
    clip = tmp_path / "video_0.mp4"
    clip.write_bytes(_MP4_720P)
    result = _make_video_result(clip, media_id="m-1080", project_id="proj-abc")

    page = MagicMock()
    page.goto = AsyncMock()
    client = MagicMock(page=page)

    async def _fake_upsample(*, media_id: str, project_id: str, scale: str, out_path: Path) -> Path:
        assert media_id == "m-1080"
        assert project_id == "proj-abc"
        assert scale == "1080p"
        out_path.write_bytes(_MP4_1080P)
        return out_path

    client.upsample_video = AsyncMock(side_effect=_fake_upsample)

    updated, warning = await upscale_downloaded_video(client, result, project_id="proj-fallback")

    assert warning is None
    assert updated.local_path == clip
    assert clip.read_bytes() == _MP4_1080P
    assert not (tmp_path / "video_0.1080p.mp4").exists()
    page.goto.assert_awaited_once_with(
        "https://flow.google.com/project/proj-abc",
        wait_until="domcontentloaded",
        timeout=45_000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        UpscaleUnavailableError("1080p menu item disabled"),
        MediaDownloadError(detail="signed download timed out"),
    ],
)
async def test_upscale_downloaded_video_failure_preserves_720p_cleans_tmp_and_returns_to_project(
    tmp_path: Path, exc: Exception
) -> None:
    clip = tmp_path / "video_0.mp4"
    clip.write_bytes(_MP4_720P)
    result = _make_video_result(clip, media_id="m-fail-1", project_id="proj-xyz")

    page = MagicMock()
    page.goto = AsyncMock()
    client = MagicMock(page=page)

    async def _failing_upsample(*, out_path: Path, **_kwargs: Any) -> Path:
        # Simulate partial write before failure
        out_path.write_bytes(b"partial-corrupt")
        raise exc

    client.upsample_video = AsyncMock(side_effect=_failing_upsample)

    updated, warning = await upscale_downloaded_video(client, result, project_id="proj-xyz")

    assert updated.local_path == clip
    assert clip.read_bytes() == _MP4_720P
    assert not (tmp_path / "video_0.1080p.mp4").exists()
    assert warning is not None
    assert "m-fail-1" in warning
    assert "gflow video upscale m-fail-1 --scale 1080p --project proj-xyz" in warning
    page.goto.assert_awaited_once_with(
        "https://flow.google.com/project/proj-xyz",
        wait_until="domcontentloaded",
        timeout=45_000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_id", "project_id", "has_file"),
    [
        ("", "proj-1", True),
        ("m-1", None, True),
        ("m-1", "proj-1", False),
    ],
)
async def test_upscale_downloaded_video_missing_prereqs_skips_without_calling_upsample(
    tmp_path: Path,
    media_id: str,
    project_id: str | None,
    has_file: bool,
) -> None:
    clip = tmp_path / "video_0.mp4"
    if has_file:
        clip.write_bytes(_MP4_720P)
    result = _make_video_result(
        clip if has_file else None,
        media_id=media_id,
        project_id=project_id,
    )
    client = MagicMock()
    client.upsample_video = AsyncMock()

    updated, warning = await upscale_downloaded_video(client, result, project_id=project_id)

    assert updated is result
    assert warning is not None
    assert "skipped" in warning
    client.upsample_video.assert_not_awaited()
    if has_file:
        assert clip.read_bytes() == _MP4_720P


@pytest.mark.asyncio
async def test_upscale_downloaded_video_swallows_return_to_project_navigation_error(
    tmp_path: Path,
) -> None:
    clip = tmp_path / "video_0.mp4"
    clip.write_bytes(_MP4_720P)
    result = _make_video_result(clip, media_id="m-nav", project_id="proj-nav")

    page = MagicMock()
    page.goto = AsyncMock(side_effect=RuntimeError("frame detached"))
    client = MagicMock(page=page)

    async def _fake_upsample(*, out_path: Path, **_kwargs: Any) -> Path:
        out_path.write_bytes(_MP4_1080P)
        return out_path

    client.upsample_video = AsyncMock(side_effect=_fake_upsample)

    updated, warning = await upscale_downloaded_video(client, result, project_id="proj-nav")

    assert warning is None
    assert updated.local_path == clip
    assert clip.read_bytes() == _MP4_1080P
