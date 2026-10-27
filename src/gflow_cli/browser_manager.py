"""Chrome discovery and Playwright-channel helpers.

.. note::

   This module previously also shipped a packaged CDP (Chrome DevTools
   Protocol) attach/spawn lifecycle — ``get_or_launch_browser`` /
   ``close_browser`` / lockfile-managed detached Chrome — for research use
   and non-Flow use cases. It was removed 2026-07-19 (see
   ``.superpowers/sdd/cdp-decision.md`` in the
   ``chore/production-readiness-hardening`` history): zero production
   consumers shipped in the wheel, the no-lock attach path would connect to
   *any* Chrome answering on the port (a documented hijack vector), and the
   externally-discoverable CDP debug port this pattern relies on was itself
   rejected by Google's Flow surface on `aisandbox-pa.googleapis.com` in a
   2026-05-12 empirical test. The production transport is
   :class:`gflow_cli.api.transports.ui_automation.UiAutomationTransport`,
   which uses Playwright's internal CDP port (Playwright manages it
   privately, never externally exposed) rather than an externally-exposed
   debug port. CDP-attach as a distinct opt-in transport remains a parked
   backlog idea — see PLAN.md ADR #13 and the "CDP Attach Transport —
   BACKLOG" section — should a future contributor want to revisit it with a
   safe ownership model.

Single responsibility: locate the system Chrome binary and decide which
Playwright ``channel`` (if any) a given profile should launch with.

Public API
----------
is_chrome_available() -> bool
    True if a Google Chrome (or Chromium fallback) binary can be found.

resolved_chrome_binary() -> str | None
    The resolved Chrome binary path, or None. Never raises.

is_playwright_chrome_channel_available() -> bool
    True only if Google Chrome proper sits at one of the paths Playwright's
    ``channel="chrome"`` hard-codes. Stricter than ``is_chrome_available()``,
    which accepts Chromium; ``CHROME_BINARY`` does not satisfy it.

channel_for_profile(profile_dir) -> str | None
    ``"chrome"`` if the profile's strategy marker requests it AND Google
    Chrome proper is available at Playwright's expected paths; else None.

chrome_strategy_requested(profile_dir) -> bool
    True if the profile's ``.gflow_browser_strategy`` marker requests
    system Chrome (independent of whether Chrome is actually available).

ensure_profile_engine_compatible(profile_dir, channel) -> None
    Raise ``ProfileEngineDowngradeError`` when the bundled Chromium about to
    open ``profile_dir`` is older than the Chromium that last wrote it (#477).

Internal helpers (exported for tests)
--------------------------------------
_find_chrome_binary() -> str
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from gflow_cli.errors import (
    ConfigurationError,
    ProfileEngineDowngradeError,
    ProfileLockedError,
)

if sys.platform != "win32":
    import fcntl

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Chrome binary detection
# ---------------------------------------------------------------------------


def _find_chrome_binary() -> str:
    """Locate the system Chrome binary.

    Resolution order:
    1. ``CHROME_BINARY`` env var (override)
    2. ``shutil.which("chrome")`` / ``shutil.which("google-chrome")``
    3. Platform-standard install paths (including Chromium as last resort for
       non-Playwright-channel uses such as auth login).

    .. note::
        This function accepts Chromium as a fallback for the auth use-case.
        It must NOT be used to decide whether Playwright's ``channel="chrome"``
        is available — use :func:`is_playwright_chrome_channel_available` for
        that, which checks only the exact paths Playwright hard-codes.

    Raises ``ConfigurationError`` if nothing found.
    """
    # 1. Environment override
    env_override = os.environ.get("CHROME_BINARY")
    if env_override:
        return env_override

    # 2. PATH probe — Google Chrome names first, Chromium last-resort
    for candidate in ("chrome", "google-chrome", "chromium"):
        found = shutil.which(candidate)
        if found:
            return found

    # 3. Platform-specific standard paths
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        platform_paths = [
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path(local_app_data or "") / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    elif sys.platform == "darwin":
        platform_paths = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        # Linux / other POSIX
        platform_paths = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
        ]

    for p in platform_paths:
        if p.exists():
            return str(p)

    msg = (
        "Chrome not found. Install from https://www.google.com/chrome/"
        " or set CHROME_BINARY env var."
    )
    raise ConfigurationError(
        msg,
    )


def is_playwright_chrome_channel_available() -> bool:
    """Return True only when Playwright's ``channel="chrome"`` can find Chrome.

    Playwright's ``launch_persistent_context(channel="chrome")`` looks for
    **Google Chrome proper** at platform-specific hardcoded paths — it does NOT
    accept a plain Chromium binary. This function replicates those paths so
    callers can gate the ``channel="chrome"`` argument on a binary that Playwright
    will actually find, avoiding the misleading
    ``Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome``
    error that occurs when only system Chromium is present.

    ``CHROME_BINARY`` is deliberately **ignored** here, unlike in
    :func:`_find_chrome_binary`: Playwright honours a custom binary only via
    ``executable_path=``, never via ``channel=``. Treating the env var as proof of
    a resolvable channel passed this gate and then failed at launch.
    """
    # Playwright's own resolution paths for channel="chrome". Derived from
    # playwright/_impl/_browser_type.py executables(). channel="chrome" resolves
    # ONLY to these exact Google-Chrome paths — a system Chromium does NOT
    # satisfy it, so we must not list Chromium or distro chrome shims here.
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        chrome_paths: list[Path] = [
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path(local_app_data or "") / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    elif sys.platform == "darwin":
        chrome_paths = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        # Linux — Playwright resolves channel="chrome" only to this path.
        chrome_paths = [
            Path("/opt/google/chrome/chrome"),
        ]

    return any(p.exists() for p in chrome_paths)


def is_chrome_available() -> bool:
    """Return True if a Google Chrome (or Chromium fallback) binary can be found.

    This is used for the auth login flow. It intentionally accepts Chromium as
    a fallback so the auth browser can open even when only Chromium is
    installed. For deciding whether Playwright's ``channel="chrome"`` can be
    used, call :func:`is_playwright_chrome_channel_available` instead.
    """
    try:
        _find_chrome_binary()
        return True
    except ConfigurationError:
        return False


def resolved_chrome_binary() -> str | None:
    """Return the system Chrome binary path, or ``None`` if Chrome can't be found.

    Public, exception-free wrapper around :func:`_find_chrome_binary` for
    diagnostics/logging callers that want the path without handling errors. It
    must NEVER raise: it feeds ``_log_and_guard_launch``'s observability log, and
    a best-effort path probe should never abort a launch. Besides
    :class:`ConfigurationError` (no binary found), ``shutil.which`` can raise on
    odd environments (e.g. an OS-detection mismatch), so any error resolves to
    ``None``.
    """
    try:
        return _find_chrome_binary()
    except Exception:  # noqa: BLE001 — exception-free by contract; any failure → None
        return None


def window_position_args(position: str) -> list[str]:
    """Chrome args placing the headed generation window (#923); '' keeps Chrome's own.

    Shared by both generation launch sites (``FlowApiClient`` and the standalone
    ``UiAutomationTransport``) so they cannot drift. Login launches stay visible.
    """
    return [f"--window-position={position}"] if position else []


ANTI_THROTTLING_ARGS: tuple[str, ...] = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)

_MACOS_CHROME_APP = Path("/Applications/Google Chrome.app")
_MACOS_BG_LOCK_TIMEOUT_SECONDS = 120.0
_MACOS_BG_LOCK_POLL_SECONDS = 0.2


def _remove_macos_app_bundle(app_path: Path) -> None:
    """Rename away the ``.app`` suffix before removal to avoid macOS AppManagement TCC."""
    if not app_path.exists():
        return
    tmp_del = app_path.with_name(app_path.name.replace(".app", "") + "_del")
    if tmp_del.exists():
        shutil.rmtree(tmp_del, ignore_errors=True)
    try:
        app_path.rename(tmp_del)
        shutil.rmtree(tmp_del, ignore_errors=True)
    except OSError:
        shutil.rmtree(app_path, ignore_errors=True)


@contextmanager
def _macos_bg_lock(
    lock_path: Path,
    timeout: float | None = None,
) -> Generator[None, None, None]:
    """Acquire an exclusive ``fcntl.flock`` on ``<home>/macos-bg/.lock``."""
    wait_timeout = _MACOS_BG_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if sys.platform != "win32":
            deadline = time.monotonic() + wait_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        msg = (
                            f"Timed out after {wait_timeout:g}s waiting for macOS background "
                            f"Chrome build lock at {lock_path}."
                        )
                        raise ConfigurationError(msg) from exc
                    time.sleep(_MACOS_BG_LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            if acquired and sys.platform != "win32":
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _is_macos_bg_chrome_current(
    target_bin: Path,
    target_plist: Path,
    src_version: str,
) -> bool:
    if not (target_bin.is_file() and target_plist.is_file()):
        return False
    try:
        with target_plist.open("rb") as f:
            dst_data = plistlib.load(f)
        return (
            str(dst_data.get("CFBundleVersion", "")) == src_version
            and dst_data.get("LSBackgroundOnly") in (True, "1", 1)
            and "LSUIElement" not in dst_data
        )
    except Exception:  # noqa: BLE001 — rebuild on any corrupt plist
        return False


def ensure_macos_background_chrome(
    home: Path,
    source_app: Path = _MACOS_CHROME_APP,
) -> Path:
    """Prepare or refresh ``<home>/macos-bg/Google Chrome.app`` for ``window_mode=background``.

    Clones the installed Google Chrome bundle via ``cp -c -R`` (APFS clonefile, falling back
    to ``cp -R`` across volumes), sets ``LSBackgroundOnly = True`` in ``Contents/Info.plist``
    (omitting ``LSUIElement``, which Chromium's ``base/apple/foundation_util.mm`` uses to
    identify nested helper bundles), strips extended attributes, and re-signs ad-hoc with
    ``codesign --force --deep --sign -``. Rebuilds automatically when ``CFBundleVersion``
    of the source Chrome changes, serialized across parallel lanes via ``<home>/macos-bg/.lock``.
    """
    source_plist = source_app / "Contents" / "Info.plist"
    if not source_plist.is_file():
        msg = (
            f"Google Chrome not found at {source_app}; required for "
            "GFLOW_CLI_WINDOW_MODE=background on macOS."
        )
        raise ConfigurationError(msg)

    with source_plist.open("rb") as f:
        src_data = plistlib.load(f)
    src_version = str(src_data.get("CFBundleVersion", ""))

    bg_dir = home / "macos-bg"
    target_app = bg_dir / "Google Chrome.app"
    target_bin = target_app / "Contents" / "MacOS" / "Google Chrome"
    target_plist = target_app / "Contents" / "Info.plist"

    with _macos_bg_lock(bg_dir / ".lock"):
        if _is_macos_bg_chrome_current(target_bin, target_plist, src_version):
            return target_bin

        staging = bg_dir / "chrome_staging_build"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _remove_macos_app_bundle(target_app)

        cp_res = subprocess.run(
            ["cp", "-c", "-R", str(source_app), str(staging)],
            capture_output=True,
            text=True,
            check=False,
        )
        if cp_res.returncode != 0:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            subprocess.run(
                ["cp", "-R", str(source_app), str(staging)],
                capture_output=True,
                text=True,
                check=True,
            )

        staging_plist = staging / "Contents" / "Info.plist"
        with staging_plist.open("rb") as f:
            plist_data = plistlib.load(f)
        plist_data.pop("LSUIElement", None)
        plist_data["LSBackgroundOnly"] = True
        with staging_plist.open("wb") as f:
            plistlib.dump(plist_data, f)

        subprocess.run(
            ["xattr", "-cr", str(staging)],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(staging)],
            capture_output=True,
            text=True,
            check=True,
        )
        staging.rename(target_app)
        return target_bin


def window_mode_args(window_mode: str, position: str) -> list[str]:
    """Build Chrome window-placement and anti-throttling flags for ``window_mode``."""
    mode = str(window_mode).lower()
    if mode == "normal":
        return []
    if mode == "background":
        if sys.platform != "darwin":
            log.warning(
                "browser_manager.window_mode_background_unsupported",
                platform=sys.platform,
                fallback="offscreen",
            )
            return window_position_args(position)
        return [*window_position_args(position), *ANTI_THROTTLING_ARGS]
    return window_position_args(position)


def generation_window_config(
    window_mode: str,
    position: str,
    *,
    home: Path,
) -> tuple[list[str], Path | None]:
    """Return ``(window_args, executable_path)`` for a generation browser launch."""
    mode = str(window_mode).lower()
    args = window_mode_args(mode, position)
    if mode == "background" and sys.platform == "darwin":
        return args, ensure_macos_background_chrome(home)
    return args, None


#: Playwright's launch error when Chrome hands the profile to a running instance
#: (#984, measured 2026-10-23, Windows, Chrome 141), e.g. the user's own Chrome window.
_PROFILE_IN_USE_MARKER = "profile is already in use by another instance"


def profile_in_use_error(exc: BaseException, profile_dir: Path) -> ProfileLockedError | None:
    """The typed error for a launch refused because another Chrome holds the profile.

    ``None`` for any other launch failure. ``ProfileLease`` cannot see a Chrome gflow did
    not start, so this message is the only signal; it states the cause outright.
    """
    msg = str(exc)
    # A read-only profile shares the header; its lock-file line marks it (#1059).
    if _PROFILE_IN_USE_MARKER not in msg or "Lock file can not be created" in msg:
        return None
    return ProfileLockedError(
        detail=(
            f"Chrome reports the profile dir {profile_dir} is already in use by another "
            "Chrome window, so gflow cannot open it"
        ),
        remediation_hint=(
            "Close the Chrome window that is using this profile (one you opened yourself, "
            "or one left by a crashed gflow run) and re-run, or use a different --profile."
        ),
    )


def browser_proxy_kwargs(proxy: str | None) -> dict[str, Any]:
    """Playwright ``proxy`` launch kwarg for GFLOW_CLI_BROWSER_PROXY (#1036); {} when unset.

    Always the kwarg, never ``--proxy-server`` in ``args``, so every launch site
    carries it the same way. The value is already validated by ``Settings``.
    """
    return {"proxy": {"server": proxy}} if proxy else {}


def channel_for_profile(profile_dir: Path) -> str | None:
    """Return the Playwright channel to use for ``profile_dir``, or None.

    Reads the ``.gflow_browser_strategy`` marker written by
    :class:`~gflow_cli.auth.real_chrome.RealChromeStrategy`. When the marker
    is ``"chrome"`` and **Google Chrome proper** is available at the paths
    Playwright expects, returns ``"chrome"`` so callers can pass it to
    ``launch_persistent_context(channel=...)`` — avoiding the downgrade-cleanup
    exit-33 that occurs when Playwright's bundled Chromium opens a profile
    created by Chrome 130+.

    Critically, this gate uses :func:`is_playwright_chrome_channel_available`
    (not :func:`is_chrome_available`) so that a system with only Chromium
    installed does NOT request ``channel="chrome"`` — Playwright's
    ``channel="chrome"`` resolves to hardcoded Google-Chrome paths and would
    otherwise fail with
    ``Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome``.

    Logs a warning when the marker requests Chrome but Chrome is no longer
    available at the expected Playwright paths, as the resulting launch against
    bundled Chromium will likely fail with the same exit-33 error.
    """
    import structlog as _structlog

    _log = _structlog.get_logger(__name__)
    marker = profile_dir / ".gflow_browser_strategy"
    if not marker.exists():
        return None
    strategy = marker.read_text(encoding="utf-8").strip()
    if strategy != "chrome":
        return None
    if is_playwright_chrome_channel_available():
        return "chrome"
    _log.warning(
        "browser_manager.chrome_marker_but_unavailable",
        profile_dir=str(profile_dir),
        hint="Profile was captured with system Chrome but Google Chrome is not "
        "found at the paths Playwright expects (e.g. /opt/google/chrome/chrome). "
        "Falling back to Playwright's bundled Chromium. "
        "Re-run `gflow auth login --browser chrome` after installing Chrome if "
        "you need the Chrome channel.",
    )
    return None


def chrome_strategy_requested(profile_dir: Path) -> bool:
    """True if the profile's ``.gflow_browser_strategy`` marker requests system Chrome.

    Distinct from :func:`channel_for_profile`, which returns ``None`` both when no
    marker is present (legitimate bundled-Chromium use) AND when the marker says
    ``chrome`` but Chrome can't be found (a silent downgrade). Callers use this to
    tell those two cases apart — see ``FlowApiClient._log_and_guard_launch`` and
    issue #222 (macOS: a chrome→bundled downgrade yields cookies the bundled
    Chromium can't decrypt → 401).
    """
    marker = profile_dir / ".gflow_browser_strategy"
    try:
        return marker.exists() and marker.read_text(encoding="utf-8").strip() == "chrome"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# #477: profile-engine (Chromium version) downgrade guard
# ---------------------------------------------------------------------------


def profile_last_version(profile_dir: Path) -> str | None:
    """The Chromium version that last wrote ``profile_dir``, or None.

    Chromium maintains a plain-text ``Last Version`` file at the user-data-dir
    root; it is exactly what Chromium's own startup downgrade detection reads.
    Best-effort: missing/blank/unreadable resolves to None.
    """
    try:
        text = (profile_dir / "Last Version").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def installed_chromium_version() -> str | None:
    """The bundled Chromium version of the ACTIVE engine's package, or None.

    Read from the driver's ``browsers.json`` registry (the same file
    ``playwright install`` uses), so no browser process is spawned. Resolves the
    registry via the active engine — ``patchright`` when
    ``GFLOW_CLI_BROWSER_ENGINE`` selects it, ``playwright`` otherwise — so the
    guard compares against the Chromium that will actually open the profile.
    Best-effort: any failure resolves to None.
    """
    try:
        import importlib

        from gflow_cli.api._engine import active_engine
        from gflow_cli.config import BrowserEngine

        module_name = "patchright" if active_engine() == BrowserEngine.PATCHRIGHT else "playwright"
        module = importlib.import_module(module_name)
        module_file = module.__file__
        if not module_file:
            return None
        registry_path = Path(module_file).parent / "driver" / "package" / "browsers.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for browser in registry.get("browsers", []):
            if browser.get("name") == "chromium":
                version = browser.get("browserVersion")
                return str(version) if version else None
    except Exception:  # noqa: BLE001 — best-effort by contract; any failure → None
        return None
    return None


def _major_version(version: str) -> int | None:
    try:
        return int(version.split(".", 1)[0])
    except ValueError:
        return None


def ensure_profile_engine_compatible(profile_dir: Path, channel: str | None) -> None:
    """Refuse to open ``profile_dir`` with an older bundled Chromium **major
    version** than last wrote it (#477).

    Opening a Chrome profile with an older engine triggers Chromium's downgrade
    cleanup, which can leave the newer store — session cookies included —
    unreadable, surfacing later as a mystery logout. The compare is
    major-version (milestone) only: the destructive cleanup is keyed to
    milestone downgrades, and a same-milestone build rollback (e.g. pinning
    Playwright back one patch release) is safe and must not brick generation.
    Skipped when ``channel`` resolves to ``"chrome"`` (real Google Chrome
    manages its own profile lifecycle) and whenever either version is unknown
    or unparseable (best-effort guard, never a new failure mode on odd hosts).
    Login is deliberately unguarded: it re-mints the session and rewrites the
    profile, making it the recovery path the remediation points at.
    """
    if channel is not None:
        return
    profile_version = profile_last_version(profile_dir)
    engine_version = installed_chromium_version()
    if profile_version is None or engine_version is None:
        return
    profile_major = _major_version(profile_version)
    engine_major = _major_version(engine_version)
    if profile_major is None or engine_major is None:
        return
    if engine_major >= profile_major:
        return
    log.error(
        "browser_manager.profile_engine_downgrade",
        profile_dir=str(profile_dir),
        profile_chromium=profile_version,
        engine_chromium=engine_version,
    )
    msg = (
        f"Profile at {profile_dir} was last written by Chromium {profile_version}, "
        "but the bundled Chromium about to open it is an older major version "
        f"({engine_version}). Opening it would trigger Chromium's downgrade cleanup, "
        "which can leave the session store unreadable."
    )
    # Remedy lives in ProfileEngineDowngradeError._default_remediation — naming
    # it here too printed the remedy twice in the CLI human output.
    raise ProfileEngineDowngradeError(msg)
