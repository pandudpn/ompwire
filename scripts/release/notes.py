"""Release notes grouped from the commits themselves.

`gh release create --generate-notes` lists merged pull requests only, and most changes here go
straight to main. These notes are built from the main line instead: a merged pull request is one
entry linked to the pull request, its own commits folded into it; a commit made on main is one
entry linked to the commit. Subjects are parsed as conventional commits and grouped, and the
people who wrote the release are listed at the end, with first-time contributors called out.

  python -m scripts.release.notes --to v1.4.0
  python -m scripts.release.notes --from v1.3.0 --to v1.4.0 --write RELEASE_NOTES.md

A release can also speak in a person's voice: commit a SUMMARY.md with the release title and
a short version of what it brings, and --summary SUMMARY.md puts it above the generated body.
The bump commit clears the file, so one release's words never speak for the next.
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
MERGE = re.compile(r"^Merge pull request #(?P<number>\d+)(?: from \S+)?(?::\s*(?P<title>.*))?$")
# A version bump, or the revert of one, says nothing a reader of the release notes needs.
RELEASE_BUMP = re.compile(r'^(?:Revert ")?chore\(release\):')
# fields and records of `git log`, chosen so no subject or body can contain them
FIELD, RECORD = "\x1f", "\x1e"


@dataclass(frozen=True)
class Author:
    name: str
    email: str

    @property
    def bot(self) -> bool:
        return "[bot]" in self.name or "[bot]" in self.email


@dataclass
class Commit:
    sha: str
    subject: str
    kind: str = "other"
    scope: str = ""
    text: str = ""
    references: list[int] = field(default_factory=list)
    pull: int = 0  # the pull request this entry stands for; 0 for a commit made on main
    authors: list[Author] = field(default_factory=list)


def parse(sha: str, subject: str, pull: int = 0) -> Commit:
    commit = Commit(sha=sha, subject=subject, pull=pull)
    match = SUBJECT.match(subject)
    if match:
        commit.kind = match["type"].lower()
        commit.scope = (match["scope"] or "").strip()
        commit.text = match["text"].strip()
    else:
        commit.text = subject.strip()
    if match and match["breaking"]:
        commit.kind = "breaking"
    commit.references = sorted({int(number) for number in ISSUE.findall(subject)} - {pull})
    return commit


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout


def authors_of(revision: str) -> list[Author]:
    """Everyone who wrote a commit in the revision range, in the order they first appear."""
    seen: dict[str, Author] = {}
    for line in git("log", "--reverse", f"--format=%an{FIELD}%ae", revision).splitlines():
        name, _, email = line.partition(FIELD)
        seen.setdefault(email.lower(), Author(name, email))
    return list(seen.values())


def log(from_tag: str, to_tag: str) -> list[Commit]:
    """The main line between two tags: one entry per commit made on main or pull request merged."""
    revision = f"{from_tag}..{to_tag}" if from_tag else to_tag
    out = git("log", "--first-parent", f"--format=%h{FIELD}%P{FIELD}%an{FIELD}%ae{FIELD}%s{FIELD}%b{RECORD}",
              revision)
    commits = []
    for record in out.split(RECORD):
        parts = record.strip("\n").split(FIELD)
        if len(parts) < 6:
            continue
        sha, parents, name, email, subject, body = parts
        parents = parents.split()
        if RELEASE_BUMP.match(subject):
            continue
        merged = MERGE.match(subject)
        if merged and len(parents) == 2:
            title = next((line.strip() for line in body.splitlines() if line.strip()),
                         merged["title"] or "")
            commit = parse(sha, title or subject, pull=int(merged["number"]))
            commit.authors = authors_of(f"{parents[0]}..{parents[1]}")
        elif len(parents) > 1:
            continue  # a merge that is not a pull request carries no change of its own
        else:
            commit = parse(sha, subject)
            commit.authors = [Author(name, email)]
        commits.append(commit)
    return commits


def previous_tag(to_tag: str) -> str:
    try:
        out = git("describe", "--tags", "--abbrev=0", f"{to_tag}^")
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip()


def known_emails(from_tag: str) -> set[str]:
    """Author emails already in the history the previous release shipped."""
    if not from_tag:
        return set()
    return {email.lower() for email in git("log", "--format=%ae", from_tag).splitlines()}


def repo_slug() -> str:
    try:
        out = git("remote", "get-url", "origin").strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    for pattern in (r"^git@github\.com:(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
                    r"^https://(?:[^@]+@)?github\.com/(?P<slug>[^/]+/[^/]+?)(?:\.git)?$"):
        match = re.match(pattern, out)
        if match:
            return match["slug"]
    return ""


def github_logins(commits: list[Commit], slug: str) -> dict[str, str]:
    """GitHub usernames by author email, looked up from one commit each author wrote.

    An author GitHub cannot match to an account (or no network, or no gh) keeps their git name
    in the notes, without the @ that would link the wrong person."""
    logins: dict[str, str] = {}
    if not slug:
        return logins
    for commit in commits:
        for author in commit.authors:
            email = author.email.lower()
            if email in logins or author.bot:
                continue
            try:
                sha = git("log", "-1", "--fixed-strings", f"--author=<{author.email}>",
                          "--format=%H", commit.sha if not commit.pull else f"{commit.sha}^2").strip()
                login = subprocess.run(
                    ["gh", "api", f"repos/{slug}/commits/{sha}", "--jq", '.author.login // ""'],
                    capture_output=True, text=True, check=True, timeout=30).stdout.strip() if sha else ""
            except (OSError, subprocess.SubprocessError):
                login = ""
            logins[email] = login
    return logins


def render(commits: list[Commit], from_tag: str, to_tag: str, base: str,
           logins: dict[str, str] | None = None, known: set[str] | None = None) -> str:
    logins, known = logins or {}, known or set()

    def link(commit: Commit) -> str:
        if commit.pull:
            return f"[#{commit.pull}]({base}/pull/{commit.pull})"
        return f"[{commit.sha}]({base}/commit/{commit.sha})"

    def who(author: Author) -> str:
        login = logins.get(author.email.lower(), "")
        return f"@{login}" if login else author.name

    def entry(commit: Commit, scoped: bool = True) -> str:
        text = f"**{commit.scope}**: {commit.text}" if scoped and commit.scope else commit.text
        credit = "".join(f" {who(author)}" for author in commit.authors if not author.bot)
        line = f"- {text} ({link(commit)}{credit})"
        for number in commit.references:
            line += f", #{number}"
        return line

    sections: dict[str, list[Commit]] = {}
    for commit in commits:
        sections.setdefault(commit.kind, []).append(commit)

    lines: list[str] = []
    for kind, heading in GROUPS:
        group = sections.pop(kind, [])
        if not group:
            continue
        lines += [f"### {heading}", ""]
        lines += [entry(commit) for commit in group]
        lines.append("")

    # anything whose type is not in the table still belongs in the notes, in one section
    leftover = [commit for group in sections.values() for commit in group]
    if leftover:
        lines += ["### Other changes", ""]
        lines += [entry(commit, scoped=False) for commit in leftover]
        lines.append("")

    # Oldest first, so a contribution is credited to the entry that brought the person in.
    people: dict[str, tuple[Author, Commit]] = {}
    for commit in reversed(commits):
        for author in commit.authors:
            if not author.bot:
                people.setdefault(author.email.lower(), (author, commit))

    if people:
        lines += ["### Contributors", ""]
        names = sorted({who(author) for author, _ in people.values()}, key=str.lower)
        lines += [f"- {name}" for name in names]
        lines.append("")
    newcomers = [(author, commit) for email, (author, commit) in people.items()
                 if from_tag and email not in known]
    if newcomers:
        lines += ["### New contributors", ""]
        lines += [f"- {who(author)} made their first contribution in {link(commit)}"
                  for author, commit in newcomers]
        lines.append("")

    if from_tag:
        lines.append(f"**Full changelog**: {base}/compare/{from_tag}...{to_tag}")
    return "\n".join(lines).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notes", description=__doc__)
    parser.add_argument("--from", dest="from_tag", default="", help="default: previous tag")
    parser.add_argument("--to", dest="to_tag", default="HEAD", help="default: HEAD")
    parser.add_argument("--write", dest="out", default="", help="write to a file instead of stdout")
    parser.add_argument("--summary", dest="summary", default="",
                        help="prepend this hand-written title and short version when the file exists")
    args = parser.parse_args(argv)

    from_tag = args.from_tag or previous_tag(args.to_tag)
    try:
        commits = log(from_tag, args.to_tag)
        known = known_emails(from_tag)
    except subprocess.CalledProcessError as exc:
        print(f"error: git log failed: {exc}", file=sys.stderr)
        return 1
    if not commits:
        print("error: no commits in range, refusing to publish empty notes", file=sys.stderr)
        return 1

    slug = repo_slug()
    base = f"https://github.com/{slug}" if slug else ""
    body = render(commits, from_tag, args.to_tag, base, github_logins(commits, slug), known)
    if args.summary:
        path = Path(args.summary)
        if path.is_file():
            intro = path.read_text(encoding="utf-8").strip()
            if intro:
                body = f"{intro}\n\n{body}"
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"wrote {args.out} ({len(commits)} entries, {len(body)} bytes)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
