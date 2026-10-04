"""Build a source-only ZIP from an explicit allowlist, after a secret scan."""

import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from check_publication import ROOT, check

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "dist" / "trading-robot-source.zip"
    )
    args = parser.parse_args()
    files, problems = check()
    if problems:
        for problem in problems:
            print(problem)
        raise SystemExit("Publication check failed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED) as archive:
        for p in files:
            archive.write(p, str(p.relative_to(ROOT)))
    print(f"Exported {len(files)} source files to {args.output}")
