"""Which shell commands may run without a human.

With `approval_mode = write`, omp sends a permission request for every shell command. Gating a
`git log` behind a button trains the person to tap Allow without reading, which is worse than not
asking at all. This module names the commands that cannot change the machine, so the gate stays
where it matters: anything that writes, deletes, deploys, or runs a pipeline that does.

The rule is narrow, but not so narrow that ordinary inspection trips it. Real turns ask for
`ls ... 2>/dev/null`, `find ... -printf '%s %p\\n' | sort`, `git log && git status`. Those read.
So the command is tokenised rather than pattern-matched: a pipeline is read-only when every
command in it is, a chain is read-only when every link is, and a redirect is allowed only when it
throws output away.
"""

from __future__ import annotations

import shlex

# First words that identify a command that only reads. Tuples mean "this verb, then this one".
READ_ONLY: tuple[tuple[str, ...], ...] = (
    ("ls",), ("cat",), ("head",), ("tail",), ("rg",), ("grep",), ("find",), ("wc",),
    ("stat",), ("du",), ("df",), ("tree",), ("file",), ("which",), ("whereis",),
    ("ps",), ("pgrep",), ("uname",), ("uptime",), ("free",), ("id",), ("hostname",),
    ("date",), ("pwd",), ("whoami",), ("echo",), ("jq",), ("sort",), ("uniq",), ("cut",),
    ("tr",), ("diff",), ("column",), ("basename",), ("dirname",), ("realpath",),
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"), ("git", "branch"),
    ("git", "remote"), ("git", "rev-parse"), ("git", "ls-files"), ("git", "describe"),
    ("git", "blame"), ("git", "shortlog"), ("git", "config", "--get"), ("git", "count-objects"),
    ("gh", "repo", "view"), ("gh", "repo", "list"), ("gh", "pr", "view"), ("gh", "pr", "list"),
    ("gh", "pr", "diff"), ("gh", "issue", "view"), ("gh", "issue", "list"),
    ("gh", "run", "view"), ("gh", "run", "list"), ("gh", "api"),
    ("kubectl", "get"), ("kubectl", "describe"), ("kubectl", "logs"), ("kubectl", "top"),
    ("docker", "ps"), ("docker", "images"), ("docker", "logs"), ("docker", "inspect"),
    ("docker", "stats"),
    ("systemctl", "status"), ("systemctl", "list-units"), ("systemctl", "list-timers"),
    ("systemctl", "show"), ("journalctl",), ("omp", "--version"), ("omp", "--help"),
)

# Flags that turn a read into a write, or start another command.
WRITE_FLAGS = ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-i", "--in-place", "--output")

# find's own file-writing outputs, and sort's in-place flag (-o is also find's positional
# print target, so it only counts as a write for sort).
WRITE_FLAGS += ("-fprint", "-fprint0", "-fprintf", "-fls")

SORT_WRITE_FLAGS = ("-o",)

# `git branch` / `git remote` are read-only for status, but these words mutate.
GIT_MUTATING = frozenset({
    "-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy",
    "--set-upstream-to", "-u", "add", "remove", "rm", "rename", "set-url", "prune",
    "set-head", "set-branches",
})

# gh api is read-only only when it is not given a method or a field.
GH_WRITE_FLAGS = ("-X", "--method", "-f", "-F", "--input", "--field")

# Syntax that can hide a second, unexamined command.
BAD_SYNTAX = ("$(", "`", "<<", "xargs", "eval", "sh -c", "bash -c", "sudo")

SEPARATORS = ("|", "||", "&&", ";", "&")
REDIRECTS = (">", ">>", ">|", "<", "<>")


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    return list(lexer)


def _reads(words: list[str]) -> bool:
    if not words:
        return True
    if any(word in WRITE_FLAGS for word in words[1:]):
        return False
    if tuple(words[:2]) == ("gh", "api") and any(word in GH_WRITE_FLAGS for word in words[2:]):
        return False
    if tuple(words[:2]) in (("git", "branch"), ("git", "remote")) \
            and any(word in GIT_MUTATING for word in words[2:]):
        return False
    if words[0] == "sort" and any(word in SORT_WRITE_FLAGS for word in words[1:]):
        return False
    return any(tuple(words[:len(prefix)]) == prefix for prefix in READ_ONLY)


def is_read_only(command: str) -> bool:
    """True when the command as written cannot change anything."""
    text = (command or "").strip()
    if not text:
        return False
    if any(bad in text for bad in BAD_SYNTAX):
        return False
    words: list[str] = []
    index = 0
    try:
        tokens = _tokens(text)
    except ValueError:
        return False
    while index < len(tokens):
        token = tokens[index]
        if token in REDIRECTS:
            index += 1
            target = tokens[index] if index < len(tokens) else ""
            # `2>/dev/null` arrives as ["2", ">", "/dev/null"]; `2>&1` as ["2", ">", "&", "1"]
            if target == "&":
                index += 2  # fd duplication: 2>&1
                continue
            if target != "/dev/null":
                return False  # writing somewhere real
            index += 1
            continue
        if token in SEPARATORS:
            if token == "&":
                return False  # backgrounding hides the next command
            if not _reads(words):
                return False
            words = []
            index += 1
            continue
        if token.startswith("(") or token.startswith(")"):
            return False
        words.append(token)
        index += 1
    return _reads(words)
