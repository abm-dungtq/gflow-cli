"""Sequential single-session video batch runner for ``gflow video batch --config``.

Runs multiple image-to-video (``i2v``) generations sequentially through a single
:class:`~gflow_cli.api.client.FlowApiClient` browser session and shared Flow
project, avoiding repeated Chrome startups and project creations across clips.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from rich.console import Console
from rich.table import Table

from gflow_cli._cli_helpers import _FLOW_ID_RE, safe_path_text
from gflow_cli.api.client import FlowApiClient, validate_image_file
from gflow_cli.api.video import (
    Aspect,
    GenerateVideoRequest,
    Mode,
    VideoModel,
    VideoResult,
    VideoStarted,
    validate_duration_for_model,
)
from gflow_cli.auth.auto_renew import (
    current_tracker,
    mark_current_submitted,
    run_with_auto_renew,
)
from gflow_cli.config import get_settings, parse_jitter_range
from gflow_cli.data.models import OperationKind
from gflow_cli.data.recorder import OperationRecorder, record_failed_operation_safe
from gflow_cli.errors import (
    AuthExpiredError,
    AuthMissingError,
    BrowserEngineUnavailableError,
    BrowserSessionClosedError,
    ConfigurationError,
    DataStoreError,
    FlowAccessUnavailableError,
    FlowAccountChooserError,
    FlowAgentUiError,
    FlowHostMigratedError,
    GFlowError,
    InsufficientCreditsError,
    ModelModeIncompatibilityError,
    RateLimitError,
    UiModeUnavailableError,
    UiSelectorDriftError,
    VideoModelSelectionError,
    WafRejectionError,
)
from gflow_cli.image_batch import (
    MAX_PROMPT_FILE_BYTES,
    MAX_PROMPTS,
    MAX_TEXT_LEN,
    MIN_PROMPTS,
    MIN_TEXT_LEN,
    resolve_exit_code,
    safe_prompt_preview,
    safe_terminal_text,
)
from gflow_cli.paths import looks_like_video
from gflow_cli.storage import cloud_info_from_path

if TYPE_CHECKING:
    from collections.abc import Callable

console = Console()
logger = structlog.get_logger(__name__)

# Default inter-submission jitter (seconds) for video batches — wider than image
# batches (0.5-1.5s) because video submissions are heavier and more rate-sensitive.
VIDEO_BATCH_JITTER_MIN_SECONDS: float = 2.0
VIDEO_BATCH_JITTER_MAX_SECONDS: float = 5.0

_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "profile",
        "project",
        "output_dir",
        "model",
        "duration",
        "aspect",
        "aspect_ratio",
        "resolution",
        "videos",
        "clips",
    }
)

_ALLOWED_VIDEO_ITEM_KEYS: frozenset[str] = frozenset(
    {
        "start",
        "initial_frame",
        "end",
        "end_frame",
        "prompt",
        "prompt_file",
        "model",
        "duration",
        "aspect",
        "aspect_ratio",
        "resolution",
        "output",
    }
)

_ALLOWED_VIDEO_ASPECTS: tuple[str, ...] = ("9:16", "16:9")
_ALLOWED_VIDEO_RESOLUTIONS: frozenset[str] = frozenset({"360p", "720p"})
DEFAULT_VIDEO_BATCH_MODEL: str = "omni-flash"

# Fatal session/lane/account errors that MUST halt the entire video batch
# immediately even when --continue-on-error is active. Continuing after these
# would either spin against a dead browser/session, burn WAF heat, or fail every
# remaining clip identically.
_FATAL_VIDEO_BATCH_ERRORS: tuple[type[GFlowError], ...] = (
    AuthExpiredError,
    AuthMissingError,
    RateLimitError,
    WafRejectionError,
    ConfigurationError,
    BrowserSessionClosedError,
    DataStoreError,
    ModelModeIncompatibilityError,
    VideoModelSelectionError,
    UiSelectorDriftError,
    BrowserEngineUnavailableError,
    FlowAgentUiError,
    UiModeUnavailableError,
    FlowHostMigratedError,
    InsufficientCreditsError,
    FlowAccountChooserError,
    FlowAccessUnavailableError,
)


def is_fatal_video_batch_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a non-retryable session/lane error that stops the batch."""
    return isinstance(exc, _FATAL_VIDEO_BATCH_ERRORS)


