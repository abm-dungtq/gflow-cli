"""What does Flow see when gflow opens real Chrome headless? (no submit, $0)

Opens ``FlowApiClient`` on one profile, once headed and once headless, loads Flow and
records the browser identity the page reports: user agent, UA-CH brands, and
``navigator.webdriver``. Generation itself is measured separately by running
``GFLOW_CLI_HEADLESS=true gflow image t2i ...``; this script only answers whether the
headless arm advertises itself.

Usage:
    uv run python scripts/dev/spike_headless_generation.py --profile thu-headless

Output: ``scripts/dev/_spike_out/spike_headless_generation_<ts>.json`` (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from _spike_common import build_client, default_out_path, resolve_profile_dir, step

FLOW_URL = "https://labs.google/fx/tools/flow"
_IDENTITY_JS = """async () => ({
    url: location.href,
    userAgent: navigator.userAgent,
    brands: navigator.userAgentData ? navigator.userAgentData.brands : null,
    webdriver: navigator.webdriver,
    visibility: document.visibilityState,
    screen: [screen.width, screen.height],
})"""


async def _probe(profile: str, *, headless: bool) -> dict[str, Any]:
    async with build_client(resolve_profile_dir(profile), headless=headless) as client:
        page = client.page
        await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3000)
        identity: dict[str, Any] = await page.evaluate(_IDENTITY_JS)
    identity["headless"] = headless
    identity["headless_marker_in_ua"] = "HeadlessChrome" in identity["userAgent"]
    return identity


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    results = []
    for headless in (False, True):
        step("probe", f"headless={headless}")
        results.append(await _probe(args.profile, headless=headless))
        step("seen", json.dumps(results[-1], ensure_ascii=False))
    out = default_out_path("spike_headless_generation")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    step("done", str(out))


if __name__ == "__main__":
    asyncio.run(main())
