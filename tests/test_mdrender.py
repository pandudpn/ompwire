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
