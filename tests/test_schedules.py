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
    unit = schedules.render_service(item, "/etc/ompwire/config.toml", "/etc/ompwire/.env",
                                    "/home/you", "/usr/local/bin/ompwire")

    assert "Type=oneshot" in unit
    assert "ExecStart=/usr/local/bin/ompwire schedule _run morning" in unit
    assert "TimeoutStartSec=600" in unit
    assert "MemoryMax=1G" in unit
    assert "KillMode=control-group" in unit


def test_the_unit_carries_a_path_so_the_agent_is_findable():
    """Found by running it: the timer fired, the process started, and the turn died with
    "ACP command not found: omp" because a user unit does not inherit the shell's PATH."""
    item = schedules.Schedule(name="morning", cron="0 7 * * *", prompt="x")
    unit = schedules.render_service(item, "/c.toml", "/e.env", "/home/you", "/usr/bin/ompwire",
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

    from ompwire import gateways, schedule_run, telegram

    sent: list[tuple[int, str]] = []

    class FakeBot:
        def __init__(self, _token):
            pass

        async def send_message(self, chat_id, text, **_kw):
            sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "Bot", FakeBot)
    gateway = gateways.Gateway(name="telegram", kind="telegram", allowed=[42])
    monkeypatch.setattr(schedule_run.gateways, "get", lambda _name: gateway)
    monkeypatch.setattr(schedule_run.gateways, "token", lambda _gw: "t")
    item = schedules.Schedule(name="pagi", cron="0 7 * * *", prompt="ringkas", gateway="telegram")

    count = asyncio.run(schedule_run._deliver(item, "**selesai**", [], [], {"ok": True}))
    assert count == 1
    chat, body = sent[0]
    assert chat == 42
    assert "pagi" in body and "<b>selesai</b>" in body


def test_a_gateway_kind_without_a_delivery_is_refused():
    from ompwire import delivery, gateways

    gateway = gateways.Gateway(name="wa", kind="whatsapp", allowed=[1])
    with pytest.raises(gateways.GatewayError, match="no delivery exists"):
        delivery.for_gateway(gateway)
