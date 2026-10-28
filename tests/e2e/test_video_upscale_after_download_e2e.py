"""Live proof that ``--upscale`` / MCP ``upscale=True`` deliver a 1080p clip.

Offline tests fake ``client.upsample_video``, so they show the helper swaps files and
returns the page to the project; only a live run shows Flow's 1080p export is reachable
right after a fresh generation, on the page the generation left behind. The MCP case is
the queued-worker path, which is different code from the CLI path and runs separately.

Opt-in: ``-m e2e_video`` with ``GFLOW_CLI_E2E_PROFILE``, ``GFLOW_CLI_E2E_PROJECT`` and
``GFLOW_CLI_E2E_RUN_VIDEO=1``. Each test bills one ``veo-lite`` 4 s clip; the 1080p export
itself spends no credits.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from gflow_cli.api.transports.migrated_video_upscale import _tkhd_dimensions  # pyright: ignore[reportPrivateUsage]
from gflow_cli.mcp import tools as mcp_tools

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_video]

_PROMPT = "a paper boat drifting on calm water, slow push in"


def _require_live() -> tuple[str, str]:
    if os.environ.get("GFLOW_CLI_E2E_RUN_VIDEO", "") != "1":
        pytest.skip("set GFLOW_CLI_E2E_RUN_VIDEO=1 to run (spends 1 Veo credit per test)")
    project = os.environ.get("GFLOW_CLI_E2E_PROJECT", "").strip()
    if not project:
        pytest.skip("GFLOW_CLI_E2E_PROJECT must name an existing Flow project id")
    return os.environ["GFLOW_CLI_E2E_PROFILE"].strip(), project


def _write_frame(path: Path) -> Path:
    """A real 1280x720 gradient PNG (Flow refuses tiny synthetic uploads)."""
    width, height = 1280, 720

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes(((x * 255) // width, (y * 255) // height, 160))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
    return path


def _short_side(path: Path) -> int:
    data = path.read_bytes()
    return max(min(w, h) for w, h in _tkhd_dimensions(data, 0, len(data)))


def test_e2e_cli_i2v_upscale_writes_a_1080p_clip(e2e_profile_dir: Path, tmp_path: Path) -> None:
    """``gflow video i2v --upscale`` leaves one 1080p file at ``-o`` and reports it."""
    del e2e_profile_dir
    profile, project = _require_live()
    out = tmp_path / "clip.mp4"
    proc = subprocess.run(  # noqa: S603 - our own CLI, fixed argv
        [
            sys.executable,
            "-m",
            "gflow_cli",
            "video",
            "i2v",
            _PROMPT,
            "--initial-frame",
            str(_write_frame(tmp_path / "frame.png")),
            "--model",
            "veo-lite",
            "--aspect",
            "16:9",
            "--profile",
            profile,
            "--project",
            project,
            "--upscale",
            "-o",
            str(out),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = json.loads(proc.stdout)
    assert payload.get("upscaled") == "1080p", payload
    assert _short_side(out) >= 1080
    assert sorted(p.name for p in tmp_path.glob("*.mp4")) == ["clip.mp4"]


async def test_e2e_mcp_generate_video_upscale_writes_a_1080p_clip(
    e2e_profile_dir: Path, tmp_path: Path
) -> None:
    """MCP ``gflow_generate_video(upscale=True)`` carries the flag through the queue."""
    del e2e_profile_dir
    profile, project = _require_live()
    out = tmp_path / "mcp-clip.mp4"
    result = await mcp_tools.gflow_generate_video(
        prompt=_PROMPT,
        mode="i2v",
        initial_frame=str(_write_frame(tmp_path / "frame.png")),
        model="veo-lite",
        duration=4,
        aspect="16:9",
        profile=profile,
        project=project,
        output=str(out),
        upscale=True,
        wait=True,
    )
    assert result["status"] == "completed", result
    assert result.get("upscaled") == "1080p", result
    assert _short_side(out) >= 1080
