"""One owner for the version number.

The release workflow refuses a tag that disagrees with `pyproject.toml` or
`src/ompwire/__init__.py`, but that guard only reports the problem after someone has already
typed a version three times. This script is where the number is decided: it computes the next
version from the git tags, writes both files, and prints what it did.

  python -m scripts.release.version show
  python -m scripts.release.version bump minor
  python -m scripts.release.version check v1.2.3
  python -m scripts.release.version current --ref v1.2.3   # which tag is the latest
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "ompwire" / "__init__.py"

STABLE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
PRERELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-([0-9A-Za-z.-]+)$")


class VersionError(RuntimeError):
    pass


def parse(tag: str) -> tuple[int, int, int, str]:
    """(major, minor, patch, prerelease) from a tag or a bare version."""
    value = tag.strip().lstrip("v")
    stable = re.match(r"^(\d+)\.(\d+)\.(\d+)$", value)
    if stable:
        return int(stable[1]), int(stable[2]), int(stable[3]), ""
    pre = re.match(r"^(\d+)\.(\d+)\.(\d+)-([0-9A-Za-z.-]+)$", value)
    if pre:
        return int(pre[1]), int(pre[2]), int(pre[3]), pre[4]
    raise VersionError(f"not a version: {tag!r} (expected vMAJOR.MINOR.PATCH[-pre])")


def current() -> tuple[str, str]:
    """The version in the files, from pyproject as the source of truth."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pyproject = data["project"]["version"]
    match = re.search(r'__version__ = "([^"]+)"', INIT.read_text(encoding="utf-8"))
    if match is None:
        raise VersionError(f"no __version__ in {INIT}")
    return pyproject, match.group(1)


def latest_tag() -> str:
    try:
        out = subprocess.run(["git", "tag", "--list", "v*", "--sort=-v:refname"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if STABLE.match(line.strip()) or PRERELEASE.match(line.strip()):
            return line.strip()
    return ""


def next_version(bump: str, from_tag: str = "") -> str:
    base = from_tag or latest_tag()
    if base:
        major, minor, patch, _pre = parse(base)
    else:
        major, minor, patch, _pre = parse(current()[0])
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    # an explicit version is passed through, so `--bump` stays a choice and not a parser
    return bump.lstrip("v")


def write(version: str) -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    updated = re.sub(r'(?m)^version = "[^"]+"', f'version = "{version}"', pyproject, count=1)
    if updated == pyproject and f'version = "{version}"' not in pyproject:
        raise VersionError("could not find the version line in pyproject.toml")
    PYPROJECT.write_text(updated, encoding="utf-8")

    init = INIT.read_text(encoding="utf-8")
    replaced = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init, count=1)
    if replaced == init and f'__version__ = "{version}"' not in init:
        raise VersionError("could not find __version__ in src/ompwire/__init__.py")
    INIT.write_text(replaced, encoding="utf-8")


def check(tag: str) -> str:
    """Fail when the tag and the two files disagree, which is the whole point."""
    major, minor, patch, pre = parse(tag)
    wanted = f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
    pyproject, init = current()
    problems = []
    if pyproject != wanted:
        problems.append(f"pyproject.toml says {pyproject}")
    if init != wanted:
        problems.append(f"src/ompwire/__init__.py says {init}")
    if problems:
        raise VersionError(f"tag {tag} does not match: " + ", ".join(problems))
    return wanted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="version", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="print the version currently in the files")
    sub.add_parser("latest", help="print the newest version tag")
    bump = sub.add_parser("bump", help="compute the next version, write it, print it")
    bump.add_argument("kind", help="major | minor | patch | an explicit version")
    bump.add_argument("--from", dest="base", default="", help="base tag instead of the newest")
    look = sub.add_parser("check", help="fail unless the tag matches both files")
    look.add_argument("tag")

    args = parser.parse_args(argv)
    try:
        if args.action == "show":
            pyproject, init = current()
            print(f"{pyproject} (pyproject.toml) / {init} (__init__.py)")
            return 0
        if args.action == "latest":
            print(latest_tag())
            return 0
        if args.action == "check":
            print(check(args.tag))
            return 0
        version = next_version(args.kind, args.base)
        parse(version)          # refuse to write something we cannot parse back
        write(version)
        print(version)
        return 0
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
