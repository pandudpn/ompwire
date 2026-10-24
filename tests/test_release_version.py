"""The version guard: one number, or the release is refused."""

from __future__ import annotations

import pytest

from scripts.release import version as release_version


@pytest.fixture
def files(monkeypatch, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "ompwire"\nversion = "1.4.2"\n', encoding="utf-8")
    init = tmp_path / "init.py"
    init.write_text('__version__ = "1.4.2"\n', encoding="utf-8")
    monkeypatch.setattr(release_version, "PYPROJECT", pyproject)
    monkeypatch.setattr(release_version, "INIT", init)
    return pyproject, init


def test_show_reads_both_files(files, capsys):
    assert release_version.main(["show"]) == 0
    assert "1.4.2" in capsys.readouterr().out


def test_check_accepts_a_tag_that_matches(files):
    assert release_version.check("v1.4.2") == "1.4.2"


def test_check_refuses_a_tag_that_does_not(files):
    with pytest.raises(release_version.VersionError, match=r"pyproject\.toml says 1\.4\.2"):
        release_version.check("v1.4.3")


def test_a_file_that_drifted_is_named(files):
    _pyproject, init = files
    init.write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    with pytest.raises(release_version.VersionError, match=r"__init__\.py says 0\.0\.1"):
        release_version.check("v1.4.2")


def test_bump_writes_both_files_together(files):
    pyproject, init = files
    assert release_version.main(["bump", "patch", "--from", "v1.4.2"]) == 0
    assert 'version = "1.4.3"' in pyproject.read_text()
    assert '__version__ = "1.4.3"' in init.read_text()


@pytest.mark.parametrize(("kind", "expected"), [
    ("patch", "1.4.3"),
    ("minor", "1.5.0"),
    ("major", "2.0.0"),
    ("2.1.0-rc.1", "2.1.0-rc.1"),
])
def test_next_version(kind, expected):
    assert release_version.next_version(kind, from_tag="v1.4.2") == expected


def test_a_patch_bump_graduates_a_prerelease_instead_of_moving_the_number():
    assert release_version.next_version("patch", from_tag="v1.4.0-rc.2") == "1.4.0"
    assert release_version.next_version("minor", from_tag="v1.4.0-rc.2") == "1.5.0"


def test_a_prerelease_tag_is_parsed_but_not_confused_with_stable():
    major, minor, patch, pre = release_version.parse("v1.4.2-rc.1")
    assert (major, minor, patch, pre) == (1, 4, 2, "rc.1")
    assert release_version.parse("v1.4.2")[3] == ""


def test_nonsense_is_refused():
    with pytest.raises(release_version.VersionError, match="not a version"):
        release_version.parse("v1.4")
