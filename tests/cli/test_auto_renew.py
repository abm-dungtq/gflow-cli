"""Tests for one-shot pre-submit session auto-renewal across CLI generation commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing
from click.testing import CliRunner

from gflow_cli.api.dto import GeneratedImage, ProjectInfo
from gflow_cli.api.image import Aspect, GenerateImageRequest, ImageRef, Model
from gflow_cli.api.video import (
    Aspect as VideoAspect,
)
from gflow_cli.api.video import (
    GenerateVideoRequest,
    Mode,
    VideoModel,
    VideoResult,
    VideoStarted,
    VideoStatus,
)
from gflow_cli.auth.auto_renew import SubmitTracker, run_with_auto_renew
from gflow_cli.auth.renew import RenewResult
from gflow_cli.auth.verification import FlowSessionOutcome, FlowSessionStatus
from gflow_cli.cli import main as cli_main
from gflow_cli.cli_image import _I2IParams, _run_i2i, _run_t2i
from gflow_cli.cli_run import _run_batch
from gflow_cli.cli_video import _generate_and_report
from gflow_cli.config import get_settings
from gflow_cli.errors import (
    AuthExpiredError,
    AuthMissingError,
    ProfileLockedError,
)
from gflow_cli.image_batch import BatchPromptItem, resolve_exit_code
from gflow_cli.video_batch import VideoBatchItem, run_video_batch

_VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_VALID_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def _auth_renew_result(*, renewed: bool = True) -> RenewResult:
    outcome = FlowSessionOutcome.AUTHENTICATED if renewed else FlowSessionOutcome.NO_SESSION
    return RenewResult(
        verdict=FlowSessionStatus(
            outcome=outcome,
            user_email="user@example.com" if renewed else None,
            source="chrome",
        ),
        landing=None,
        labs_stale=False,
    )


def _fake_generated_image() -> GeneratedImage:
    return GeneratedImage(
        media_name="11111111-2222-3333-4444-555555555555",
        workflow_id="wf-1",
        seed=123,
        prompt="cat",
        model_name_type="NARWHAL",
        aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
        fife_url="https://example.com/img.png",
        dimensions=(1024, 576),
    )


class TestAutoRenewHelper:
    """Unit tests for `gflow_cli.auth.auto_renew.run_with_auto_renew`."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc_cls", [AuthExpiredError, AuthMissingError])
    async def test_pre_submit_auth_error_renews_once_and_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exc_cls: type[AuthExpiredError | AuthMissingError],
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()
        attempts = 0

        async def _op(_tracker: SubmitTracker) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise exc_cls(detail="session expired before submit")
            return "ok"

        with (
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
            structlog.testing.capture_logs() as cap_logs,
        ):
            res = await run_with_auto_renew(tmp_path / "profile_default", _op, source="image.t2i")

        assert res == "ok"
        assert attempts == 2
        assert mock_renew.await_count == 1
        events = [e for e in cap_logs if e.get("event") == "auth.auto_renew"]
        assert len(events) == 1
        assert events[0]["renewed"] is True
        assert events[0]["source"] == "image.t2i"
        assert events[0]["error_type"] == exc_cls.__name__
        assert "cookie" not in str(events[0]).lower()
        assert "token" not in str(events[0]).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc_cls", "expected_exit"),
        [(AuthExpiredError, 3), (AuthMissingError, 8)],
    )
    async def test_renew_failure_reraises_original_error_and_exit_code(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exc_cls: type[AuthExpiredError | AuthMissingError],
        expected_exit: int,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()
        attempts = 0
        original = exc_cls(detail="signed out at session open")

        async def _op(_tracker: SubmitTracker) -> str:
            nonlocal attempts
            attempts += 1
            raise original

        with (
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=False)),
            ) as mock_renew,
            pytest.raises(exc_cls) as exc_info,
        ):
            await run_with_auto_renew(tmp_path / "profile_default", _op, source="image.t2i")

        assert exc_info.value is original
        assert resolve_exit_code(exc_info.value) == expected_exit
        assert attempts == 1
        assert mock_renew.await_count == 1

    @pytest.mark.asyncio
    async def test_renew_exception_reraises_original_auth_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()
        original = AuthExpiredError(detail="expired")

        async def _op(_tracker: SubmitTracker) -> str:
            raise original

        with (
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(side_effect=RuntimeError("browser crashed")),
            ) as mock_renew,
            pytest.raises(AuthExpiredError) as exc_info,
        ):
            await run_with_auto_renew(tmp_path / "profile_default", _op, source="video.t2v")

        assert exc_info.value is original
        assert mock_renew.await_count == 1

    @pytest.mark.asyncio
    async def test_renew_propagates_profile_locked_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()

        async def _op(_tracker: SubmitTracker) -> str:
            raise AuthExpiredError(detail="expired")

        with (
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(side_effect=ProfileLockedError(detail="locked")),
            ),
            pytest.raises(ProfileLockedError),
        ):
            await run_with_auto_renew(tmp_path / "profile_default", _op, source="image.t2i")

    @pytest.mark.asyncio
    async def test_second_auth_error_after_successful_renew_does_not_renew_again(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()
        attempts = 0

        async def _op(_tracker: SubmitTracker) -> str:
            nonlocal attempts
            attempts += 1
            raise AuthExpiredError(detail=f"attempt {attempts}")

        with (
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
            pytest.raises(AuthExpiredError, match="attempt 2"),
        ):
            await run_with_auto_renew(tmp_path / "profile_default", _op, source="image.t2i")

        assert attempts == 2
        assert mock_renew.await_count == 1

    @pytest.mark.asyncio
    async def test_post_submit_auth_error_never_renews(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()
        attempts = 0

        async def _op(tracker: SubmitTracker) -> str:
            nonlocal attempts
            attempts += 1
            tracker.mark_submitted()
            raise AuthExpiredError(detail="401 after generation submit")

        with (
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
            pytest.raises(AuthExpiredError, match="401 after generation submit"),
        ):
            await run_with_auto_renew(tmp_path / "profile_default", _op, source="image.t2i")

        assert attempts == 1
        assert mock_renew.await_count == 0

    @pytest.mark.asyncio
    async def test_auto_renew_disabled_via_env_never_renews(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "false")
        get_settings.cache_clear()
        attempts = 0

        async def _op(_tracker: SubmitTracker) -> str:
            nonlocal attempts
            attempts += 1
            raise AuthExpiredError(detail="expired pre-submit")

        with (
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
            pytest.raises(AuthExpiredError),
        ):
            await run_with_auto_renew(tmp_path / "profile_default", _op, source="image.t2i")

        assert attempts == 1
        assert mock_renew.await_count == 0
        get_settings.cache_clear()


class TestCliAutoRenewCommands:
    """Integration tests across `image t2i/i2i`, `video t2v/i2v/r2v`, `run`, `video batch`."""

    @pytest.mark.asyncio
    async def test_image_t2i_renews_once_on_aenter_auth_error_then_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()

        entries = 0

        def _make_client(**_kwargs: Any) -> MagicMock:
            nonlocal entries
            entries += 1
            inst = MagicMock()
            if entries == 1:
                inst.__aenter__ = AsyncMock(side_effect=AuthExpiredError(detail="expired"))
            else:
                inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            inst.create_project = AsyncMock(
                return_value=ProjectInfo(project_id="proj-1", title="t2i")
            )
            inst.generate_image = AsyncMock(return_value=_fake_generated_image())

            async def _dl(_img: GeneratedImage, target: Path) -> Path:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_VALID_PNG)
                return target

            inst.download_image = AsyncMock(side_effect=_dl)
            return inst

        with (
            patch("gflow_cli.cli_image.FlowApiClient", side_effect=_make_client),
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
        ):
            await _run_t2i(
                profile_name="default",
                profile_dir=tmp_path / "profile_default",
                headless=False,
                req=GenerateImageRequest(
                    prompt="cat",
                    aspect=Aspect.LANDSCAPE,
                    model=Model.from_cli("nano2"),
                ),
                count=1,
                out=tmp_path / "out",
                output_root=tmp_path / "out",
                as_json=True,
            )

        assert entries == 2
        assert mock_renew.await_count == 1

    @pytest.mark.asyncio
    async def test_image_t2i_post_submit_auth_error_does_not_renew(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()

        inst = MagicMock()
        inst.__aenter__ = AsyncMock(return_value=inst)
        inst.__aexit__ = AsyncMock(return_value=None)
        inst.create_project = AsyncMock(return_value=ProjectInfo(project_id="proj-1", title="t2i"))
        inst.generate_image = AsyncMock(
            side_effect=AuthExpiredError(detail="401 on batchGenerateImages")
        )

        with (
            patch("gflow_cli.cli_image.FlowApiClient", return_value=inst),
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
            pytest.raises(AuthExpiredError, match="401 on batchGenerateImages"),
        ):
            await _run_t2i(
                profile_name="default",
                profile_dir=tmp_path / "profile_default",
                headless=False,
                req=GenerateImageRequest(
                    prompt="cat",
                    aspect=Aspect.LANDSCAPE,
                    model=Model.from_cli("nano2"),
                ),
                count=1,
                out=tmp_path / "out",
                output_root=tmp_path / "out",
                as_json=True,
            )

        assert mock_renew.await_count == 0

    @pytest.mark.asyncio
    async def test_image_i2i_renews_once_on_pre_submit_auth_error_and_skips_post_submit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()

        ref_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        params = _I2IParams(
            prompt="stylize",
            classified_refs=[ImageRef(name=ref_id)],
            aspect=Aspect.LANDSCAPE,
            model=Model.from_cli("nano2"),
            reference_entities=(),
            reference_entity_names=(),
        )

        entries = 0

        def _make_client(**_kwargs: Any) -> MagicMock:
            nonlocal entries
            entries += 1
            inst = MagicMock()
            if entries == 1:
                inst.__aenter__ = AsyncMock(side_effect=AuthMissingError(detail="no cookies"))
            else:
                inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            inst.create_project = AsyncMock(
                return_value=ProjectInfo(project_id="proj-i2i", title="i2i")
            )
            inst.generate_image = AsyncMock(return_value=_fake_generated_image())

            async def _dl(_img: GeneratedImage, target: Path) -> Path:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_VALID_PNG)
                return target

            inst.download_image = AsyncMock(side_effect=_dl)
            return inst

        with (
            patch("gflow_cli.cli_image.FlowApiClient", side_effect=_make_client),
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
        ):
            await _run_i2i(
                profile_name="default",
                profile_dir=tmp_path / "profile_default",
                headless=False,
                params=params,
                count=1,
                out=tmp_path / "out",
                output_root=tmp_path / "out",
                as_json=True,
            )

        assert entries == 2
        assert mock_renew.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [Mode.T2V, Mode.I2V, Mode.R2V])
    async def test_video_commands_renew_once_pre_submit_and_never_post_submit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: Mode,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()

        frame = tmp_path / "frame.png"
        frame.write_bytes(_VALID_PNG)
        req = GenerateVideoRequest(
            prompt="slow pan",
            mode=mode,
            aspect=VideoAspect.LANDSCAPE,
            model=VideoModel.VEO_3_1_LITE,
            start_image=frame if mode == Mode.I2V else None,
            reference_images=(frame,) if mode == Mode.R2V else (),
        )

        # 1) Pre-submit (__aenter__) failure -> renews once and succeeds
        entries = 0

        def _make_pre_client(**_kwargs: Any) -> MagicMock:
            nonlocal entries
            entries += 1
            inst = MagicMock()
            if entries == 1:
                inst.__aenter__ = AsyncMock(side_effect=AuthExpiredError(detail="expired"))
            else:
                inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mp4 = tmp_path / f"out_{mode.value}.mp4"
            mp4.write_bytes(_VALID_MP4)
            inst.generate_video = AsyncMock(
                return_value=VideoResult(
                    status=VideoStatus(
                        media_id="vid-1",
                        status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
                    ),
                    local_path=mp4,
                    project_id="proj-v",
                    flow_operation_id="op-1",
                )
            )
            return inst

        with (
            patch("gflow_cli.cli_video.FlowApiClient", side_effect=_make_pre_client),
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew,
        ):
            await _generate_and_report(
                profile_name="default",
                profile_dir=tmp_path / "profile_default",
                request=req,
                out_dir=tmp_path,
                output_file=None,
                as_json=True,
                command=f"video {mode.value}",
            )

        assert entries == 2
        assert mock_renew.await_count == 1

        # 2) Post-submit (inside generate_video) failure -> does NOT renew
        post_inst = MagicMock()
        post_inst.__aenter__ = AsyncMock(return_value=post_inst)
        post_inst.__aexit__ = AsyncMock(return_value=None)

        async def _fail_after_started(**kwargs: Any) -> VideoResult:
            if (cb := kwargs.get("on_started")) is not None:
                cb(VideoStarted(media_id="vid-x", project_id="proj-v", flow_operation_id="op-x"))
            raise AuthExpiredError(detail="expired while polling video:status")

        post_inst.generate_video = AsyncMock(side_effect=_fail_after_started)

        with (
            patch("gflow_cli.cli_video.FlowApiClient", return_value=post_inst),
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew_post,
            pytest.raises(AuthExpiredError, match="expired while polling"),
        ):
            await _generate_and_report(
                profile_name="default",
                profile_dir=tmp_path / "profile_default",
                request=req,
                out_dir=tmp_path,
                output_file=None,
                as_json=True,
                command=f"video {mode.value}",
            )

        assert mock_renew_post.await_count == 0

    @pytest.mark.asyncio
    async def test_batch_run_and_video_batch_renew_at_most_once_per_batch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()

        # --- Part A: `gflow run` (`_run_batch`) session open renews once ---
        run_entries = 0

        def _make_run_client(**_kwargs: Any) -> MagicMock:
            nonlocal run_entries
            run_entries += 1
            inst = MagicMock()
            if run_entries == 1:
                inst.__aenter__ = AsyncMock(side_effect=AuthExpiredError(detail="batch open 401"))
            else:
                inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            inst.create_project = AsyncMock(
                return_value=ProjectInfo(project_id="proj-run", title="run")
            )
            inst.generate_image = AsyncMock(return_value=_fake_generated_image())

            async def _dl(_img: GeneratedImage, target: Path) -> Path:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_VALID_PNG)
                return target

            inst.download_image = AsyncMock(side_effect=_dl)
            return inst

        items = (
            BatchPromptItem(index=0, text="one", count=1, aspect_ratio="16:9", model="nano2"),
            BatchPromptItem(index=1, text="two", count=1, aspect_ratio="16:9", model="nano2"),
        )
        with (
            patch("gflow_cli.cli_run.FlowApiClient", side_effect=_make_run_client),
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
            ) as mock_renew_run,
        ):
            outcomes = await _run_batch(
                profile_dir=tmp_path / "profile_default",
                headless=False,
                transport=None,
                prompts=items,
                output_dir=tmp_path / "run_out",
                continue_on_error=True,
                profile_name="default",
            )

        assert len(outcomes) == 2
        assert all(o.status == "ok" for o in outcomes)
        assert run_entries == 2
        assert mock_renew_run.await_count == 1

        # --- Part B: `gflow video batch` (`run_video_batch`) session open vs row failure ---
        frame = tmp_path / "f.png"
        frame.write_bytes(_VALID_PNG)
        v_items = (
            VideoBatchItem(
                index=0,
                start=str(frame),
                prompt="clip 1",
                model="veo-lite",
                duration=8,
                aspect="16:9",
                output=str(tmp_path / "v1.mp4"),
            ),
            VideoBatchItem(
                index=1,
                start=str(frame),
                prompt="clip 2",
                model="veo-lite",
                duration=8,
                aspect="16:9",
                output=str(tmp_path / "v2.mp4"),
            ),
        )

        vb_entries = 0

        def _make_vb_client(**_kwargs: Any) -> MagicMock:
            nonlocal vb_entries
            vb_entries += 1
            inst = MagicMock()
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            if vb_entries == 1:
                inst.create_project = AsyncMock(
                    side_effect=AuthExpiredError(detail="create_project 401")
                )
            else:
                inst.create_project = AsyncMock(return_value=MagicMock(project_id="proj-vb"))
            # Row 0 fails mid-batch with AuthExpiredError -> must NOT trigger per-row renew!
            inst.generate_video = AsyncMock(
                side_effect=AuthExpiredError(detail="row 0 expired after submit")
            )
            return inst

        with patch(
            "gflow_cli.auth.auto_renew.renew_profile",
            new=AsyncMock(return_value=_auth_renew_result(renewed=True)),
        ) as mock_renew_vb:
            v_outcomes, proj_id = await run_video_batch(
                profile_dir=tmp_path / "profile_default",
                headless=False,
                videos=v_items,
                output_dir=tmp_path / "vb_out",
                continue_on_error=True,
                project_title="vb",
                profile_name="default",
                jitter_range=(0.0, 0.0),
                client_factory=_make_vb_client,
            )

        assert proj_id == "proj-vb"
        assert vb_entries == 2
        # Only 1 renew for create_project; row 0's AuthExpiredError did NOT trigger another renew!
        assert mock_renew_vb.await_count == 1
        assert v_outcomes[0].status == "fail"
        assert v_outcomes[1].status == "skipped"

    def test_cli_run_exits_with_auth_exit_code_when_renew_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_CLI_AUTO_RENEW", "true")
        get_settings.cache_clear()
        (tmp_path / "profile_default").mkdir(parents=True, exist_ok=True)

        cfg = tmp_path / "batch.json"
        cfg.write_text(
            json.dumps(
                {
                    "profile": "default",
                    "prompts": [{"text": "a cat"}],
                }
            ),
            encoding="utf-8",
        )

        inst = MagicMock()
        inst.__aenter__ = AsyncMock(side_effect=AuthExpiredError(detail="session expired"))
        inst.__aexit__ = AsyncMock(return_value=None)

        runner = CliRunner()
        with (
            patch("gflow_cli.cli_run.FlowApiClient", return_value=inst),
            patch(
                "gflow_cli.auth.auto_renew.renew_profile",
                new=AsyncMock(return_value=_auth_renew_result(renewed=False)),
            ) as mock_renew,
        ):
            res = runner.invoke(cli_main, ["run", "--config", str(cfg)])

        assert res.exit_code == 3
        assert mock_renew.await_count == 1
