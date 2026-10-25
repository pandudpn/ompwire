"""Gateways: one bot per definition, one credential each, and the CLI that manages them."""

from __future__ import annotations

import tomllib

import pytest

from ompwire import gateways


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """A private config dir: gateways.toml and .env land under tmp_path."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()

    class Paths:
        def __call__(self):
            return config_dir, config_dir / "config.toml", config_dir / ".env"

    monkeypatch.setattr("ompwire.setup._paths", Paths())
    monkeypatch.setattr(gateways, "gateways_file", lambda: config_dir / "gateways.toml")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return config_dir


class TestDefinitions:
    def test_add_writes_one_directory_per_gateway(self, sandbox):
        gateways.add("telegram", "telegram", allowed=[42], home=42)
        gateways.add("telegram-2", "telegram", allowed=[7])

        first = tomllib.loads((sandbox / "gateways" / "telegram" / "gateway.toml").read_text())
        second = tomllib.loads((sandbox / "gateways" / "telegram-2" / "gateway.toml").read_text())
        assert first["gateway"]["telegram"]["allowed_users"] == [42]
        assert first["gateway"]["telegram"]["home_channel"] == 42
        assert "telegram-2" not in first["gateway"]
        assert second["gateway"]["telegram-2"]["enabled"] is True

    def test_load_round_trips(self, sandbox):
        gateways.add("telegram", "telegram", allowed=[42, 43], home=42)
        items = gateways.load()
        assert [item.name for item in items] == ["telegram"]
        assert items[0].allowed == ["42", "43"]
        assert items[0].home == "42"
        assert items[0].kind == "telegram"

    def test_a_duplicate_name_is_refused(self, sandbox):
        gateways.add("telegram")
        with pytest.raises(gateways.GatewayError, match="already exists"):
            gateways.add("telegram")

    def test_an_unknown_type_is_refused(self, sandbox):
        with pytest.raises(gateways.GatewayError, match="unknown gateway type"):
            gateways.add("signal", "signal")

    def test_names_are_restricted_to_something_a_unit_file_can_hold(self, sandbox):
        for bad in ("Telegram Bot", "-x", "a", "with/slash", ""):
            with pytest.raises(gateways.GatewayError):
                gateways.validate_name(bad)
        assert gateways.validate_name("Telegram-2") == "telegram-2"

    def test_remove_reports_a_missing_name(self, sandbox):
        gateways.add("telegram")
        assert gateways.remove("telegram").name == "telegram"
        with pytest.raises(gateways.GatewayError, match="no gateway named"):
            gateways.remove("telegram")

    def test_a_whatsapp_gateway_keeps_its_groups_and_mode(self, sandbox):
        gateways.save([gateways.Gateway(name="wa", kind="whatsapp", allowed=["+62 812-2227"],
                                        groups=["120363@g.us"], group_mode="all")])
        item = gateways.get("wa")
        assert item.allowed == ["628122227"]  # a typed number matches what WhatsApp reports
        assert (item.groups, item.group_mode) == (["120363@g.us"], "all")

    def test_a_telegram_gateway_keeps_its_signed_chats_and_their_own_modes(self, sandbox):
        """A Telegram group or channel id is negative; stripping the sign named another chat."""
        gateways.save([gateways.Gateway(name="tg", allowed=[7], home="-1001234",
                                        groups=["-1001234", "-1005678"], group_mode="mention",
                                        group_modes={"-1005678": "all"})])
        item = gateways.get("tg")
        assert (item.home, item.groups) == ("-1001234", ["-1001234", "-1005678"])
        assert (item.mode_for("-1001234"), item.mode_for("-1005678")) == ("mention", "all")

    def test_an_unknown_group_mode_falls_back_to_mention(self, sandbox):
        (sandbox / "gateways").mkdir()
        (sandbox / "gateways" / "wa").mkdir()
        (sandbox / "gateways" / "wa" / "gateway.toml").write_text(
            '[gateway.wa]\ntype = "whatsapp"\nallowed_users = [1]\ngroup_mode = "loud"\n')
        assert gateways.get("wa").group_mode == "mention"


class TestSelection:
    def test_selecting_by_name(self, sandbox):
        gateways.add("telegram")
        gateways.add("second")
        assert gateways.selected(None, "second").name == "second"

    def test_one_gateway_needs_no_name(self, sandbox):
        gateways.add("telegram")
        assert gateways.selected(None).name == "telegram"

    def test_two_gateways_require_a_name(self, sandbox):
        gateways.add("telegram")
        gateways.add("second")
        with pytest.raises(gateways.GatewayError, match="name one"):
            gateways.selected(None)

    def test_no_gateway_says_what_to_run(self, sandbox):
        with pytest.raises(gateways.GatewayError, match="gateway start"):
            gateways.selected(None)

    def test_an_unknown_name_lists_what_exists(self, sandbox):
        gateways.add("telegram")
        with pytest.raises(gateways.GatewayError, match="configured: telegram"):
            gateways.get("nope")


class TestCredentials:
    def test_the_telegram_gateway_keeps_the_old_env_key(self, sandbox):
        assert gateways.Gateway(name="telegram").token_var() == "TELEGRAM_BOT_TOKEN"

    def test_other_gateways_get_their_own_key(self, sandbox):
        assert gateways.Gateway(name="telegram-2").token_var() == \
            "OMPWIRE_GATEWAY_TELEGRAM_2_TOKEN"

    def test_set_token_only_touches_its_own_line(self, sandbox):
        own = gateways.env_file("telegram")
        own.parent.mkdir(parents=True)
        own.write_text("OTHER=keep\nTELEGRAM_BOT_TOKEN=old\n", encoding="utf-8")
        path = gateways.set_token(gateways.Gateway(name="telegram"), "new")
        assert path == own
        text = path.read_text()
        assert "OTHER=keep" in text
        assert "TELEGRAM_BOT_TOKEN=new" in text
        assert "old" not in text
        assert path.stat().st_mode & 0o777 == 0o600

    def test_two_gateways_do_not_share_a_credential(self, sandbox):
        gateways.set_token(gateways.Gateway(name="telegram"), "one")
        gateways.set_token(gateways.Gateway(name="telegram-2"), "two")
        assert gateways.token(gateways.Gateway(name="telegram")) == "one"
        assert gateways.token(gateways.Gateway(name="telegram-2")) == "two"

    def test_the_environment_wins_over_the_file(self, sandbox, monkeypatch):
        gateways.set_token(gateways.Gateway(name="telegram"), "from-file")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
        assert gateways.token(gateways.Gateway(name="telegram")) == "from-env"


class TestApply:
    def test_the_transport_reads_the_selected_gateway(self, sandbox):
        from ompwire.config import Config

        gateways.set_token(gateways.Gateway(name="telegram"), "tok")
        gateway = gateways.add("telegram", "telegram", allowed=[42], home=42)
        config = Config(data={"telegram": {"busy_message": "queue"}})

        gateways.apply(config, gateway)

        assert config.get("gateway", "credential") == "tok"
        assert config.get("gateway", "allowed_users") == ["42"]
        assert config.get("gateway", "home_channel") == "42"
        assert config.get("gateway", "name") == "telegram"
        # the platform section keeps only platform behaviour; no credential is copied into it
        assert config.data["telegram"] == {"busy_message": "queue"}


class TestLayout:
    """One directory per gateway, and the move from the old shared files."""

    @pytest.fixture
    def install(self, sandbox, monkeypatch, tmp_path):
        from ompwire import config as config_module
        from ompwire.store import SessionStore

        state = tmp_path / "install-state"
        state.mkdir()
        monkeypatch.setattr(config_module, "default_state_dir", lambda: state)
        (sandbox / "gateways.toml").write_text(
            '[gateway.telegram]\ntype = "telegram"\nallowed_users = [1]\n\n'
            '[gateway.work]\ntype = "telegram"\nallowed_users = [2]\n\n'
            '[gateway.work.agent]\nmodel = "w/m"\n', encoding="utf-8")
        (sandbox / ".env").write_text(
            "# a comment\nTELEGRAM_BOT_TOKEN=1:main\nOMPWIRE_GATEWAY_WORK_TOKEN=2:work\n"
            "OTHER=keep\n", encoding="utf-8")
        store = SessionStore(str(state / "sessions.db"))
        store.put("tg:7", "main-session", "/home")
        store.put("cli:/repo", "cli-session", "/repo")
        store.begin_turn("tg:7", "unfinished prompt")
        store.close()
        (state / "overrides.json").write_text('{"stream": {"show_reasoning": true}}')
        (state / "ompwire.log").write_text("old log\n")
        return state

    def test_migration_gives_every_gateway_its_own_files(self, sandbox, install):
        from ompwire.store import SessionStore

        moved = gateways.migrate(state_for="telegram")

        assert moved
        assert [item.name for item in gateways.load()] == ["telegram", "work"]
        assert gateways.get("work").settings == {"agent": {"model": "w/m"}}
        assert (sandbox / "gateways.toml.migrated").is_file()
        assert gateways.token(gateways.get("telegram")) == "1:main"
        assert gateways.token(gateways.get("work")) == "2:work"
        assert "1:main" not in (sandbox / ".env").read_text()  # out of the shared file
        assert "OTHER=keep" in (sandbox / ".env").read_text()  # anything else stays

        home = install / "gateways" / "telegram"
        assert not (install / "sessions.db").exists()
        store = SessionStore(str(home / "sessions.db"))
        assert store.get("tg:7").session_id == "main-session"
        assert store.interrupted("tg:") == [("tg:7", "unfinished prompt", 0)]  # resumes
        store.close()
        assert (home / "overrides.json").is_file() and (home / "ompwire.log").is_file()

        cli = SessionStore(str(install / "cli" / "sessions.db"))
        assert cli.get("cli:/repo").session_id == "cli-session"
        cli.close()

    def test_migration_can_run_again(self, sandbox, install):
        gateways.migrate(state_for="telegram")
        assert gateways.migrate(state_for="telegram") == []

    def test_state_held_by_a_running_gateway_is_not_moved(self, sandbox, install):
        from ompwire import lock

        handle = lock.acquire(install / "serve-telegram.lock")
        try:
            gateways.migrate(state_for="telegram")
        finally:
            lock.release(handle, install / "serve-telegram.lock")

        assert (install / "sessions.db").is_file()
        assert not (install / "gateways" / "telegram" / "sessions.db").exists()
        # definitions and credentials still move: they are read, not held
        assert gateways.token(gateways.get("telegram")) == "1:main"

    def test_a_gateway_config_points_inside_its_own_directory(self, sandbox, install):
        gateways.migrate(state_for="telegram")
        work = gateways.config_for(gateways.get("work"))
        home = install / "gateways" / "work"
        assert work.get("session", "store") == str(home / "sessions.db")
        assert work.overrides_path == home / "overrides.json"
        assert work.get("agent", "model") == "w/m"

    def test_the_agent_never_sees_a_bot_token(self, monkeypatch):
        from ompwire.engine import child_env

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:main")
        monkeypatch.setenv("OMPWIRE_GATEWAY_WORK_TOKEN", "2:work")
        monkeypatch.setenv("KEEP_ME", "yes")
        env = child_env()
        assert "TELEGRAM_BOT_TOKEN" not in env and "OMPWIRE_GATEWAY_WORK_TOKEN" not in env
        assert env["KEEP_ME"] == "yes"

    def test_removing_a_credential_removes_its_file(self, sandbox, install):
        gateways.migrate(state_for="telegram")
        gateways.unset_token(gateways.get("work"))
        assert not gateways.env_file("work").exists()
        assert gateways.gateway_file("work").is_file()  # the definition is remove()'s job


    def test_an_empty_store_at_the_target_does_not_block_the_move(self, sandbox, install):
        from ompwire.store import SessionStore

        SessionStore(str(install / "gateways" / "telegram" / "sessions.db")).close()
        gateways.migrate(state_for="telegram")
        store = SessionStore(str(install / "gateways" / "telegram" / "sessions.db"))
        assert store.get("tg:7").session_id == "main-session"
        store.close()


def test_status_does_not_create_a_store_it_only_reads(tmp_path):
    from ompwire import status

    target = tmp_path / "none" / "sessions.db"
    assert "none yet" in status._sessions(str(target))
    assert not target.exists()


def test_a_v09_telegram_block_becomes_a_gateway_on_migration(sandbox, monkeypatch):
    """v0.14 still ran an install whose only bot was config.toml's [telegram] block."""
    (sandbox / "config.toml").write_text(
        "[agent]\nmodel = \"m\"\n\n[telegram]\nallowed_users = [42]\nhome_channel = 42\n")
    (sandbox / ".env").write_text("TELEGRAM_BOT_TOKEN=9:old\n")

    gateways.migrate()

    item = gateways.get("telegram")
    assert (item.kind, item.allowed, item.home) == ("telegram", ["42"], "42")
    assert gateways.token(item) == "9:old"
    assert gateways.migrate() == []  # once


