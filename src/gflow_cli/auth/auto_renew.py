"""One-shot pre-submit session auto-renewal helper for CLI generation commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import structlog

from gflow_cli.auth.renew import renew_profile
from gflow_cli.config import get_settings
from gflow_cli.errors import (
    AuthExpiredError,
    AuthMissingError,
    ProfileEngineDowngradeError,
    ProfileLockedError,
)

logger = structlog.get_logger(__name__)
_T = TypeVar("_T")
_RETRY_ACTIVE: ContextVar[bool] = ContextVar("_RETRY_ACTIVE", default=False)
_CURRENT_TRACKER: ContextVar[SubmitTracker | None] = ContextVar("_CURRENT_TRACKER", default=None)


@dataclass
class SubmitTracker:
    """Tracks whether a generation attempt has reached the request submission step."""

    submitted: bool = False

    def mark_submitted(self) -> None:
        self.submitted = True


def current_tracker() -> SubmitTracker | None:
    return _CURRENT_TRACKER.get()


def mark_current_submitted() -> None:
    if (tracker := _CURRENT_TRACKER.get()) is not None:
        tracker.mark_submitted()


async def try_auto_renew(
    profile_dir: Path,
    exc: BaseException,
    tracker: SubmitTracker,
    *,
    source: str = "auto_renew",
) -> bool:
    """Attempt a one-shot session renewal if ``exc`` is a pre-submit auth error."""
    if (
        _RETRY_ACTIVE.get()
        or tracker.submitted
        or not isinstance(exc, (AuthExpiredError, AuthMissingError))
        or not get_settings().auto_renew
    ):
        return False
    try:
        result = await renew_profile(profile_dir, source=source)
    except (ProfileLockedError, ProfileEngineDowngradeError):
        raise
    except Exception as renew_exc:
        logger.warning(
            "auth.auto_renew",
            profile=profile_dir.name,
            source=source,
            error_type=type(exc).__name__,
            renewed=False,
            result="error",
        )
        raise exc from renew_exc
    renewed = bool(getattr(result, "renewed", False))
    logger.info(
        "auth.auto_renew",
        profile=profile_dir.name,
        source=source,
        error_type=type(exc).__name__,
        renewed=renewed,
        result=getattr(result, "result", None),
    )
    return renewed


async def run_retry(coro: Awaitable[_T]) -> _T:
    """Execute ``coro`` as the single post-renewal retry attempt."""
    token = _RETRY_ACTIVE.set(True)
    try:
        return await coro
    finally:
        _RETRY_ACTIVE.reset(token)


async def run_with_auto_renew(
    profile_dir: Path,
    operation: Callable[[SubmitTracker], Awaitable[_T]],
    *,
    source: str = "auto_renew",
) -> _T:
    """Run ``operation`` and auto-renew at most once on pre-submit auth errors."""
    for is_retry in (False, True):
        tracker = SubmitTracker()
        tok = _CURRENT_TRACKER.set(tracker)
        try:
            coro = operation(tracker)
            return await (run_retry(coro) if is_retry else coro)
        except (AuthExpiredError, AuthMissingError) as auth_exc:
            if is_retry or not await try_auto_renew(profile_dir, auth_exc, tracker, source=source):
                raise
        finally:
            _CURRENT_TRACKER.reset(tok)
    raise AssertionError("unreachable")
