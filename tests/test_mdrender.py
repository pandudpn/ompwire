"""Markdown -> Telegram HTML conversion, including the awkward streaming cases."""

from __future__ import annotations

from ompwire.mdrender import to_html, to_plain


def test_fenced_block_becomes_pre_code_with_language():
    html = to_html("```bash\nls -la\n```")
    assert html == '<pre><code class="language-bash">ls -la</code></pre>'


def test_unterminated_fence_is_closed_not_swallowed():
    # a half-streamed reply must still produce valid HTML
    html = to_html("intro\n\n```python\nprint(1)\n")
    assert html.startswith("intro")
    assert html.endswith("</code></pre>")
    assert "print(1)" in html

def test_a_fence_inside_a_list_item_keeps_its_code_unindented():
    """Models indent a fence under a bullet; the indent is markdown, not part of the code."""
    html = to_html("- step one\n  ```bash\n  ls -la\n  ```\n- step two")
    assert '<pre><code class="language-bash">ls -la</code></pre>' in html
    assert "step two" in html


def test_a_tilde_fence_is_code_not_strikethrough():
    assert to_html("~~~\nhello\n~~~") == "<pre>hello</pre>"


def test_a_longer_fence_can_hold_a_shorter_one():
    html = to_html("````md\n```bash\nls\n```\n````")
    assert html == '<pre><code class="language-md">```bash\nls\n```</code></pre>'


def test_a_fence_with_windows_line_endings_is_code():
    html = to_html("x\r\n```bash\r\nls\r\n```\r\n")
    assert '<pre><code class="language-bash">ls</code></pre>' in html


def test_a_long_answer_is_split_outside_its_code_blocks():
    """A cut inside a fence left an unclosed block in one message and bare code in the next."""
    from ompwire.mdrender import split_markdown as _chunks

    code = "\n".join(f"line {i}" for i in range(400))
    text = "Intro paragraph.\n\n```bash\n" + code + "\n```\n\nOutro."
    parts = _chunks(text, 1000)
    assert len(parts) > 1
    for part in parts:
        html = to_html(part)
        assert html.count("<pre") == html.count("</pre>")
        assert "```" not in html, part[:80]
        # every code line still reads as code: inside a <pre> of its own message
        outside = html
        for block in html.split("<pre")[1:]:
            outside = outside.replace("<pre" + block.split("</pre>", 1)[0] + "</pre>", "")
        assert "line 2" not in outside and "line 3" not in outside, part[:80]


def test_a_split_inside_a_four_backtick_block_closes_it():
    from ompwire.mdrender import split_markdown as _chunks

    code = "\n".join(f"line {i}" for i in range(400))
    for part in _chunks("````md\n" + code + "\n````", 1000):
        html = to_html(part)
        assert html.startswith("<pre") and html.endswith("</pre>"), part[-40:]
        assert "`" not in html, part[-40:]


def test_an_overlong_code_line_stays_code_in_every_message():
    from ompwire.mdrender import split_markdown as _chunks

    for part in _chunks("```text\n" + "x" * 2500 + "\n```", 1000):
        html = to_html(part)
        assert html.startswith("<pre") and html.endswith("</pre>"), part[:40]
        assert len(part) <= 1000



def test_inline_code_and_escaping():
    html = to_html("use `parse_mode=HTML` for <b>raw</b> & safe")
    assert "<code>parse_mode=HTML</code>" in html
    assert "&lt;b&gt;raw&lt;/b&gt;" in html
    assert "&amp; safe" in html


def test_headings_bold_italic_strike_links_bullets_quotes():
    html = to_html("## Judul\n**tebal** dan *miring* dan ~~coret~~\n- satu\n> kutipan\n"
                   "[repo](https://example.com/x)")
    assert "<b>Judul</b>" in html
    assert "<b>tebal</b>" in html and "<i>miring</i>" in html and "<s>coret</s>" in html
    assert "• satu" in html
    assert "<blockquote>kutipan</blockquote>" in html
    assert '<a href="https://example.com/x">repo</a>' in html


def test_plain_fallback_drops_markers():
    text = "**bold** and `code` and ```block```"
    plain = to_plain(text)
    assert "**" not in plain
    assert "`" not in plain


def test_empty_input():
    assert to_html("") == ""


def test_bar_chart_lines_become_a_pre_block():
    sample = ("Context window: 1048576 tokens (4% used)\n"
              "  System prompt    [░░░░░░░░] 1%  6292 tokens\n"
              "  Free             [████████] 66%  689874 tokens\n"
              "\nTail text")
    html = to_html(sample)
    assert "Context window" in html
    pre = html.split("<pre>")[1].split("</pre>")[0]
    assert "[░░░░░░░░] 1%" in pre and "[████████] 66%" in pre
    assert "Tail text" not in pre