def resolve_video_batch_jitter_range(spec: str | None) -> tuple[float, float]:
    """Resolve inter-submission jitter range for ``gflow video batch``.

    Precedence: explicit ``--jitter`` flag > ``GFLOW_CLI_JITTER_RANGE`` setting >
    the video-batch default ``(2.0, 5.0)`` seconds.
    """
    if spec is None:
        spec = get_settings().jitter_range
    if spec is None:
        return (VIDEO_BATCH_JITTER_MIN_SECONDS, VIDEO_BATCH_JITTER_MAX_SECONDS)
    try:
        return parse_jitter_range(spec)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from None


@dataclass(frozen=True)
class VideoBatchItem:
    """One validated video generation entry in a batch config."""

    start: str
    prompt: str
    model: str = DEFAULT_VIDEO_BATCH_MODEL
    duration: int | None = None
    aspect: str = "9:16"
    resolution: str | None = None
    end: str | None = None
    output: str | None = None
    index: int = 0


@dataclass
class VideoBatchOutcome:
    """Per-row execution outcome for ``gflow video batch``."""

    index: int
    item: VideoBatchItem
    status: str  # "ok" | "resumed" | "fail" | "skipped"
    saved_path: Path | None = None
    media_id: str | None = None
    project_id: str | None = None
    error: str | None = None
    exit_code: int = 0


def _resolve_input_file(raw_path: str, *, label: str, base_dir: Path) -> Path:
    """Resolve a local file path relative to ``base_dir`` (with CWD fallback)."""
    raw = Path(raw_path).expanduser()
    if raw.is_absolute():
        target = raw
    elif (base_dir / raw).exists():
        target = base_dir / raw
    elif raw.exists():
        target = raw
    else:
        target = base_dir / raw
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        msg = f"{label} {raw_path!r} does not exist (relative to {base_dir})."
        raise ConfigurationError(msg) from None
    if not resolved.is_file():
        msg = f"{label} {raw_path!r} is not a file."
        raise ConfigurationError(msg)
    return resolved


def _validate_frame_file(raw_path: str, *, label: str, base_dir: Path) -> str:
    """Resolve and validate an image frame file before any browser starts."""
    resolved = _resolve_input_file(raw_path, label=label, base_dir=base_dir)
    try:
        asyncio.run(validate_image_file(resolved))
    except (ValueError, OSError) as exc:
        msg = f"{label} {raw_path!r}: {exc}"
        raise ConfigurationError(msg) from None
    return str(resolved)


def _read_single_prompt_file(raw_path: str, *, label: str, base_dir: Path) -> str:
    """Read a UTF-8 prompt file for one video row."""
    resolved = _resolve_input_file(raw_path, label=label, base_dir=base_dir)
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        msg = f"{label} {raw_path!r}: cannot stat file: {exc}"
        raise ConfigurationError(msg) from None
    if size > MAX_PROMPT_FILE_BYTES:
        msg = f"{label} {raw_path!r}: file must be at most 512 KiB."
        raise ConfigurationError(msg)
    try:
        text = resolved.read_text(encoding="utf-8").removeprefix("\ufeff").strip()
    except UnicodeDecodeError as exc:
        msg = f"{label} {raw_path!r}: file must be valid UTF-8."
        raise ConfigurationError(msg) from exc
    except OSError as exc:
        msg = f"{label} {raw_path!r}: failed to read file: {exc}"
        raise ConfigurationError(msg) from exc
    if not (MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN):
        msg = (
            f"{label} {raw_path!r}: prompt length must be between {MIN_TEXT_LEN} "
            f"and {MAX_TEXT_LEN} characters (got {len(text)})."
        )
        raise ConfigurationError(msg)
    return text


