"""Schedules: the cron translation, the units, the store, and the marker that only proposes."""

from __future__ import annotations

import subprocess

import pytest

from ompwire import schedules


@pytest.fixture(autouse=True)
def sandbox_config(monkeypatch, tmp_path):
    monkeypatch.setenv("OMPWIRE_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(("cron", "expected"), [
    ("0 7 * * *", "*-*-* 07:00:00"),
    ("30 6 * * 1-5", "Mon..Fri *-*-* 06:30:00"),
    ("*/15 * * * *", "*-*-* *:00/15:00"),
    ("0 */6 * * *", "*-*-* 00/06:00:00"),
    ("15 * * * *", "*-*-* *:15:00"),
    ("0 7,19 * * *", "*-*-* 07:00:00,19:00:00"),
    ("0 9 * * 0", "Sun *-*-* 09:00:00"),
    ("0 9 * * 7", "Sun *-*-* 09:00:00"),
    ("0 9 1 * *", "*-*-01 09:00:00"),
    ("0 9 * 3 *", "*-03-* 09:00:00"),
    ("0 7 * * 1,3,5", "Mon,Wed,Fri *-*-* 07:00:00"),
])
def test_cron_becomes_a_calendar_expression(cron, expected):
    assert schedules.to_on_calendar(cron) == expected


@pytest.mark.parametrize("cron", [
    "* * * *",            # four fields
    "0 7 * * 9",          # no such weekday
    "0 7 1-5 * *",        # a day range cron can do and this cannot
    "*/5 7 * * *",        # a step that only makes sense with every hour
    "x y z a b",          # not cron at all
])
def test_what_cannot_be_translated_is_refused_plainly(cron):
    with pytest.raises(schedules.ScheduleError):
        schedules.to_on_calendar(cron)


def test_the_clock_is_the_one_the_bot_shows():
    item = schedules.Schedule(name="morning", cron="0 7 * * *", prompt="x")
    assert item.on_calendar.endswith("Asia/Jakarta")
    assert item.on_calendar.startswith("*-*-* 07:00:00")


def test_a_systemd_expression_can_be_used_directly():
    item = schedules.Schedule(name="weekday", cron="", calendar="Mon..Fri 07:00", prompt="x")
    assert item.on_calendar == "Mon..Fri 07:00 Asia/Jakarta"


def test_saving_and_loading_round_trips(tmp_path):
    schedules.save({"morning": schedules.Schedule(
        name="morning", cron="0 7 * * *", prompt='ringkas "repo"\\semalam',
        gateway="telegram", timeout=600.0)})
    loaded = schedules.load()

    assert list(loaded) == ["morning"]
    item = loaded["morning"]
    assert item.prompt == 'ringkas "repo"\\semalam'
    assert item.timeout == 600.0
    assert item.ephemeral is True


def test_names_are_checked():
    assert schedules.validate_name(" Morning-1 ") == "morning-1"
    for bad in ("", "has space", "UPPER!", "x" * 40):
        with pytest.raises(schedules.ScheduleError):
            schedules.validate_name(bad)


def test_adding_the_same_name_twice_is_refused(monkeypatch):
    monkeypatch.setattr(schedules, "validate_calendar", lambda _expression: None)
    monkeypatch.setattr(schedules.gateways, "load", lambda config=None: [])
    schedules.add("morning", "0 7 * * *", "ringkas repo")
    with pytest.raises(schedules.ScheduleError, match="already exists"):
        schedules.add("morning", "0 8 * * *", "other")


def test_an_unknown_gateway_is_refused(monkeypatch):
    monkeypatch.setattr(schedules, "validate_calendar", lambda _expression: None)
    monkeypatch.setattr(schedules.gateways, "load", lambda config=None: [
        schedules.gateways.Gateway(name="telegram", kind="telegram")])
    with pytest.raises(schedules.ScheduleError, match="does not exist"):
        schedules.add("morning", "0 7 * * *", "x", gateway="nope")


