"""Tests for automatic stranded-clip recovery in `gflow video batch`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gflow_cli.api.transports.migrated_recover import RecoveredClip
from gflow_cli.api.video import VideoResult, VideoStarted, VideoStatus
from gflow_cli.cli import main as cli_main
from gflow_cli.errors import (
    ContentPolicyError,
    MediaDownloadError,
    TransportTimeoutError,
    WireFormatError,
)

if TYPE_CHECKING:
    from pathlib import Path

_VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_VALID_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def _write_frame(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_VALID_PNG)
    return path


def _write_video_config(tmp_path: Path, payload: Any, name: str = "videos.json") -> Path:
    cfg = tmp_path / name
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    return cfg


def _make_batch_client(
    tmp_path: Path,
    *,
    row_handler: Any,
) -> MagicMock:
    inst = MagicMock()
    inst.__aenter__ = AsyncMock(return_value=inst)
    inst.__aexit__ = AsyncMock(return_value=None)
    inst.create_project = AsyncMock(return_value=MagicMock(project_id="proj-batch-recover"))
    inst.page = MagicMock()
    inst.page.goto = AsyncMock(return_value=None)

    call_idx = {"n": 0}

    async def _gen(
        *,
        req: Any,
        project_id: str | None = None,
        out_dir: Path | None = None,
        download: bool = True,
        on_started: Any = None,
        **_kwargs: Any,
    ) -> VideoResult:
        idx = call_idx["n"]
        call_idx["n"] += 1
        return await row_handler(
            idx=idx,
            req=req,
            project_id=project_id or "proj-batch-recover",
            out_dir=out_dir or tmp_path,
            download=download,
            on_started=on_started,
        )

    inst.generate_video = AsyncMock(side_effect=_gen)
    return inst


class TestVideoBatchStrandedClipRecovery:
    """Automatic one-shot `recover_clip` when a clip finished with `media_id`."""

    @pytest.mark.parametrize(
        "failure_cls",
        [
            lambda mid: WireFormatError(
                detail=(
                    "migrated host: the generation finished but no signed media URL was "
                    "observed within the 20s grace"
                ),
                route="batchexecute:as29s",
            ),
            lambda mid: MediaDownloadError(
                media_id=mid,
                detail="migrated host: signed media URL returned HTTP 403",
                status=403,
                route="flow-content.google",
            ),
        ],
    )
    def test_recover_clip_success_marks_row_ok_relocates_file_and_runs_next_row(
        self,
        tmp_path: Path,
        failure_cls: Any,
    ) -> None:
        f0 = _write_frame(tmp_path / "k0.png")
        f1 = _write_frame(tmp_path / "k1.png")
        out0 = tmp_path / "gen" / "clip0.mp4"
        out1 = tmp_path / "gen" / "clip1.mp4"
        cfg_path = _write_video_config(
            tmp_path,
            {
                "project": "proj-batch-recover",
                "videos": [
                    {"start": str(f0), "prompt": "clip 0", "output": str(out0)},
                    {"start": str(f1), "prompt": "clip 1", "output": str(out1)},
                ],
            },
        )
        stranded_id = "25bf9733-5cbf-4052-9968-d465f084fe2c"

        async def _row_handler(
            *,
            idx: int,
            project_id: str,
            out_dir: Path,
            on_started: Any,
            **_kw: Any,
        ) -> VideoResult:
            if idx == 0:
                on_started(
                    VideoStarted(
                        media_id=stranded_id,
                        project_id=project_id,
                        flow_operation_id="op-0",
                        workflow_id="wf-0",
                    )
                )
                raise failure_cls(stranded_id)
            media_id = "normal-media-1"
            on_started(
                VideoStarted(
                    media_id=media_id,
                    project_id=project_id,
                    flow_operation_id="op-1",
                )
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / f"{media_id}.mp4"
            p.write_bytes(_VALID_MP4)
            return VideoResult(
                status=VideoStatus(
                    media_id=media_id,
                    status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
                ),
                local_path=p,
                project_id=project_id,
                flow_operation_id="op-1",
            )

        async def _fake_recover(
            _page: Any,
            *,
            project_id: str,
            media_id: str,
            out_dir: Path,
        ) -> RecoveredClip:
            out_dir.mkdir(parents=True, exist_ok=True)
            raw_path = out_dir / f"{media_id}.mp4"
            raw_path.write_bytes(_VALID_MP4)
            return RecoveredClip(
                media_id=media_id,
                workflow_id="wf-recovered-0",
                path=raw_path,
                bytes=len(_VALID_MP4),
            )

        fake_client = _make_batch_client(tmp_path, row_handler=_row_handler)
        mock_recover = AsyncMock(side_effect=_fake_recover)
        mock_recorder = MagicMock()

        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
            patch("gflow_cli.video_batch.recover_clip", mock_recover),
            patch("gflow_cli.cli_video.OperationRecorder.open", return_value=mock_recorder),
            patch("gflow_cli.cli_video._resolve_profile", return_value="meo-muop"),
            patch("gflow_cli.cli_video._make_provider_dir", return_value=tmp_path / "prof"),
        ):
            result = CliRunner().invoke(
                cli_main,
                ["video", "batch", "--config", str(cfg_path), "--jitter", "0", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert fake_client.generate_video.call_count == 2
        assert mock_recover.call_count == 1
        assert mock_recover.call_args.kwargs == {
            "project_id": "proj-batch-recover",
            "media_id": stranded_id,
            "out_dir": out0.parent,
        }
        # Returned page to the project editor before row 1 ran.
        fake_client.page.goto.assert_awaited_once_with(
            "https://flow.google.com/project/proj-batch-recover",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        # Relocated from <media_id>.mp4 to the row's target output.
        assert out0.is_file()
        assert out0.read_bytes() == _VALID_MP4
        assert not (out0.parent / f"{stranded_id}.mp4").exists()
        assert out1.is_file()

        # Both rows recorded as completed videos; no failed operation recorded.
        assert mock_recorder.record_completed_video.call_count == 2
        assert mock_recorder.record_failed_operation.call_count == 0

        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["exit_code"] == 0
        assert payload["succeeded"] == 2
        assert payload["failed"] == 0
        row0, row1 = payload["results"]
        assert row0["status"] == "ok"
        assert row0["exit_code"] == 0
        assert row0["media_id"] == stranded_id
        assert row0["output"] == str(out0)
        assert row0["recovered"] is True
        # Normal row JSON is unchanged (no `recovered` key injected).
        assert row1["status"] == "ok"
        assert "recovered" not in row1

    def test_recover_clip_failure_keeps_original_error_and_appends_download_hint(
        self, tmp_path: Path
    ) -> None:
        f0 = _write_frame(tmp_path / "k0.png")
        f1 = _write_frame(tmp_path / "k1.png")
        out0 = tmp_path / "gen" / "clip0.mp4"
        out1 = tmp_path / "gen" / "clip1.mp4"
        cfg_path = _write_video_config(
            tmp_path,
            {
                "project": "proj-batch-recover",
                "videos": [
                    {"start": str(f0), "prompt": "clip 0", "output": str(out0)},
                    {"start": str(f1), "prompt": "clip 1", "output": str(out1)},
                ],
            },
        )
        stranded_id = "25bf9733-5cbf-4052-9968-d465f084fe2c"

        async def _row_handler(
            *,
            idx: int,
            project_id: str,
            out_dir: Path,
            on_started: Any,
            **_kw: Any,
        ) -> VideoResult:
            if idx == 0:
                on_started(
                    VideoStarted(
                        media_id=stranded_id,
                        project_id=project_id,
                        flow_operation_id="op-0",
                    )
                )
                raise WireFormatError(
                    detail=(
                        "migrated host: the generation finished but no signed media URL was "
                        "observed within the 20s grace"
                    ),
                    route="batchexecute:as29s",
                )
            media_id = "normal-media-1"
            on_started(VideoStarted(media_id=media_id, project_id=project_id))
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / f"{media_id}.mp4"
            p.write_bytes(_VALID_MP4)
            return VideoResult(
                status=VideoStatus(
                    media_id=media_id,
                    status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
                ),
                local_path=p,
                project_id=project_id,
            )

        fake_client = _make_batch_client(tmp_path, row_handler=_row_handler)
        mock_recover = AsyncMock(
            side_effect=WireFormatError(
                detail="no signed media URL within 45s",
                route="batchexecute:as29s",
            )
        )

        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
            patch("gflow_cli.video_batch.recover_clip", mock_recover),
            patch("gflow_cli.cli_video._resolve_profile", return_value="meo-muop"),
            patch("gflow_cli.cli_video._make_provider_dir", return_value=tmp_path / "prof"),
        ):
            result = CliRunner().invoke(
                cli_main,
                [
                    "video",
                    "batch",
                    "--config",
                    str(cfg_path),
                    "--continue-on-error",
                    "--jitter",
                    "0",
                    "--json",
                ],
                catch_exceptions=False,
            )

        # Exit code is 7 (original WireFormatError), batch did not abort before row 1.
        assert result.exit_code == 7, result.output
        assert fake_client.generate_video.call_count == 2
        assert mock_recover.call_count == 1
        assert out1.is_file()
        assert "video_batch.recover_failed" in result.stderr

        payload = json.loads(result.stdout)
        row0, row1 = payload["results"]
        assert row0["status"] == "fail"
        assert row0["exit_code"] == 7
        assert row0["media_id"] == stranded_id
        assert "20s grace" in row0["error"]
        assert f"gflow data download {stranded_id}" in row0["error"]
        assert "recovered" not in row0
        assert row1["status"] == "ok"

    def test_no_media_id_does_not_call_recover_clip_and_never_retries_submit(
        self, tmp_path: Path
    ) -> None:
        f0 = _write_frame(tmp_path / "k0.png")
        f1 = _write_frame(tmp_path / "k1.png")
        cfg_path = _write_video_config(
            tmp_path,
            {
                "videos": [
                    {"start": str(f0), "prompt": "clip 0"},
                    {"start": str(f1), "prompt": "clip 1"},
                ],
            },
        )

        async def _row_handler(
            *,
            idx: int,
            project_id: str,
            out_dir: Path,
            on_started: Any,
            **_kw: Any,
        ) -> VideoResult:
            if idx == 0:
                # No on_started callback fired (e.g. eb1hJf submit reply body unreadable
                # or WireFormatError before media_id is known).
                raise WireFormatError(
                    detail="migrated host: eb1hJf submit reply body unreadable",
                    route="batchexecute:eb1hJf",
                )
            media_id = "normal-media-1"
            on_started(VideoStarted(media_id=media_id, project_id=project_id))
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / f"{media_id}.mp4"
            p.write_bytes(_VALID_MP4)
            return VideoResult(
                status=VideoStatus(
                    media_id=media_id,
                    status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
                ),
                local_path=p,
                project_id=project_id,
            )

        fake_client = _make_batch_client(tmp_path, row_handler=_row_handler)
        mock_recover = AsyncMock()

        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
            patch("gflow_cli.video_batch.recover_clip", mock_recover),
            patch("gflow_cli.cli_video._resolve_profile", return_value="meo-muop"),
            patch("gflow_cli.cli_video._make_provider_dir", return_value=tmp_path / "prof"),
        ):
            result = CliRunner().invoke(
                cli_main,
                [
                    "video",
                    "batch",
                    "--config",
                    str(cfg_path),
                    "--continue-on-error",
                    "--jitter",
                    "0",
                    "--json",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 7, result.output
        assert fake_client.generate_video.call_count == 2
        mock_recover.assert_not_called()

    @pytest.mark.parametrize(
        "other_exc",
        [
            ContentPolicyError(detail="safety filter triggered"),
            TransportTimeoutError(
                detail="migrated host: generation wf-0 was not terminal within the poll timeout",
                retryable=False,
            ),
        ],
    )
    def test_other_error_types_even_with_media_id_do_not_call_recover_clip(
        self,
        tmp_path: Path,
        other_exc: Exception,
    ) -> None:
        f0 = _write_frame(tmp_path / "k0.png")
        f1 = _write_frame(tmp_path / "k1.png")
        cfg_path = _write_video_config(
            tmp_path,
            {
                "videos": [
                    {"start": str(f0), "prompt": "clip 0"},
                    {"start": str(f1), "prompt": "clip 1"},
                ],
            },
        )

        async def _row_handler(
            *,
            idx: int,
            project_id: str,
            out_dir: Path,
            on_started: Any,
            **_kw: Any,
        ) -> VideoResult:
            on_started(VideoStarted(media_id=f"media-{idx}", project_id=project_id))
            if idx == 0:
                raise other_exc
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / f"media-{idx}.mp4"
            p.write_bytes(_VALID_MP4)
            return VideoResult(
                status=VideoStatus(
                    media_id=f"media-{idx}",
                    status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
                ),
                local_path=p,
                project_id=project_id,
            )

        fake_client = _make_batch_client(tmp_path, row_handler=_row_handler)
        mock_recover = AsyncMock()

        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
            patch("gflow_cli.video_batch.recover_clip", mock_recover),
            patch("gflow_cli.cli_video._resolve_profile", return_value="meo-muop"),
            patch("gflow_cli.cli_video._make_provider_dir", return_value=tmp_path / "prof"),
        ):
            result = CliRunner().invoke(
                cli_main,
                [
                    "video",
                    "batch",
                    "--config",
                    str(cfg_path),
                    "--continue-on-error",
                    "--jitter",
                    "0",
                    "--json",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        # Exactly 1 generate_video call per row (2 rows total), zero recover_clip calls.
        assert fake_client.generate_video.call_count == 2
        mock_recover.assert_not_called()
