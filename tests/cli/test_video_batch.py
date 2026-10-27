"""Tests for `gflow video batch --config` — single-session sequential i2v batch runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gflow_cli.api.video import VideoModel, VideoResult, VideoStarted, VideoStatus
from gflow_cli.cli import main as cli_main
from gflow_cli.errors import (
    AuthExpiredError,
    ContentPolicyError,
    InsufficientCreditsError,
    UiSelectorDriftError,
    WafRejectionError,
)
from gflow_cli.video_batch import VideoBatchConfig

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


def _make_fake_video_client(
    tmp_path: Path,
    *,
    side_effect: Any = None,
) -> MagicMock:
    inst = MagicMock()
    inst.__aenter__ = AsyncMock(return_value=inst)
    inst.__aexit__ = AsyncMock(return_value=None)
    inst.create_project = AsyncMock(return_value=MagicMock(project_id="proj-video-batch"))

    call_idx = {"n": 0}

    async def _default_gen(
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
        media_id = f"vid-media-{idx}"
        if on_started is not None:
            on_started(
                VideoStarted(
                    media_id=media_id,
                    project_id=project_id or "proj-video-batch",
                    flow_operation_id=f"op-{idx}",
                )
            )
        if side_effect is not None:
            res = side_effect(idx, req, project_id, out_dir)
            if isinstance(res, BaseException):
                raise res
            if res is not None:
                return res
        dl_dir = out_dir or tmp_path
        dl_dir.mkdir(parents=True, exist_ok=True)
        local_mp4 = dl_dir / f"{media_id}.mp4"
        local_mp4.write_bytes(_VALID_MP4)
        return VideoResult(
            status=VideoStatus(
                media_id=media_id,
                status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
            ),
            local_path=local_mp4,
            project_id=project_id or "proj-video-batch",
            flow_operation_id=f"op-{idx}",
        )

    inst.generate_video = AsyncMock(side_effect=_default_gen)
    return inst


class TestVideoBatchConfigValidation:
    """Pre-flight validation of `gflow video batch --config` JSON files."""

    def test_valid_config_with_prompt_and_prompt_file(self, tmp_path: Path) -> None:
        f1 = _write_frame(tmp_path / "frames" / "k1.jpg")
        _write_frame(tmp_path / "frames" / "k2.jpg")
        pfile = tmp_path / "prompts" / "v2.txt"
        pfile.parent.mkdir(parents=True, exist_ok=True)
        pfile.write_text("The cockroach speaks to the viewer.\n", encoding="utf-8")

        cfg_path = _write_video_config(
            tmp_path,
            {
                "profile": "meo-muop",
                "project": "proj-123",
                "videos": [
                    {
                        "start": str(f1),
                        "prompt": "Pan right slowly",
                        "model": "omni-flash",
                        "duration": 4,
                        "aspect": "9:16",
                        "output": str(tmp_path / "gen" / "v1.mp4"),
                    },
                    {
                        "start": "frames/k2.jpg",
                        "prompt_file": "prompts/v2.txt",
                        "model": "omni-flash",
                        "duration": 6,
                        "aspect": "16:9",
                        "resolution": "720p",
                        "output": "gen/v2.mp4",
                    },
                ],
            },
        )
        cfg = VideoBatchConfig.from_json_path(cfg_path)
        assert cfg.profile == "meo-muop"
        assert cfg.project == "proj-123"
        assert len(cfg.videos) == 2
        assert cfg.videos[0].prompt == "Pan right slowly"
        assert cfg.videos[0].duration == 4
        assert cfg.videos[1].prompt == "The cockroach speaks to the viewer."
        assert cfg.videos[1].duration == 6
        assert cfg.videos[1].aspect == "16:9"
        assert cfg.videos[1].resolution == "720p"

    def test_top_level_array_is_accepted(self, tmp_path: Path) -> None:
        f1 = _write_frame(tmp_path / "k1.png")
        cfg_path = _write_video_config(
            tmp_path,
            [{"start": str(f1), "prompt": "zoom in", "model": "omni-flash"}],
        )
        cfg = VideoBatchConfig.from_json_path(cfg_path)
        assert len(cfg.videos) == 1
        assert cfg.videos[0].model == "omni-flash"

    @pytest.mark.parametrize(
        ("row_override", "expected_match"),
        [
            ({"start": "missing.png"}, "does not exist"),
            ({"prompt": "a", "prompt_file": "b.txt"}, "cannot specify both"),
            ({"prompt": ""}, "length must be between"),
            ({"model": "unknown-model"}, "invalid"),
            ({"model": "veo-lite", "duration": 10}, "caps at 8s"),
            ({"model": "omni-flash", "duration": 5}, "duration"),
            ({"aspect": "4:3"}, "invalid"),
            ({"model": "veo-lite", "resolution": "720p"}, "only supported on omni-flash"),
            ({"extra_field": 1}, "unknown key"),
        ],
    )
    def test_invalid_row_raises_configuration_error_exit_11(
        self,
        tmp_path: Path,
        row_override: dict[str, Any],
        expected_match: str,
    ) -> None:
        f1 = _write_frame(tmp_path / "k1.png")
        base_row: dict[str, Any] = {
            "start": str(f1),
            "prompt": "motion prompt",
            "model": "omni-flash",
        }
        base_row.update(row_override)
        cfg_path = _write_video_config(tmp_path, {"videos": [base_row]})
        with patch("gflow_cli.video_batch.FlowApiClient") as client_cls:
            result = CliRunner().invoke(
                cli_main,
                ["video", "batch", "--config", str(cfg_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 11, result.output
        assert expected_match.lower() in " ".join(result.output.split()).lower()
        client_cls.assert_not_called()


class TestVideoBatchExecution:
    """Single-session execution, `--resume`, error semantics, and `--json`."""

    def test_three_videos_run_in_one_browser_session(self, tmp_path: Path) -> None:
        frames = [_write_frame(tmp_path / f"k{i}.png") for i in (1, 2, 3)]
        outputs = [tmp_path / "out" / f"v{i}.mp4" for i in (1, 2, 3)]
        cfg_path = _write_video_config(
            tmp_path,
            {
                "videos": [
                    {
                        "start": str(frames[i]),
                        "prompt": f"clip {i}",
                        "model": "omni-flash",
                        "duration": 4,
                        "aspect": "9:16",
                        "output": str(outputs[i]),
                    }
                    for i in range(3)
                ]
            },
        )
        fake_client = _make_fake_video_client(tmp_path)
        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
            patch("gflow_cli.cli_video._resolve_profile", return_value="meo-muop"),
            patch("gflow_cli.cli_video._make_provider_dir", return_value=tmp_path / "prof"),
        ):
            result = CliRunner().invoke(
                cli_main,
                ["video", "batch", "--config", str(cfg_path), "--jitter", "0"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        # Strictly ONE FlowApiClient session across all 3 clips.
        assert fake_client.__aenter__.call_count == 1
        assert fake_client.__aexit__.call_count == 1
        assert fake_client.create_project.call_count == 1
        assert fake_client.generate_video.call_count == 3
        for idx, out_file in enumerate(outputs):
            assert out_file.is_file()
            req = fake_client.generate_video.call_args_list[idx].kwargs["req"]
            assert req.mode == "i2v"
            assert req.model is VideoModel.OMNI_FLASH
            assert req.duration == 4
            assert req.start_image == frames[idx].resolve()
        assert "3/3 succeeded" in result.output

    def test_resume_skips_existing_valid_mp4_files(self, tmp_path: Path) -> None:
        f1 = _write_frame(tmp_path / "k1.png")
        f2 = _write_frame(tmp_path / "k2.png")
        out1 = tmp_path / "gen" / "v1.mp4"
        out2 = tmp_path / "gen" / "v2.mp4"
        out1.parent.mkdir(parents=True, exist_ok=True)
        out1.write_bytes(_VALID_MP4)  # already generated

        cfg_path = _write_video_config(
            tmp_path,
            {
                "videos": [
                    {"start": str(f1), "prompt": "clip 1", "output": str(out1)},
                    {"start": str(f2), "prompt": "clip 2", "output": str(out2)},
                ]
            },
        )
        fake_client = _make_fake_video_client(tmp_path)
        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
            patch("gflow_cli.cli_video._resolve_profile", return_value="meo-muop"),
            patch("gflow_cli.cli_video._make_provider_dir", return_value=tmp_path / "prof"),
        ):
            result = CliRunner().invoke(
                cli_main,
                ["video", "batch", "--config", str(cfg_path), "--resume", "--jitter", "0"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert fake_client.generate_video.call_count == 1
        assert fake_client.generate_video.call_args.kwargs["req"].prompt == "clip 2"
        assert "RESUMED" in result.output
        assert out2.is_file()

    def test_resume_when_all_done_does_not_open_browser(self, tmp_path: Path) -> None:
        f1 = _write_frame(tmp_path / "k1.png")
        out1 = tmp_path / "gen" / "v1.mp4"
        out1.parent.mkdir(parents=True, exist_ok=True)
        out1.write_bytes(_VALID_MP4)

        cfg_path = _write_video_config(
            tmp_path,
            {"videos": [{"start": str(f1), "prompt": "clip 1", "output": str(out1)}]},
        )
        fake_client = _make_fake_video_client(tmp_path)
        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
            patch("gflow_cli.cli_video._resolve_profile", return_value="meo-muop"),
            patch("gflow_cli.cli_video._make_provider_dir", return_value=tmp_path / "prof"),
        ):
            result = CliRunner().invoke(
                cli_main,
                ["video", "batch", "--config", str(cfg_path), "--resume"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert fake_client.__aenter__.call_count == 0
        assert fake_client.generate_video.call_count == 0

    @pytest.mark.parametrize(
        ("fatal_exc", "expected_exit"),
        [
            (AuthExpiredError(detail="session expired"), 3),
            (WafRejectionError(detail="reCAPTCHA 403"), 10),
            (UiSelectorDriftError(detail="create button missing"), 23),
            (InsufficientCreditsError(detail="0 credits remaining"), 37),
        ],
    )
    def test_fatal_error_halts_entire_batch_even_with_continue_on_error(
        self,
        tmp_path: Path,
        fatal_exc: Exception,
        expected_exit: int,
    ) -> None:
        frames = [_write_frame(tmp_path / f"k{i}.png") for i in (1, 2, 3)]
        cfg_path = _write_video_config(
            tmp_path,
            {
                "videos": [
                    {"start": str(frames[i]), "prompt": f"clip {i}", "model": "omni-flash"}
                    for i in range(3)
                ]
            },
        )

        def _fail_on_second(idx: int, *_args: Any) -> Any:
            if idx == 1:
                return fatal_exc
            return None

        fake_client = _make_fake_video_client(tmp_path, side_effect=_fail_on_second)
        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
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
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == expected_exit, result.output
        # Row 0 succeeded, row 1 hit fatal error, row 2 was NEVER submitted.
        assert fake_client.generate_video.call_count == 2
        assert "SKIPPED" in result.output

    def test_per_row_content_policy_error_continues_under_continue_on_error(
        self, tmp_path: Path
    ) -> None:
        frames = [_write_frame(tmp_path / f"k{i}.png") for i in (1, 2, 3)]
        cfg_path = _write_video_config(
            tmp_path,
            {
                "videos": [
                    {"start": str(frames[i]), "prompt": f"clip {i}", "model": "omni-flash"}
                    for i in range(3)
                ]
            },
        )

        def _policy_fail_middle(idx: int, *_args: Any) -> Any:
            if idx == 1:
                return ContentPolicyError(detail="safety filter")
            return None

        fake_client = _make_fake_video_client(tmp_path, side_effect=_policy_fail_middle)
        with (
            patch("gflow_cli.video_batch.FlowApiClient", return_value=fake_client),
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

        assert result.exit_code == 5, result.output
        # All 3 rows attempted (row 1 was NOT retried, row 2 still ran).
        assert fake_client.generate_video.call_count == 3
        payload = json.loads(result.output)
        assert payload["command"] == "video batch"
        assert payload["succeeded"] == 2
        assert payload["failed"] == 1
        assert [r["status"] for r in payload["results"]] == ["ok", "fail", "ok"]