def _resolve_video_output_path(raw_output: str, *, idx: int, base_dir: Path) -> str:
    """Resolve a row's ``output`` .mp4 path and reject existing directories."""
    p = Path(raw_output).expanduser()
    if not p.is_absolute():
        if (base_dir / p).exists() or (p.parent != Path() and (base_dir / p.parent).exists()):
            p = base_dir / p
        elif p.exists() or (p.parent != Path() and p.parent.exists()):
            pass
        else:
            p = base_dir / p
    if p.exists() and p.is_dir():
        msg = f"videos[{idx}].output {raw_output!r} is an existing directory."
        raise ConfigurationError(msg)
    return str(p)


def _parse_prompt_from_row(row: dict[str, Any], *, idx: int, base_dir: Path) -> str:
    """Extract and validate ``prompt`` or ``prompt_file`` from ``videos[idx]``."""
    prompt_raw = row.get("prompt")
    prompt_file_raw = row.get("prompt_file")
    if prompt_raw is not None and prompt_file_raw is not None:
        msg = f"videos[{idx}] cannot specify both 'prompt' and 'prompt_file'."
        raise ConfigurationError(msg)
    if prompt_file_raw is not None:
        if not isinstance(prompt_file_raw, str) or not prompt_file_raw.strip():
            msg = f"videos[{idx}].prompt_file must be a non-empty string."
            raise ConfigurationError(msg)
        return _read_single_prompt_file(
            prompt_file_raw,
            label=f"videos[{idx}].prompt_file",
            base_dir=base_dir,
        )
    if not isinstance(prompt_raw, str):
        msg = f"videos[{idx}] requires 'prompt' or 'prompt_file' ( must be a string)."
        raise ConfigurationError(msg)
    prompt = prompt_raw.strip()
    if not (MIN_TEXT_LEN <= len(prompt) <= MAX_TEXT_LEN):
        msg = (
            f"videos[{idx}].prompt length must be between {MIN_TEXT_LEN} "
            f"and {MAX_TEXT_LEN} (got {len(prompt)})."
        )
        raise ConfigurationError(msg)
    return prompt


def _parse_frame_from_row(
    row: dict[str, Any],
    *,
    primary_key: str,
    alias_key: str,
    idx: int,
    base_dir: Path,
    required: bool,
) -> str | None:
    """Extract and validate ``start``/``initial_frame`` or ``end``/``end_frame``."""
    primary = row.get(primary_key)
    alias = row.get(alias_key)
    if primary is not None and alias is not None:
        msg = f"videos[{idx}] cannot specify both {primary_key!r} and {alias_key!r}."
        raise ConfigurationError(msg)
    field_name = alias_key if alias is not None else primary_key
    val = alias if alias is not None else primary
    if val is None:
        if required:
            msg = f"videos[{idx}].{primary_key} is required."
            raise ConfigurationError(msg)
        return None
    if not isinstance(val, str) or not val.strip():
        msg = f"videos[{idx}].{field_name} must be a non-empty string."
        raise ConfigurationError(msg)
    return _validate_frame_file(val, label=f"videos[{idx}].{field_name}", base_dir=base_dir)


