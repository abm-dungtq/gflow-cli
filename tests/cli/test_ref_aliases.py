"""Tests for local reference image naming (`--ref name=path`, config `{"name", "path"}`)
and `@name` -> `reference image N` prompt replacement before `resolve_and_apply`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from gflow_cli.cli import main as cli_main
from gflow_cli.cli_run import BatchConfig
from gflow_cli.errors import ConfigurationError
from gflow_cli.image_batch import parse_batch_item_dict
from gflow_cli.ref_aliases import (
    build_alias_map,
    default_ref_name,
    replace_ref_aliases,
    split_cli_ref,
)

if TYPE_CHECKING:
    from pathlib import Path

_VALID_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestSplitCliRefAndDefaultName:
    def test_split_cli_ref_with_valid_name_and_existing_file(self, tmp_path: Path) -> None:
        img = tmp_path / "cuong.jpg"
        img.write_bytes(_VALID_PNG)
        name, path_str = split_cli_ref(f"cuong={img}")
        assert name == "cuong"
        assert path_str == str(img)

    def test_split_cli_ref_without_name_returns_none_and_raw(self, tmp_path: Path) -> None:
        img = tmp_path / "dep.jpg"
        img.write_bytes(_VALID_PNG)
        name, path_str = split_cli_ref(str(img))
        assert name is None
        assert path_str == str(img)

    def test_split_cli_ref_with_equals_but_missing_file_treats_whole_string_as_path(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "khong-ton-tai.jpg"
        raw = f"cuong={missing}"
        name, path_str = split_cli_ref(raw)
        assert name is None
        assert path_str == raw

        # Also when a file literally has '=' in its name and the RHS is not a file:
        eq_file = tmp_path / "a=b.png"
        eq_file.write_bytes(_VALID_PNG)
        name2, path_str2 = split_cli_ref(str(eq_file))
        assert name2 is None
        assert path_str2 == str(eq_file)

    def test_split_cli_ref_with_invalid_name_before_equals(self, tmp_path: Path) -> None:
        img = tmp_path / "cuong.jpg"
        img.write_bytes(_VALID_PNG)
        raw = f"bad name={img}"
        name, path_str = split_cli_ref(raw)
        assert name is None
        assert path_str == raw

    def test_default_ref_name_uses_stem_and_ignores_batch_ref(self) -> None:
        assert default_ref_name("/duong/dan/cuong-ref.jpg") == "cuong-ref"
        assert default_ref_name("dep.png") == "dep"
        assert default_ref_name("batch:0") is None


class TestAliasReplacementAndDuplicates:
    def test_shared_name_is_ambiguous_only_when_mentioned(self) -> None:
        assert build_alias_map(("Cuong", "dep", "cuong")) == {"cuong": None, "dep": 2}
        # Same-named files without any @mention keep working, prompt untouched.
        assert replace_ref_aliases("no mention", ("cuong", "cuong")) == "no mention"
        out = replace_ref_aliases("@dep only", ("cuong", "dep", "cuong"))
        assert out == "reference image 2 only"
        with pytest.raises(ConfigurationError, match="more than one reference image"):
            replace_ref_aliases("@cuong", ("cuong", "cuong"))

    def test_replaces_mentions_by_1_based_order_case_insensitive_and_whole_word(self) -> None:
        prompt = "@cuong cầm @dep, @CUONG nhìn @Dep nhưng không ăn vào @cuong2 hay @cuong-2"
        out = replace_ref_aliases(prompt, ("cuong", "dep"))
        assert out == (
            "reference image 1 cầm reference image 2, "
            "reference image 1 nhìn reference image 2 "
            "nhưng không ăn vào @cuong2 hay @cuong-2"
        )

    def test_numbering_preserves_position_when_mixed_with_unnamed_refs(self) -> None:
        # Position 1 is batch:0 (None), position 2 is cuong, position 3 is dep-ref
        out = replace_ref_aliases("@cuong và @dep-ref", (None, "cuong", "dep-ref"))
        assert out == "reference image 2 và reference image 3"

    def test_unknown_mentions_and_escaped_at_stay_untouched(self) -> None:
        prompt = "@cuong đứng cạnh @hero_project và @@cuong cùng mail@cuong.vn"
        out = replace_ref_aliases(prompt, ("cuong",))
        assert out == "reference image 1 đứng cạnh @hero_project và @@cuong cùng mail@cuong.vn"

    def test_prompt_unchanged_byte_for_byte_when_no_named_refs_or_no_at(self) -> None:
        raw_with_at = "Cường @cuong đi bộ\n\t✨ byte-for-byte"
        out_no_aliases = replace_ref_aliases(raw_with_at, ())
        assert out_no_aliases == raw_with_at
        assert out_no_aliases.encode("utf-8") == raw_with_at.encode("utf-8")

        out_none_aliases = replace_ref_aliases(raw_with_at, (None, None))
        assert out_none_aliases == raw_with_at
        assert out_none_aliases.encode("utf-8") == raw_with_at.encode("utf-8")

        raw_no_at = "Cường cầm chiếc dép tổ ong\n\t✨ không có ký tự a-còng"
        out_no_at = replace_ref_aliases(raw_no_at, ("cuong", "dep"))
        assert out_no_at == raw_no_at
        assert out_no_at.encode("utf-8") == raw_no_at.encode("utf-8")


class TestConfigRefsParsingAndRun:
    def test_parse_batch_item_dict_supports_object_and_string_refs_and_batch_n(self) -> None:
        item = parse_batch_item_dict(
            {
                "text": "@cuong cầm @dep-to-ong trong cảnh tiếp theo",
                "refs": [
                    "batch:0",
                    {"name": "cuong", "path": "refs/nv-cuong.jpg"},
                    "refs/dep-to-ong.jpg",
                ],
            },
            1,
        )
        assert item.refs == ("batch:0", "refs/nv-cuong.jpg", "refs/dep-to-ong.jpg")
        assert item.ref_aliases == (None, "cuong", "dep-to-ong")
        assert item.text == "reference image 2 cầm reference image 3 trong cảnh tiếp theo"

    def test_parse_batch_item_dict_supports_single_ref_object(self) -> None:
        item = parse_batch_item_dict(
            {
                "text": "@nhan-vat cười tươi",
                "ref": {"name": "nhan-vat", "path": "cuong.png"},
            },
            0,
        )
        assert item.ref == "cuong.png"
        assert item.ref_aliases == ("nhan-vat",)
        assert item.text == "reference image 1 cười tươi"

    def test_parse_batch_item_dict_rejects_duplicate_ref_names(self) -> None:
        with pytest.raises(ConfigurationError, match="more than one reference image"):
            parse_batch_item_dict(
                {
                    "text": "@cuong",
                    "refs": [
                        {"name": "cuong", "path": "a.jpg"},
                        "dir/cuong.jpg",
                    ],
                },
                0,
            )

    def test_parse_batch_item_dict_rejects_invalid_ref_object(self) -> None:
        with pytest.raises(ConfigurationError, match=r"prompts\[0\]\.refs\[0\]\.name"):
            parse_batch_item_dict(
                {"text": "x", "refs": [{"name": "bad name", "path": "a.jpg"}]},
                0,
            )
        with pytest.raises(ConfigurationError, match=r"prompts\[0\]\.ref object"):
            parse_batch_item_dict(
                {"text": "x", "ref": {"name": "cuong"}},
                0,
            )

    def test_batch_config_resolves_relative_object_paths_and_rewrites_prompt(
        self, tmp_path: Path
    ) -> None:
        sub = tmp_path / "assets"
        sub.mkdir()
        cuong = sub / "nv-cuong.png"
        dep = sub / "dep.png"
        cuong.write_bytes(_VALID_PNG)
        dep.write_bytes(_VALID_PNG)

        cfg_file = tmp_path / "batch.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "prompts": [
                        {
                            "text": "@cuong đi chân đất tìm @dep",
                            "refs": [
                                {"name": "cuong", "path": "assets/nv-cuong.png"},
                                "assets/dep.png",
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        cfg = BatchConfig.from_json_path(cfg_file)
        assert cfg.prompts[0].refs == (str(cuong.resolve()), str(dep.resolve()))
        assert cfg.prompts[0].text == "reference image 1 đi chân đất tìm reference image 2"


class TestI2iCliNamedRefs:
    def test_i2i_cli_passes_replaced_prompt_before_resolve_and_apply(self, tmp_path: Path) -> None:
        from gflow_cli.services import mentions as mentions_mod
        from tests.cli.test_cli_image import _make_i2i_client

        cuong_file = tmp_path / "nv-tieu-cuong.png"
        dep_file = tmp_path / "dep.png"
        cuong_file.write_bytes(_VALID_PNG)
        dep_file.write_bytes(_VALID_PNG)
        out_dir = tmp_path / "out"

        client = _make_i2i_client(upload_uuid="unused")
        seen_before_resolve: list[str] = []
        real_resolve = mentions_mod.resolve_and_apply

        async def _spy_resolve(c: Any, req: Any, **kwargs: Any) -> Any:
            seen_before_resolve.append(req.prompt)
            return await real_resolve(c, req, **kwargs)

        with (
            patch("gflow_cli.cli_image.FlowApiClient", return_value=client),
            patch("gflow_cli.cli_image._make_provider_dir", return_value=tmp_path / "prof"),
            patch("gflow_cli.cli_image._resolve_profile", return_value="default"),
            patch(
                "gflow_cli.services.mentions.resolve_and_apply",
                AsyncMock(side_effect=_spy_resolve),
            ),
        ):
            result = CliRunner().invoke(
                cli_main,
                [
                    "image",
                    "i2i",
                    "@cuong cầm @dep ngoài sân",
                    "--ref",
                    f"cuong={cuong_file}",
                    "--ref",
                    str(dep_file),
                    "--out",
                    str(out_dir),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert seen_before_resolve == ["reference image 1 cầm reference image 2 ngoài sân"]
        req = client.generate_image.call_args.kwargs["req"]
        assert req.prompt == "reference image 1 cầm reference image 2 ngoài sân"
        assert req.ref_paths == (cuong_file.resolve(), dep_file.resolve())

    def test_i2i_cli_duplicate_ref_names_exits_11(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.png"
        f2 = tmp_path / "b.png"
        f1.write_bytes(_VALID_PNG)
        f2.write_bytes(_VALID_PNG)

        result = CliRunner().invoke(
            cli_main,
            [
                "image",
                "i2i",
                "@cuong",
                "--ref",
                f"cuong={f1}",
                "--ref",
                f"cuong={f2}",
            ],
        )
        assert result.exit_code == 11, result.output