def test_the_service_unit_is_capped_and_runs_one_turn():
    item = schedules.Schedule(name="morning", cron="0 7 * * *", prompt="x", timeout=600.0)
    unit = schedules.render_service(item, "/etc/ompwire/config.toml", "/home/you",
                                    "/usr/local/bin/ompwire")

    assert "Type=oneshot" in unit
    assert "EnvironmentFile" not in unit  # no bot token for the agent the run starts
    assert "ExecStart=/usr/local/bin/ompwire schedule _run morning" in unit
    assert "TimeoutStartSec=600" in unit
    assert "MemoryMax=1G" in unit
    assert "KillMode=control-group" in unit


def test_the_unit_carries_a_path_so_the_agent_is_findable():
    """Found by running it: the timer fired, the process started, and the turn died with
    "ACP command not found: omp" because a user unit does not inherit the shell's PATH."""
    item = schedules.Schedule(name="morning", cron="0 7 * * *", prompt="x")
    unit = schedules.render_service(item, "/c.toml", "/home/you", "/usr/bin/ompwire",
                                    path="/home/you/.local/bin:/usr/bin:/bin")

    assert "Environment=PATH=/home/you/.local/bin:/usr/bin:/bin" in unit


def test_the_timer_catches_a_missed_run():
    item = schedules.Schedule(name="morning", cron="0 7 * * *", prompt="x")
    unit = schedules.render_timer(item)

    assert "OnCalendar=*-*-* 07:00:00 Asia/Jakarta" in unit
    assert "Persistent=true" in unit
    assert "Unit=ompwire-schedule-morning.service" in unit


def test_unit_names_are_derived_from_the_schedule_name():
    assert schedules.unit_names("morning") == (
        "ompwire-schedule-morning.service", "ompwire-schedule-morning.timer")


def test_a_marker_becomes_a_proposal_and_leaves_the_text_alone():
    text = ("siap, saya jadwalkan.\n"
            "<<schedule: 0 7 * * * | ringkas repo semalam>>\n"
            "dan ini untuk bot kerja:\n"
            "<<schedule: Mon..Fri 07:00 | gateway=telegram-work | cek deploy>>")
    clean, proposals = schedules.extract(text)

    assert "<<schedule" not in clean
    assert clean.startswith("siap, saya jadwalkan.")
    assert [p.cron for p in proposals] == ["0 7 * * *", ""]
    assert proposals[0].prompt == "ringkas repo semalam"
    assert proposals[0].expression == "*-*-* 07:00:00"
    assert proposals[1].gateway == "telegram-work"
    assert proposals[1].expression == "Mon..Fri 07:00"


def test_a_marker_without_a_prompt_is_ignored():
    _clean, proposals = schedules.extract("<<schedule: 0 7 * * *>>")
    assert proposals == []


def test_a_half_written_marker_is_hidden_while_streaming():
    assert schedules.hide_partial("ok\n<<schedule: 0 7 *", final=False) == "ok"
    assert schedules.hide_partial("ok\n<<schedule: 0 7 * * * | x>>", final=True) == "ok"


@pytest.mark.parametrize(("name", "expected"), [
    ("ask", "always-ask"),
    ("write", "write"),
    ("yolo", "yolo"),
    ("ALWAYS-ASK", "always-ask"),
])
def test_approval_names_map_to_what_the_agent_accepts(name, expected):
    assert schedules.agent_approval(name) == expected


def test_an_unknown_approval_is_refused():
    with pytest.raises(schedules.ScheduleError, match="is not one of"):
        schedules.agent_approval("deny")


def test_the_timezone_must_exist(monkeypatch):
    monkeypatch.setattr(schedules, "validate_calendar", lambda _expression: None)
    monkeypatch.setattr(schedules.gateways, "load", lambda config=None: [])
    with pytest.raises(schedules.ScheduleError, match="unknown timezone"):
        schedules.add("morning", "0 7 * * *", "x", timezone="Mars/Olympus")