def parse_video_batch_item_dict(
    row: dict[str, Any],
    idx: int,
    *,
    base_dir: Path,
    default_model: str = DEFAULT_VIDEO_BATCH_MODEL,
    default_duration: int | None = None,
    default_aspect: str = "9:16",
    default_resolution: str | None = None,
) -> VideoBatchItem:
    """Validate one dictionary entry from ``videos`` into a :class:`VideoBatchItem`."""
    unknown = set(row) - _ALLOWED_VIDEO_ITEM_KEYS
    if unknown:
        msg = (
            f"videos[{idx}] has unknown key(s) {sorted(unknown)!r}. "
            f"Valid: {sorted(_ALLOWED_VIDEO_ITEM_KEYS)!r}."
        )
        raise ConfigurationError(msg)

    start = _parse_frame_from_row(
        row,
        primary_key="start",
        alias_key="initial_frame",
        idx=idx,
        base_dir=base_dir,
        required=True,
    )
    assert start is not None
    end = _parse_frame_from_row(
        row,
        primary_key="end",
        alias_key="end_frame",
        idx=idx,
        base_dir=base_dir,
        required=False,
    )
    prompt = _parse_prompt_from_row(row, idx=idx, base_dir=base_dir)

    model_raw = row.get("model", default_model)
    if not isinstance(model_raw, str) or not model_raw.strip():
        msg = f"videos[{idx}].model must be a non-empty string."
        raise ConfigurationError(msg)
    try:
        resolved_model = VideoModel.from_cli(model_raw)
    except ValueError as exc:
        msg = f"videos[{idx}].model {model_raw!r} is invalid: {exc}"
        raise ConfigurationError(msg) from None
    assert resolved_model is not None

    duration_raw = row.get("duration", default_duration)
    if duration_raw is not None:
        if isinstance(duration_raw, bool) or not isinstance(duration_raw, int):
            msg = f"videos[{idx}].duration must be an integer (4, 6, 8, or 10)."
            raise ConfigurationError(msg)
        try:
            validate_duration_for_model(resolved_model, duration_raw)
        except ValueError as exc:
            msg = f"videos[{idx}].duration: {exc}"
            raise ConfigurationError(msg) from None

    if "aspect" in row and "aspect_ratio" in row:
        msg = f"videos[{idx}] cannot specify both 'aspect' and 'aspect_ratio'."
        raise ConfigurationError(msg)
    aspect_raw = row.get("aspect", row.get("aspect_ratio", default_aspect))
    if not isinstance(aspect_raw, str) or aspect_raw not in _ALLOWED_VIDEO_ASPECTS:
        msg = (
            f"videos[{idx}].aspect {aspect_raw!r} is invalid. "
            f"Valid: {list(_ALLOWED_VIDEO_ASPECTS)!r}."
        )
        raise ConfigurationError(msg)

    resolution_raw = row.get("resolution", default_resolution)
    if resolution_raw is not None:
        if not isinstance(resolution_raw, str) or resolution_raw not in _ALLOWED_VIDEO_RESOLUTIONS:
            msg = (
                f"videos[{idx}].resolution {resolution_raw!r} is invalid. "
                f"Valid: {sorted(_ALLOWED_VIDEO_RESOLUTIONS)!r}."
            )
            raise ConfigurationError(msg)
        if resolved_model is not VideoModel.OMNI_FLASH:
            msg = (
                f"videos[{idx}].resolution is only supported on omni-flash "
                f"(row model is {model_raw!r})."
            )
            raise ConfigurationError(msg)

    output_raw = row.get("output")
    resolved_output: str | None = None
    if output_raw is not None:
        if not isinstance(output_raw, str) or not output_raw.strip():
            msg = f"videos[{idx}].output must be a non-empty string."
            raise ConfigurationError(msg)
        resolved_output = _resolve_video_output_path(output_raw, idx=idx, base_dir=base_dir)

    return VideoBatchItem(
        start=start,
        end=end,
        prompt=prompt,
        model=model_raw,
        duration=duration_raw,
        aspect=aspect_raw,
        resolution=resolution_raw,
        output=resolved_output,
        index=idx,
    )


