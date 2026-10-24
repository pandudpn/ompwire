"""Catalog parsing: providers come from omp's menu, skills from SKILL.md files."""

from __future__ import annotations

from pathlib import Path

from ompwire.catalog import (Skill, list_skills, parse_menu, skill_body,
                            strip_ansi)


def test_menu_parsing_keeps_order_and_drops_stray_numbers():
    screen = strip_ansi(
        "Select a provider:\r\n\r\n 1. ChatGPT Plus/Pro (Codex Subscription)\r\n"
        " 2. Anthropic (Claude Pro/Max)\r\n 3. Z.AI (GLM Coding Plan)\r\n"
    )
    assert parse_menu(screen) == [
        (1, "ChatGPT Plus/Pro (Codex Subscription)"),
        (2, "Anthropic (Claude Pro/Max)"),
        (3, "Z.AI (GLM Coding Plan)"),
    ]


def test_ansi_codes_are_stripped():
    assert strip_ansi("\x1b[32mok\x1b[0m") == "ok"


def test_skills_are_read_from_frontmatter(tmp_path):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo skill for tests\n---\n\n# Body\ntext\n",
        encoding="utf-8",
    )
    skills = list_skills((str(tmp_path),), cwd=str(tmp_path))
    assert [s.name for s in skills] == ["demo-skill"]
    assert skills[0].description == "A demo skill for tests"
    assert skill_body(skills[0]) == "# Body\ntext"


def test_skill_body_truncates(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("x" * 500, encoding="utf-8")
    body = skill_body(Skill("big", "", path), limit=100)
    assert body.endswith("… truncated")
    assert len(body) < 200


def test_missing_skill_dir_is_not_fatal(tmp_path):
    assert list_skills((str(tmp_path / "nope"),)) == []


def test_credentials_in_db_reads_names_and_types(tmp_path):
    import sqlite3

    from ompwire.catalog import credentials_in_db

    db = tmp_path / "agent.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE auth_credentials (provider TEXT, credential_type TEXT, data TEXT)")
    con.executemany("INSERT INTO auth_credentials VALUES (?,?,?)",
                    [("zai", "api_key", "secret-shape"), ("anthropic", "oauth", "secret-shape")])
    con.commit()
    con.close()

    assert credentials_in_db(str(db)) == {"zai": "api_key", "anthropic": "oauth"}


def test_credentials_in_db_tolerates_missing_or_foreign_files(tmp_path):
    from ompwire.catalog import credentials_in_db

    assert credentials_in_db(str(tmp_path / "nope.db")) == {}
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert credentials_in_db(str(empty)) == {}
