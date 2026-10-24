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


def test_strip_html_unwraps_a_rendered_tool_block():
    from ompwire.format import tool_block
    from ompwire.mdrender import strip_html

    body = tool_block("execute", "", command="ls | head -1", output="a<b")
    plain = strip_html(body)
    assert "<pre>" not in plain and "<code>" not in plain
    assert "a<b" in plain  # escaped in HTML, restored by the plain-text fallback
    assert plain.splitlines()[0] == "🔧 execute"
