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
    monkeypatch.setattr(gateways, "_from_config", lambda config=None: [])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return config_dir


class TestDefinitions:
    def test_add_writes_one_section_per_gateway(self, sandbox):
        gateways.add("telegram", "telegram", allowed=[42], home=42)
        gateways.add("telegram-2", "telegram", allowed=[7])

        parsed = tomllib.loads((sandbox / "gateways.toml").read_text())
        assert parsed["gateway"]["telegram"]["allowed_users"] == [42]
        assert parsed["gateway"]["telegram"]["home_channel"] == 42
        assert parsed["gateway"]["telegram-2"]["enabled"] is True

    def test_load_round_trips(self, sandbox):
        gateways.add("telegram", "telegram", allowed=[42, 43], home=42)
        items = gateways.load()
        assert [item.name for item in items] == ["telegram"]
        assert items[0].allowed == [42, 43]
        assert items[0].home == 42
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

    def test_whatsapp_is_known_but_not_implemented(self, sandbox):
        item = gateways.add("whatsapp", "whatsapp")
        assert item.kind == "whatsapp"
        assert item.implemented is False
        assert gateways.get("whatsapp").implemented is False


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


class TestLegacyConfig:
    def test_a_telegram_block_in_config_toml_still_counts(self):
        class Config:
            def get(self, section, key, default=None):
                data = {"telegram": {"allowed_users": [42], "home_channel": 42}}
                return data.get(section, {}).get(key, default)

        items = gateways._from_config(Config())
        assert [(item.name, item.kind, item.allowed) for item in items] == [
            ("telegram", "telegram", [42])]

    def test_an_empty_config_is_no_gateway(self):
        class Config:
            def get(self, section, key, default=None):
                return default

        assert gateways._from_config(Config()) == []


class TestCredentials:
    def test_the_telegram_gateway_keeps_the_old_env_key(self, sandbox):
        assert gateways.Gateway(name="telegram").token_var() == "TELEGRAM_BOT_TOKEN"

    def test_other_gateways_get_their_own_key(self, sandbox):
        assert gateways.Gateway(name="telegram-2").token_var() == \
            "OMPWIRE_GATEWAY_TELEGRAM_2_TOKEN"

    def test_set_token_only_touches_its_own_line(self, sandbox):
        (sandbox / ".env").write_text("OTHER=keep\nTELEGRAM_BOT_TOKEN=old\n", encoding="utf-8")
        path = gateways.set_token(gateways.Gateway(name="telegram"), "new")
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
        config = Config(data={"telegram": {"verbose": True}})

        gateways.apply(config, gateway)

        assert config.get("telegram", "bot_token") == "tok"
        assert config.get("telegram", "allowed_users") == [42]
        assert config.get("telegram", "home_channel") == 42
        assert config.get("telegram", "verbose") is True      # other keys survive
        assert config.data["_gateway"]["name"] == "telegram"