def test_installing_writes_both_units(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules, "systemd_dir", lambda: tmp_path / "systemd")
    monkeypatch.setattr(schedules, "schedules_file", lambda: tmp_path / "schedules.toml")
    monkeypatch.setattr(schedules, "validate_calendar", lambda _expression: None)
    monkeypatch.setattr(schedules.gateways, "load", lambda config=None: [])
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run",
                        lambda command, **_kwargs: calls.append(list(command)) or
                        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())

    item = schedules.add("morning", "0 7 * * *", "ringkas repo")
    service_unit, timer_unit = schedules.install(item)
    schedules.enable(item.name)

    assert service_unit.exists() and timer_unit.exists()
    assert "OnCalendar=*-*-* 07:00:00 Asia/Jakarta" in timer_unit.read_text()
    assert ["systemctl", "--user", "enable", "--now", "ompwire-schedule-morning.timer"] in calls


@pytest.mark.parametrize(("cron", "words"), [
    ("0 7 * * *", "every day at 07:00"),
    ("30 6 * * 1-5", "weekdays at 06:30"),
    ("*/15 * * * *", "every 15 minutes"),
    ("0 */6 * * *", "every 6 hours"),
    ("0 9 * * 0", "every Sunday at 09:00"),
    ("0 9 1 * *", "day 1 of every month at 09:00"),
    ("0 0 * * 0,6", "weekends at 00:00"),
])
def test_a_schedule_says_itself_in_words(cron, words):
    assert schedules.describe(schedules.Schedule(name="x", cron=cron, prompt="p")) == words


def test_an_explicit_calendar_is_shown_as_written():
    item = schedules.Schedule(name="x", cron="", calendar="Mon..Fri 07:00", prompt="p")
    assert schedules.describe(item) == "Mon..Fri 07:00"


def test_hour_and_minute_ranges_expand_into_the_calendar():
    assert schedules.to_on_calendar("0 9-17 * * *") == \
        "*-*-* 09:00:00,10:00:00,11:00:00,12:00:00,13:00:00,14:00:00,15:00:00,16:00:00,17:00:00"
    assert schedules.to_on_calendar("15,45 2 * * *") == "*-*-* 02:15:00,02:45:00"


def test_a_stepped_hour_range_is_refused_with_direction_not_a_traceback():
    with pytest.raises(schedules.ScheduleError) as excinfo:
        schedules.to_on_calendar("0 9-17/2 * * *")
    assert "--on-calendar" in str(excinfo.value)


def test_a_scheduled_result_is_delivered_as_a_card(monkeypatch):
    """The delivery path only runs at the scheduled hour; a broken card should fail here first."""
    import asyncio

    from ompwire import gateways, schedule_run, tg_surface

    sent: list[tuple[int, str]] = []

    class FakeBot:
        def __init__(self, _token):
            pass

        async def send_message(self, chat_id, text, **_kw):
            sent.append((chat_id, text))

    monkeypatch.setattr(tg_surface, "Bot", FakeBot)
    gateway = gateways.Gateway(name="telegram", kind="telegram", allowed=[42])
    monkeypatch.setattr(schedule_run.gateways, "get", lambda _name: gateway)
    monkeypatch.setattr(schedule_run.gateways, "token", lambda _gw: "t")
    item = schedules.Schedule(name="pagi", cron="0 7 * * *", prompt="ringkas", gateway="telegram")

    count = asyncio.run(schedule_run._deliver(item, "**selesai**", [], [], {"ok": True}))
    assert count == 1
    chat, body = sent[0]
    assert chat == 42
    assert "pagi" in body and "<b>selesai</b>" in body



