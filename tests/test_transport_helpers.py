"""Offline tests for the picker grouping: behaviour that only shows up against the real agent,
pinned here without needing a bot token or an agent process.
"""

from __future__ import annotations

from ompwire.config import load_config
from ompwire.telegram import Transport


class FakeAgent:
    def __init__(self, options):
        self.options = options


def test_providers_group_by_slash_prefix():
    agent = FakeAgent({
        "model": {
            "currentValue": "zai/glm-4.6",
            "options": [
                {"value": "zai/glm-4.6"},
                {"value": "zai/glm-5.1"},
                {"value": "commandcode/claude-fable-5"},
                {"value": "bare-model"},
            ],
        }
    })
    transport = Transport(load_config(env={}))
    providers = transport._providers(agent, "model")
    assert [name for name, _ in providers] == ["commandcode", "other", "zai"]
    counts = dict(providers)
    assert len(counts["zai"]) == 2
    assert len(counts["other"]) == 1
    assert transport._current(agent, "model") == "zai/glm-4.6"
    assert transport._values(agent, "model")[0] == "zai/glm-4.6"


def test_single_provider_stays_on_one_list():
    agent = FakeAgent({"mode": {"currentValue": "default", "options": [{"value": "default"}, {"value": "plan"}]}})
    transport = Transport(load_config(env={}))
    assert [name for name, _ in transport._providers(agent, "mode")] == ["other"]


def test_page_slice_clamps_and_pages():
    values = [str(i) for i in range(20)]
    window, page, pages = Transport._page_slice(values, 0, 8)
    assert (window[0], len(window), page, pages) == ("0", 8, 0, 3)
    window, page, pages = Transport._page_slice(values, 2, 8)
    assert (window[0], len(window), page, pages) == ("16", 4, 2, 3)
    # out of range clamps instead of raising
    window, page, pages = Transport._page_slice(values, 99, 8)
    assert (page, pages) == (2, 3)
    assert Transport._page_slice([], 0, 8) == ([], 0, 1)


def test_pick_indices_map_to_the_full_option_list():
    """A page inside a provider must select the global option, not a local one."""
    agent = FakeAgent({
        "model": {
            "currentValue": "zai/a",
            "options": [{"value": "commandcode/x"}, {"value": "zai/a"}, {"value": "zai/b"}],
        }
    })
    transport = Transport(load_config(env={}))
    providers = dict(transport._providers(agent, "model"))
    values = transport._values(agent, "model")
    zai_indices = providers["zai"]
    assert [values[i] for i in zai_indices] == ["zai/a", "zai/b"]
    assert zai_indices == [1, 2]


def test_uptime_is_compact():
    from ompwire.format import uptime

    assert uptime(8) == "8s"
    assert uptime(72) == "1m 12s"
    assert uptime(3725) == "1h 02m"
    assert uptime(90_000) == "1d 01h"


def test_grid_pads_labels_and_escapes_values():
    from ompwire.tg_html import grid

    block = grid([("cwd", "/home/you"), ("model", "a<b")])
    assert block.startswith("<pre>") and block.endswith("</pre>")
    assert "cwd    /home/you" in block
    assert "a&lt;b" in block


def test_card_and_change_render_html():
    from ompwire.tg_html import card, change

    rendered = card("🟢 ompwire", "0.1.0", [("agent", "omp 18.12.6")], "footer")
    assert "<b>🟢 ompwire</b>" in rendered
    assert "<pre>" in rendered and "footer" in rendered
    line = change("model", "old/model", "new/model", "note")
    assert "<s>old/model</s>" in line and "<code>new/model</code>" in line
    assert "note" in line


def test_cards_never_put_html_inside_pre():
    """<pre> is not parsed, so tags there would leak as literal text."""
    import re

    from ompwire.config import load_config
    from ompwire.telegram import Transport

    transport = Transport(load_config(env={}))
    for body in (transport.approval_card("ask"), transport.approval_card("allow", "saved")):
        for block in re.findall(r"<pre>(.*?)</pre>", body, re.DOTALL):
            assert "<b>" not in block and "<i>" not in block
        assert "ask" in body


def test_approval_keyboard_marks_current_policy():
    from ompwire.config import load_config
    from ompwire.telegram import Transport

    transport = Transport(load_config(env={}))
    rows = [[button.text for button in row] for row in transport.approval_keyboard("deny").inline_keyboard]
    assert rows == [["ask"], ["allow"], ["• deny"]]


