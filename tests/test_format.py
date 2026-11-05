"""The pieces that turn agent output into something readable in a chat window."""

from __future__ import annotations

from ompwire.format import uptime
from ompwire.tg_html import (
    TOOL_LINE_WIDTH,
    card,
    change,
    esc,
    grid,
    help_sections,
    tool_block,
)


class TestEscaping:
    def test_angle_brackets_cannot_break_the_html(self):
        assert esc("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;"

    def test_quotes_are_left_alone(self):
        # Telegram only needs <, > and & escaped; quote=False keeps commands readable
        assert esc('grep "x"') == 'grep "x"'

    def test_ampersand_is_escaped(self):
        assert esc("a && b") == "a &amp;&amp; b"


class TestUptime:
    def test_seconds(self):
        assert uptime(8) == "8s"

    def test_minutes_pad_the_seconds(self):
        assert uptime(252) == "4m 12s"

    def test_hours_pad_the_minutes(self):
        assert uptime(7500) == "2h 05m"

    def test_days_pad_the_hours(self):
        assert uptime(3 * 86400 + 4 * 3600) == "3d 04h"

    def test_negative_is_zero_not_a_crash(self):
        assert uptime(-5) == "0s"


class TestGrid:
    def test_keys_are_padded_to_one_column(self):
        block = grid([("cwd", "/srv"), ("model", "zai/glm")])
        assert block == "<pre>cwd    /srv\nmodel  zai/glm</pre>"

    def test_empty_values_are_dropped(self):
        assert grid([("a", ""), ("b", None)]) == ""

    def test_no_rows_is_an_empty_string(self):
        assert grid([]) == ""


class TestCard:
    def test_title_subtitle_grid_and_footer(self):
        body = card("Gateways", "2 running", [("pid", 4242)], "click to stop")
        assert body.startswith("<b>Gateways</b> · <i>2 running</i>")
        assert "pid" in body and "4242" in body
        assert body.endswith("<i>click to stop</i>")

    def test_a_card_without_parts_is_just_the_title(self):
        assert card("Only") == "<b>Only</b>"

    def test_html_in_a_value_is_escaped(self):
        assert "&lt;script&gt;" in card("t", rows=[("k", "<script>")])


class TestChange:
    def test_old_value_is_struck_through_and_new_one_is_code(self):
        line = change("model", "zai/glm-4", "zai/glm-5")
        assert "<s>zai/glm-4</s>" in line and "<code>zai/glm-5</code>" in line
        assert "\n" not in line

    def test_a_note_becomes_the_second_line(self):
        assert change("mode", "plan", "default", "takes effect next turn").endswith(
            "<i>takes effect next turn</i>")


class TestToolBlock:
    def test_command_and_output_share_one_fence(self):
        body = tool_block("bash", "run tests", command="pytest -q", output="3 passed")
        assert body.count("<pre>") == 1 and body.count("</pre>") == 1
        assert "$ pytest -q" in body and "3 passed" in body

    def test_long_output_keeps_the_first_lines_and_counts_the_rest(self):
        output = "\n".join(f"line {index}" for index in range(12))
        body = tool_block("bash", "many", output=output)
        assert "line 0" in body and "line 7" in body
        assert "line 8" not in body
        assert "… +4 more line(s)" in body

    def test_a_long_line_is_clipped_with_an_ellipsis(self):
        body = tool_block("bash", "wide", output="x" * (TOOL_LINE_WIDTH + 20))
        assert "…" in body
        assert "x" * (TOOL_LINE_WIDTH + 20) not in body

    def test_a_heredoc_keeps_its_shape(self):
        command = "cat <<'EOF' > x\none\ntwo\nEOF"
        body = tool_block("bash", "write", command=command)
        # inside the fence the text is still HTML-escaped: Telegram parses the message
        # as HTML even within <pre>, so arrows have to arrive as entities
        assert "$ cat &lt;&lt;'EOF' &gt; x" in body
        assert "  one" in body and "  two" in body

    def test_only_the_first_six_command_lines_are_kept(self):
        command = "\n".join(f"step{index}" for index in range(9))
        body = tool_block("bash", "long", command=command)
        assert "step5" in body and "step6" not in body
        assert "  …" in body

    def test_running_and_failing_statuses_are_labelled(self):
        assert "running" in tool_block("bash", "x", status="in_progress")
        assert "failed" in tool_block("bash", "x", status="failed")

    def test_completed_is_not_noise(self):
        assert "completed" not in tool_block("bash", "x", status="completed")

    def test_a_card_with_no_body_still_keeps_a_fenced_block(self):
        body = tool_block("bash", "", status="bg_1 · running · 3s")
        assert "<pre>bg_1 · running · 3s</pre>" in body
        assert tool_block("bash", "").endswith("<pre>working…</pre>")


class TestHelpSections:
    def test_groups_commands_under_their_heading(self):
        body = help_sections([("Chat", [("new", "start over"), ("stop", "cancel")])])
        assert "<b>Chat</b>" in body
        assert "/new · <i>start over</i>" in body
        assert body.endswith("/stop · <i>cancel</i>")

    def test_no_trailing_blank_lines(self):
        assert not help_sections([("A", [("x", "y")])]).endswith("\n")


class TestShortAction:
    def test_a_cut_never_leaves_a_quote_open(self):
        from ompwire.engine import _short_action

        assert _short_action('sleep 90; echo "selesai $(date +%H:%M:%S)"') == "sleep 90; echo…"

    def test_a_short_command_is_left_alone(self):
        from ompwire.engine import _short_action

        assert _short_action("pytest -q") == "pytest -q"


class TestNetworkRetry:
    """A Telegram read timeout is a blip on the wire, not a reason to lose the turn."""

    def test_a_timed_out_send_is_retried(self):
        import asyncio

        from telegram.error import TimedOut

        from ompwire.tg_surface import send_html

        tries = []

        class Bot:
            async def send_message(self, chat_id, text, **_kw):
                tries.append(text)
                if len(tries) < 3:
                    raise TimedOut()
                return "sent"

        assert asyncio.run(send_html(Bot(), 1, "<b>x</b>")) == "sent"
        assert len(tries) == 3

    def test_a_dead_connection_gives_up_after_three_tries(self):
        import asyncio

        import pytest
        from telegram.error import TimedOut

        from ompwire.tg_surface import send_html

        tries = []

        class Bot:
            async def send_message(self, chat_id, text, **_kw):
                tries.append(text)
                raise TimedOut()

        with pytest.raises(TimedOut):
            asyncio.run(send_html(Bot(), 1, "<b>x</b>"))
        assert len(tries) == 3

    def test_a_rate_limit_is_not_retried(self):
        import asyncio

        import pytest
        from telegram.error import RetryAfter

        from ompwire.tg_surface import send_html

        tries = []

        class Bot:
            async def send_message(self, chat_id, text, **_kw):
                tries.append(text)
                raise RetryAfter(7)

        with pytest.raises(RetryAfter):
            asyncio.run(send_html(Bot(), 1, "<b>x</b>"))
        assert len(tries) == 1  # its window is the caller's to wait out


class TestOptionLabels:
    """A button label is one line on a phone: a cut option must not look like a whole one."""

    def test_a_short_option_keeps_its_words(self):
        from ompwire.tg_surface import _option_label

        assert _option_label(0, "ya") == "1. ya"

    def test_a_long_option_shows_its_number_only(self):
        from ompwire.tg_surface import _option_label

        long = "Hapus semua gateway lalu jalankan ulang dari konfigurasi default yang baru"
        assert _option_label(2, long) == "3."