def test_a_long_scheduled_report_arrives_in_messages_telegram_accepts(monkeypatch):
    """A run's own report can outrun one message, and a card cut at a fixed number of
    characters landed inside its code block: Telegram refused the markup, so the whole report
    was lost hours after the run."""
    import asyncio
    import re

    from telegram.error import BadRequest

    from ompwire import gateways, mdrender, schedule_run, tg_surface

    half_an_entity = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+);)")

    def refuses(text: str) -> str:
        """Telegram's two answers to a message it cannot take: too long, or not parseable."""
        if mdrender.visible_length(text) > 4096:
            return "Message is too long"
        if half_an_entity.search(text):
            return "can't parse entities: unmatched '&'"
        if any(text.count(f"<{tag}") != text.count(f"</{tag}>")
               for tag in ("b", "i", "s", "pre", "code", "blockquote")):
            return "can't parse entities: unclosed tag"
        return ""

    sent: list[str] = []

    class FakeBot:
        def __init__(self, _token):
            pass

        async def send_message(self, chat_id, text, **_kw):
            problem = refuses(text)
            if problem:
                raise BadRequest(problem)
            sent.append(text)

    monkeypatch.setattr(tg_surface, "Bot", FakeBot)
    gateway = gateways.Gateway(name="telegram", kind="telegram", allowed=[42])
    monkeypatch.setattr(schedule_run.gateways, "get", lambda _name: gateway)
    monkeypatch.setattr(schedule_run.gateways, "token", lambda _gw: "t")
    item = schedules.Schedule(name="pagi", cron="0 7 * * *", prompt="ringkas", gateway="telegram")
    report = ("## ringkasan\n\n```bash\n"
              + "\n".join(f"2026-10-26 07:{i % 60:02d}:00 ERROR <nilai> & gagal" for i in range(300))
              + "\n```")

    count = asyncio.run(schedule_run._deliver(item, report, [], [], {"ok": True}))

    assert count == len(sent) > 1
    assert "<b>pagi</b>" in sent[0] and "07:00:00" in sent[0]
    assert any("07:59:00" in body and "ERROR" in body for body in sent)
    assert sent[-1].endswith("pagi</i>")  # the footer still closes the card

def test_a_gateway_kind_without_a_delivery_is_refused():
    from ompwire import delivery, gateways

    gateway = gateways.Gateway(name="x", kind="signal", allowed=[1])
    with pytest.raises(gateways.GatewayError, match="no delivery exists"):
        delivery.for_gateway(gateway)


def test_a_command_schedule_round_trips_without_a_prompt(tmp_path):
    schedules.save({"disk": schedules.Schedule(
        name="disk", cron="30 3 * * *", prompt="", command='~/bin/clean.sh --dry "run"')})
    item = schedules.load()["disk"]
    assert item.command == '~/bin/clean.sh --dry "run"'
    assert item.prompt == ""


@pytest.mark.parametrize(("prompt", "command"), [("", ""), ("ringkas", "echo x")])
def test_a_schedule_needs_exactly_one_of_prompt_and_command(monkeypatch, prompt, command):
    monkeypatch.setattr(schedules, "validate_calendar", lambda _expression: None)
    monkeypatch.setattr(schedules.gateways, "load", lambda config=None: [])
    with pytest.raises(schedules.ScheduleError, match="either a prompt"):
        schedules.add("job", "0 7 * * *", prompt=prompt, command=command)


