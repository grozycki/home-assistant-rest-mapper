"""Copy the rest-mapper library into the integration, or check the copy.

While the repositories are private, Home Assistant cannot install the library with pip.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
VENDOR = ROOT / "custom_components" / "rest_mapper" / "_vendor"
TARGET = VENDOR / "rest_mapper"
DIGEST = VENDOR / "DIGEST"
DEFAULT_SOURCE = ROOT.parent / "rest-mapper" / "src" / "rest_mapper"


def digest(package: Path) -> str:
    """Hash the package's files."""
    hasher = hashlib.sha256()
    for path in sorted(package.glob("*")):
        if path.is_file():
            hasher.update(path.name.encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def copy(source: Path) -> None:
    """Replace the copy."""
    shutil.rmtree(TARGET, ignore_errors=True)
    shutil.copytree(source, TARGET, ignore=shutil.ignore_patterns("__pycache__"))
    (VENDOR / "__init__.py").write_text('"""Copied libraries."""\n')
    DIGEST.write_text(f"{digest(TARGET)}\n")


def check(source: Path) -> int:
    """Return an exit status: the copy must match its digest and any local source."""
    recorded = DIGEST.read_text().strip()
    if digest(TARGET) != recorded:
        print("The copied library was edited by hand.", file=sys.stderr)
        return 1
    if source.is_dir() and digest(source) != recorded:
        print("The copied library differs from the source.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    """Run from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    arguments = parser.parse_args()
    source: Path = arguments.source
    if arguments.check:
        return check(source)
    copy(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
