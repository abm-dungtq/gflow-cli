"""``gflow run`` — execute a JSON-described batch of image generations.

A batch config is a JSON file with a top-level ``prompts`` list. Each prompt
produces one or more PNGs in ``output_dir``. Prompts run sequentially through
a single :class:`FlowApiClient` session so the Playwright browser and Flow
project context persist across the loop (per-prompt costs are essentially
just the per-call reCAPTCHA mint + ``batchGenerateImages`` round-trip).

See ``docs/superpowers/plans/2026-05-12-gflow-cli-d2-continuation/AUDIT_E1.md``
Section D for the formal schema.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import click
from rich.console import Console
from rich.markup import escape

from gflow_cli._cli_helpers import (
    _FLOW_ID_RE,
    _make_provider_dir,
    _resolve_profile,
    _validate_project_id,
    default_project,
)
from gflow_cli.api.client import FlowApiClient, validate_image_file
from gflow_cli.api.dto import GeneratedImage
from gflow_cli.api.transports import EXPERIMENTAL_TRANSPORTS
from gflow_cli.config import get_settings
from gflow_cli.data.recorder import OperationRecorder
from gflow_cli.data.repository import DataRepository
from gflow_cli.data.store import DataStore
from gflow_cli.errors import ConfigurationError, DataStoreError
from gflow_cli.image_batch import (
    MAX_PROMPTS,
    MIN_PROMPTS,
    BatchOutcome,
    BatchPromptItem,
    batch_parents,
    order_batch_rows,
    parse_batch_item_dict,
    render_image_batch_summary,
    resolve_exit_code,
    run_image_batch,
    target_for_batch_item_image,
)
from gflow_cli.paths import resolve_batch_output_dir

console = Console()


_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"profile", "transport", "output_dir", "project", "prompts"},
)
# ---------------------------------------------------------------------------
# Dataclasses — validated config + per-prompt outcome.
# ---------------------------------------------------------------------------


def _validate_local_ref_file(raw_ref: str, *, label: str, base_dir: Path) -> str:
    """Resolve one local reference image path and verify it is a valid image."""
    raw = Path(raw_ref).expanduser()
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
        msg = f"{label} {raw_ref!r} does not exist (relative to {base_dir})."
        raise ConfigurationError(msg) from None
    if not resolved.is_file():
        msg = f"{label} {raw_ref!r} is not a file."
        raise ConfigurationError(msg)
    try:
        asyncio.run(validate_image_file(resolved))
    except (ValueError, OSError) as exc:
        msg = f"{label} {raw_ref!r}: {exc}"
        raise ConfigurationError(msg) from None
    return str(resolved)


def _resolve_output_path(raw_output: str, *, idx: int, base_dir: Path) -> str:
    """Resolve a row's ``output`` file path and reject existing directories."""
    p = Path(raw_output).expanduser()
    if not p.is_absolute():
        if (base_dir / p).exists() or (p.parent != Path() and (base_dir / p.parent).exists()):
            p = base_dir / p
        elif p.exists() or (p.parent != Path() and p.parent.exists()):
            pass
        else:
            p = base_dir / p
    if p.exists() and p.is_dir():
        msg = f"prompts[{idx}].output {raw_output!r} is an existing directory."
        raise ConfigurationError(msg)
    return str(p)


