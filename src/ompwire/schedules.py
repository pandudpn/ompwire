"""Scheduled turns: one cron entry, one systemd timer, one process that exits when it is done.

A schedule is a row in `schedules.toml`. Installing it writes two user units: a timer that fires on
the calendar and a `oneshot` service that runs exactly one turn. Between runs nothing of ompwire is
running; the cost of a schedule is a timer entry and about a kilobyte of text.

Delivery is a property of the schedule, not a decision made at run time: an unattended turn that
picks its own destination is a mistake nobody is watching. `gateway` names which bot gets the
result, and the runner talks to the Bot API directly, so a result still arrives when that gateway's
poller is down.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import gateways

DEFAULT_TIMEZONE = "Asia/Jakarta"  # the clock the bot already shows

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CRON_TOKEN_RE = re.compile(r"^(\*|\*/\d+|\d+(-\d+)?(/\d+)?)$")
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
FIELD_NAMES = ("minute", "hour", "day of month", "month", "day of week")


class ScheduleError(ValueError):
    """Something a user can fix, said plainly."""


@dataclass
class Schedule:
    name: str
    cron: str
    prompt: str
    calendar: str = ""
    gateway: str = "telegram"
    ephemeral: bool = True   # a run leaves no session behind unless asked
    # What an unattended run may do without a human. Measured against omp, not assumed:
    #   ask   - reads only; a write is refused, the shell is refused
    #   write - reads and file writes; a shell command is refused
    #   yolo  - everything, nothing is asked
    # Default yolo, because a scheduled job that cannot run git or kubectl is a job that does
    # nothing useful. The prompt is stored with the schedule, so what it may do is what the prompt
    # says; `--approval ask` or `write` is there for a run that only reports.
    approval: str = "yolo"
    timeout: float = 1800.0  # seconds, also the unit's TimeoutStartSec
    timezone: str = DEFAULT_TIMEZONE  # "0 7 * * *" means 07:00 here, not on the server's clock
    last_status: str = ""    # what the last run did, for /schedule to show

    @property
    def on_calendar(self) -> str:
        expression = self.calendar or to_on_calendar(self.cron)
        return f"{expression} {self.timezone}" if self.timezone else expression


def schedules_file() -> Path:
    from .setup import _paths

    return _paths()[0] / "schedules.toml"


def validate_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    if not NAME_RE.match(cleaned):
        raise ScheduleError(
            "a name is 1-32 characters: lowercase letters, digits, dash or underscore")
    return cleaned


def parse(expr: str) -> str:
    """Cron with five fields to a systemd OnCalendar expression.

    Refuses instead of guessing, and the result is checked against systemd's own parser before it
    is written anywhere, so a schedule that installs is a schedule systemd understands.
    """
    cleaned = " ".join((expr or "").split())
    parts = cleaned.split(" ")
    if len(parts) != 5:
        raise ScheduleError(
            "expected five fields (minute hour day month weekday); for systemd syntax like "
            "'Mon..Fri 07:00' pass --on-calendar instead")
    minute, hour, dom, month, dow = parts
    for value, label in zip(parts, FIELD_NAMES, strict=True):
        for token in value.split(","):
            if not CRON_TOKEN_RE.match(token):
                raise ScheduleError(
                    f"{label} '{value}' is not supported; use a number, list, range, "
                    f"or */step, or pass --on-calendar")

    time_spec = _time_spec(minute, hour)
    date_spec = _date_spec(dom, month)
    weekday = _weekday_spec(dow)
    return " ".join(part for part in (weekday, date_spec, time_spec) if part)


def _expand(value: str) -> list[int]:
    """A cron field to sorted numbers: `9-17`, `1,5`, or a plain number. Steps raise ValueError."""
    out: set[int] = set()
    for token in value.split(","):
        if "/" in token:
            raise ValueError(token)
        if "-" in token:
            low, high = (int(part) for part in token.split("-", 1))
            out.update(range(low, high + 1))
        else:
            out.add(int(token))
    return sorted(out)


def _expand_field(value: str) -> list[int]:
    try:
        return _expand(value)
    except ValueError:
        raise ScheduleError(
            f"'{value}' with a step is not supported; use --on-calendar") from None


def _time_spec(minute: str, hour: str) -> str:
    if hour.startswith("*/"):
        # the step belongs in the hour field: 00/2 is every two hours, not every two minutes
        if minute.startswith("*/"):
            raise ScheduleError("two steps in one entry cannot be expressed; use --on-calendar")
        exacts = [0] if minute == "*" else _expand_field(minute)
        return ",".join(f"00/{int(hour[2:]):02d}:{part:02d}:00" for part in exacts)
    if hour == "*":
        if minute.startswith("*/"):
            return f"*:00/{int(minute[2:]):02d}:00"
        if minute == "*":
            return "*:*:00"
        return f"*:{int(minute):02d}:00"
    if minute.startswith("*/"):
        raise ScheduleError("a minute step needs every hour (minute */N hour *)")
    hours = ",".join(f"{part:02d}" for part in _expand_field(hour))
    if minute == "*":
        return f"{hours}:*:00"
    if "," in hours:
        return ",".join(f"{h}:{m:02d}:00" for h in hours.split(",") for m in _expand_field(minute))
    return ",".join(f"{hours}:{m:02d}:00" for m in _expand_field(minute))


def _date_spec(dom: str, month: str) -> str:
    if dom.startswith("*/") or month.startswith("*/") or "-" in dom or "-" in month:
        raise ScheduleError(
            "day and month ranges or steps are not supported; use --on-calendar for those")
    day = f"{int(dom):02d}" if dom != "*" else "*"
    mon = f"{int(month):02d}" if month != "*" else "*"
    return f"*-{mon}-{day}"


def _weekday_spec(dow: str) -> str:
    if dow == "*":
        return ""
    names: list[str] = []
    for token in dow.split(","):
        if "-" in token:
            start, end = (int(part) for part in token.split("-", 1))
            low, high = _weekday_number(start), _weekday_number(end)
            if high < low:
                raise ScheduleError(f"weekday range '{token}' ends before it starts")
            names.append(f"{WEEKDAYS[low]}..{WEEKDAYS[high]}")
        else:
            names.append(WEEKDAYS[_weekday_number(int(token))])
    return ",".join(names)


def _weekday_number(value: int) -> int:
    if value == 7:  # cron accepts 7 for Sunday as well as 0
        return 0
    if not 0 <= value <= 6:
        raise ScheduleError(f"day of week {value} is outside 0-7")
    return value


def to_on_calendar(expr: str) -> str:
    return parse(expr)


def validate_calendar(expression: str) -> None:
    """Ask systemd whether it agrees. The real parser beats a second implementation of it."""
    if shutil.which("systemd-analyze") is None:
        return
    result = subprocess.run(["systemd-analyze", "calendar", expression],
                            capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ScheduleError(f"systemd rejects '{expression}': {detail[0] if detail else 'invalid'}")


def load() -> dict[str, Schedule]:
    path = schedules_file()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ScheduleError(f"{path} is not valid TOML ({exc})") from exc
    items: dict[str, Schedule] = {}
    for name, row in (data.get("schedule") or {}).items():
        if not isinstance(row, dict):
            continue
        items[validate_name(name)] = Schedule(
            name=validate_name(name),
            cron=str(row.get("cron", "")),
            calendar=str(row.get("calendar", "")),
            prompt=str(row.get("prompt", "")),
            gateway=str(row.get("gateway", "telegram")),
            ephemeral=bool(row.get("ephemeral", True)),
            approval=str(row.get("approval", "yolo")),
            timeout=float(row.get("timeout", 1800.0)),
            timezone=str(row.get("timezone", DEFAULT_TIMEZONE)),
            last_status=str(row.get("last_status", "")),
        )
    return items


def render(items: dict[str, Schedule]) -> str:
    lines = ["# ompwire schedules — written by `ompwire schedule add`", ""]
    for name in sorted(items):
        item = items[name]
        lines.append(f"[schedule.{name}]")
        if item.calendar:
            lines.append(f"calendar = {_toml(item.calendar)}")
        else:
            lines.append(f"cron = {_toml(item.cron)}")
        lines.append(f"prompt = {_toml(item.prompt)}")
        lines.append(f"gateway = {_toml(item.gateway)}")
        if item.timezone:
            lines.append(f"timezone = {_toml(item.timezone)}")
        lines.append(f"ephemeral = {str(item.ephemeral).lower()}")
        lines.append(f"timeout = {item.timeout:g}")
        lines.append(f"approval = {_toml(item.approval)}")
        if item.last_status:
            lines.append(f"last_status = {_toml(item.last_status)}")
        lines.append("")
    return "\n".join(lines)


def _toml(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def save(items: dict[str, Schedule], path: Path | None = None) -> Path:
    target = path or schedules_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(items), encoding="utf-8")
    return target


APPROVAL_MODES = {"ask": "always-ask", "write": "write", "yolo": "yolo"}


def agent_approval(value: str) -> str:
    """Map the three names a person uses to the four omp accepts."""
    key = (value or "").strip().lower()
    if key in APPROVAL_MODES:
        return APPROVAL_MODES[key]
    if key in APPROVAL_MODES.values():
        return key
    raise ScheduleError(f"approval '{value}' is not one of: {', '.join(APPROVAL_MODES)}")


def add(name: str, cron: str, prompt: str, gateway: str = "telegram", ephemeral: bool = True,
        approval: str = "yolo", timeout: float = 1800.0, calendar: str = "",
        timezone: str = DEFAULT_TIMEZONE) -> Schedule:
    cleaned = validate_name(name)
    if not prompt.strip():
        raise ScheduleError("a schedule needs a prompt")
    known = {item.name for item in gateways.load()}
    if known and gateway not in known:
        raise ScheduleError(
            f"gateway '{gateway}' does not exist; known gateways: {', '.join(sorted(known))}")
    if timezone:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(timezone)
        except Exception as exc:
            raise ScheduleError(f"unknown timezone '{timezone}' ({exc})") from exc
    expression = calendar.strip() or to_on_calendar(cron)
    validate_calendar(f"{expression} {timezone}".strip())
    agent_approval(approval)
    items = load()
    if cleaned in items:
        raise ScheduleError(f"schedule '{cleaned}' already exists; remove it first")
    item = Schedule(name=cleaned, cron=cron, prompt=prompt.strip(), gateway=gateway,
                    ephemeral=ephemeral, approval=approval, timeout=timeout,
                    calendar=calendar.strip(), timezone=timezone)
    items[cleaned] = item
    save(items)
    return item


def remove(name: str) -> Schedule:
    items = load()
    if name not in items:
        raise ScheduleError(f"no schedule named '{name}'")
    item = items.pop(name)
    save(items)
    return item


def get(name: str) -> Schedule:
    items = load()
    if name not in items:
        raise ScheduleError(f"no schedule named '{name}'")
    return items[name]


def mark(name: str, status: str) -> None:
    """Record what the last run did, so /schedule can show it without reading the journal."""
    items = load()
    if name in items:
        items[name].last_status = status[:200]
        save(items)


def unit_names(name: str) -> tuple[str, str]:
    return f"ompwire-schedule-{name}.service", f"ompwire-schedule-{name}.timer"


def render_service(item: Schedule, config_path: str, workdir: str,
                   command: str, path: str = "") -> str:
    """One thing to run, capped so a runaway turn cannot hurt the rest of the machine."""
    return "\n".join([
        "[Unit]",
        f"Description=ompwire schedule {item.name}",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={command} schedule _run {item.name}",
        f"WorkingDirectory={workdir}",
        # no EnvironmentFile: the run reads its gateway's token from that gateway's own folder
        # when it delivers, and the agent it starts must not inherit any bot token
        f"Environment=OMPWIRE_CONFIG={config_path}",
        # The agent is not on systemd's default path, so the unit carries the one this install
        # used. Without it the timer fires, the process starts, and the turn fails with
        # "ACP command not found" hours later, in a journal nobody is reading.
        f"Environment=PATH={path or _service_path()}",
        # no human is watching: a tool that needs approval is denied, not waited on
        f"TimeoutStartSec={int(item.timeout)}",
        "TimeoutStopSec=30",
        "KillMode=control-group",
        "MemoryMax=1G",
        "CPUQuota=200%",
        "Nice=10",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def render_timer(item: Schedule) -> str:
    return "\n".join([
        "[Unit]",
        f"Description=ompwire schedule {item.name} ({describe(item)})",
        "",
        "[Timer]",
        f"OnCalendar={item.on_calendar}",
        # a machine that was off at the appointed hour still does the work once
        "Persistent=true",
        "AccuracySec=1min",
        "Unit=" + unit_names(item.name)[0],
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])


def systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _service_path() -> str:
    from . import service

    return service.service_path(service.agent_command())


def install(item: Schedule) -> tuple[Path, Path]:
    """Write both units, then let systemd take it from there."""
    from . import service

    directory = systemd_dir()
    directory.mkdir(parents=True, exist_ok=True)
    from .config import DEFAULT_CONFIG_PATHS

    config_path = ""
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.expanduser().is_file():
            config_path = str(candidate.expanduser())
            break
    service_name, timer_name = unit_names(item.name)
    service_unit = directory / service_name
    timer_unit = directory / timer_name
    service_unit.write_text(
        render_service(item, config_path, service._workdir(),
                       service.command_path(), service.service_path(service.agent_command())),
        encoding="utf-8")
    timer_unit.write_text(render_timer(item), encoding="utf-8")
    return service_unit, timer_unit


def uninstall(name: str) -> list[Path]:
    import subprocess

    service_name, timer_name = unit_names(name)
    subprocess.run(["systemctl", "--user", "disable", "--now", timer_name],
                   capture_output=True, text=True, check=False, timeout=60)
    removed: list[Path] = []
    for unit in (timer_name, service_name):
        path = systemd_dir() / unit
        if path.exists():
            path.unlink()
            removed.append(path)
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True, check=False, timeout=60)
    return removed


def enable(name: str) -> None:
    import subprocess

    _service, timer = unit_names(name)
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True, check=False, timeout=60)
    result = subprocess.run(["systemctl", "--user", "enable", "--now", timer],
                            capture_output=True, text=True, check=False, timeout=60)
    if result.returncode != 0:
        raise ScheduleError(f"systemd refused the timer: {(result.stderr or '').strip()}")


def next_elapse(name: str) -> str:
    """When systemd says it will fire next, so /schedule can be trusted without the journal."""
    import subprocess

    _service, timer = unit_names(name)
    result = subprocess.run(
        ["systemctl", "--user", "show", timer, "-p", "NextElapseUSecRealtime", "--value"],
        capture_output=True, text=True, check=False, timeout=30)
    return (result.stdout or "").strip()


# ---------------------------------------------------------------- the marker
#
# The agent cannot call this file, so it proposes: `<<schedule: 0 7 * * * | prompt>>`, with an
# optional `gateway=name` part. The transport never creates anything from the marker alone. A
# schedule is a promise to run an agent later, and a model that misreads "every morning" as every
# minute would be discovered when the machine fills up, not when it happens. One button fixes that.

# own line only: explaining the marker in prose must not propose a timer
MARKER_RE = re.compile(r"^[ \t]*<<\s*schedule\s*:\s*(?P<body>[^>\n]+?)\s*>>",
                       re.IGNORECASE | re.MULTILINE)
PARTIAL_RE = re.compile(r"^[ \t]*<<\s*schedule\s*:(?:(?!>>).)*\Z",
                        re.IGNORECASE | re.DOTALL | re.MULTILINE)


@dataclass
class Proposal:
    """What the agent asked for, before anyone agreed to it."""

    cron: str = ""
    calendar: str = ""
    prompt: str = ""
    gateway: str = ""

    @property
    def expression(self) -> str:
        return self.calendar or to_on_calendar(self.cron)


def extract(text: str) -> tuple[str, list[Proposal]]:
    proposals: list[Proposal] = []
    for match in MARKER_RE.finditer(text):
        parts = [part.strip() for part in match.group("body").split("|")]
        if len(parts) < 2:
            continue
        first, rest = parts[0], parts[1:]
        gateway = ""
        kept: list[str] = []
        for part in rest:
            lowered = part.lower()
            if lowered.startswith("gateway="):
                gateway = part.split("=", 1)[1].strip()
            else:
                kept.append(part)
        prompt = " | ".join(kept).strip()
        if not prompt:
            continue
        if re.match(r"^[\d*/,\- ]+$", first) and len(first.split()) == 5:
            proposals.append(Proposal(cron=first, prompt=prompt, gateway=gateway))
        else:
            proposals.append(Proposal(calendar=first, prompt=prompt, gateway=gateway))
    return MARKER_RE.sub("", text).strip(), proposals


def hide_partial(text: str, final: bool) -> str:
    if final:
        return MARKER_RE.sub("", text).strip()
    return PARTIAL_RE.sub("", MARKER_RE.sub("", text)).rstrip()


DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def describe(item: Schedule) -> str:
    """The schedule in words, because `0 7 * * *` is a puzzle at seven in the morning."""
    if item.calendar:
        return item.calendar
    parts = " ".join((item.cron or "").split()).split(" ")
    if len(parts) != 5:
        return item.cron or ""
    minute, hour, dom, month, dow = parts
    clock = f"{int(hour):02d}:{int(minute):02d}" if hour.isdigit() and minute.isdigit() else ""
    if minute.startswith("*/"):
        return f"every {int(minute[2:])} minutes"
    if hour.startswith("*/"):
        base = f"every {int(hour[2:])} hours"
        return base + (f" at :{int(minute):02d}" if minute.isdigit() and int(minute) else "")
    if not clock:
        return item.cron
    if dow == "1-5" and dom == "*" and month == "*":
        return f"weekdays at {clock}"
    if dow == "0,6" and dom == "*" and month == "*":
        return f"weekends at {clock}"
    if dow.isdigit() and dom == "*" and month == "*":
        return f"every {DAYS[_weekday_number(int(dow))]} at {clock}"
    if dom.isdigit() and month == "*" and dow == "*":
        return f"day {int(dom)} of every month at {clock}"
    if month.isdigit() and dom == "*" and dow == "*":
        return f"every day in month {int(month)} at {clock}"
    if dom == "*" and month == "*" and dow == "*":
        return f"every day at {clock}"
    return f"{item.cron} ({clock} {item.timezone})"
