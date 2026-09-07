"""Build Blackdog's reproducible stdlib-only executable Python archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from blackdog.runtime_distribution import write_release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SOURCE_ROOT / "dist" / "blackdog.pyz")
    args = parser.parse_args()
    digest = write_release(args.output, source_root=SOURCE_ROOT)
    checksum = args.output.with_name(args.output.name + ".sha256")
    checksum.write_text(f"{digest}  {args.output.name}\n", encoding="utf-8")
    print(json.dumps({"artifact": str(args.output.resolve()), "sha256": digest, "requires_python": ">=3.11"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