def _resolve_file_ref(item: BatchPromptItem, base_dir: Path) -> BatchPromptItem:
    """Resolve a row's local-file ``ref``/``refs`` and ``output``, or refuse (exit 11).

    Relative paths resolve against the config file's folder (with fallback to CWD when
    the caller provides a CWD-relative path). Symlinks are resolved strictly, and the
    image check (size, magic bytes) is the one every upload applies: a manifest must
    not be a way to send ``~/.ssh/id_rsa`` to Google.
    """
    resolved_ref = item.ref
    if item.ref is not None and not item.ref.startswith("batch:"):
        resolved_ref = _validate_local_ref_file(
            item.ref,
            label=f"prompts[{item.index}].ref",
            base_dir=base_dir,
        )
    resolved_refs: tuple[str, ...] = item.refs
    if item.refs:
        resolved_list: list[str] = []
        for r_idx, ref_spec in enumerate(item.refs):
            if ref_spec.startswith("batch:"):
                resolved_list.append(ref_spec)
            else:
                resolved_list.append(
                    _validate_local_ref_file(
                        ref_spec,
                        label=f"prompts[{item.index}].refs[{r_idx}]",
                        base_dir=base_dir,
                    )
                )
        resolved_refs = tuple(resolved_list)
    resolved_output = item.output
    if item.output is not None:
        resolved_output = _resolve_output_path(item.output, idx=item.index, base_dir=base_dir)
    return replace(item, ref=resolved_ref, refs=resolved_refs, output=resolved_output)


@dataclass(frozen=True)
class BatchConfig:
    """Parsed + validated batch config."""

    prompts: tuple[BatchPromptItem, ...]
    profile: str | None = None
    transport: str | None = None
    output_dir: str | None = None
    project: str | None = None

    @classmethod
    def from_json_path(cls, path: Path) -> BatchConfig:
        """Parse, validate, and return a BatchConfig.

        Raises :class:`ConfigurationError` on any validation failure with a
        message that points at the offending key.
        """
        if not path.exists():
            msg = f"Config file not found: {path}"
            raise ConfigurationError(msg)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as e:
            msg = f"Failed to read {path}: {e}"
            raise ConfigurationError(msg) from e
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            msg = f"Failed to parse {path}: line {e.lineno}:{e.colno} {e.msg}"
            raise ConfigurationError(
                msg,
            ) from e
        if not isinstance(data, dict):
            msg = f"Config root must be a JSON object, got {type(data).__name__}."
            raise ConfigurationError(
                msg,
            )
        return cls._from_dict(cast("dict[str, Any]", data), base_dir=path.parent)

    @classmethod
    def _from_dict(cls, data: dict[str, Any], *, base_dir: Path | None = None) -> BatchConfig:
        unknown = set(data) - _ALLOWED_TOP_LEVEL_KEYS
        if unknown:
            msg = (
                f"Unknown key(s) {sorted(unknown)!r} in config. "
                f"Valid: {sorted(_ALLOWED_TOP_LEVEL_KEYS)!r}."
            )
            raise ConfigurationError(
                msg,
            )
        prompts_raw_obj = data.get("prompts")
        if not isinstance(prompts_raw_obj, list):
            msg = "'prompts' must be a JSON array."
            raise ConfigurationError(msg)
        prompts_raw = cast("list[Any]", prompts_raw_obj)

        if not (MIN_PROMPTS <= len(prompts_raw) <= MAX_PROMPTS):
            msg = (
                f"'prompts' must have between {MIN_PROMPTS} and "
                f"{MAX_PROMPTS} entries (got {len(prompts_raw)})."
            )
            raise ConfigurationError(
                msg,
            )
        prompts: list[BatchPromptItem] = []
        for idx, p in enumerate(prompts_raw):
            if not isinstance(p, dict):
                msg = f"prompts[{idx}] must be a JSON object."
                raise ConfigurationError(msg)
            prompts.append(parse_batch_item_dict(cast("dict[str, Any]", p), idx))
        # Local-file refs resolve against the config's folder, so a config is portable,
        # and must be a real image before any browser work (#913).
        prompts = [_resolve_file_ref(p, base_dir or Path.cwd()) for p in prompts]
        # Validate `batch:N` references (range, self, cycle, multi-image parent) here, so
        # a bad config exits 11 before any browser work (#913).
        order_batch_rows(prompts)

        profile = data.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile):
            msg = "'profile' must be a non-empty string."
            raise ConfigurationError(msg)
        transport = data.get("transport")
        if transport is not None and (not isinstance(transport, str) or not transport):
            msg = "'transport' must be a non-empty string."
            raise ConfigurationError(msg)
        output_dir = data.get("output_dir")
        if output_dir is not None and (not isinstance(output_dir, str) or not output_dir):
            msg = "'output_dir' must be a non-empty string."
            raise ConfigurationError(msg)
        project = data.get("project")
        if project is not None and (
            not isinstance(project, str) or not _FLOW_ID_RE.fullmatch(project)
        ):
            msg = "'project' must be a Flow project id: 1-128 letters, digits, or hyphens."
            raise ConfigurationError(msg)

        return cls(
            prompts=tuple(prompts),
            profile=profile,
            transport=transport,
            output_dir=output_dir,
            project=project,
        )


