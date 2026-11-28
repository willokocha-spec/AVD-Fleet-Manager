"""Build the release artifacts: UI assets inside the package, wheel, sdist.

Usage:
    python scripts/build_release.py            # assumes ui/dist was built
    python scripts/build_release.py --build-ui # run pnpm build first

The daemon serves ``src/avdfleet/api/static``; this script copies the Vite build
there, then builds a wheel and verifies the critical data files are inside it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
STATIC = ROOT / "src" / "avdfleet" / "api" / "static"
DIST = ROOT / "dist"
# Toolchain is discovered, not hardcoded: set AVDFLEET_NODE / AVDFLEET_PNPM to
# override, otherwise the PATH is used. PNPM may be a .mjs script that needs node.
NODE = os.environ.get("AVDFLEET_NODE") or shutil.which("node") or ""
PNPM = os.environ.get("AVDFLEET_PNPM") or shutil.which("pnpm") or ""


def pnpm_command(*args: str) -> list[str]:
    """Build a command for pnpm, whether it is a shim or a .mjs script."""
    if not PNPM:
        raise SystemExit(
            "pnpm not found; install it or set AVDFLEET_PNPM (and AVDFLEET_NODE"
            " when pointing at pnpm.mjs)"
        )
    if PNPM.endswith(".mjs"):
        if not NODE:
            raise SystemExit("node not found; set AVDFLEET_NODE")
        return [NODE, PNPM, *args]
    return [PNPM, *args]


def run(command: list[str], cwd: Path) -> None:
    print("+", " ".join(str(part) for part in command), f"(cwd={cwd})")
    subprocess.run(command, cwd=cwd, check=True)


def build_ui() -> None:
    run(pnpm_command("install", "--frozen-lockfile"), UI)
    run(pnpm_command("build"), UI)


def copy_static() -> None:
    index = UI / "dist" / "index.html"
    if not index.exists():
        raise SystemExit(
            "ui/dist/index.html is missing; run with --build-ui or `pnpm build` in ui/"
        )
    STATIC.mkdir(parents=True, exist_ok=True)
    for child in STATIC.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in (UI / "dist").iterdir():
        if child.name == "mockServiceWorker.js":
            # The production daemon serves the real API; the MSW worker is only
            # for Vite/Playwright mock runs and should not ship.
            continue
        target = STATIC / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    print(f"copied {sum(1 for _ in STATIC.rglob('*') if _.is_file())} UI files into {STATIC}")


def build_wheel() -> Path:
    DIST.mkdir(exist_ok=True)
    run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(DIST)], ROOT)
    wheels = sorted(DIST.glob("avdfleet-*.whl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not wheels:
        raise SystemExit("wheel build produced no artifact")
    wheel = wheels[0]
    required = [
        "avdfleet/api/static/index.html",
        "avdfleet/fleetspec/fleet.schema.json",
        "avdfleet/platform/images/catalogue.json",
        "avdfleet/platform/images/oobe_matrix.seed.json",
        "avdfleet/py.typed",
    ]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    missing = [name for name in required if name not in names]
    if missing:
        raise SystemExit(f"wheel is missing required assets: {missing}")
    print(f"wheel ok: {wheel} ({wheel.stat().st_size / 1024 / 1024:.1f} MiB, {len(names)} entries)")
    return wheel


def build_sdist() -> Path:
    run([sys.executable, "-m", "pip", "install", "--quiet", "build"], ROOT)
    run([sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(DIST)], ROOT)
    sdists = sorted(DIST.glob("avdfleet-*.tar.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not sdists:
        raise SystemExit("sdist build produced no artifact")
    print(f"sdist ok: {sdists[0]}")
    return sdists[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-ui", action="store_true", help="run pnpm install+build first")
    parser.add_argument("--no-sdist", action="store_true")
    args = parser.parse_args()
    if args.build_ui:
        build_ui()
    copy_static()
    build_wheel()
    if not args.no_sdist:
        build_sdist()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