@pytest.fixture
def command_run(monkeypatch, tmp_path):
    """A command schedule run for real (bash, the output file, the status) with the Bot API faked:
    `sent` is every message the gateway would have received."""
    from ompwire import gateways, schedule_run, tg_surface

    sent: list[str] = []

    class FakeBot:
        def __init__(self, _token):
            pass

        async def send_message(self, chat_id, text, **_kw):
            sent.append(text)

    monkeypatch.setattr(tg_surface, "Bot", FakeBot)
    gateway = gateways.Gateway(name="ops", kind="telegram", allowed=[42])
    monkeypatch.setattr(schedule_run.gateways, "get", lambda _name: gateway)
    monkeypatch.setattr(schedule_run.gateways, "token", lambda _gw: "t")
    monkeypatch.setattr(schedule_run, "default_state_dir", lambda: tmp_path / "state")

    def run(command, timeout=60.0):
        schedules.save({"job": schedules.Schedule(name="job", cron="0 7 * * *", prompt="",
                                                  command=command, gateway="ops", timeout=timeout)})
        code = schedule_run.run("job")
        return code, schedules.get("job").last_status

    run.sent = sent
    run.outputs = lambda: sorted((tmp_path / "state" / "schedules" / "job").glob("*.txt"))
    return run


def test_a_command_that_prints_nothing_sends_nothing(command_run):
    """A watcher that found no change is silent; a message every quarter hour saying so is noise."""
    code, status = command_run("true")
    assert code == 0
    assert command_run.sent == []
    assert status == "ok · nothing to report"
    assert len(command_run.outputs()) == 1, "the run is still kept on disk"


def test_what_a_command_prints_is_sent_as_written(command_run):
    code, status = command_run("printf 'stok tersedia\\n<b>2 vCPU</b>\\n'")
    assert code == 0
    assert status == "ok · 1 message(s)"
    assert "stok tersedia" in command_run.sent[0]
    assert "&lt;b&gt;2 vCPU&lt;/b&gt;" in command_run.sent[0], "output is text, not markup"


def test_a_failing_command_reports_its_exit_code_and_output(command_run):
    code, status = command_run("echo 'disk penuh' >&2; exit 3")
    assert code == 3
    assert status == "failed: exit 3"
    assert "exit 3" in command_run.sent[0] and "disk penuh" in command_run.sent[0]


def test_a_command_that_runs_past_its_timeout_is_reported(command_run):
    code, status = command_run("sleep 5", timeout=0.5)
    assert code == 1
    assert status == "failed: timed out"
    assert "timed out" in command_run.sent[0]


def test_a_command_does_not_see_the_bot_tokens(command_run, monkeypatch):
    """A scheduled script runs with the gateway's environment; printing it must not leak a token."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-a")
    monkeypatch.setenv("OMPWIRE_GATEWAY_OPS_TOKEN", "secret-b")
    command_run("env")
    assert command_run.sent
    assert "secret-a" not in command_run.sent[0] and "secret-b" not in command_run.sent[0]


def test_a_timed_out_command_leaves_nothing_running(command_run, tmp_path):
    """A script's own children outlived the shell that started them, still running after the
    timeout reported the run as over."""
    import time

    marker = tmp_path / "child-alive"
    command_run(f"(sleep 1.5; touch {marker}) & wait", timeout=0.3)
    time.sleep(2)
    assert not marker.exists(), "the command's child kept running after the timeout"


def test_the_cli_adds_lists_and_shows_a_command_schedule(monkeypatch, capsys):
    """`--command` once shadowed the subcommand the CLI dispatches on: every `ompwire schedule …`
    printed the main help and did nothing."""
    from ompwire import __main__ as cli

    monkeypatch.setattr(schedules, "validate_calendar", lambda _expression: None)
    monkeypatch.setattr(schedules.gateways, "load", lambda config=None: [])
    monkeypatch.setattr(schedules, "next_elapse", lambda _name: "")
    assert cli.main(["schedule", "add", "disk", "--cron", "30 3 * * *", "--no-start",
                     "--command", "~/bin/clean.sh"]) == 0
    assert schedules.get("disk").command == "~/bin/clean.sh"
    capsys.readouterr()
    assert cli.main(["schedule", "list"]) == 0
    assert "disk" in capsys.readouterr().out
    assert cli.main(["schedule", "show", "disk"]) == 0
    assert "~/bin/clean.sh" in capsys.readouterr().out
