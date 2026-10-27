"""Check local Markdown file references; remote links are not fetched."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    failures = []
    files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in files:
        if not path.exists():
            failures.append(f"missing required document: {path.relative_to(ROOT)}")
            continue
        for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text()):
            if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            local = target.split("#", 1)[0]
            if local and not (path.parent / local).exists():
                failures.append(f"{path.relative_to(ROOT)}: missing {local}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Documentation links: {len(files)} files checked")


if __name__ == "__main__":
    main()
