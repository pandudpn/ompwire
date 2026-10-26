"""The commands a chat drives its session with: the menu, /busy, /sessions and the omp
features behind /stats, /name, /export, /handoff and /btw."""

from __future__ import annotations

import asyncio
import types

from telegram.error import TelegramError

from ompwire import telegram
from ompwire.config import load_config
from ompwire.rpc import AgentError


class Bot:
    """A stand-in for the Bot API: everything sent, in order, as (text, markup)."""

    def __init__(self):
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, _chat_id, text, reply_markup=None, **_kw):
        self.sent.append((text, reply_markup))
        return types.SimpleNamespace(edit_text=self._record, reply_text=self._record,
                                     set_reaction=self._noop)

    async def _record(self, body, **_kw):
        self.sent.append((body, None))

    async def _noop(self, *_a, **_k):
        return None

    async def send_chat_action(self, *_a, **_k):
        return None

    def bodies(self) -> list[str]:
        return [body for body, _markup in self.sent]


def _config(tmp_path):
    config = load_config(env={})
    config.data["session"]["store"] = str(tmp_path / "s.db")
    config.data["gateway"] = {"name": "telegram", "allowed_users": [7]}
    return config


def _transport(config):
    transport = telegram.Transport(config)
    transport.app = types.SimpleNamespace(bot=Bot())
    return transport


class Query:
    """A button press; its answer and card edits are kept apart from the bot's transcript."""

    def __init__(self, data, bot):
        self.data = data
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.message = types.SimpleNamespace(reply_text=self._reply, edit_text=self._edit)

    async def answer(self, text="", **_kw):
        self.answers.append(text)

    async def edit_message_text(self, body, **_kw):
        self.edits.append(body)

    async def _reply(self, *_a, **_k):
        return None

    async def _edit(self, body, **_kw):
        self.edits.append(body)


def _update(transport, args=None, query=None):
    """An update for chat 7; every reply lands in the fake bot's transcript."""
    bot = transport.app.bot

    async def reply_text(body, reply_markup=None, **_kw):
        bot.sent.append((body, reply_markup))

    message = types.SimpleNamespace(text=" ".join(args or []), reply_text=reply_text,
                                    edit_text=reply_text)
    update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=7),
                                   effective_chat=types.SimpleNamespace(id=7, type="private"),
                                   effective_message=message, callback_query=query)
    return update, types.SimpleNamespace(args=args or [])


def _buttons(markup):
    return [(button.text, button.callback_data)
            for row in (markup.inline_keyboard if markup else []) for button in row]


def _by_data(markup):
    return {data: label for label, data in _buttons(markup)}


# ----------------------------------------------------------------------- the menu
def test_the_menu_carries_the_new_commands_and_no_duplicates():
    names = [name for name, _ in telegram.COMMANDS]
    assert len(names) == len(set(names))  # Telegram rejects a duplicate command name
    assert {"busy", "stats", "name", "export", "handoff", "btw"} <= set(names)
    assert not {"steer", "queue", "resume", "continue"} & set(names)
    assert all(description for description in dict(telegram.COMMANDS).values())

def test_every_command_in_the_menu_has_its_own_handler(tmp_path):
    """/reasoning was listed but its handler was dropped: the agent got it and nothing answered."""
    from telegram.ext import Application, CommandHandler

    transport = telegram.Transport(_config(tmp_path))
    app = Application.builder().token("1:x").build()
    transport.register(app)
    handled = {name for group in app.handlers.values() for handler in group
               if isinstance(handler, CommandHandler) for name in handler.commands}
    assert {name for name, _ in telegram.COMMANDS} <= handled