# ---------------------------------------------------------------------------
# Helpers — experimental-transport gating.
# ---------------------------------------------------------------------------


def _is_valid_image_on_disk(path: Path) -> bool:
    """Return True if ``path`` is a non-empty regular file with image magic bytes."""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size <= 0 or size > 20 * 1024 * 1024:
            return False
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
        len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    )


def _candidate_output_files(item: BatchPromptItem, output_dir: Path, n: int) -> list[Path]:
    """Candidate on-disk files for image ``n`` of ``item`` (accounting for #96 extension fix)."""
    if item.output is not None:
        base_target = target_for_batch_item_image(item, output_dir, n)
        candidates = [
            base_target,
            *(
                base_target.with_suffix(ext)
                for ext in (".png", ".jpg", ".jpeg", ".webp")
                if ext != base_target.suffix.lower()
            ),
        ]
        return [p for p in candidates if p.is_file()]
    stem = glob.escape(item.output_filename or f"prompt_{item.index}")
    return [p for p in sorted(output_dir.glob(f"{stem}_{n}.*")) if p.is_file()]


def _resumed_rows(
    prompts: tuple[BatchPromptItem, ...],
    output_dir: Path,
    project_id: str | None,
    profile_name: str,
) -> dict[int, BatchOutcome]:
    """Rows a previous run already made (#934), by row index.

    For rows using ``output_dir``/``output_filename``, a row counts only when EVERY file it
    writes (``<stem>_<n>.<ext>``) exists, the catalog records exactly those bytes at that
    path as an image of this profile in ``project_id``, and its recorded prompt is the row's.
    For rows with an explicit ``output`` file path, an existing valid image file at that path
    (or its magic-byte-corrected sibling extension) also resumes the row when no ``batch:N``
    child requires a catalog display name.
    """
    parents = {p for item in prompts for p in batch_parents(item)}
    done: dict[int, BatchOutcome] = {}
    try:
        with DataStore.open(get_settings().resolved_db_path()) as store:
            repo = DataRepository(store)
            for item in prompts:
                paths: list[Path] = []
                images: list[GeneratedImage] = []
                for n in range(item.count):
                    candidates = _candidate_output_files(item, output_dir, n)
                    found = [
                        (path, seed)
                        for path in candidates
                        if project_id is not None
                        and (
                            seed := repo.resolve_seed_image_by_file(
                                profile_name,
                                path.resolve(),
                                hashlib.sha256(path.read_bytes()).hexdigest(),
                            )
                        )
                        and seed.flow_project_id == project_id
                        and seed.prompt in (item.text, item.original_prompt)
                    ]
                    if found:
                        path, seed = found[0]
                        paths.append(path)
                        asset = repo.get_asset_by_flow_media_id(profile_name, seed.flow_media_id)
                        name = asset.metadata_json.get("display_name") if asset else None
                        images.append(
                            GeneratedImage(
                                media_name=seed.flow_media_id,
                                workflow_id=seed.flow_workflow_id or "",
                                seed=0,
                                prompt=item.text,
                                model_name_type=seed.model,
                                aspect_ratio=seed.aspect_ratio or "",
                                fife_url="",
                                dimensions=(seed.width or 0, seed.height or 0),
                                display_name=name if isinstance(name, str) and name else None,
                            )
                        )
                        continue
                    if item.output is not None:
                        valid_files = [p for p in candidates if _is_valid_image_on_disk(p)]
                        if valid_files:
                            path = valid_files[0]
                            # If the catalog knows this file in this project under a DIFFERENT
                            # prompt, the row's prompt was edited and must be regenerated.
                            if project_id is not None:
                                existing_seed = repo.resolve_seed_image_by_file(
                                    profile_name,
                                    path.resolve(),
                                    hashlib.sha256(path.read_bytes()).hexdigest(),
                                )
                                if (
                                    existing_seed is not None
                                    and existing_seed.flow_project_id == project_id
                                    and existing_seed.prompt
                                    not in (item.text, item.original_prompt)
                                ):
                                    break
                            paths.append(path)
                            images.append(
                                GeneratedImage(
                                    media_name=f"resumed-{item.index}-{n}",
                                    workflow_id="",
                                    seed=0,
                                    prompt=item.text,
                                    model_name_type=item.model,
                                    aspect_ratio=item.aspect_ratio,
                                    fife_url="",
                                    dimensions=(0, 0),
                                )
                            )
                            continue
                    break
                complete = len(images) == item.count
                if complete and (item.index not in parents or images[0].display_name):
                    done[item.index] = BatchOutcome(
                        index=item.index,
                        prompt=item,
                        status="resumed",
                        saved_paths=paths,
                        images=images,
                    )
        # A child built from a parent that is about to be regenerated is stale too.
        while stale := [
            i for i, o in done.items() if any(p not in done for p in batch_parents(o.prompt))
        ]:
            for i in stale:
                del done[i]
    except (DataStoreError, OSError) as exc:
        # An unreadable catalog resumes nothing: every row is generated, never guessed.
        console.print(f"[yellow]Resume skipped:[/yellow] catalog unreadable ({escape(str(exc))})")
        return {}
    return done


