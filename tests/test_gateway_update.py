"""`ompwire gateway update`: change an existing gateway, the same way on every platform."""

from __future__ import annotations

import json

import pytest

from ompwire import __main__ as cli
from ompwire import gateways
from ompwire.store import SessionStore


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    from ompwire import config as config_module

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    state = tmp_path / "state"
    monkeypatch.setattr("ompwire.setup._paths",
                        lambda: (config_dir, config_dir / "config.toml", config_dir / ".env"))
    monkeypatch.setattr(gateways, "gateways_file", lambda: config_dir / "gateways.toml")
    monkeypatch.setattr(config_module, "default_state_dir", lambda: state)
    monkeypatch.setattr("ompwire.service._systemd_available", lambda: False)
    monkeypatch.setenv("OMPWIRE_CONFIG", str(config_dir / "config.toml"))
    return state


@pytest.fixture
def units(monkeypatch):
    """systemd, recorded: which gateways run, and which got restarted."""
    from types import SimpleNamespace

    units = SimpleNamespace(active=set(), restarted=[])
    monkeypatch.setattr("ompwire.service.state",
                        lambda name: "active" if name in units.active else "no unit")
    monkeypatch.setattr("ompwire.service.restart",
                        lambda name, **_k: units.restarted.append(name) or 0)
    return units


def _menu(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0) if queue else "")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


@pytest.mark.parametrize("kind", ["telegram", "whatsapp"])
def test_flags_set_every_setting_on_any_platform(sandbox, units, kind, tmp_path):
    gateways.save([gateways.Gateway(name="bot", kind=kind, allowed=[1])])
    work = tmp_path / "work"
    work.mkdir()

    assert cli.main(["gateway", "update", "bot", "--model", "a/b", "--cwd", str(work),
                     "--approval", "yolo", "--thinking", "high", "--reasoning", "on",
                     "--verbose", "off"]) == 0

    assert gateways.get("bot").settings == {
        "agent": {"model": "a/b", "cwd": str(work), "approval_mode": "yolo", "thinking": "high"},
        "stream": {"show_reasoning": True, "show_tool_calls": False},
    }


def test_inherit_drops_the_gateway_s_own_value(sandbox, units):
    gateways.save([gateways.Gateway(name="bot", allowed=[1],
                                    settings={"agent": {"model": "a/b", "thinking": "low"}})])

    assert cli.main(["gateway", "update", "bot", "--model", "inherit"]) == 0

    assert gateways.get("bot").settings == {"agent": {"thinking": "low"}}


def test_a_bad_value_writes_nothing(sandbox, units, capsys):
    gateways.save([gateways.Gateway(name="bot", allowed=[1])])

    assert cli.main(["gateway", "update", "bot", "--model", "x/y", "--cwd", "/no/such/dir"]) == 1

    assert gateways.get("bot").settings == {}
    assert "not a directory" in capsys.readouterr().err


def test_groups_and_mode_by_flag_keep_the_rest(sandbox, units):
    gateways.save([gateways.Gateway(name="bot", allowed=[1], settings={"agent": {"model": "a/b"}})])

    assert cli.main(["gateway", "update", "bot", "--groups", "-1001,-1002:all",
                     "--group-mode", "read"]) == 0

    item = gateways.get("bot")
    assert (item.groups, item.group_modes, item.group_mode) == (
        ["-1001", "-1002"], {"-1002": "all"}, "read")
    assert item.settings == {"agent": {"model": "a/b"}}


def test_a_running_gateway_restarts_once_and_a_stopped_one_does_not(sandbox, units):
    gateways.save([gateways.Gateway(name="on", allowed=[1]), gateways.Gateway(name="off", allowed=[1])])
    units.active = {"on"}

    assert cli.main(["gateway", "update", "on", "--thinking", "high", "--verbose", "off"]) == 0
    assert cli.main(["gateway", "update", "off", "--thinking", "high"]) == 0
    assert cli.main(["gateway", "update", "on", "--reasoning", "on", "--no-start"]) == 0

    assert units.restarted == ["on"]


def test_two_gateways_need_a_name_without_a_terminal(sandbox, units, capsys):
    gateways.save([gateways.Gateway(name="a", allowed=[1]), gateways.Gateway(name="b", allowed=[1])])

    assert cli.main(["gateway", "update", "--thinking", "high", "--yes"]) == 1

    assert all(item.settings == {} for item in gateways.load())


