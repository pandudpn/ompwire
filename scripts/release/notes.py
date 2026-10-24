"""Release notes grouped from the commits themselves.

`gh release create --generate-notes` lists merged pull requests. This repository commits
straight to main, so that command produces a release body with nothing but a changelog link.
These notes are built from the log instead: subjects are parsed as conventional commits and
grouped, so the release page says what changed and who asked for it.

  python -m scripts.release.notes --to v1.4.0
  python -m scripts.release.notes --from v1.3.0 --to v1.4.0 --write RELEASE_NOTES.md
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Display order matters more than the alphabet: what a user must act on comes first.
GROUPS = [
    ("breaking", "Breaking changes"),
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("ci", "Build and CI"),
    ("build", "Build and CI"),
    ("chore", "Chores"),
    ("other", "Other changes"),
]

SUBJECT = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?: (?P<text>.+)$")
ISSUE = re.compile(r"(?<![&\w])#(\d+)")


@dataclass
class Commit:
    sha: str
    subject: str
    kind: str = "other"
    scope: str = ""
    text: str = ""
    references: list[int] = field(default_factory=list)


def parse(sha: str, subject: str) -> Commit:
    commit = Commit(sha=sha, subject=subject)
    match = SUBJECT.match(subject)
    if match:
        commit.kind = match["type"].lower()
        commit.scope = (match["scope"] or "").strip()
        commit.text = match["text"].strip()
    else:
        commit.text = subject.strip()
    if match and match["breaking"]:
        commit.kind = "breaking"
    commit.references = sorted({int(number) for number in ISSUE.findall(subject)})
    return commit


def log(from_tag: str, to_tag: str) -> list[Commit]:
    revision = f"{from_tag}..{to_tag}" if from_tag else to_tag
    out = subprocess.run(["git", "log", "--no-merges", "--format=%h %s", revision],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    commits = []
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        if not subject:
            continue
        # A version bump says nothing a reader of the release notes needs.
        if re.match(r"^chore\(release\):", subject):
            continue
        commits.append(parse(sha, subject))
    return commits


def previous_tag(to_tag: str) -> str:
    try:
        out = subprocess.run(["git", "describe", "--tags", "--abbrev=0", f"{to_tag}^"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip()


def repo_url() -> str:
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    for pattern in (r"^git@github\.com:(?P<slug>[^/]+/[^/.]+)(?:\.git)?$",
                    r"^https://(?:[^@]+@)?github\.com/(?P<slug>[^/]+/[^/.]+)(?:\.git)?$"):
        match = re.match(pattern, out)
        if match:
            return f"https://github.com/{match['slug'].removesuffix('.git')}"
    return ""


def render(commits: list[Commit], from_tag: str, to_tag: str, base: str) -> str:
    sections: dict[str, list[Commit]] = {}
    for commit in commits:
        sections.setdefault(commit.kind, []).append(commit)

    lines: list[str] = []
    for kind, heading in GROUPS:
        group = sections.pop(kind, [])
        if not group:
            continue
        lines.append(f"### {heading}")
        lines.append("")
        for commit in group:
            bullet = f"- {commit.text} ([{commit.sha}]({base}/commit/{commit.sha}))"
            if commit.scope:
                bullet = f"- **{commit.scope}**: {commit.text} ([{commit.sha}]({base}/commit/{commit.sha}))"
            for number in commit.references:
                bullet += f", #{number}"
            lines.append(bullet)
        lines.append("")

    # anything whose type is not in the table still belongs in the notes, in one section
    leftover = [commit for group in sections.values() for commit in group]
    if leftover:
        lines.append("### Other changes")
        lines.append("")
        for commit in leftover:
            lines.append(f"- {commit.text} ([{commit.sha}]({base}/commit/{commit.sha}))")
        lines.append("")

    if from_tag:
        lines.append(f"**Full changelog**: {base}/compare/{from_tag}...{to_tag}")
    return "\n".join(lines).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notes", description=__doc__)
    parser.add_argument("--from", dest="from_tag", default="", help="default: previous tag")
    parser.add_argument("--to", dest="to_tag", default="HEAD", help="default: HEAD")
    parser.add_argument("--write", dest="out", default="", help="write to a file instead of stdout")
    args = parser.parse_args(argv)

    from_tag = args.from_tag or previous_tag(args.to_tag)
    try:
        commits = log(from_tag, args.to_tag)
    except subprocess.CalledProcessError as exc:
        print(f"error: git log failed: {exc}", file=sys.stderr)
        return 1
    if not commits:
        print("error: no commits in range, refusing to publish empty notes", file=sys.stderr)
        return 1

    body = render(commits, from_tag, args.to_tag, repo_url())
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"wrote {args.out} ({len(commits)} commits, {len(body)} bytes)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