@dataclass(frozen=True)
class VideoBatchConfig:
    """Parsed and validated ``gflow video batch --config`` configuration."""

    videos: tuple[VideoBatchItem, ...]
    profile: str | None = None
    project: str | None = None
    output_dir: str | None = None

    @classmethod
    def from_json_path(cls, path: Path) -> VideoBatchConfig:
        """Load and validate a video batch JSON config file."""
        if not path.exists():
            msg = f"Config file not found: {path}"
            raise ConfigurationError(msg)
        if path.is_dir():
            msg = f"Config path is a directory, expected a JSON file: {path}"
            raise ConfigurationError(msg)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"Failed to read {path}: {exc}"
            raise ConfigurationError(msg) from exc
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            msg = f"Failed to parse {path}: line {exc.lineno}:{exc.colno} {exc.msg}"
            raise ConfigurationError(msg) from exc
        base_dir = path.parent
        if isinstance(data, list):
            return cls._from_dict({"videos": data}, base_dir=base_dir)
        if not isinstance(data, dict):
            msg = f"Config root must be a JSON object or array, got {type(data).__name__}."
            raise ConfigurationError(msg)
        return cls._from_dict(cast("dict[str, Any]", data), base_dir=base_dir)

    @classmethod
    def _from_dict(cls, data: dict[str, Any], *, base_dir: Path) -> VideoBatchConfig:
        unknown = set(data) - _ALLOWED_TOP_LEVEL_KEYS
        if unknown:
            msg = (
                f"Unknown key(s) {sorted(unknown)!r} in video batch config. "
                f"Valid: {sorted(_ALLOWED_TOP_LEVEL_KEYS)!r}."
            )
            raise ConfigurationError(msg)
        if "videos" in data and "clips" in data:
            msg = "Config cannot specify both 'videos' and 'clips'."
            raise ConfigurationError(msg)
        videos_raw_obj = data.get("videos") if "videos" in data else data.get("clips")
        if not isinstance(videos_raw_obj, list):
            msg = "'videos' (or 'clips') must be a JSON array."
            raise ConfigurationError(msg)
        videos_raw = cast("list[Any]", videos_raw_obj)
        if not (MIN_PROMPTS <= len(videos_raw) <= MAX_PROMPTS):
            msg = (
                f"'videos' must have between {MIN_PROMPTS} and "
                f"{MAX_PROMPTS} entries (got {len(videos_raw)})."
            )
            raise ConfigurationError(msg)

        profile = data.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile.strip()):
            msg = "'profile' must be a non-empty string."
            raise ConfigurationError(msg)
        project = data.get("project")
        if project is not None and (
            not isinstance(project, str) or not _FLOW_ID_RE.fullmatch(project)
        ):
            msg = "'project' must be a Flow project id: 1-128 letters, digits, or hyphens."
            raise ConfigurationError(msg)
        output_dir = data.get("output_dir")
        if output_dir is not None and (not isinstance(output_dir, str) or not output_dir.strip()):
            msg = "'output_dir' must be a non-empty string."
            raise ConfigurationError(msg)

        default_model = data.get("model", DEFAULT_VIDEO_BATCH_MODEL)
        if not isinstance(default_model, str) or not default_model.strip():
            msg = "'model' must be a non-empty string."
            raise ConfigurationError(msg)
        default_duration = data.get("duration")
        if default_duration is not None and (
            isinstance(default_duration, bool) or not isinstance(default_duration, int)
        ):
            msg = "'duration' must be an integer."
            raise ConfigurationError(msg)
        if "aspect" in data and "aspect_ratio" in data:
            msg = "Config cannot specify both 'aspect' and 'aspect_ratio'."
            raise ConfigurationError(msg)
        default_aspect = data.get("aspect", data.get("aspect_ratio", "9:16"))
        if not isinstance(default_aspect, str) or default_aspect not in _ALLOWED_VIDEO_ASPECTS:
            msg = (
                f"'aspect' {default_aspect!r} is invalid. Valid: {list(_ALLOWED_VIDEO_ASPECTS)!r}."
            )
            raise ConfigurationError(msg)
        default_resolution = data.get("resolution")
        if default_resolution is not None and not isinstance(default_resolution, str):
            msg = "'resolution' must be a string."
            raise ConfigurationError(msg)

        items: list[VideoBatchItem] = []
        for idx, raw_item in enumerate(videos_raw):
            if not isinstance(raw_item, dict):
                msg = f"videos[{idx}] must be a JSON object."
                raise ConfigurationError(msg)
            items.append(
                parse_video_batch_item_dict(
                    cast("dict[str, Any]", raw_item),
                    idx,
                    base_dir=base_dir,
                    default_model=default_model,
                    default_duration=default_duration,
                    default_aspect=default_aspect,
                    default_resolution=default_resolution,
                )
            )
        return cls(
            videos=tuple(items),
            profile=profile,
            project=project,
            output_dir=output_dir,
        )


def target_path_for_video_item(item: VideoBatchItem, output_dir: Path) -> Path:
    """Resolve the destination ``.mp4`` file path for ``item``."""
    if item.output is not None:
        return Path(item.output).expanduser()
    return output_dir / f"video_{item.index}.mp4"