def test_agent_mcp_failure_notice_is_stripped():
    from ompwire.mdrender import strip_agent_noise

    noise = ('MCP srv "honcho" failed to connect: MCP subprocess closed stdout before '
             'responding. Its tools are unavailable until the client restarts the session.')
    assert strip_agent_noise(noise * 4) == ""
    assert strip_agent_noise(f"{noise}Jawaban asli.") == "Jawaban asli."
    assert strip_agent_noise("MCP servers 'foo' failed to connect: boom\n\nThe answer is 42.") \
        == "The answer is 42."


def test_a_quote_in_a_url_cannot_break_out_of_the_href():
    from ompwire.mdrender import to_html

    html = to_html('[click](https://example.com/a"onmouseover="alert(1))')
    assert '"onmouseover' not in html


def test_strip_html_unwraps_a_rendered_tool_block():
    from ompwire.mdrender import strip_html
    from ompwire.tg_html import tool_block

    body = tool_block("execute", "", command="ls | head -1", output="a<b")
    plain = strip_html(body)
    assert "<pre>" not in plain and "<code>" not in plain
    assert "a<b" in plain  # escaped in HTML, restored by the plain-text fallback
    assert plain.splitlines()[0] == "🔧 execute"


def test_markdown_table_becomes_a_monospace_block():
    """Tables in SKILL.md files are full of pipes; a mono block keeps them readable."""
    from ompwire.mdrender import to_html

    html = to_html("| Provider | Status |\n|---|---|\n| **Z.AI** | `ok` |\n")
    assert "<pre>" in html and "</pre>" in html
    assert "|" not in html                      # the pipes are gone
    assert "<b>" not in html and "<code>Provider" not in html
    assert "Z.AI" in html and "Provider" in html
    assert html.startswith("<pre>") and not html.startswith("<pre>\n")


def test_single_pipe_line_is_left_alone():
    from ompwire.mdrender import to_html

    assert "<pre>" not in to_html("| not a table")


def test_bold_can_wrap_inline_code():
    """`**not `x` here**` used to leave the asterisks literal."""
    from ompwire.mdrender import to_html

    assert to_html("no **`MenuBarExtra`** at all") == "no <b><code>MenuBarExtra</code></b> at all"
    wrapped = to_html("**`a/b` is dead.** Do not use it.")
    assert wrapped == "<b><code>a/b</code> is dead.</b> Do not use it."


def test_code_content_is_escaped_not_parsed():
    from ompwire.mdrender import to_html

    assert to_html("a `<tag>` and & here") == "a <code>&lt;tag&gt;</code> and &amp; here"


def test_split_blocks_keeps_a_table_together():
    from ompwire.mdrender import split_blocks, to_html

    body = "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n" + ("x" * 60)
    chunks = split_blocks(body, 40)
    table_chunk = next(c for c in chunks if "| a |" in c)
    assert "| 1 | 2 |" in table_chunk            # header and body stayed together
    assert "<pre>" in to_html(table_chunk)


def test_options_marker_is_extracted_and_hidden():
    from ompwire.questions import extract, hide_partial

    text = "Which database should we use?\n\n<<options: postgres | mysql | sqlite>>"
    cleaned, options = extract(text)
    assert cleaned == "Which database should we use?"
    assert options == ["postgres", "mysql", "sqlite"]

    assert extract("no marker here") == ("no marker here", [])
    assert hide_partial("pick one\n<<options: a | b", final=False) == "pick one"
    assert hide_partial("done", final=True) == "done"


def test_options_are_deduped_and_capped():
    from ompwire.questions import extract

    many = " | ".join(f"o{i}" for i in range(12))
    _, options = extract(f"q\n<<options: {many}>>")
    assert len(options) == 6
    _, deduped = extract("q\n<<options: a | a | b>>")
    assert deduped == ["a", "b"]


def test_options_card_has_a_free_text_button():
    import asyncio
    from types import SimpleNamespace

    from ompwire.config import load_config
    from ompwire.engine import ChatAgent
    from ompwire.telegram import TelegramSurface

    sent = {}

    async def fake_send_message(chat_id, text, reply_markup=None, **kwargs):
        sent["text"], sent["kb"] = text, reply_markup
        return SimpleNamespace(chat_id=chat_id)

    async def main():
        # ChatAgent binds the loop at construction time, so build it inside one
        surface = TelegramSurface(SimpleNamespace(bot=SimpleNamespace(
            send_message=fake_send_message)), 5)
        agent = ChatAgent(load_config(env={}), "tg:5", surface)
        await agent.offer_options(["postgres", "mysql"])
        return agent

    agent = asyncio.run(main())
    labels = [button.text for row in sent["kb"].inline_keyboard for button in row]
    assert labels == ["1. postgres", "2. mysql", "✏️ Other answer", "⏭️ Skip"]
    assert sent["kb"].inline_keyboard[-1][0].callback_data == "qo:other"
    assert sent["kb"].inline_keyboard[-1][1].callback_data == "qo:skip"
    assert agent.pending_options == ["postgres", "mysql"]


