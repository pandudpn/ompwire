"""SOUL.md: drafting, the approve-before-write wizard, and how it reaches the system prompt."""

from __future__ import annotations

import copy

from ompwire import soul
from ompwire.config import DEFAULTS, PROTOCOL_HINT, Config, system_prompt


def _config(tmp_path, persona: str = "") -> Config:
    config = Config(data=copy.deepcopy(DEFAULTS))
    config.data["session"]["store"] = str(tmp_path / "sessions.db")
    config.data["agent"]["persona"] = persona
    return config


def _answers(*values: str):
    queue = list(values)
    return lambda prompt="": queue.pop(0)


def test_a_whole_file_fence_is_removed_but_inner_fences_stay():
    wrapped = "```markdown\n# Soul\nUse `ls`:\n```bash\nls\n```\n```"
    assert soul.strip_fences(wrapped) == "# Soul\nUse `ls`:\n```bash\nls\n```"
    assert soul.strip_fences("  plain text \n") == "plain text"


def test_nothing_is_written_until_the_draft_is_accepted(tmp_path):
    drafts: list[str] = []

    def draft(description: str, model: str) -> str:
        drafts.append(description)
        assert not (tmp_path / "SOUL.md").exists()  # shown first, written after the answer
        return f"You are terse. (draft {len(drafts)})"

    path = soul.wizard(tmp_path, "zai/glm", ask=_answers("terse", "y"), draft_with=draft)
    assert path.read_text() == "You are terse. (draft 1)\n"


def test_redraft_carries_the_correction_into_the_next_draft(tmp_path):
    seen: list[str] = []

    def draft(description: str, model: str) -> str:
        seen.append(description)
        return f"draft {len(seen)}"

    soul.wizard(tmp_path, ask=_answers("blunt", "r", "less formal", "y"), draft_with=draft)
    assert seen[0] == "blunt"
    assert "less formal" in seen[1]
    assert (tmp_path / "SOUL.md").read_text() == "draft 2\n"


def test_declining_or_a_failed_draft_leaves_a_template_with_no_persona(tmp_path):
    soul.wizard(tmp_path, ask=_answers("blunt", "n"), draft_with=lambda d, m: "You are blunt.")
    assert system_prompt(_config(tmp_path), None).startswith("You are answering inside")

    def broken(description: str, model: str) -> str:
        raise RuntimeError("timeout")

    other = tmp_path / "other"
    soul.wizard(other, ask=_answers("blunt"), draft_with=broken)
    assert (other / "SOUL.md").is_file()


def test_an_existing_soul_is_kept_unless_replacing_is_confirmed(tmp_path):
    (tmp_path / "SOUL.md").write_text("mine\n")
    soul.wizard(tmp_path, ask=_answers("n"), draft_with=lambda d, m: "new")
    assert (tmp_path / "SOUL.md").read_text() == "mine\n"


def test_no_terminal_skips_the_question_instead_of_crashing(tmp_path):
    def closed(prompt: str = "") -> str:
        raise EOFError

    soul.wizard(tmp_path, ask=closed, draft_with=lambda d, m: "unused")
    assert (tmp_path / "SOUL.md").is_file()


def test_prompt_order_is_persona_then_format_then_protocol(tmp_path):
    (tmp_path / "SOUL.md").write_text("<!-- note -->\nYou are Vidura.\n")
    prompt = system_prompt(_config(tmp_path, persona="config persona"), None)
    assert prompt.startswith("You are Vidura.")  # SOUL.md beats the config persona
    assert "note" not in prompt
    assert prompt.index("fenced code") < prompt.index(PROTOCOL_HINT)
    assert "config persona" not in prompt


def test_config_persona_is_used_when_there_is_no_soul(tmp_path):
    prompt = system_prompt(_config(tmp_path, persona="You are calm."), None)
    assert prompt.startswith("You are calm.")


def test_a_config_from_before_the_split_keeps_its_prose_as_the_format_hint(tmp_path):
    from ompwire.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[agent]\nappend_system_prompt = "my old rules"\n', encoding="utf-8")
    config = load_config(path, env={})
    assert config.get("agent", "chat_format_hint") == "my old rules"
    assert any("chat_format_hint" in warning for warning in config.warnings)