def is_valid_video_on_disk(path: Path) -> bool:
    """Return True if ``path`` exists, is a regular file, and has MP4/WebM magic bytes."""
    try:
        if not path.is_file():
            return False
        if path.stat().st_size <= 0:
            return False
        with path.open("rb") as fh:
            head = fh.read(32)
    except OSError:
        return False
    return looks_like_video(head)


def resumed_video_rows(
    videos: tuple[VideoBatchItem, ...],
    output_dir: Path,
) -> dict[int, VideoBatchOutcome]:
    """Identify rows whose output ``.mp4`` already exists and is a valid video file."""
    done: dict[int, VideoBatchOutcome] = {}
    for item in videos:
        target = target_path_for_video_item(item, output_dir)
        if is_valid_video_on_disk(target):
            done[item.index] = VideoBatchOutcome(
                index=item.index,
                item=item,
                status="resumed",
                saved_path=target,
            )
    return done


def _relocate_downloaded_video(result: VideoResult, target: Path) -> VideoResult:
    """Move ``result.local_path`` to ``target`` if needed and return updated ``VideoResult``."""
    if result.local_path is None:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if result.local_path.resolve() != target.resolve():
        result.local_path.replace(target)
    return replace(result, local_path=target)


async def _run_one_video_item(
    *,
    client: Any,
    project_id: str,
    item: VideoBatchItem,
    output_dir: Path,
    profile_name: str,
    profile_dir: Path,
    recorder: OperationRecorder | None,
) -> tuple[VideoBatchOutcome, BaseException | None]:
    """Run one ``i2v`` item on the shared ``client``.

    Returns ``(outcome, fatal_exception)``. Never retries a submitted video row.
    """
    target = target_path_for_video_item(item, output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    req = GenerateVideoRequest(
        prompt=item.prompt,
        mode=Mode.I2V,
        aspect=Aspect.from_cli(item.aspect),
        start_image=Path(item.start),
        end_image=Path(item.end) if item.end is not None else None,
        model=VideoModel.from_cli(item.model),
        duration=item.duration,
        resolution=item.resolution,
    )

    started_holder: list[VideoStarted] = []

    def _on_started(started: VideoStarted) -> None:
        started_holder.append(started)
        if recorder is None:
            return
        try:
            recorder.record_started_video(
                profile_name=profile_name,
                profile_dir=profile_dir,
                request=req,
                started=started,
            )
        except DataStoreError as exc:
            logger.warning(
                "video_batch.persistence_failed_on_start",
                index=item.index,
                error_class=type(exc).__name__,
                media_id=started.media_id,
            )

    try:
        result: VideoResult = await client.generate_video(
            req=req,
            project_id=project_id,
            out_dir=target.parent,
            download=True,
            on_started=_on_started,
        )
        if result.status.succeeded:
            result = _relocate_downloaded_video(result, target)
    except Exception as exc:
        record_failed_operation_safe(
            recorder,
            logger=logger,
            profile_name=profile_name,
            profile_dir=profile_dir,
            command="video batch",
            mode=OperationKind.I2V,
            exc=exc,
            request=req,
            flow_project_id=started_holder[0].project_id if started_holder else project_id,
            flow_media_ids=([started_holder[0].media_id] if started_holder else ()),
        )
        if not isinstance(exc, GFlowError):
            raise
        outcome = VideoBatchOutcome(
            index=item.index,
            item=item,
            status="fail",
            media_id=started_holder[0].media_id if started_holder else None,
            project_id=project_id,
            error=f"{type(exc).__name__}: {exc}",
            exit_code=resolve_exit_code(exc),
        )
        if is_fatal_video_batch_error(exc):
            return outcome, exc
        return outcome, None

    if recorder is not None:
        try:
            cloud_info = (
                cloud_info_from_path(result.local_path) if result.local_path is not None else None
            )
            recorder.record_completed_video(
                profile_name=profile_name,
                _profile_dir=profile_dir,
                request=req,
                result=result,
                cloud_storage_info=cloud_info,
            )
        except DataStoreError as exc:
            logger.warning(
                "video_batch.persistence_failed_after_completion",
                index=item.index,
                error_class=type(exc).__name__,
                media_id=result.status.media_id,
            )

    if not result.status.succeeded:
        reasons = ", ".join(result.status.failure_reasons) or result.status.status
        return (
            VideoBatchOutcome(
                index=item.index,
                item=item,
                status="fail",
                media_id=result.status.media_id,
                project_id=result.project_id or project_id,
                error=f"video generation failed: {reasons}",
                exit_code=1,
            ),
            None,
        )

    return (
        VideoBatchOutcome(
            index=item.index,
            item=item,
            status="ok",
            saved_path=result.local_path,
            media_id=result.status.media_id,
            project_id=result.project_id or project_id,
            exit_code=0,
        ),
        None,
    )


async def run_video_batch(
    *,
    profile_dir: Path,
    headless: bool,
    videos: tuple[VideoBatchItem, ...],
    output_dir: Path,
    continue_on_error: bool,
    project_title: str,
    profile_name: str,
    recorder: OperationRecorder | None = None,
    project_id: str | None = None,
    resumed: dict[int, VideoBatchOutcome] | None = None,
    jitter_range: tuple[float, float] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> tuple[list[VideoBatchOutcome], str | None]:
    """Run ``videos`` sequentially in ONE :class:`FlowApiClient` session.

    Returns ``(outcomes, resolved_project_id)``.
    - If all rows are already in ``resumed``, returns immediately without opening a browser.
    - Fatal session/lane errors (auth, WAF, selector drift, insufficient credits, etc.)
      stop the batch immediately even under ``continue_on_error=True``.
    """
    if jitter_range is None:
        jitter_range = resolve_video_batch_jitter_range(None)
    output_dir.mkdir(parents=True, exist_ok=True)

    if resumed and all(item.index in resumed for item in videos):
        return [resumed[item.index] for item in videos], project_id

    if current_tracker() is None:
        return await run_with_auto_renew(
            profile_dir,
            lambda _tracker: run_video_batch(
                profile_dir=profile_dir,
                headless=headless,
                videos=videos,
                output_dir=output_dir,
                continue_on_error=continue_on_error,
                project_title=project_title,
                profile_name=profile_name,
                recorder=recorder,
                project_id=project_id,
                resumed=resumed,
                jitter_range=jitter_range,
                client_factory=client_factory,
            ),
            source="video.batch",
        )

    outcomes: list[VideoBatchOutcome] = []
    factory = client_factory or FlowApiClient
    submitted_count = 0
    run_project_id: str | None = project_id

    async with factory(profile_dir=profile_dir, headless=headless) as client:
        if run_project_id is None:
            run_project_id = str((await client.create_project(title=project_title)).project_id)
        mark_current_submitted()
        for pos, item in enumerate(videos):
            if resumed and (done := resumed.get(item.index)) is not None:
                outcomes.append(replace(done, project_id=run_project_id))
                continue
            if submitted_count > 0 and jitter_range[1] > 0:
                delay = random.uniform(*jitter_range)  # noqa: S311 - pacing, not crypto
                logger.info("video_batch.jitter_sleep", seconds=round(delay, 2), index=item.index)
                await asyncio.sleep(delay)
            submitted_count += 1
            outcome, fatal_exc = await _run_one_video_item(
                client=client,
                project_id=run_project_id,
                item=item,
                output_dir=output_dir,
                profile_name=profile_name,
                profile_dir=profile_dir,
                recorder=recorder,
            )
            outcomes.append(outcome)
            if fatal_exc is not None or (outcome.status == "fail" and not continue_on_error):
                reason = (
                    f"aborted after fatal {type(fatal_exc).__name__} on row {item.index}"
                    if fatal_exc is not None
                    else f"stopped after failure on row {item.index} (--fail-fast)"
                )
                for remaining in videos[pos + 1 :]:
                    if resumed and (done := resumed.get(remaining.index)) is not None:
                        outcomes.append(replace(done, project_id=run_project_id))
                    else:
                        outcomes.append(
                            VideoBatchOutcome(
                                index=remaining.index,
                                item=remaining,
                                status="skipped",
                                project_id=run_project_id,
                                error=reason,
                            )
                        )
                break

    return outcomes, run_project_id


def compute_video_batch_exit_code(outcomes: list[VideoBatchOutcome]) -> int:
    """Compute the final exit code for a video batch run."""
    failed = [o for o in outcomes if o.status == "fail"]
    if not failed:
        return 0
    return max((o.exit_code or 1) for o in failed)


def render_video_batch_summary(
    outcomes: list[VideoBatchOutcome],
    *,
    title: str = "gflow video batch",
) -> int:
    """Render a Rich summary table of video batch outcomes and return the exit code."""
    table = Table(title=f"{safe_terminal_text(title)} summary", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Status")
    table.add_column("Start Frame")
    table.add_column("Prompt (truncated)")
    table.add_column("Output / Error")

    succeeded = 0
    resumed_count = 0
    skipped = 0
    for o in outcomes:
        preview = safe_prompt_preview(o.item.prompt)
        start_name = safe_terminal_text(Path(o.item.start).name)
        if o.status in ("ok", "resumed"):
            succeeded += 1
            if o.status == "resumed":
                resumed_count += 1
            badge = "[green]OK[/green]" if o.status == "ok" else "[cyan]RESUMED[/cyan]"
            detail = safe_path_text(o.saved_path) if o.saved_path is not None else ""
        elif o.status == "skipped":
            skipped += 1
            badge = "[yellow]SKIPPED[/yellow]"
            detail = safe_terminal_text(o.error) if o.error else "—"
        else:
            badge = "[red]FAIL[/red]"
            detail = safe_terminal_text(o.error or "unknown error")
        table.add_row(str(o.index), badge, start_name, preview, detail)

    console.print()
    console.print(table)
    total = len(outcomes)
    notes = [
        *([f"{resumed_count} resumed"] if resumed_count else []),
        *([f"{skipped} skipped"] if skipped else []),
    ]
    suffix = f" ({', '.join(notes)})" if notes else ""
    console.print(f"\n[bold]{succeeded}/{total} succeeded[/bold]{suffix}")
    return compute_video_batch_exit_code(outcomes)


def build_video_batch_json_payload(
    outcomes: list[VideoBatchOutcome],
    *,
    profile_name: str,
    project_id: str | None,
) -> dict[str, Any]:
    """Build machine-readable JSON payload for ``gflow video batch --json``."""
    exit_code = compute_video_batch_exit_code(outcomes)
    ok_count = sum(1 for o in outcomes if o.status == "ok")
    resumed_count = sum(1 for o in outcomes if o.status == "resumed")
    fail_count = sum(1 for o in outcomes if o.status == "fail")
    skip_count = sum(1 for o in outcomes if o.status == "skipped")
    return {
        "status": "ok" if exit_code == 0 else "fail",
        "command": "video batch",
        "profile": profile_name,
        "project_id": project_id,
        "total": len(outcomes),
        "succeeded": ok_count + resumed_count,
        "generated": ok_count,
        "resumed": resumed_count,
        "failed": fail_count,
        "skipped": skip_count,
        "exit_code": exit_code,
        "results": [
            {
                "index": o.index,
                "status": o.status,
                "start": o.item.start,
                "end": o.item.end,
                "prompt": o.item.prompt,
                "model": o.item.model,
                "duration": o.item.duration,
                "aspect": o.item.aspect,
                "resolution": o.item.resolution,
                "output": str(o.saved_path) if o.saved_path is not None else o.item.output,
                "media_id": o.media_id,
                "project_id": o.project_id,
                "error": o.error,
                "exit_code": o.exit_code,
            }
            for o in outcomes
        ],
    }


__all__ = [
    "VIDEO_BATCH_JITTER_MAX_SECONDS",
    "VIDEO_BATCH_JITTER_MIN_SECONDS",
    "VideoBatchConfig",
    "VideoBatchItem",
    "VideoBatchOutcome",
    "build_video_batch_json_payload",
    "compute_video_batch_exit_code",
    "is_fatal_video_batch_error",
    "is_valid_video_on_disk",
    "parse_video_batch_item_dict",
    "render_video_batch_summary",
    "resolve_video_batch_jitter_range",
    "resumed_video_rows",
    "run_video_batch",
    "target_path_for_video_item",
]