def _check_transport_gated(transport: str | None) -> None:
    """Per spec D.1 rule 5: experimental transports require env var.

    Raises :class:`ConfigurationError` if the named transport is in
    :data:`EXPERIMENTAL_TRANSPORTS` and ``GFLOW_CLI_EXPERIMENTAL_TRANSPORTS``
    is unset.
    """
    if transport is None or transport not in EXPERIMENTAL_TRANSPORTS:
        return
    import os

    if os.getenv("GFLOW_CLI_EXPERIMENTAL_TRANSPORTS") != "1":
        msg = (
            f"Transport {transport!r} is experimental. "
            f"Set GFLOW_CLI_EXPERIMENTAL_TRANSPORTS=1 to enable."
        )
        raise ConfigurationError(
            msg,
        )


# ---------------------------------------------------------------------------
# Core orchestration — sequential per-prompt loop on one client session.
# ---------------------------------------------------------------------------


async def _run_batch(
    *,
    profile_dir: Path,
    headless: bool,
    transport: str | None,
    prompts: tuple[BatchPromptItem, ...],
    output_dir: Path,
    continue_on_error: bool,
    profile_name: str | None = None,
    recorder: OperationRecorder | None = None,
    project_id: str | None = None,
    resumed: dict[int, BatchOutcome] | None = None,
) -> list[BatchOutcome]:
    """Run ``prompts`` sequentially through ONE FlowApiClient session.

    Per AUDIT_E1 D.2: a single ``async with FlowApiClient(...)`` block
    wraps the whole loop so the browser/page/project context persists
    across iterations. On the UI transport Flow's page mints its own reCAPTCHA
    token per submit (#891).
    """
    outcomes = await run_image_batch(
        profile_dir=profile_dir,
        headless=headless,
        transport=transport,
        prompts=prompts,
        output_dir=output_dir,
        continue_on_error=continue_on_error,
        project_title="gflow-cli run",
        client_factory=FlowApiClient,
        _profile_name=profile_name,
        _recorder=recorder,
        _command="run",
        project_id=project_id,
        resumed=resumed,
    )
    return outcomes