def test_a_new_default_clears_the_direct_chat_choice_that_would_hide_it(sandbox, units):
    """/thinking or /reasoning in a direct chat is stored for that chat and beats the gateway's
    setting; changing the setting here must reach that chat. A group keeps its own choice."""
    gateways.save([gateways.Gateway(name="bot", allowed=[1])])
    home = gateways.state_dir("bot")
    home.mkdir(parents=True, exist_ok=True)
    store = SessionStore(str(home / "sessions.db"))
    store.set_pref("tg:42", "thinking", "low")
    store.set_pref("tg:42", "model", "keep/me")
    store.set_pref("tg:-1001", "thinking", "minimal")
    store.close()
    (home / "overrides.json").write_text(json.dumps({"stream": {"show_reasoning": True}}))

    assert cli.main(["gateway", "update", "bot", "--thinking", "high", "--reasoning", "off"]) == 0

    store = SessionStore(str(home / "sessions.db"))
    assert store.prefs("tg:42") == {"model": "keep/me"}
    assert store.prefs("tg:-1001") == {"thinking": "minimal"}
    store.close()
    assert json.loads((home / "overrides.json").read_text()) == {}


def test_the_menu_picks_a_seen_group_by_number_and_saves(sandbox, units, monkeypatch):
    gateways.save([gateways.Gateway(name="bot", kind="whatsapp", allowed=[1])])
    gateways.remember_chat("bot", "120363001@g.us", "Family", "group")
    gateways.remember_chat("bot", "120363002@g.us", "Office", "group")
    # 1 = groups; "2" switches on Office (sorted by name); mode "2" = all; then 9 = save
    _menu(monkeypatch, "1", "2", "2", "9")

    assert cli.main(["gateway", "update", "bot"]) == 0

    item = gateways.get("bot")
    assert item.groups == ["120363002@g.us"]
    assert item.group_modes == {"120363002@g.us": "all"}


def test_the_menu_sets_thinking_from_a_list_and_quit_saves_nothing(sandbox, units, monkeypatch):
    gateways.save([gateways.Gateway(name="bot", allowed=[1])])
    monkeypatch.setattr("ompwire.gateway_update._agent_choices",
                        lambda cwd: {"models": [], "thinking": ["off", "low", "high"]})
    # 6 = thinking level; "3" = high; then 10 = quit without saving
    _menu(monkeypatch, "6", "3", "10")
    assert cli.main(["gateway", "update", "bot"]) == 0
    assert gateways.get("bot").settings == {}

    _menu(monkeypatch, "6", "3", "9")
    assert cli.main(["gateway", "update", "bot"]) == 0
    assert gateways.get("bot").settings == {"agent": {"thinking": "high"}}


def test_a_broken_agent_is_named_instead_of_offering_no_models(sandbox, monkeypatch, capsys):
    """An agent that failed to start read as an agent offering nothing."""
    from ompwire.gateway_update import _agent_choices

    def broken(cwd, command="omp"):
        raise RuntimeError("omp exited 127")

    monkeypatch.setattr("ompwire.setup.agent_catalog", broken)
    assert _agent_choices("/tmp") == {}
    assert "could not ask the agent for its models: omp exited 127" in capsys.readouterr().out


def test_a_seen_chat_keeps_its_title_when_a_message_brings_none(sandbox):
    assert gateways.remember_chat("bot", "120363001@g.us", "Family", "group")
    assert not gateways.remember_chat("bot", "120363001@g.us", "", "group")
    assert gateways.remember_chat("bot", "-1001", "Ops", "channel")

    assert gateways.seen_chats("bot") == {
        "120363001@g.us": {"title": "Family", "kind": "group"},
        "-1001": {"title": "Ops", "kind": "channel"},
    }


def test_a_list_of_negative_telegram_ids_is_a_value_not_a_flag(sandbox, units):
    """The guide's own example: argparse used to stop at `--groups -100…,-100…`."""
    gateways.save([gateways.Gateway(name="bot", allowed=[1])])

    assert cli.main(["gateway", "update", "bot", "--groups", "-1001234,-1009876:all"]) == 0

    assert gateways.get("bot").groups == ["-1001234", "-1009876"]