def test_model_picker_lays_out_two_columns_and_strips_prefix():
    import asyncio
    from types import SimpleNamespace

    from ompwire.config import load_config
    from ompwire.telegram import Transport

    captured = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            captured.append((text, reply_markup))
            return SimpleNamespace(edit_text=lambda *a, **k: None)

    transport = Transport(load_config(env={}))
    transport.app = SimpleNamespace(bot=FakeBot())
    agent = SimpleNamespace(
        options={"model": {"currentValue": "zai/a", "options": [
            {"value": f"zai/m{i}"} for i in range(18)]}},
        session_id="01a16c67aa", cwd="/tmp",
    )
    asyncio.run(transport._show_config(agent, 1, "model", screen="l", provider=0, page=0))
    text, keyboard = captured[-1]
    rows = [[button.text for button in row] for row in keyboard.inline_keyboard]
    # ten buttons over five rows, two per row, provider prefix stripped
    assert len(rows) == 6  # 5 model rows + nav
    assert rows[0] == ["m0", "m1"]
    assert rows[4] == ["m8", "m9"]
    assert "page 1/2" in text


def test_mode_picker_stays_single_column():
    import asyncio
    from types import SimpleNamespace

    from ompwire.config import load_config
    from ompwire.telegram import Transport

    captured = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            captured.append((text, reply_markup))
            return SimpleNamespace(edit_text=lambda *a, **k: None)

    transport = Transport(load_config(env={}))
    transport.app = SimpleNamespace(bot=FakeBot())
    agent = SimpleNamespace(
        options={"mode": {"currentValue": "default",
                          "options": [{"value": "default"}, {"value": "plan"}]}},
        session_id="01a16c67aa", cwd="/tmp",
    )
    asyncio.run(transport._show_config(agent, 1, "mode"))
    _, keyboard = captured[-1]
    assert [[b.text for b in row] for row in keyboard.inline_keyboard] == [["• default"], ["plan"]]


def test_tool_block_fences_command_and_output_together():
    from ompwire.tg_html import tool_block

    block = tool_block("execute", "", command="ls -1 | head -2",
                       output="a\nb", status="completed")
    assert block.startswith("🔧 <code>execute</code>")
    assert block.count("<pre>") == 1  # one fenced block, not inline code plus output
    body = block.split("<pre>")[1].split("</pre>")[0]
    assert body.splitlines()[0] == "$ ls -1 | head -2"
    assert body.splitlines()[1:] == ["a", "b"]


def test_tool_block_keeps_multiline_commands_readable():
    from ompwire.tg_html import tool_block

    command = "python3 - <<'PY'\nprint(1)\nPY"
    body = tool_block("execute", "", command=command).split("<pre>")[1].split("</pre>")[0]
    assert body.splitlines()[0] == "$ python3 - &lt;&lt;'PY'"  # escaped for HTML
    assert body.splitlines()[1] == "  print(1)"


def test_tool_block_escapes_html_in_output():
    from ompwire.tg_html import tool_block

    body = tool_block("execute", "", command="echo '<b>'", output="<b>x</b>")
    assert "&lt;b&gt;" in body


def _client_args(prose=None, mode="default"):
    """The omp command line a chat's agent starts with, built without a running loop."""
    from ompwire.config import load_config
    from ompwire.engine import ChatAgent

    config = load_config(env={})
    if prose is not None:
        config.data.setdefault("agent", {})["append_system_prompt"] = prose
    agent = ChatAgent.__new__(ChatAgent)
    agent.config = config
    agent.cwd = "/tmp"
    agent.yolo = False
    agent.options = {"mode": {"currentValue": mode}}
    agent._on_update = None
    agent._on_dialog = None
    return ChatAgent._build_client(agent).args


def test_chat_formatting_hint_is_forwarded_to_the_agent():
    """Without the hint the model writes commands inline; omp needs to be told."""
    args = _client_args()
    assert args[:2] == ["--mode", "rpc-ui"]
    hint = args[args.index("--append-system-prompt") + 1]
    assert "fenced code" in hint


def test_the_marker_protocol_is_always_described():
    """There is no switch: a reply that cannot carry a file is a reply this transport does not
    understand, and an upgraded install keeps its old config prose, so the markers cannot live
    there."""
    args = _client_args(prose="")
    hint = args[args.index("--append-system-prompt") + 1]
    assert "<<file:" in hint and "<<options:" in hint


def test_plan_mode_starts_the_agent_asking_before_every_tool():
    """Plan mode only reads: every call must come through the approval hook to be refused."""
    assert "--approval-mode=always-ask" in _client_args(mode="plan")
    assert "--approval-mode=always-ask" not in _client_args()


def test_the_subagent_is_named_in_the_tool_block():
    """omp runs subagents through the `task` tool; ACP has no event of its own for them."""
    from ompwire.tg_html import tool_block

    block = tool_block("other", "Reading note.txt via subagent", "", "", "in_progress",
                       "ReadNote (sonic)")
    assert "subagent: ReadNote (sonic)" in block


def test_reasoning_is_off_until_asked():
    from ompwire.config import load_config
    from ompwire.engine import ChatAgent

    config = load_config(env={})
    agent = ChatAgent.__new__(ChatAgent)
    agent.config = config
    agent.reasoning = bool(config.get("stream", "show_reasoning", False))
    assert agent.reasoning is False
