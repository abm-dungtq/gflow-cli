"""Local reference image naming and ``@name`` -> ``reference image N`` prompt replacement."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gflow_cli.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Sequence

_REF_NAME_RE = re.compile(r"^[\w-]+$")
_MENTION_TOKEN_RE = re.compile(r"@@|(?<!\w)@([\w-]+)(?![\w-])")


def is_valid_ref_name(name: str) -> bool:
    """Return True when ``name`` matches ``^[\\w-]+$``."""
    return bool(_REF_NAME_RE.fullmatch(name))


def default_ref_name(path_or_spec: str | Path) -> str | None:
    """Return the default alias (filename stem) for a local reference path."""
    if isinstance(path_or_spec, str) and path_or_spec.startswith("batch:"):
        return None
    stem = Path(path_or_spec).stem
    return stem or None


def split_cli_ref(raw: str) -> tuple[str | None, str]:
    """Split CLI ``--ref name=path`` when ``name`` matches ``^[\\w-]+$`` and ``path`` is a file.

    Otherwise returns ``(None, raw)`` so ``raw`` is treated as an ordinary path or UUID.
    """
    if "=" in raw:
        name_part, path_part = raw.split("=", 1)
        if is_valid_ref_name(name_part) and path_part and Path(path_part).expanduser().is_file():
            return name_part, path_part
    return None, raw


def parse_config_ref_entry(raw: Any, *, label: str) -> tuple[str | None, str]:
    """Parse one ``ref`` or ``refs[i]`` entry from a ``gflow run`` config.

    Accepts a non-empty string or ``{"name": "<alias>", "path": "<path>"}``.
    Returns ``(explicit_name_or_none, raw_path_str)``.
    """
    if isinstance(raw, str):
        if not raw.strip():
            msg = f"{label} must be a non-empty string."
            raise ConfigurationError(msg)
        return None, raw
    if isinstance(raw, dict):
        raw_dict = cast("dict[str, Any]", raw)
        if set(raw_dict) != {"name", "path"}:
            msg = f"{label} object must contain only 'name' and 'path'."
            raise ConfigurationError(msg)
        name = raw_dict.get("name")
        path = raw_dict.get("path")
        if not isinstance(name, str) or not is_valid_ref_name(name):
            msg = f"{label}.name must match ^[\\w-]+$ (got {name!r})."
            raise ConfigurationError(msg)
        if not isinstance(path, str) or not path.strip():
            msg = f"{label}.path must be a non-empty string."
            raise ConfigurationError(msg)
        return name, path
    msg = f"{label} must be a non-empty string or {{'name', 'path'}} object."
    raise ConfigurationError(msg)


def build_alias_map(aliases: Sequence[str | None]) -> dict[str, int | None]:
    """Map each case-folded alias to its 1-based reference index.

    A name shared by two references maps to ``None``: it is only an error when a
    prompt actually mentions it, so same-named files keep working without ``@``.
    """
    mapping: dict[str, int | None] = {}
    for idx, alias in enumerate(aliases, start=1):
        if alias:
            key = alias.casefold()
            mapping[key] = None if key in mapping else idx
    return mapping


def replace_ref_aliases(prompt: str, aliases: Sequence[str | None]) -> str:
    """Replace ``@<alias>`` tokens matching local reference images with ``reference image N``.

    * Matching is case-insensitive and whole-word (``@cuong`` does not match ``@cuong2``).
    * Unmatched ``@token``s (and ``@@`` escapes) are preserved untouched for ``resolve_and_apply``.
    * Mentioning a name two references share raises :class:`ConfigurationError`.
    * When ``aliases`` has no named entries or ``prompt`` contains no ``@``, ``prompt`` is returned
      unchanged byte-for-byte.
    """
    if "@" not in prompt:
        return prompt
    alias_map = build_alias_map(aliases)
    if not alias_map:
        return prompt

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        if token is None or (key := token.casefold()) not in alias_map:
            return match.group(0)
        idx = alias_map[key]
        if idx is None:
            msg = f"@{token} matches more than one reference image; give them distinct names."
            raise ConfigurationError(msg)
        return f"reference image {idx}"

    return _MENTION_TOKEN_RE.sub(_sub, prompt)
