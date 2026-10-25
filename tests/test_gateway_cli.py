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


def _envs(sandbox) -> str:
    """Every credential file under the sandbox, joined: each gateway has its own .env."""
    return "".join(path.read_text() for path in sorted(sandbox.rglob(".env")))


def _identifies(monkeypatch, username: str = "test_bot"):
    monkeypatch.setattr("ompwire.setup.bot_identity", lambda token, timeout=10.0: username)


def test_list_says_how_to_add_the_first_gateway(sandbox, capsys):
    assert cli.main(["gateway", "list"]) == 0
    assert "gateway start" in capsys.readouterr().out


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
    assert cli.main(["gateway", "remove", "telegram", "--yes"]) == 0
    assert gateways.load() == []
    assert "removed gateway" in capsys.readouterr().out


def test_a_second_gateway_must_be_named(sandbox, capsys):
    gateways.add("telegram")
    gateways.add("telegram-2")
    assert cli.main(["gateway", "stop"]) == 1
    assert "name one" in capsys.readouterr().err


def test_start_without_a_unit_says_how_to_install_it(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "123:abc")
    # systemd is unavailable in this sandbox, which is its own honest message
    assert cli.main(["gateway", "start", "telegram", "--yes"]) == 1
    assert "not available" in capsys.readouterr().out


def test_stop_refuses_a_gateway_that_does_not_exist(sandbox, capsys):
    gateways.add("telegram")
    gateways.add("telegram-2")
    assert cli.main(["gateway", "stop", "nope"]) == 1
    assert "no gateway named" in capsys.readouterr().err


def test_a_transport_that_does_not_exist_yet_is_refused(sandbox, capsys):
    gateways.add("whatsapp", "whatsapp")
    assert cli.main(["gateway", "start", "whatsapp", "--yes"]) == 1
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


def test_start_without_systemd_says_so(sandbox, monkeypatch, capsys):
    """A gateway only runs as a unit, so no systemd means no gateway. Say it plainly."""
    _identifies(monkeypatch)
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "123:abc")
    assert cli.main(["gateway", "start", "telegram", "--yes"]) == 1
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


# ------------------------------------------------------------------ gateway start
class Units:
    """service, recorded instead of systemd: what ran, in order, and what was in .env then."""

    def __init__(self, monkeypatch, sandbox, active=()):
        self.calls: list[tuple[str, str, str]] = []
        self.active = set(active)
        self.sandbox = sandbox

        def record(action):
            def call(name, **_kw):
                text = _envs(self.sandbox)
                self.calls.append((action, name, text))
                if action == "stop":
                    self.active.discard(name)
                return 0
            return call

        monkeypatch.setattr("ompwire.service.start", record("start"))
        monkeypatch.setattr("ompwire.service.restart", record("restart"))
        monkeypatch.setattr("ompwire.service.stop", record("stop"))
        monkeypatch.setattr("ompwire.service.state",
                            lambda name: "active" if name in self.active else "no unit")


def _answers(monkeypatch, *replies, secrets=()):
    """Typed answers in order; secrets come from the hidden prompt."""
    asked: list[str] = []
    queue = list(replies)
    hidden = list(secrets)

    def answer(prompt: str = "") -> str:
        asked.append(prompt)
        return queue.pop(0) if queue else ""

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli, "_ask_secret_line", lambda prompt: asked.append(prompt) or (
        hidden.pop(0) if hidden else ""))
    return asked


def test_first_start_asks_platform_credential_and_users_then_starts(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch, "first_bot")
    units = Units(monkeypatch, sandbox)
    asked = _answers(monkeypatch, "1", "42", secrets=["123:abcdef"])

    assert cli.main(["gateway", "start"]) == 0

    assert any("platform" in prompt for prompt in asked)
    assert any("bot token" in prompt for prompt in asked)
    item = gateways.load()[0]
    assert (item.name, item.kind, item.allowed) == ("telegram", "telegram", [42])
    assert "123:abcdef" in _envs(sandbox)
    assert units.calls[-1][:2] == ("start", "telegram")
    assert "credential valid: @first_bot" in capsys.readouterr().out


def test_a_rejected_credential_writes_nothing(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch, "")
    units = Units(monkeypatch, sandbox)
    monkeypatch.setattr("sys.stdin", io.StringIO("123:wrong\n"))

    assert cli.main(["gateway", "start", "--platform", "telegram", "--token-stdin",
                     "--yes"]) == 1

    assert "no valid credential" in capsys.readouterr().err
    assert gateways.load() == []
    assert _envs(sandbox) == ""
    assert units.calls == []


