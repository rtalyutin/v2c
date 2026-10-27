"""Generate or check the code-first operator contract without opening a DB.

Run from the repository root after installation:
    python scripts/export_openapi.py
    python scripts/export_openapi.py --check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from v2c.api import create_app
from v2c.config import Settings

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs/contracts/operator-openapi.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail if the committed contract differs."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    # Explicit defaults keep operator environment values out of this artifact.
    schema = create_app(Settings()).openapi()
    document = json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text() != document:
            print("Operator OpenAPI is stale. Run python scripts/export_openapi.py")
            return 1
        print("Operator OpenAPI matches code.")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