def test_a_long_option_is_shown_whole_in_the_message():
    """A button label is cut to one line on the phone; the message body must carry the text."""
    import asyncio
    from types import SimpleNamespace

    from ompwire.config import load_config
    from ompwire.engine import ChatAgent
    from ompwire.telegram import TelegramSurface

    long = "Teks per segmen + 1 pesan per tool call, dengan timestamp asli untuk setiap langkah"
    sent = {}

    async def fake_send_message(chat_id, text, reply_markup=None, **kwargs):
        sent["text"] = text
        return SimpleNamespace(chat_id=chat_id)

    async def main():
        surface = TelegramSurface(SimpleNamespace(bot=SimpleNamespace(
            send_message=fake_send_message)), 5)
        agent = ChatAgent(load_config(env={}), "tg:5", surface)
        await agent.offer_options([long, "pendek"])

    asyncio.run(main())
    assert long in sent["text"]


def test_an_ask_question_is_the_heading_of_the_options_card():
    """omp's ask carries its question; without it the chat shows numbered options with no ask."""
    import asyncio
    from types import SimpleNamespace

    from ompwire.config import load_config
    from ompwire.engine import ChatAgent
    from ompwire.telegram import TelegramSurface

    sent = {}

    async def fake_send_message(chat_id, text, reply_markup=None, **kwargs):
        sent["text"], sent["kb"] = text, reply_markup
        return SimpleNamespace(chat_id=chat_id)

    async def main():
        surface = TelegramSurface(SimpleNamespace(bot=SimpleNamespace(
            send_message=fake_send_message)), 5)
        agent = ChatAgent(load_config(env={}), "tg:5", surface)
        await agent.offer_options(["teh", "kopi"], question="Minum apa?")

    asyncio.run(main())
    assert "Minum apa?" in sent["text"]
    labels = [button.text for row in sent["kb"].inline_keyboard for button in row]
    assert labels[:2] == ["1. teh", "2. kopi"]
    assert "teh" in sent["text"] and "kopi" in sent["text"]


def test_telegram_counts_the_message_not_the_markup():
    """The cap is on the text after entities are parsed: `&lt;` is one character there, and an
    emoji outside the BMP is two. Counting the markup or the characters is what got it wrong."""
    from ompwire.mdrender import visible_length

    assert visible_length("a &lt; b") == 5
    assert visible_length("<b>tebal</b>") == 5
    assert visible_length("🔧") == 2
    assert visible_length("") == 0


def test_a_rendered_card_is_cut_into_messages_telegram_accepts():
    """A plan card is rendered before the transport sees it, so nothing upstream keeps it short."""
    from ompwire.mdrender import split_html, strip_html, visible_length

    card = ("📋 <b>Todo</b> · 0/40 done\n"
            + "\n".join(f"⬜ step {i}: port the memory records and check each one" for i in range(40)))
    parts = split_html(card, 700)

    assert len(parts) > 1
    for part in parts:
        assert visible_length(part) <= 700, part
    assert strip_html("".join(parts)) == strip_html(card)  # nothing lost, nothing repeated


def test_a_cut_inside_a_pre_block_closes_and_reopens_it():
    """The second half of a tool trace must still be code, not bare lines of output."""
    from ompwire.mdrender import split_html, visible_length

    card = ("🔧 <code>bash</code>\n<pre>"
            + "\n".join(f"line {i} of output & more" for i in range(120)) + "</pre>")
    parts = split_html(card, 500)

    assert len(parts) > 1
    for part in parts:
        assert visible_length(part) <= 500, part
        assert part.count("<pre>") == part.count("</pre>") == 1, part[-60:]
    assert "line 0 of output & more" in parts[0]
    assert "line 119 of output & more" in parts[-1]


def test_a_cut_never_lands_inside_a_tag_or_an_entity():
    from ompwire.mdrender import split_html, visible_length

    card = "<b>" + "&amp;" * 400 + "</b>"
    parts = split_html(card, 64)

    assert len(parts) > 1
    for part in parts:
        assert visible_length(part) <= 64, part
        assert part.count("&") == part.count("&amp;"), part  # no half an entity
        assert part.count("<b>") == part.count("</b>") == 1, part


def test_a_markdown_answer_is_fitted_to_what_telegram_counts():
    """split_markdown counts characters; a message dense in emoji renders twice as wide."""
    from ompwire.mdrender import fit_markdown, to_html, visible_length

    text = "🔧" * 3000 + "\n\n```bash\n" + "\n".join(f"line {i}" for i in range(200)) + "\n```"
    parts = fit_markdown(text, 4000)

    assert len(parts) > 1
    for part in parts:
        assert visible_length(to_html(part)) <= 4000, part[:40]
        html = to_html(part)
        assert html.count("<pre") == html.count("</pre>")
    assert sum(part.count("🔧") for part in parts) == 3000
    assert any("line 199" in part for part in parts)


def test_a_markdown_answer_that_already_fits_is_one_message():
    from ompwire.mdrender import fit_markdown

    assert fit_markdown("jawaban singkat", 4000) == ["jawaban singkat"]