def test_a_rejected_credential_is_asked_again(sandbox, monkeypatch):
    accepted = {"good:token": "bot"}
    monkeypatch.setattr("ompwire.setup.bot_identity",
                        lambda token, timeout=10.0: accepted.get(token, ""))
    Units(monkeypatch, sandbox)
    _answers(monkeypatch, "1", "7", secrets=["bad:token", "good:token"])

    assert cli.main(["gateway", "start"]) == 0
    assert "good:token" in _envs(sandbox)


def test_scripted_start_needs_no_prompt(sandbox, monkeypatch):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    asked = _answers(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("123:abcdef\n"))

    assert cli.main(["gateway", "start", "--platform", "telegram", "--token-stdin", "--yes",
                     "--allowed", "11, 22"]) == 0

    assert asked == []
    assert gateways.load()[0].allowed == [11, 22]


def test_existing_profiles_are_listed_with_a_masked_credential(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    units = Units(monkeypatch, sandbox, active={"telegram"})
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "123456:secretvalue")
    asked = _answers(monkeypatch, "1", "1", "n")

    assert cli.main(["gateway", "start"]) == 0

    out = capsys.readouterr().out
    assert "telegram   (credential 1234…alue)" in out
    assert "add a new profile" in out
    assert "secretvalue" not in out
    assert not any("bot token" in prompt for prompt in asked)  # kept, not asked again
    assert units.calls == [("restart", "telegram", units.calls[0][2])]  # running: restart


def test_rerunning_start_is_safe_and_changes_nothing(sandbox, monkeypatch):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "123:abc")
    before = (_envs(sandbox), gateways.gateway_file("telegram").read_text())
    _answers(monkeypatch)

    assert cli.main(["gateway", "start", "telegram", "--yes"]) == 0
    assert cli.main(["gateway", "start", "telegram", "--yes"]) == 0

    assert (_envs(sandbox), gateways.gateway_file("telegram").read_text()) == before


def test_replacing_a_credential_stops_the_old_poller_first(sandbox, monkeypatch):
    _identifies(monkeypatch)
    units = Units(monkeypatch, sandbox, active={"telegram"})
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "111:old")
    _answers(monkeypatch, "1", "1", "y", secrets=["222:new"])

    assert cli.main(["gateway", "start"]) == 0

    assert [call[:2] for call in units.calls] == [("stop", "telegram"), ("start", "telegram")]
    assert "111:old" in units.calls[0][2]  # stopped while the old token was still stored
    assert "222:new" in units.calls[1][2]
    assert gateways.load()[0].allowed == [42]  # the allow list survives


def test_add_a_new_profile_on_a_platform_that_has_one(sandbox, monkeypatch):
    _identifies(monkeypatch)
    units = Units(monkeypatch, sandbox)
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "111:first")
    _answers(monkeypatch, "1", "2", "work", "77", secrets=["222:second"])

    assert cli.main(["gateway", "start"]) == 0

    names = {item.name: item for item in gateways.load()}
    assert set(names) == {"telegram", "work"}
    assert names["work"].allowed == [77]
    env = _envs(sandbox)
    assert "111:first" in env and "222:second" in env  # one credential each
    assert units.calls[-1][:2] == ("start", "work")


