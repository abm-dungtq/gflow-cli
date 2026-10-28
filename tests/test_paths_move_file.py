"""``move_file`` replaces the target, including across filesystems (EXDEV)."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from gflow_cli.paths import move_file


def test_same_volume_move_replaces_target(tmp_path: Path) -> None:
    src, dst = tmp_path / "a.mp4", tmp_path / "out" / "b.mp4"
    dst.parent.mkdir()
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    move_file(src, dst)
    assert dst.read_bytes() == b"new"
    assert not src.exists()


def test_cross_volume_move_copies_then_removes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = tmp_path / "a.mp4", tmp_path / "b.mp4"
    src.write_bytes(b"clip")
    dst.write_bytes(b"old")
    real_replace = os.replace

    def replace(a: object, b: object) -> None:
        if Path(str(a)) == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(a, b)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(os, "replace", replace)
    move_file(src, dst)
    assert dst.read_bytes() == b"clip"
    assert not src.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.mp4"]


def test_other_os_errors_propagate(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        move_file(tmp_path / "missing.mp4", tmp_path / "b.mp4")
