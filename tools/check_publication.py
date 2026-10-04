"""Check publishable sources; report locations, never matched secret values."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = [
    "README.md",
    ".gitignore",
    ".dockerignore",
    "Dockerfile",
    "compose.yaml",
    "requirements.txt",
    "SECURITY.md",
    "CONTRIBUTING.md",
]
PATTERNS = [
    r"\bt\.[A-Za-z0-9_-]{30,}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"/Users/[A-Za-z0-9_.-]+/",
    r"C:\\Users\\[^\\]+\\",
]


def source_files():
    files = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    for pattern in [
        "trading_bot/*.py",
        "trading_bot/requirements*.txt",
        "trading_bot/constraints.txt",
        "trading_bot/.env.example",
        "trading_bot/.gitignore",
        "trading_bot/README.md",
        "trading_bot/web/*",
        "trading_bot/strategy_profiles/*.json",
        "trading_bot/tests/*.py",
        "tools/*.py",
        "docs/*.md",
        ".github/workflows/*.yml",
    ]:
        files.extend(ROOT.glob(pattern))
    files = sorted(set(p for p in files if p.is_file()))
    if any(p.is_symlink() or not p.resolve().is_relative_to(ROOT) for p in files):
        raise ValueError("Symlinks are not allowed in publication sources")
    return files


def check():
    problems = []
    files = source_files()
    for path in files:
        content = path.read_text(encoding="utf-8")
        if path.name == ".env.example":
            for line in content.splitlines():
                if (
                    line.startswith("T_INVEST_TOKEN=")
                    and line.partition("=")[2].strip()
                ):
                    problems.append(
                        f"{path.relative_to(ROOT)}: token template must be empty"
                    )
        for number, line in enumerate(content.splitlines(), 1):
            if any(re.search(pattern, line) for pattern in PATTERNS):
                problems.append(
                    f"{path.relative_to(ROOT)}:{number}: possible secret or personal machine path"
                )
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True
        )
    except FileNotFoundError:
        tracked = None
    if tracked is not None and tracked.returncode == 0:
        allowed = {str(p.relative_to(ROOT)) for p in files}
        for name in tracked.stdout.decode().split("\0"):
            if not name:
                continue
            p = Path(name)
            if (
                (p.name.startswith(".env") and p.name != ".env.example")
                or any(
                    v in p.parts
                    for v in [".venv", ".runtime", "reports", "logs", "__pycache__"]
                )
                or ".sqlite3" in p.name
                or p.name.startswith((".bot_state.", ".smoke_state.", ".dashboard."))
            ):
                problems.append(f"{name}: private/generated file tracked by Git")
            elif name not in allowed:
                problems.append(
                    f"{name}: tracked file missing from publication allowlist"
                )
    return files, problems


if __name__ == "__main__":
    files, problems = check()
    for problem in problems:
        print(problem)
    if problems:
        raise SystemExit(1)
    print(
        f"Publication check passed: {len(files)} source files. Local secrets and generated data are excluded. This is a heuristic scan, not a guarantee."
    )
