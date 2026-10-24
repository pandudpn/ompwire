"""Catalog parsing: providers come from omp's menu, skills from SKILL.md files."""

from __future__ import annotations

from ompwire.catalog import Skill, list_skills, parse_menu, skill_body, strip_ansi


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


def test_skill_grid_is_two_columns_and_clickable(tmp_path):
    import asyncio

    from ompwire.config import load_config
    from ompwire.telegram import Transport

    class Message:
        async def edit_text(self, text, reply_markup=None, parse_mode=None):
            Message.text, Message.keyboard = text, reply_markup

    for index in range(14):   # more than one page, so the nav row exists
        folder = tmp_path / f"skill-{index}"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: skill-{index}\ndescription: demo {index}\n---\nbody\n", encoding="utf-8")

    transport = Transport(load_config(env={}))
    transport._skills[1] = list_skills((str(tmp_path),), cwd=str(tmp_path))
    asyncio.run(transport._render_skills(1, 1, Message()))

    rows = Message.keyboard.inline_keyboard
    skill_rows = [row for row in rows if row[0].callback_data.startswith("skv:")]
    assert [len(row) for row in skill_rows] == [2] * 5          # five rows, two per row
    assert [b.callback_data for b in skill_rows[0]] == ["skv:0", "skv:1"]
    assert rows[-1][0].text == "1/2"                            # nav row


def test_login_grid_marks_signed_in_providers():
    import asyncio

    from ompwire.catalog import Provider
    from ompwire.config import load_config
    from ompwire.telegram import Transport

    class Message:
        async def edit_text(self, text, reply_markup=None, parse_mode=None):
            Message.text, Message.keyboard = text, reply_markup

    transport = Transport(load_config(env={}))
    transport._login_cache = [
        Provider(1, "Anthropic (Claude Pro/Max)", "anthropic", True),
        Provider(2, "OpenRouter", "openrouter", False),
        Provider(3, "Mystery", "", False),
    ]
    asyncio.run(transport._render_login(1, 1, Message()))
    labels = [b.text for row in Message.keyboard.inline_keyboard for b in row]
    assert labels[0].startswith("✅")
    assert labels[1].startswith("⚪")
    assert labels[2].startswith("❔")
    assert Message.keyboard.inline_keyboard[0][0].callback_data == "lgv:1"


def test_login_session_announces_each_prompt_once():
    """omp redraws its prompts; the chat must not get the same line four times."""
    from ompwire.login import LoginSession

    seen: list[str] = []
    session = LoginSession.__new__(LoginSession)
    session.url = ""
    session.url_announced = False
    session.code_announced = False
    session.awaiting_code = False
    session._tail = []
    session.on_line = seen.append

    line = "Paste the authorization code (or full redirect URL):"
    for _ in range(4):
        session._handle(line)
    assert len(seen) == 1
    assert session.awaiting_code is True

    url_line = "https://claude.ai/oauth/authorize?code=true"
    for _ in range(3):
        session._handle(url_line)
    assert session.url == "https://claude.ai/oauth/authorize?code=true"
    assert sum(1 for item in seen if item.startswith("https://")) == 1


def test_credential_snapshot_sees_a_refreshed_credential(tmp_path):
    import sqlite3

    from ompwire.catalog import credential_snapshot

    db = tmp_path / "agent.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE auth_credentials "
                "(provider TEXT, credential_type TEXT, updated_at TEXT, data TEXT)")
    con.execute("INSERT INTO auth_credentials VALUES ('anthropic','oauth','100','x')")
    con.commit()
    before = credential_snapshot(str(db))

    con.execute("UPDATE auth_credentials SET updated_at='200' WHERE provider='anthropic'")
    con.commit()
    con.close()
    after = credential_snapshot(str(db))

    assert before != after                      # a re-login is visible even without a new name
    assert set(after) == {"anthropic"}


def test_login_noise_is_not_forwarded_to_the_chat():
    from ompwire.telegram import Transport

    assert all("Local shortcut" in noise for noise in ("Local shortcut",))
    for noise in ("Local shortcut (this machine only): http://localhost:1/launch",
                  "Preferred port 54545 unavailable, using port 39865",
                  "Waiting for browser authentication..."):
        assert any(part in noise for part in Transport.LOGIN_NOISE)


def test_skills_command_is_the_grid_not_the_command_search():
    """A second cmd_skills used to shadow the catalog grid in the class body."""
    import inspect

    from ompwire.telegram import Transport

    source = inspect.getsource(Transport.cmd_skills)
    assert "list_skills" in source and "InlineKeyboardMarkup" in source
    assert hasattr(Transport, "cmd_commands")   # the command search kept its own name
    assert "advertised_commands" in inspect.getsource(Transport.cmd_commands)


def test_show_skill_sends_the_card_and_the_body(tmp_path):
    """Regression: Transport once lacked the html helpers, so tapping a skill crashed."""
    import asyncio
    from types import SimpleNamespace

    from ompwire.config import load_config
    from ompwire.telegram import Transport

    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\n\n## Heading\n\nbody text\n",
        encoding="utf-8")

    sent: list[tuple[str, dict]] = []

    async def fake_send_message(chat_id, text, **kwargs):
        sent.append((text, kwargs))
        return SimpleNamespace(chat_id=chat_id)

    transport = Transport(load_config(env={}))
    transport.app = SimpleNamespace(bot=SimpleNamespace(send_message=fake_send_message))
    transport._skills[42] = list_skills((str(tmp_path),), cwd=str(tmp_path))

    asyncio.run(transport.show_skill(42, 0))

    assert sent, "nothing was sent"
    assert "demo" in sent[0][0]
    assert sent[0][1].get("parse_mode") == "HTML"
    assert any("Heading" in text for text, _ in sent[1:])
