"""Release notes come from the log, grouped, because --generate-notes needs pull requests."""

from __future__ import annotations

from scripts.release import notes as release_notes


def test_a_conventional_subject_is_split_into_type_scope_and_text():
    commit = release_notes.parse("abc1234", "feat(gateway): add whatsapp transport")
    assert (commit.kind, commit.scope, commit.text) == (
        "feat", "gateway", "add whatsapp transport")


def test_a_plain_subject_lands_in_other_changes():
    commit = release_notes.parse("abc1234", "fix the thing by hand")
    assert commit.kind == "other"
    assert commit.text == "fix the thing by hand"


def test_a_bang_marks_breaking():
    assert release_notes.parse("abc1234", "refactor(config)!: drop the old key").kind == "breaking"


def test_issue_references_are_collected():
    commit = release_notes.parse("abc1234", "fix(service): lock the unit (#12, #34)")
    assert commit.references == [12, 34]


def test_the_body_is_grouped_and_links_commits():
    commits = [
        release_notes.parse("aaaaaaa", "feat(api): expose the status endpoint"),
        release_notes.parse("bbbbbbb", "fix(logs): stop printing the token"),
        release_notes.parse("ccccccc", "docs: rewrite the README"),
        release_notes.parse("ddddddd", "ci(release): build once"),
        release_notes.parse("eeeeeee", "chore(release): v1.2.3"),
    ]
    commits = [commit for commit in commits if not commit.subject.startswith("chore(release)")]
    body = release_notes.render(commits, "v1.2.2", "v1.2.3", "https://github.com/o/r")

    headings = [line for line in body.splitlines() if line.startswith("### ")]
    assert headings == ["### Features", "### Fixes", "### Documentation", "### Build and CI"]
    assert "**api**: expose the status endpoint" in body
    assert "https://github.com/o/r/commit/aaaaaaa" in body
    assert body.rstrip().endswith("compare/v1.2.2...v1.2.3")


def test_an_unknown_type_still_appears_once_under_other_changes():
    commits = [release_notes.parse("abc1234", "wibble: something odd"),
               release_notes.parse("def5678", "spike: throwaway branch work")]
    body = release_notes.render(commits, "", "v1.0.0", "https://github.com/o/r")
    assert body.count("### Other changes") == 1
    assert "something odd" in body and "throwaway branch work" in body


def test_a_scope_is_bold_and_a_plain_subject_is_not():
    scoped = release_notes.render([release_notes.parse("abc1234", "fix(x): y")], "", "v1", "u")
    plain = release_notes.render([release_notes.parse("abc1234", "no prefix here")], "", "v1", "u")
    assert "- **x**: y" in scoped
    assert "- no prefix here" in plain