# ----------------------------------------------------------------------- /busy
def test_busy_without_an_argument_offers_the_three_modes(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        update, ctx = _update(transport)
        await transport.cmd_busy(update, ctx)
        return _buttons(transport.app.bot.sent[-1][1]), agent.busy_mode

    buttons, mode = asyncio.run(go())
    assert [data for _label, data in buttons] == ["busy:steer", "busy:followup", "busy:queue"]
    assert mode == "steer"


def test_busy_with_an_argument_stores_the_chat_preference(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        update, ctx = _update(transport, ["followup"])
        await transport.cmd_busy(update, ctx)
        return agent.busy_mode, agent.prefs().get("busy"), transport.app.bot.bodies()

    mode, stored, bodies = asyncio.run(go())
    assert mode == "followup"
    assert stored == "followup"
    assert bodies and "followup" in bodies[0]


def test_a_busy_button_changes_the_mode_and_marks_it(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        query = Query("busy:queue", transport.app.bot)
        await transport.on_callback(_update(transport, query=query)[0], None)
        update, ctx = _update(transport)
        await transport.cmd_busy(update, ctx)  # the card that comes back marks the mode
        return agent.busy_mode, query, _by_data(transport.app.bot.sent[-1][1])

    mode, query, marks = asyncio.run(go())
    assert mode == "queue"
    assert query.answers == ["busy: queue"]
    assert query.edits and "queue" in query.edits[0]
    assert marks["busy:queue"].startswith("• ")
    assert not marks["busy:steer"].startswith("• ")


def test_busy_rejects_a_mode_it_does_not_know(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        update, ctx = _update(transport, ["sometimes"])
        await transport.cmd_busy(update, ctx)
        return agent.busy_mode, transport.app.bot.bodies()

    mode, bodies = asyncio.run(go())
    assert mode == "steer"  # the old mode stands
    assert bodies and "/busy" in bodies[0]


def test_setting_the_mode_it_already_has_answers_anyway(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        update, ctx = _update(transport, ["steer"])  # steer is the default
        await transport.cmd_busy(update, ctx)
        return agent.busy_mode, agent.prefs().get("busy"), transport.app.bot.bodies()

    mode, stored, bodies = asyncio.run(go())
    assert mode == "steer"
    assert stored is None  # nothing was written for a no-op
    assert bodies and "already" in bodies[0]  # and the chat is not left without an answer


# -------------------------------------------------------------------- /sessions
def _listed(agent, sessions):
    async def ensure():
        return None

    async def list_sessions():
        return sessions

    agent.ensure = ensure
    agent.list_sessions = list_sessions


def test_sessions_offers_a_resume_button_per_session(tmp_path):
    config = _config(tmp_path)
    sessions = [
        {"sessionId": "aaa11111", "updatedAt": "2026-10-26T04:00:00Z", "_meta": {"size": 12}},
        {"sessionId": "bbb22222", "updatedAt": "2026-10-26T05:30:00Z", "_meta": {"size": 34}},
        {"sessionId": "ccc33333", "updatedAt": "2026-10-26T06:00:00Z", "_meta": {"size": 56}},
    ]

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.session_id = "bbb22222"
        _listed(agent, sessions)
        update, _ctx = _update(transport)
        await transport.cmd_sessions(update, None)
        return _by_data(transport.app.bot.sent[-1][1])

    labels = asyncio.run(go())
    assert list(labels) == ["ses:aaa11111", "ses:bbb22222", "ses:ccc33333"]  # the full id travels
    assert labels["ses:aaa11111"].startswith("aaa11111 · 2026-10-26T04:00")  # id, when, size
    assert "12B" in labels["ses:aaa11111"]
    assert labels["ses:bbb22222"].startswith("• ")  # this chat's own session is marked
    assert not labels["ses:aaa11111"].startswith("• ")


def test_a_session_button_resumes_that_session(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        queued: list[tuple[str, object]] = []
        transport.enqueue = lambda _chat, kind, payload: queued.append((kind, payload)) or 1
        query = Query("ses:aaa11111-full-id", transport.app.bot)
        await transport.on_callback(_update(transport, query=query)[0], None)
        return queued, query.answers

    queued, answers = asyncio.run(go())
    assert [kind for kind, _payload in queued] == ["resume"]
    _message, session_id = queued[0][1]
    assert session_id == "aaa11111-full-id"
    assert answers == ["resuming…"]


def test_sessions_and_resume_wait_for_a_running_turn(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.busy = True
        _listed(agent, [{"sessionId": "aaa11111", "updatedAt": "x", "_meta": {"size": 1}}])
        queued: list[tuple[str, object]] = []
        transport.enqueue = lambda _chat, kind, payload: queued.append((kind, payload)) or 1
        update, _ctx = _update(transport)
        await transport.cmd_sessions(update, None)
        query = Query("ses:aaa11111", transport.app.bot)
        await transport.on_callback(_update(transport, query=query)[0], None)
        return queued, transport.app.bot.bodies()

    queued, bodies = asyncio.run(go())
    assert queued == []
    assert bodies and "still running" in bodies[0]


# ---------------------------------------------------------------------- /stats
def test_stats_reports_the_counters_omp_reports(tmp_path):
    config = _config(tmp_path)
    stats = {"sessionId": "s", "userMessages": 3, "assistantMessages": 4, "toolCalls": 7,
             "totalMessages": 7, "premiumRequests": 2, "cost": 0.25,
             "tokens": {"input": 1200, "output": 800, "cacheRead": 300, "cacheWrite": 40,
                        "total": 2040}}

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.session_id = "abcdef12"

        async def session_stats():
            return stats

        agent.session_stats = session_stats
        update, _ctx = _update(transport)
        await transport.cmd_stats(update, None)
        return transport.app.bot.bodies()

    body = asyncio.run(go())[0]
    assert "abcdef12" in body
    assert "1.2k" in body and "800" in body  # tokens in and out, as a person reads them
    assert "7 calls" in body
    assert "$0.2500" in body
    assert "2 requests" in body


def test_stats_says_what_went_wrong_instead_of_raising(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)

        async def session_stats():
            raise AgentError("agent is not running")

        agent.session_stats = session_stats
        update, _ctx = _update(transport)
        await transport.cmd_stats(update, None)
        return transport.app.bot.bodies()

    bodies = asyncio.run(go())
    assert bodies and "agent is not running" in bodies[0]


# ----------------------------------------------------------------------- /name
def test_name_renames_the_session(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.session_id = "abcdef12"
        renamed: list[str] = []

        async def rename(name):
            renamed.append(name)

        agent.rename = rename
        update, ctx = _update(transport, ["deploy", "the", "hotfix"])
        await transport.cmd_name(update, ctx)
        return renamed, transport.app.bot.bodies()

    renamed, bodies = asyncio.run(go())
    assert renamed == ["deploy the hotfix"]
    assert bodies and "deploy the hotfix" in bodies[0]


def test_name_without_a_name_only_says_how_to_use_it(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        renamed: list[str] = []

        async def rename(name):
            renamed.append(name)

        agent.rename = rename
        update, ctx = _update(transport)
        await transport.cmd_name(update, ctx)
        return renamed, transport.app.bot.bodies()

    renamed, bodies = asyncio.run(go())
    assert renamed == []
    assert bodies and "/name" in bodies[0]


# --------------------------------------------------------------------- /export
def test_export_sends_the_html_file_as_a_document(tmp_path):
    config = _config(tmp_path)
    target = tmp_path / "session.html"
    target.write_text("<html>done</html>", encoding="utf-8")

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        sent: list[tuple[object, bool]] = []

        async def export_html():
            return target

        async def send_file(path, as_photo):
            sent.append((path, as_photo))

        agent.export_html = export_html
        agent.surface.send_file = send_file
        update, _ctx = _update(transport)
        await transport.cmd_export(update, None)
        return sent, transport.app.bot.bodies()

    sent, bodies = asyncio.run(go())
    assert sent == [(target, False)]  # a document, not a photo
    assert bodies == []


def test_export_says_what_went_wrong_instead_of_raising(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)

        async def export_html():
            raise AgentError("export refused")

        agent.export_html = export_html
        update, _ctx = _update(transport)
        await transport.cmd_export(update, None)
        return transport.app.bot.bodies()

    bodies = asyncio.run(go())
    assert bodies and "export refused" in bodies[0]


def test_export_that_cannot_be_sent_still_says_where_the_file_is(tmp_path):
    config = _config(tmp_path)
    target = tmp_path / "session.html"
    target.write_text("<html>done</html>", encoding="utf-8")

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)

        async def export_html():
            return target

        async def send_file(path, as_photo):
            raise TelegramError("file is too big")

        agent.export_html = export_html
        agent.surface.send_file = send_file
        update, _ctx = _update(transport)
        await transport.cmd_export(update, None)
        return transport.app.bot.bodies()

    bodies = asyncio.run(go())
    assert bodies and str(target) in bodies[0]
    assert "file is too big" in bodies[0]



# -------------------------------------------------------------------- /handoff
def test_handoff_passes_the_instructions_and_reports_the_session(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.session_id = "abcdef12"
        calls: list[str] = []

        async def handoff(instructions=""):
            calls.append(instructions)
            return {"savedPath": "/tmp/handoff.md"}

        agent.handoff = handoff
        update, ctx = _update(transport, ["keep", "the", "plan"])
        await transport.cmd_handoff(update, ctx)
        return calls, transport.app.bot.bodies()

    calls, bodies = asyncio.run(go())
    assert calls == ["keep the plan"]
    assert bodies and "abcdef12" in bodies[0]  # the session card names the session
    assert "/tmp/handoff.md" in bodies[0]


def test_handoff_waits_for_a_running_turn(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.busy = True
        calls: list[str] = []

        async def handoff(instructions=""):
            calls.append(instructions)
            return {}

        agent.handoff = handoff
        update, ctx = _update(transport, ["go"])
        await transport.cmd_handoff(update, ctx)
        return calls, transport.app.bot.bodies()

    calls, bodies = asyncio.run(go())
    assert calls == []
    assert bodies and "still running" in bodies[0]


# ------------------------------------------------------------------------ /btw
def test_btw_answers_while_a_turn_is_running(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        agent.busy = True
        asked: list[str] = []

        async def btw(question):
            asked.append(question)
            return "Di Jakarta, 31 derajat."

        agent.btw = btw
        update, ctx = _update(transport, ["cuaca", "jakarta?"])
        await transport.cmd_btw(update, ctx)
        return asked, transport.app.bot.bodies()

    asked, bodies = asyncio.run(go())
    assert asked == ["cuaca jakarta?"]
    assert "Di Jakarta, 31 derajat." in bodies[0]
    assert "cuaca jakarta?" in bodies[0]  # the question is quoted above the answer


def test_btw_without_a_question_only_says_how_to_use_it(tmp_path):
    config = _config(tmp_path)

    async def go():
        transport = _transport(config)
        agent = transport.agent_for(7)
        asked: list[str] = []

        async def btw(question):
            asked.append(question)
            return "?"

        agent.btw = btw
        update, ctx = _update(transport)
        await transport.cmd_btw(update, ctx)
        return asked, transport.app.bot.bodies()

    asked, bodies = asyncio.run(go())
    assert asked == []
    assert bodies and "/btw" in bodies[0]
