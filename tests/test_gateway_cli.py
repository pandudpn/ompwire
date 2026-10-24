"""`ompwire gateway ...`: the CLI that adds, lists and controls bot connections."""

from __future__ import annotations

import io

import pytest

from ompwire import __main__ as cli
from ompwire import gateways


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()

    class Paths:
        def __call__(self):
            return config_dir, config_dir / "config.toml", config_dir / ".env"

    monkeypatch.setattr("ompwire.setup._paths", Paths())
    monkeypatch.setattr(gateways, "gateways_file", lambda: config_dir / "gateways.toml")
    monkeypatch.setattr(gateways, "_from_config", lambda config=None: [])
    monkeypatch.setattr("ompwire.service._systemd_available", lambda: False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return config_dir


def _identifies(monkeypatch, username: str = "test_bot"):
    monkeypatch.setattr("ompwire.setup.bot_identity", lambda token, timeout=10.0: username)


def test_list_says_how_to_add_the_first_gateway(sandbox, capsys):
    assert cli.main(["gateway", "list"]) == 0
    assert "gateway add telegram" in capsys.readouterr().out


def test_add_reads_the_token_from_stdin(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("123:abcdef\n"))

    def answer(prompt: str = "") -> str:
        return "" if "type" in prompt else "42"

    monkeypatch.setattr("builtins.input", answer)

    assert cli.main(["gateway", "add", "telegram", "--token-stdin"]) == 0

    out = capsys.readouterr().out
    assert "token valid: @test_bot" in out
    assert "allowed users: 42" in out
    assert (sandbox / ".env").read_text().strip() == "TELEGRAM_BOT_TOKEN=123:abcdef"
    items = gateways.load()
    assert items[0].name == "telegram" and items[0].allowed == [42]


def test_a_rejected_token_writes_nothing(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch, username="")
    monkeypatch.setattr("sys.stdin", io.StringIO("123:wrong\n"))

    assert cli.main(["gateway", "add", "telegram", "--token-stdin", "--yes"]) == 1

    assert "rejected" in capsys.readouterr().err.lower()
    assert not (sandbox / "gateways.toml").exists()
    assert not (sandbox / ".env").exists()


def test_add_refuses_a_name_that_exists_without_force(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    gateways.add("telegram")
    monkeypatch.setattr("sys.stdin", io.StringIO("123:abcdef\n"))

    assert cli.main(["gateway", "add", "telegram", "--token-stdin", "--yes"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_add_replaces_the_credential_with_force(sandbox, monkeypatch):
    _identifies(monkeypatch)
    gateways.add("telegram", "telegram", allowed=[42])
    monkeypatch.setattr("sys.stdin", io.StringIO("999:new\n"))

    assert cli.main(["gateway", "add", "telegram", "--token-stdin", "--yes", "--force"]) == 0
    assert "999:new" in (sandbox / ".env").read_text()
    assert gateways.load()[0].allowed == [42]        # the allow list survives


def test_whatsapp_is_refused_with_a_reason(sandbox, capsys):
    assert cli.main(["gateway", "add", "whatsapp", "--kind", "whatsapp", "--yes"]) == 1
    out = capsys.readouterr().err
    assert "not implemented yet" in out
    assert not (sandbox / "gateways.toml").exists()


def test_an_out_of_range_menu_answer_is_an_error_not_a_traceback(sandbox, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "9")
    assert cli.main(["gateway", "add"]) == 1
    assert "unknown gateway type" in capsys.readouterr().err


def test_types_lists_both_with_their_state(sandbox, capsys):
    assert cli.main(["gateway", "types"]) == 0
    out = capsys.readouterr().out
    assert "telegram" in out and "implemented" in out
    assert "whatsapp" in out and "not implemented yet" in out


def test_list_shows_identity_users_and_state(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch, "vidura_omp_bot")
    gateways.add("telegram", "telegram", allowed=[749994884])
    gateways.set_token(gateways.Gateway(name="telegram"), "tok")

    assert cli.main(["gateway", "list"]) == 0
    out = capsys.readouterr().out
    assert "@vidura_omp_bot" in out and "749994884" in out and "no unit" in out


def test_list_marks_a_dead_credential(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch, "")
    gateways.add("telegram")
    gateways.set_token(gateways.Gateway(name="telegram"), "tok")

    assert cli.main(["gateway", "list"]) == 0
    assert "credential rejected" in capsys.readouterr().out


def test_remove_deletes_the_definition(sandbox, capsys):
    gateways.add("telegram")
    assert cli.main(["gateway", "remove", "telegram"]) == 0
    assert gateways.load() == []
    assert "removed gateway" in capsys.readouterr().out


def test_a_second_gateway_must_be_named(sandbox, capsys):
    gateways.add("telegram")
    gateways.add("telegram-2")
    assert cli.main(["gateway", "start"]) == 1
    assert "name one" in capsys.readouterr().err


def test_start_without_a_unit_says_how_to_install_it(sandbox, capsys):
    gateways.add("telegram")
    # systemd is unavailable in this sandbox, which is its own honest message
    assert cli.main(["gateway", "start", "telegram"]) == 1
    assert "not available" in capsys.readouterr().out


def test_stop_refuses_a_gateway_that_does_not_exist(sandbox, capsys):
    gateways.add("telegram")
    gateways.add("telegram-2")
    assert cli.main(["gateway", "stop", "nope"]) == 1
    assert "no gateway named" in capsys.readouterr().err


def test_a_transport_that_does_not_exist_yet_is_refused(sandbox, capsys):
    gateways.add("whatsapp", "whatsapp")
    assert cli.main(["gateway", "start", "whatsapp"]) == 1
    assert "not implemented" in capsys.readouterr().err


def test_there_is_no_serve_or_service_command_any_more():
    """One noun per concept: `gateway`, with start/stop/restart inside it."""
    for gone in ("serve", "service"):
        with pytest.raises(SystemExit):
            cli.main([gone])


def test_status_reports_state_unit_and_logs(sandbox, capsys):
    gateways.add("telegram", "telegram", allowed=[42])
    assert cli.main(["gateway", "status"]) == 0
    out = capsys.readouterr().out
    assert "telegram  telegram  no unit" in out
    assert "ompwire-telegram.service" in out
    assert "gateway start telegram" in out


def test_status_for_one_gateway_only(sandbox, capsys):
    gateways.add("telegram")
    gateways.add("telegram-2")
    assert cli.main(["gateway", "status", "telegram-2"]) == 0
    out = capsys.readouterr().out
    assert "telegram-2" in out and "telegram  telegram" not in out


def test_start_without_systemd_says_so(sandbox, capsys):
    """A gateway only runs as a unit, so no systemd means no gateway. Say it plainly."""
    gateways.add("telegram")
    assert cli.main(["gateway", "start", "telegram"]) == 1
    out = capsys.readouterr().out
    assert "systemd --user is not available" in out
    assert "gateway _run telegram" in out


def test_the_unit_entry_point_is_the_only_foreground_path(sandbox, monkeypatch):
    """`_run` exists for systemd. It is not advertised: there is no foreground mode."""
    gateways.add("telegram")
    called: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(cli, "_run_gateway",
                        lambda config, name, force: called.append((str(config), name, force)) or 0)
    assert cli.main(["gateway", "_run", "telegram"]) == 0
    assert called == [("None", "telegram", False)]
    out = cli.build_parser().format_help()
    assert "_run" not in out


def test_logs_falls_back_to_journalctl(sandbox, monkeypatch, capsys):
    gateways.add("telegram")

    class Result:
        returncode = 0
        stdout = "line one\nline two"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    assert cli.main(["gateway", "logs", "telegram"]) == 0
    assert "line one" in capsys.readouterr().out