def test_no_start_saves_without_starting(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    units = Units(monkeypatch, sandbox)
    monkeypatch.setattr("sys.stdin", io.StringIO("123:abcdef\n"))

    assert cli.main(["gateway", "start", "--platform", "telegram", "--token-stdin", "--yes",
                     "--allowed", "1", "--no-start"]) == 0

    assert units.calls == []
    assert gateways.load()[0].name == "telegram"


def test_start_preserves_a_disabled_gateway(sandbox, monkeypatch):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    gateways.add("telegram", enabled=False, allowed=[1])
    monkeypatch.setattr("sys.stdin", io.StringIO("123:abcdef\n"))

    assert cli.main(["gateway", "start", "telegram", "--token-stdin", "--yes"]) == 0
    assert gateways.load()[0].enabled is False


def test_start_rejects_an_invalid_name(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    monkeypatch.setattr("sys.stdin", io.StringIO("123:abcdef\n"))

    assert cli.main(["gateway", "start", "bad name!", "--platform", "telegram",
                     "--token-stdin", "--yes"]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_whatsapp_is_refused_before_anything_is_written(sandbox, capsys):
    assert cli.main(["gateway", "start", "--platform", "whatsapp", "--yes"]) == 1
    assert "not implemented yet" in capsys.readouterr().err
    assert gateways.load() == []


def test_an_out_of_range_menu_answer_is_an_error_not_a_traceback(sandbox, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "9")
    assert cli.main(["gateway", "start"]) == 1
    assert "not one of the platforms" in capsys.readouterr().err


def test_there_is_no_add_command_any_more():
    with pytest.raises(SystemExit):
        cli.main(["gateway", "add", "telegram"])


def test_bare_gateway_prints_its_help(sandbox, capsys):
    assert cli.main(["gateway"]) == 0
    out = capsys.readouterr().out
    assert "usage: ompwire gateway" in out and "--platform" in out


def test_input_that_ends_mid_question_is_a_message_not_a_traceback(sandbox, monkeypatch, capsys):
    def closed(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert cli.main(["gateway", "start"]) == 1
    assert "input ended" in capsys.readouterr().err
    assert gateways.load() == []


def test_every_implemented_platform_explains_its_credential_and_user_ids():
    """The how-to is part of a platform, not an afterthought of the first one."""
    for kind in gateways.IMPLEMENTED:
        spec = gateways.platform(kind)
        assert spec.credential_steps, kind
        assert spec.user_id_steps, kind


def test_first_start_shows_how_to_get_a_token_and_a_user_id(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    _answers(monkeypatch, "1", "42", secrets=["123:abcdef"])

    assert cli.main(["gateway", "start"]) == 0

    out = capsys.readouterr().out
    assert "how to get a bot token from @BotFather:" in out
    assert "/newbot" in out
    assert "who may talk to it: telegram user ids:" in out
    assert "@userinfobot" in out
    assert out.index("/newbot") < out.index("credential valid")  # before the question


def test_a_rerun_with_everything_known_prints_no_how_to(sandbox, monkeypatch, capsys):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "123:abc")

    assert cli.main(["gateway", "start", "telegram", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "/newbot" not in out and "@userinfobot" not in out


# ------------------------------------------------------------------ per-gateway isolation
def test_two_gateways_get_their_own_settings_and_state(sandbox, monkeypatch, tmp_path):
    from ompwire import config as config_module

    state = tmp_path / "state-root"
    monkeypatch.setattr(config_module, "default_state_dir", lambda: state)
    (sandbox / "config.toml").write_text('[agent]\nmodel = "shared/model"\ncwd = "/shared"\n')
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(sandbox))
    gateways.add("telegram", allowed=[1])
    work = gateways.Gateway(name="work", allowed=[2],
                            settings={"agent": {"model": "work/model", "cwd": "/work"}})
    gateways.save(gateways.load() + [work])

    shared = gateways.config_for(gateways.get("telegram"))
    own = gateways.config_for(gateways.get("work"))

    assert shared.get("agent", "model") == "shared/model"  # telegram follows config.toml
    assert (own.get("agent", "model"), own.get("agent", "cwd")) == ("work/model", "/work")
    assert own.get("agent", "thinking") == shared.get("agent", "thinking")  # rest is shared
    # never the same session store or chat overrides, so /new or /reasoning stays in one bot
    assert shared.get("session", "store") != own.get("session", "store")
    assert own.get("session", "store") == str(state / "gateways" / "work" / "sessions.db")
    assert shared.overrides_path != own.overrides_path


def test_a_chat_toggle_in_one_gateway_does_not_reach_another(sandbox, monkeypatch, tmp_path):
    from ompwire import config as config_module

    state = tmp_path / "state-root"
    monkeypatch.setattr(config_module, "default_state_dir", lambda: state)
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", state / "overrides.json")
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(sandbox))
    gateways.save([gateways.Gateway(name="telegram", allowed=[1]),
                   gateways.Gateway(name="work", allowed=[2])])

    work = gateways.config_for(gateways.get("work"))
    config_module.save_override("stream", "show_reasoning", True, work.overrides_path)

    assert gateways.config_for(gateways.get("work")).get("stream", "show_reasoning") is True
    assert gateways.config_for(gateways.get("telegram")).get("stream", "show_reasoning") is False


def test_settings_survive_a_save_and_reload(sandbox):
    item = gateways.Gateway(name="work", allowed=[2],
                            settings={"agent": {"model": "a/b", "cwd": "/w"},
                                      "stream": {"show_reasoning": True}})
    gateways.save([item])
    assert gateways.load()[0].settings == item.settings


def test_a_new_profile_can_set_its_own_model_and_cwd(sandbox, monkeypatch):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    gateways.add("telegram", allowed=[42])
    gateways.set_token(gateways.Gateway(name="telegram"), "111:first")
    _answers(monkeypatch, "1", "2", "work", "77", "2", "other/model", "/srv/work", "",
             secrets=["222:second"])

    assert cli.main(["gateway", "start"]) == 0

    work = gateways.get("work")
    assert work.settings == {"agent": {"model": "other/model", "cwd": "/srv/work"}}
    assert gateways.get("telegram").settings == {}


def test_scripted_start_sets_own_settings_with_flags(sandbox, monkeypatch):
    _identifies(monkeypatch)
    Units(monkeypatch, sandbox)
    monkeypatch.setattr("sys.stdin", io.StringIO("123:abcdef\n"))

    assert cli.main(["gateway", "start", "work", "--platform", "telegram", "--token-stdin",
                     "--allowed", "5", "--model", "x/y", "--approval", "yolo", "--yes"]) == 0

    assert gateways.get("work").settings == {"agent": {"model": "x/y", "approval_mode": "yolo"}}


# ------------------------------------------------------------------ gateway remove
def _two_bots(sandbox, monkeypatch, tmp_path):
    from ompwire import config as config_module

    state = tmp_path / "state-root"
    monkeypatch.setattr(config_module, "default_state_dir", lambda: state)
    gateways.save([gateways.Gateway(name="telegram", allowed=[1]),
                   gateways.Gateway(name="work", allowed=[2])])
    gateways.set_token(gateways.Gateway(name="telegram"), "1:main")
    gateways.set_token(gateways.Gateway(name="work"), "2:work")
    work_state = state / "gateways" / "work"
    work_state.mkdir(parents=True)
    (work_state / "sessions.db").write_text("x")
    removed: list[str] = []
    monkeypatch.setattr("ompwire.service.remove", lambda name, **_k: removed.append(name) or 0)
    return work_state, removed


def test_remove_asks_which_then_keeps_state_by_default(sandbox, monkeypatch, tmp_path, capsys):
    work_state, removed = _two_bots(sandbox, monkeypatch, tmp_path)
    _answers(monkeypatch, "2", "1", "work")

    assert cli.main(["gateway", "remove"]) == 0

    assert [item.name for item in gateways.load()] == ["telegram"]
    assert removed == ["work"]
    env = _envs(sandbox)
    assert "2:work" not in env and "1:main" in env  # only its own credential goes
    assert work_state.is_dir()  # kept
    assert "kept its sessions and log" in capsys.readouterr().out


def test_full_delete_removes_the_state_too(sandbox, monkeypatch, tmp_path):
    work_state, _removed = _two_bots(sandbox, monkeypatch, tmp_path)
    _answers(monkeypatch, "2", "2", "work")

    assert cli.main(["gateway", "remove"]) == 0
    assert not work_state.exists()


def test_remove_needs_the_name_typed_back(sandbox, monkeypatch, tmp_path, capsys):
    work_state, removed = _two_bots(sandbox, monkeypatch, tmp_path)
    _answers(monkeypatch, "2", "2", "wrok")

    assert cli.main(["gateway", "remove"]) == 1
    assert len(gateways.load()) == 2 and removed == [] and work_state.is_dir()
    assert "2:work" in _envs(sandbox)


def test_scripted_remove_and_purge(sandbox, monkeypatch, tmp_path):
    work_state, _removed = _two_bots(sandbox, monkeypatch, tmp_path)
    asked = _answers(monkeypatch)

    assert cli.main(["gateway", "remove", "work", "--yes", "--purge"]) == 0
    assert asked == [] and not work_state.exists()


def test_yes_never_picks_a_gateway_to_remove(sandbox, monkeypatch, tmp_path, capsys):
    _two_bots(sandbox, monkeypatch, tmp_path)
    assert cli.main(["gateway", "remove", "--yes"]) == 1
    assert len(gateways.load()) == 2


def test_remove_refuses_while_a_schedule_reports_to_it(sandbox, monkeypatch, tmp_path, capsys):
    from ompwire import schedules

    _two_bots(sandbox, monkeypatch, tmp_path)
    monkeypatch.setattr(schedules, "load",
                        lambda: {"morning": schedules.Schedule(name="morning", cron="0 7 * * *",
                                                               prompt="p", gateway="work")})
    assert cli.main(["gateway", "remove", "work", "--yes"]) == 1
    assert "morning" in capsys.readouterr().err
    assert len(gateways.load()) == 2
