"""Check relative links in Markdown files resolve inside the repository."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")
FILES = [ROOT / "README.md", ROOT / "FINAL_REPORT.md", ROOT / "BRIEF.md",
         ROOT / "BRIEF_ORIGINAL.md", ROOT / "DECISIONS.md", ROOT / "PLAN.md"]
FILES += sorted((ROOT / "docs").glob("*.md"))
FILES += sorted((ROOT / "bootstrap").glob("*.md"))
FILES += [ROOT / "ui" / "README.md"]


def main() -> int:
    missing: list[str] = []
    total = 0
    for source in FILES:
        if not source.exists():
            continue
        text = source.read_text(encoding="utf-8")
        for target in LINK.findall(text):
            target = target.split("#", 1)[0].strip()
            if not target or target.startswith(SKIP_PREFIXES):
                continue
            total += 1
            # Markdown images in tables use .png paths; resolve relative to source dir.
            resolved = (source.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{source.relative_to(ROOT)} -> {target}")
    print(f"checked {total} relative links in {len(FILES)} files")
    if missing:
        print("BROKEN:")
        for item in missing:
            print(" -", item)
        return 1
    print("all links resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
