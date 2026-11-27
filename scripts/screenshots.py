"""Capture GUI screenshots with Playwright + Microsoft Edge.

This runs the Vite dev server with MSW mocks so screenshots are deterministic
and require no Android tooling.  Every screenshot is labelled "mock data" in the
docs; it is UI evidence, not real-emulator evidence.

Usage:
    python scripts/screenshots.py --url http://127.0.0.1:5199 --out docs/screenshots
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

ROUTES = [
    ("dashboard", "/"),
    ("fleet", "/fleet"),
    ("instance-detail", "/fleet/avd-pixel7-01"),
    ("deploy", "/deploy"),
    ("images", "/images"),
    ("templates", "/templates"),
    ("proxies", "/proxies"),
    ("scripts", "/scripts"),
    ("runs", "/runs"),
    ("doctor", "/doctor"),
    ("settings", "/settings"),
    ("portable", "/portable"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5199")
    parser.add_argument("--out", default="docs/screenshots")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1000)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed; run: python -m pip install playwright", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    with sync_playwright() as playwright:
        kwargs = {"headless": True}
        if EDGE.exists():
            kwargs["executable_path"] = str(EDGE)
        try:
            browser = playwright.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"could not launch a Chromium-based browser: {exc}", file=sys.stderr)
            return 2
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=1,
            color_scheme="dark",
        )
        context.add_init_script(
            "localStorage.setItem('avdfleet.token', 'screenshot-mock-token');"
        )
        page = context.new_page()
        page.on("console", lambda message: None)
        for name, route in ROUTES:
            url = args.url.rstrip("/") + route
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2500)
                target = out / f"{name}.png"
                page.screenshot(path=str(target), full_page=True)
                print(f"captured {url} -> {target}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{route}: {exc}")
                print(f"FAILED {url}: {exc}", file=sys.stderr)
        context.close()
        browser.close()
    if failures:
        print(f"{len(failures)} route(s) failed:", file=sys.stderr)
        for failure in failures:
            print(" -", failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