def test_no_telegram_block_means_no_gateway(sandbox):
    (sandbox / "config.toml").write_text("[agent]\nmodel = \"m\"\n")
    gateways.migrate()
    assert gateways.load() == []


def test_a_telegram_block_with_the_token_only_in_the_environment(sandbox, monkeypatch):
    (sandbox / "config.toml").write_text("[telegram]\nallowed_users = [5]\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "7:env")

    gateways.migrate()

    item = gateways.get("telegram")
    assert item.allowed == ["5"]
    assert gateways.token(item) == "7:env"


def test_the_shared_env_is_retired_once_it_holds_no_token(sandbox):
    (sandbox / "gateways.toml").write_text('[gateway.telegram]\ntype = "telegram"\n')
    (sandbox / ".env").write_text("# Secrets for ompwire.\nTELEGRAM_BOT_TOKEN=1:a\n")

    moved = gateways.migrate()

    assert not (sandbox / ".env").exists()
    assert any("removed" in line for line in moved)
    assert gateways.token(gateways.get("telegram")) == "1:a"


def test_a_shared_env_with_other_keys_is_kept_and_named(sandbox):
    (sandbox / "gateways.toml").write_text('[gateway.telegram]\ntype = "telegram"\n')
    (sandbox / ".env").write_text("TELEGRAM_BOT_TOKEN=1:a\nHONCHO_API_KEY=x\n")

    moved = gateways.migrate()

    assert (sandbox / ".env").read_text().strip() == "HONCHO_API_KEY=x"
    assert any("HONCHO_API_KEY" in line for line in moved)


def test_a_token_left_in_the_shared_env_is_not_read(sandbox):
    """Only migrate() reads the old file: a stray key there must not become a gateway's token."""
    gateways.add("telegram")
    (sandbox / ".env").write_text("TELEGRAM_BOT_TOKEN=stray\n")
    assert gateways.token(gateways.get("telegram")) == ""