# ---------------------------------------------------------------------------
# Click command — `gflow run --config <file>`
# ---------------------------------------------------------------------------


@click.command("run")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a JSON batch config (schema: docs/USAGE.md, gflow run).",
)
@click.option(
    "--output-dir",
    "output_dir_override",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Override output_dir from the config.",
)
@click.option(
    "--profile",
    default=None,
    help="Profile name (overrides default and any 'profile' in the config).",
)
@click.option(
    "--project",
    "project_id",
    default=None,
    callback=_validate_project_id,
    help=(
        "Generate every prompt into this existing Flow project id instead of a new "
        "'gflow-cli run' project (overrides any 'project' in the config)."
    ),
)
@click.option(
    "--continue-on-error/--fail-fast",
    default=True,
    show_default=True,
    help=(
        "On per-prompt failure: --continue-on-error attempts the remaining "
        "prompts (default); --fail-fast halts the batch immediately."
    ),
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help=(
        "Skip rows a previous run already made in this project (their files are in the "
        "output dir and recorded in the local catalog); a batch:N child references them "
        "in place. Needs --project or a config 'project'."
    ),
)
def run(
    config_path: Path,
    output_dir_override: Path | None,
    profile: str | None,
    project_id: str | None,
    continue_on_error: bool,
    resume: bool,
) -> None:
    """Execute a JSON-described batch of image generations sequentially."""
    try:
        cfg = BatchConfig.from_json_path(config_path)
        _check_transport_gated(cfg.transport)
    except ConfigurationError as e:
        console.print(f"[red]Config error:[/red] {escape(str(e))}")
        sys.exit(resolve_exit_code(e))

    profile_name = _resolve_profile(profile or cfg.profile)
    provider_dir = _make_provider_dir(profile_name)
    settings = get_settings()
    output_dir = resolve_batch_output_dir(
        cli_override=output_dir_override,
        config_value=cfg.output_dir,
        output_root=settings.output_dir,
    )

    console.print(
        f"\n[bold]gflow run[/bold] · profile=[bold]{profile_name}[/bold] "
        f"· transport=[bold]{cfg.transport or '(default)'}[/bold] "
        f"· {len(cfg.prompts)} prompt(s)",
    )
    console.print(f"  output_dir: [dim]{output_dir}[/dim]")
    project_id = default_project(project_id or cfg.project, None, profile_name)
    if project_id:
        console.print(f"  project: [dim]{project_id}[/dim] [dim](existing)[/dim]")
    if not continue_on_error:
        console.print("  mode: [yellow]fail-fast[/yellow]")
    resumed: dict[int, BatchOutcome] = {}
    if resume:
        needs_project = any(
            item.output is None or bool(batch_parents(item)) for item in cfg.prompts
        )
        if not project_id and needs_project:
            err = ConfigurationError(
                detail="--resume needs the run's project: pass --project <id> or set 'project'"
            )
            console.print(f"[red]Config error:[/red] {escape(err.detail)}")
            sys.exit(resolve_exit_code(err))
        resumed = _resumed_rows(cfg.prompts, output_dir, project_id, profile_name)
        console.print(f"  resume: [dim]{len(resumed)} row(s) already made[/dim]")

    recorder = OperationRecorder.open(settings)
    try:
        outcomes = asyncio.run(
            _run_batch(
                profile_dir=provider_dir,
                headless=settings.headless,
                transport=cfg.transport,
                prompts=cfg.prompts,
                output_dir=output_dir,
                continue_on_error=continue_on_error,
                profile_name=profile_name,
                recorder=recorder,
                project_id=project_id,
                resumed=resumed,
            ),
        )
    finally:
        recorder.close()
    exit_code = render_image_batch_summary(outcomes, title="gflow run")
    if exit_code != 0:
        sys.exit(exit_code)
