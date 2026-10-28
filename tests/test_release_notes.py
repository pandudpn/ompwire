"""Release notes come from the main line, grouped, with pull requests and the people behind them."""

from __future__ import annotations

import subprocess

import pytest

from scripts.release import notes as release_notes

BASE = "https://github.com/o/r"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository with v1.0.0 by Ana, then commits on main, a merged pull request and a
    newcomer, a version bump and the revert of one."""
    monkeypatch.setattr(release_notes, "ROOT", tmp_path)

    def git(*args: str, who: str = "Ana") -> str:
        env = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": f"{who.lower()}@example.com",
               "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": f"{who.lower()}@example.com",
               "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
        return subprocess.run(["git", *args], cwd=tmp_path, env=env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def commit(subject: str, who: str = "Ana") -> None:
        git("commit", "-q", "--allow-empty", "-m", subject, who=who)

    git("init", "-q", "-b", "main")
    commit("feat: first")
    git("tag", "v1.0.0")
    commit("fix(api): keep the token out of the log")
    git("checkout", "-q", "-b", "feat/rpc")
    commit("feat(agent): talk over rpc", who="Budi")
    commit("fix the rpc test", who="Budi")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "feat/rpc", "-m",
        "Merge pull request #9: talk over rpc\n\nfeat(agent): talk over rpc instead of acp")
    commit("chore(release): v1.1.0")
    commit('Revert "chore(release): v1.1.0"')
    git("tag", "v1.1.0")
    return git


def test_a_merged_pull_request_is_one_entry_linked_to_it_and_its_author(repo):
    commits = release_notes.log("v1.0.0", "v1.1.0")
    body = release_notes.render(commits, "v1.0.0", "v1.1.0", BASE,
                                logins={"budi@example.com": "budi-gh"})
    assert ("- **agent**: talk over rpc instead of acp ([#9](https://github.com/o/r/pull/9) @budi-gh)"
            in body)
    # the pull request's own commits are folded into its entry, not listed again
    assert "fix the rpc test" not in body
    # a commit on main credits its author too; one GitHub cannot match keeps the git name, no @
    assert "**api**: keep the token out of the log ([" in body
    api = next(line for line in body.splitlines() if "keep the token" in line)
    assert api.endswith(" Ana)") and "@Ana" not in api


def test_a_version_bump_and_its_revert_stay_out_of_the_notes(repo):
    subjects = [commit.subject for commit in release_notes.log("v1.0.0", "v1.1.0")]
    assert not any("chore(release)" in subject for subject in subjects)


def test_everyone_who_wrote_the_release_is_credited_and_a_newcomer_called_out(repo):
    commits = release_notes.log("v1.0.0", "v1.1.0")
    body = release_notes.render(commits, "v1.0.0", "v1.1.0", BASE,
                                logins={"budi@example.com": "budi-gh"},
                                known=release_notes.known_emails("v1.0.0"))
    contributors = body.split("### Contributors")[1].split("###")[0]
    assert set(contributors.split()) >= {"@budi-gh", "Ana"}
    newcomers = body.split("### New contributors")[1]
    assert "@budi-gh made their first contribution in [#9]" in newcomers
    assert "Ana" not in newcomers


def test_a_bot_is_never_credited():
    commit = release_notes.parse("abc1234", "fix: y")
    commit.authors = [release_notes.Author("github-actions[bot]", "41898282+github-actions[bot]@x")]
    assert "Contributors" not in release_notes.render([commit], "v1", "v2", BASE)


def test_the_first_release_names_no_newcomers(repo):
    commits = release_notes.log("", "v1.0.0")
    body = release_notes.render(commits, "", "v1.0.0", BASE)
    assert "### Contributors" in body and "New contributors" not in body


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


def test_a_summary_file_is_prepended_to_the_written_notes(repo, tmp_path):
    summary = tmp_path / "SUMMARY.md"
    summary.write_text("## The short version\n\n- personas per bot\n", encoding="utf-8")
    out = tmp_path / "RELEASE_NOTES.md"
    rc = release_notes.main(["--from", "v1.0.0", "--to", "v1.1.0",
                             "--summary", str(summary), "--write", str(out)])
    assert rc == 0
    body = out.read_text(encoding="utf-8")
    assert body.startswith("## The short version")
    assert body.index("personas per bot") < body.index("### Features")


def test_a_missing_summary_file_changes_nothing(repo, tmp_path):
    out = tmp_path / "RELEASE_NOTES.md"
    rc = release_notes.main(["--from", "v1.0.0", "--to", "v1.1.0",
                             "--summary", str(tmp_path / "absent.md"), "--write", str(out)])
    assert rc == 0
    assert "The short version" not in out.read_text(encoding="utf-8")
