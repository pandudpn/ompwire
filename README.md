# ompwire

Chat harness that drives an [OMP](https://omp.sh) agent over the **Agent Client Protocol** instead of shipping its own agent loop. Telegram is the target front-end; the CLI in this repo is the same harness without the chat transport, so the agent path can be tested without a bot token.

## Why this shape

OMP already owns the expensive half: agent loop, tools, compaction, subagents, MCP sessions, permission prompts, usage accounting. What a chat front-end needs is a transport shell around `omp --mode acp`. Verified against **omp 18.12.6**:

- `initialize` → `agentInfo.name = "omp"`, `loadSession: true`, session `list/fork/resume/close`, MCP `http/sse`, prompt `image`
- `session/new` → `sessionId` + `configOptions` (`model`, `mode`)
- `session/prompt` → streams `session/update` (`agent_message_chunk`, `tool_call`, `usage_update`, `available_commands_update`), returns `{stopReason, usage}`
- `session/load` → a **new** process resumes an existing conversation; the harness stores only `chat_key → sessionId`, never the transcript. It also **replays the transcript** as `session/update` frames, so a resumed chat must mute updates while loading or it dumps every prior answer into the room (implemented in `cli.ChatSession.open`).
- `session/request_permission` (server → client) → reply `{outcome: {outcome, optionId}}`

`--mode acp` is not advertised in `omp --help`. That makes `tests/test_acp_contract.py` load-bearing: it asserts the wire shapes above against the real binary and fails on upstream drift. Pin the agent version you tested against.

## Install

```bash
cd ~/ompwire
uv venv && source .venv/bin/activate
uv pip install -e .
cp config.toml.example ~/.config/ompwire/config.toml
cp .env.example .env && chmod 600 .env    # fill TELEGRAM_BOT_TOKEN later
ompwire doctor
```

## Use

```bash
ompwire chat                       # interactive, resumes the session for this cwd
ompwire chat -q "explain this repo" # one-shot
ompwire chat --new                 # handled in-session with /new
ompwire sessions                   # recorded chat_key -> sessionId rows
ompwire config                     # resolved config, secrets redacted
```

In-session: `/new`, `/model <id>`, `/mode <default|plan>`, `/exit`.

## Layout

```
src/ompwire/acp.py       JSON-RPC/stdio ACP client (no dependencies)
src/ompwire/cli.py       turn loop, streaming printer, permission prompts
src/ompwire/memory.py    optional Honcho prefetch preamble
src/ompwire/store.py     sqlite chat_key -> sessionId
src/ompwire/config.py    TOML + env merge, defaults, redaction
tests/                   live contract tests against `omp --mode acp`
```

## Config format

TOML, not YAML: Python 3.11+ reads it with stdlib `tomllib` (no PyYAML dependency), it cannot express the indentation traps that bite YAML, and secrets live in `.env` rather than in the config file — so the config stays safe to commit as `config.toml.example`.

## Not built yet

- Telegram transport (`telegram.py`) — needs its own bot token; sharing the Hermes bot token would make two pollers fight over the same updates
- cron/webhook triggers
- multi-platform adapters

## Honest limits

- One ACP subprocess per conversation: fine for personal use, ~44k baseline input tokens per turn (OMP's system prompt), so keep the process warm instead of spawning per message.
- Honcho prefetch is one HTTP call with defensive parsing; it is not yet exercised against a live key.
- OMP memory (`mnemopi`/`hindsight`) and Honcho can both be active and will drift apart. Pick one owner for long-term memory.
