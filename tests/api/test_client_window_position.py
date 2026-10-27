"""GFLOW_CLI_BROWSER_WINDOW_POSITION places the headed generation window (#923).

Off-screen by default so a generation run does not cover the user's desktop; any
``X,Y`` places it somewhere visible; empty restores Chrome's own placement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from gflow_cli.api.client import FlowApiClient
from gflow_cli.config import Settings

if TYPE_CHECKING:
    from pathlib import Path


def _position_args(tmp_path: Path, settings: Settings) -> list[str]:
    kwargs = FlowApiClient(profile_dir=tmp_path, settings=settings)._persistent_context_kwargs()  # noqa: SLF001
    return [a for a in kwargs["args"] if a.startswith("--window-position")]


def test_default_is_off_screen(tmp_path: Path) -> None:
    assert _position_args(tmp_path, Settings()) == ["--window-position=-30000,-30000"]


def test_env_places_window_where_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GFLOW_CLI_BROWSER_WINDOW_POSITION", "100,100")
    assert _position_args(tmp_path, Settings()) == ["--window-position=100,100"]


def test_empty_restores_chrome_placement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GFLOW_CLI_BROWSER_WINDOW_POSITION", "")
    assert _position_args(tmp_path, Settings()) == []


@pytest.mark.parametrize("bad", ["abc", "100", "1,2,3", "10 ,20", "--x=1,2"])
def test_malformed_position_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        Settings(browser_window_position=bad)


# ---------------------------------------------------------------------------
# GFLOW_CLI_WINDOW_MODE (offscreen | background | normal)
# ---------------------------------------------------------------------------


def test_window_mode_default_is_offscreen_and_args_unchanged(tmp_path: Path) -> None:
    from gflow_cli.config import WindowMode

    settings = Settings()
    assert settings.window_mode == WindowMode.OFFSCREEN
    kwargs = FlowApiClient(profile_dir=tmp_path, settings=settings)._persistent_context_kwargs()  # noqa: SLF001
    assert kwargs["args"] == [
        "--password-store=basic",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--window-position=-30000,-30000",
    ]
    assert "executable_path" not in kwargs


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("offscreen", "offscreen"),
        ("background", "background"),
        ("normal", "normal"),
        ("BACKGROUND", "background"),
        (" Normal ", "normal"),
    ],
)
def test_window_mode_parses_valid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    from gflow_cli.config import WindowMode

    monkeypatch.setenv("GFLOW_CLI_WINDOW_MODE", raw)
    assert Settings().window_mode == WindowMode(expected)


@pytest.mark.parametrize("bad", ["hidden", "headless", "invalid", "123"])
def test_window_mode_rejects_invalid_value(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("GFLOW_CLI_WINDOW_MODE", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_window_mode_normal_omits_position_and_throttling_args(tmp_path: Path) -> None:
    from gflow_cli.config import WindowMode

    settings = Settings(window_mode=WindowMode.NORMAL)
    kwargs = FlowApiClient(profile_dir=tmp_path, settings=settings)._persistent_context_kwargs()  # noqa: SLF001
    assert kwargs["args"] == [
        "--password-store=basic",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
    ]
    assert "executable_path" not in kwargs


def test_window_mode_background_on_darwin_uses_cloned_app_and_anti_throttling_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from gflow_cli.browser_manager import ANTI_THROTTLING_ARGS
    from gflow_cli.config import WindowMode

    fake_bin = tmp_path / "macos-bg" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    settings = Settings(home=tmp_path, window_mode=WindowMode.BACKGROUND)
    client = FlowApiClient(profile_dir=tmp_path, settings=settings)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "gflow_cli.browser_manager.ensure_macos_background_chrome",
        lambda home: fake_bin,
    )
    (tmp_path / ".gflow_browser_strategy").write_text("chrome", encoding="utf-8")

    kwargs = client._persistent_context_kwargs()  # noqa: SLF001
    assert kwargs["executable_path"] == str(fake_bin)
    assert kwargs["channel"] is None
    assert "--window-position=-30000,-30000" in kwargs["args"]
    for flag in ANTI_THROTTLING_ARGS:
        assert flag in kwargs["args"]

    # Guard must recognize executable_path as satisfying the chrome strategy on darwin.
    client._log_and_guard_launch(kwargs)  # noqa: SLF001


def test_window_mode_background_off_darwin_falls_back_to_offscreen_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from unittest.mock import patch

    import structlog

    from gflow_cli.config import WindowMode

    settings = Settings(home=tmp_path, window_mode=WindowMode.BACKGROUND)
    client = FlowApiClient(profile_dir=tmp_path, settings=settings)

    monkeypatch.setattr(sys, "platform", "linux")
    cap = structlog.testing.LogCapture()
    with patch(
        "gflow_cli.browser_manager.log",
        structlog.wrap_logger(None, processors=[cap]),
    ):
        kwargs = client._persistent_context_kwargs()  # noqa: SLF001

    assert "executable_path" not in kwargs
    assert kwargs["args"] == [
        "--password-store=basic",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--window-position=-30000,-30000",
    ]
    assert any(
        e["event"] == "browser_manager.window_mode_background_unsupported" for e in cap.entries
    )


def test_login_launch_is_unaffected_by_window_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gflow_cli.auth.internal_chromium import login_launch_kwargs
    from gflow_cli.auth.real_chrome import _build_chrome_args  # noqa: PLC2701
    from gflow_cli.browser_manager import ANTI_THROTTLING_ARGS
    from gflow_cli.config import reset_settings

    monkeypatch.setenv("GFLOW_CLI_WINDOW_MODE", "background")
    reset_settings()
    try:
        kw = login_launch_kwargs(tmp_path, headless=False, channel="chrome")
        assert kw["headless"] is False
        assert kw["channel"] == "chrome"
        assert "executable_path" not in kw
        assert "--window-size=1920,1080" in kw["args"]
        assert not any(a.startswith("--window-position") for a in kw["args"])
        for flag in ANTI_THROTTLING_ARGS:
            assert flag not in kw["args"]

        raw_args = _build_chrome_args(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", tmp_path, False
        )
        assert "--window-size=1920,1080" in raw_args
        assert not any(a.startswith("--window-position") for a in raw_args)
    finally:
        reset_settings()


def test_ensure_macos_background_chrome_clones_and_refreshes_on_version_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plistlib
    import shutil
    import subprocess

    from gflow_cli.browser_manager import ensure_macos_background_chrome
    from gflow_cli.errors import ConfigurationError

    home = tmp_path / "home"
    src_app = tmp_path / "Applications" / "Google Chrome.app"

    # Missing source Chrome -> ConfigurationError
    with pytest.raises(ConfigurationError, match="Google Chrome not found"):
        ensure_macos_background_chrome(home, source_app=src_app)

    # Create fake source Chrome bundle v1
    (src_app / "Contents" / "MacOS").mkdir(parents=True)
    src_bin = src_app / "Contents" / "MacOS" / "Google Chrome"
    src_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    src_plist = src_app / "Contents" / "Info.plist"
    with src_plist.open("wb") as f:
        plistlib.dump(
            {"CFBundleVersion": "8078.47", "CFBundleIdentifier": "com.google.Chrome"},
            f,
        )

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append([str(c) for c in cmd])
        if cmd[0] == "cp":
            dst = tmp_path / "home" / "macos-bg" / "chrome_staging_build"
            shutil.copytree(src_app, dst)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("gflow_cli.browser_manager.subprocess.run", fake_run)

    # 1. First call creates the clone and runs cp, xattr, codesign
    out_bin = ensure_macos_background_chrome(home, source_app=src_app)
    assert (
        out_bin == home / "macos-bg" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    )
    assert out_bin.is_file()
    assert any(c[0] == "cp" and "-c" in c for c in calls)
    assert any(c[0] == "codesign" for c in calls)

    cloned_plist_path = home / "macos-bg" / "Google Chrome.app" / "Contents" / "Info.plist"
    with cloned_plist_path.open("rb") as f:
        cloned_plist = plistlib.load(f)
    assert cloned_plist["LSBackgroundOnly"] is True
    assert "LSUIElement" not in cloned_plist
    assert cloned_plist["CFBundleVersion"] == "8078.47"

    # 2. Second call with unchanged version reuses the clone without calling subprocess
    calls.clear()
    out_bin_2 = ensure_macos_background_chrome(home, source_app=src_app)
    assert out_bin_2 == out_bin
    assert calls == []

    # 3. Source Chrome updates to 8078.48; simulate cp -c failing -> fallback to cp -R
    with src_plist.open("wb") as f:
        plistlib.dump(
            {"CFBundleVersion": "8078.48", "CFBundleIdentifier": "com.google.Chrome"},
            f,
        )

    def fake_run_cross_vol(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append([str(c) for c in cmd])
        if cmd[0] == "cp" and "-c" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="cross-device"
            )
        if cmd[0] == "cp":
            dst = tmp_path / "home" / "macos-bg" / "chrome_staging_build"
            shutil.copytree(src_app, dst)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("gflow_cli.browser_manager.subprocess.run", fake_run_cross_vol)
    out_bin_3 = ensure_macos_background_chrome(home, source_app=src_app)
    assert out_bin_3 == out_bin
    assert any(c[0] == "cp" and "-c" not in c for c in calls)
    with cloned_plist_path.open("rb") as f:
        refreshed_plist = plistlib.load(f)
    assert refreshed_plist["CFBundleVersion"] == "8078.48"
    assert refreshed_plist["LSBackgroundOnly"] is True


def test_ensure_macos_background_chrome_acquires_lock_and_times_out_on_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import plistlib
    import shutil
    import subprocess

    fcntl = pytest.importorskip("fcntl")

    import gflow_cli.browser_manager as bm
    from gflow_cli.browser_manager import ensure_macos_background_chrome
    from gflow_cli.errors import ConfigurationError

    home = tmp_path / "home"
    src_app = tmp_path / "Applications" / "Google Chrome.app"
    (src_app / "Contents" / "MacOS").mkdir(parents=True)
    (src_app / "Contents" / "MacOS" / "Google Chrome").write_text("#!/bin/sh\n", encoding="utf-8")
    with (src_app / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleVersion": "8078.48"}, f)

    lock_path = home / "macos-bg" / ".lock"
    observed_locked_during_build = False

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal observed_locked_during_build
        if cmd[0] == "cp":
            probe_fd = os.open(lock_path, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed_locked_during_build = True
            finally:
                os.close(probe_fd)
            shutil.copytree(src_app, home / "macos-bg" / "chrome_staging_build")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("gflow_cli.browser_manager.subprocess.run", fake_run)
    ensure_macos_background_chrome(home, source_app=src_app)
    assert observed_locked_during_build is True

    # After return, the lock is released; holding it past timeout raises ConfigurationError.
    holder_fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(bm, "_MACOS_BG_LOCK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(bm, "_MACOS_BG_LOCK_POLL_SECONDS", 0.01)
        with pytest.raises(ConfigurationError, match=r"Timed out.*macos-bg/\.lock"):
            ensure_macos_background_chrome(home, source_app=src_app)
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_ensure_macos_background_chrome_skips_rebuild_when_other_lane_just_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import plistlib

    fcntl = pytest.importorskip("fcntl")

    from gflow_cli.browser_manager import ensure_macos_background_chrome

    home = tmp_path / "home"
    src_app = tmp_path / "Applications" / "Google Chrome.app"
    (src_app / "Contents" / "MacOS").mkdir(parents=True)
    (src_app / "Contents" / "MacOS" / "Google Chrome").write_text("#!/bin/sh\n", encoding="utf-8")
    with (src_app / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleVersion": "8078.48"}, f)

    # Target starts with outdated version 8078.47 while Lane 1 holds the lock.
    target_app = home / "macos-bg" / "Google Chrome.app"
    (target_app / "Contents" / "MacOS").mkdir(parents=True)
    target_bin = target_app / "Contents" / "MacOS" / "Google Chrome"
    target_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    target_plist = target_app / "Contents" / "Info.plist"
    with target_plist.open("wb") as f:
        plistlib.dump({"CFBundleVersion": "8078.47", "LSBackgroundOnly": True}, f)

    lock_path = home / "macos-bg" / ".lock"
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    waited_for_lock = False

    def fake_sleep(_seconds: float) -> None:
        nonlocal waited_for_lock
        waited_for_lock = True
        # Simulate Lane 1 finishing the 8078.48 build and releasing the lock.
        with target_plist.open("wb") as f:
            plistlib.dump({"CFBundleVersion": "8078.48", "LSBackgroundOnly": True}, f)
        fcntl.flock(holder_fd, fcntl.LOCK_UN)

    def fail_if_rebuilt(cmd: list[str], **_kwargs: object) -> None:
        pytest.fail(f"Expected no rebuild after Lane 1 finished, but ran: {cmd}")

    try:
        monkeypatch.setattr("gflow_cli.browser_manager.time.sleep", fake_sleep)
        monkeypatch.setattr("gflow_cli.browser_manager.subprocess.run", fail_if_rebuilt)
        out_bin = ensure_macos_background_chrome(home, source_app=src_app)
        assert waited_for_lock is True
        assert out_bin == target_bin
    finally:
        os.close(holder_fd)


@pytest.mark.asyncio
async def test_ui_automation_setup_respects_window_mode_background_on_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    from gflow_cli.api.transports.ui_automation import UiAutomationTransport
    from gflow_cli.browser_manager import ANTI_THROTTLING_ARGS
    from gflow_cli.config import reset_settings

    fake_bin = tmp_path / "macos-bg" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    monkeypatch.setenv("GFLOW_CLI_WINDOW_MODE", "background")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "gflow_cli.browser_manager.ensure_macos_background_chrome",
        lambda home: fake_bin,
    )
    reset_settings()

    fake_page = MagicMock()
    fake_page.goto = AsyncMock()
    fake_ctx = MagicMock()
    fake_ctx.pages = [fake_page]
    fake_ctx.add_init_script = AsyncMock()
    fake_ctx.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)
    fake_pw = MagicMock()
    fake_pw.chromium = fake_chromium
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    try:
        with patch(
            "gflow_cli.api.transports.ui_automation.async_playwright",
            MagicMock(return_value=mock_cm),
        ):
            transport = UiAutomationTransport()
            try:
                await transport.setup(profile_dir=tmp_path)
                call_kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
                assert call_kwargs["executable_path"] == str(fake_bin)
                assert call_kwargs["channel"] is None
                for flag in ANTI_THROTTLING_ARGS:
                    assert flag in call_kwargs["args"]
            finally:
                await transport.teardown()
    finally:
        reset_settings()
