# Architecture

ompwire is the transport between a chat platform and an OMP agent. Everything that decides
*what* to answer lives in the agent. This document is about *how the message gets there and
back*, and where the seams are.

```
+--------+        +---------+        +------------------+        +----------------------+
| chat   | <----> | ompwire | <----> | omp --mode acp   | <----> | tools, skills, MCP   |
| surface|        |         | stdio  | loop, memory     |        | providers, files     |
+--------+        +---------+        +------------------+        +----------------------+
                       |
                  sessions.db
```

One `omp` subprocess per conversation, one `ompwire` process per gateway. Nothing else.

## Who owns what

| Layer | Owns | When it fails |
| --- | --- | --- |
| Transport (`telegram.py`) | routing, streaming edits, approvals, rendering, per-chat prefs | a Telegram edit fails or markup is rejected; the reply is resent as plain text instead of being lost |
| `sessions.db` (`store.py`) | chat key to session id, prefs, approval override | unreadable: a new agent session, the chat key is kept |
| ACP client (`acp.py`) | stdio framing, request ids, timeouts | the subprocess dies mid-turn: respawn, then `session/load` on the same id |
| Agent (`omp`) | loop, tools, MCP, compaction, skills, memory, prompt | provider or tool error: reported to the chat as the agent's own text |
| Catalogs (`catalog.py`) | model, mode, thinking, provider, skill lists | the agent will not answer a probe: an empty list, and the command says so instead of inventing options |
| Gateways (`gateways.py`) | one bot definition: platform, allow list, credential | unreadable file: refuse to start rather than guess |
| Service (`service.py`) | the systemd unit per gateway | no `systemd --user`: `start` refuses, nothing is left unsupervised |

## Modules

| File | Lines | What it is |
| --- | --- | --- |
| `telegram.py` | ~1850 | the Telegram transport: handlers, streaming, approvals, pickers, rendering |
| `__main__.py` | ~650 | the CLI: one function per command, `argparse` wiring |
| `setup.py` | ~580 | the wizard, `.env` writing token normalisation, `bot_identity` verification |
| `update.py` | ~370 | install mode detection, update, restart of running gateways |
| `acp.py` | ~330 | the ACP client over stdio |
| `gateways.py` | ~260 | gateway definitions, credentials per gateway, selection rules |
| `config.py` | ~250 | layered config: defaults, `config.toml`, environment, redaction of dumps |
| `service.py` | ~235 | systemd unit rendering, start, stop, remove, state |
| `catalog.py` | ~230 | model, mode, thinking, provider and skill lists read from the agent |
| `cli.py` | ~225 | `ompwire chat`, the terminal surface |
| `uninstall.py` | ~225 | what to remove, and what to leave alone |
| `mdrender.py` | ~210 | markdown to Telegram HTML |
| `status.py` | ~175 | one screen of current state, no handshake |
| `mcp_hub.py` | ~150 | `servers.yaml` to `session/new` `mcpServers` |
| `login.py` | ~135 | provider login on a pty, driven from chat |
| `store.py` | ~127 | sqlite: chat key to session id |
| `format.py` | ~118 | message cards, banners, usage lines |
| `lock.py` | ~100 | one flock per gateway |
| `logs.py` | ~62 | redaction filter |
| `questions.py` | ~40 | the `<<options: a \| b>>` marker turned into buttons |
| `attachments.py` | ~150 | the `<<file: path>>` marker, its guard rails, and incoming documents |

Line counts move. If a number here is wrong, the file is right and this table is stale.

## A turn, end to end

1. Telegram delivers an update. The transport drops it unless the sender is on the gateway's
   allow list.
2. The chat key resolves to a session id in `sessions.db`, or a new ACP session is created.
3. Stored prefs (model, mode, thinking, yolo) are replayed onto the session with
   `session/set_config_option`.
4. The message becomes a `session/prompt`. Images arrive as ACP image content blocks.
5. `session/update` frames stream back: prose is batched into edits to one message, tool calls
   go to a separate trace message so the answer is never clobbered.
6. `session/request_permission` becomes an inline keyboard. The answer is an option id from
   what the agent offered, plus a persisted override so the same tool is not asked twice.
7. Every action lands in a rotating log with token-shaped strings redacted before the handler
   sees them.
8. If the agent ends with `<<options: a | b>>`, the marker is stripped and turned into buttons.
   The chosen text is sent back as an ordinary message.

## Attachments

ACP has no way for the agent to attach a file and no general attachment block going the other way,
so the transport owns both directions.

Out: the agent names a path in its reply with `<<file: /abs/path>>`, one marker may carry several
paths separated by a pipe. The marker is stripped from everything the chat sees, including the
streaming edits, and the files are uploaded once the turn ends. The guard rails are the point:
the path must resolve inside the agent's working directory (a model that read `~/.ssh` must not be
able to post it), it must exist and be a file, it must be under `files.max_mb`, and at most five
files go out per turn. An image under 10 MB travels as a photo, anything else as a document.
Whatever is refused is reported in one short line naming the file and the reason, because a marker
that vanishes silently looks like a model that forgot.

In: a document from the chat is written to `files.incoming_dir/<chat id>/` under a name reduced to
`[A-Za-z0-9._-]` and without overwriting an earlier file, then the agent is told the path, the
mime type and the size. The agent has a shell, so a readable path beats a content type the
transport would have to guess at. Images keep their own path: they go to the agent as an ACP
image content block.

The markers are documented to the agent in `PROTOCOL_HINT`, appended to the session on top of the
configured `append_system_prompt`. There is no switch and no `[files] enabled` key: a reply that
cannot carry a file is a reply this transport does not understand, so attachments are part of the
protocol rather than a preference. The hint is appended rather than stored in the config because
setup writes the config once: a marker added later would otherwise never reach an install that
upgraded.

## Storage

| Path | Holds |
| --- | --- |
| `~/.config/ompwire/config.toml` | agent settings |
| `~/.config/ompwire/gateways.toml` | gateway definitions |
| `~/.config/ompwire/.env` | one credential per gateway, mode `0600` |
| `~/.local/state/ompwire/sessions.db` | chat key to session id, per-chat prefs |
| `~/.local/state/ompwire/overrides.json` | approval overrides set from chat |
| `~/.local/state/ompwire/files/<chat id>/` | documents sent from the chat, for the agent to read |
| `~/.local/state/ompwire/serve-<gateway>.lock` | flock, one process per gateway |
| `~/.local/state/ompwire/ompwire.log` | rotating log, redacted |

`~/.omp` is the agent's own store: sessions, provider credentials, MCP config. ompwire reads
`~/.omp/agent/mcp.json` only to write MCP servers during setup, and uninstall never touches it.

## The ACP surface used

Verified against omp 18.12.6. `omp --mode acp` is not documented in `omp --help`, which is why
`tests/test_acp_contract.py` asserts these shapes: an undocumented mode can change without a
deprecation cycle.

| Method | Direction | Used for |
| --- | --- | --- |
| `initialize` | to agent | handshake, agent name, `loadSession`, capabilities |
| `session/new` | to agent | open a conversation, read `configOptions` |
| `session/load` | to agent | resume after a restart |
| `session/prompt` | to agent | one turn, returns `stopReason` and usage |
| `session/cancel` | to agent | `/stop` |
| `session/set_config_option` | to agent | model, mode, thinking |
| `session/fork` | to agent | `/fork` |
| `session/list` | to agent | `/sessions` |
| `session/close` | to agent | idle eviction |
| `_session/steering` | to agent | `/steer`, experimental extension |
| `session/update` | from agent | prose chunks, tool calls, usage, available commands |
| `session/request_permission` | from agent | approval prompts |

## Decisions and their costs

| Decision | Alternative rejected | Why | What it costs |
| --- | --- | --- | --- |
| Transport only | a chat client with its own loop and tools | OMP already owns the loop, the tools and compaction; a second copy is where frontends rot | features that need agent state must go upstream |
| ACP over stdio | calling provider HTTP APIs here | the agent authenticates itself, so no provider keys live in this repo | the ACP shape is a contract to follow |
| One subprocess per conversation | one shared agent with multiplexed sessions | a stuck turn cannot block another chat, and `cwd` is per chat | memory per live conversation |
| SQLite for routing | a database server | one row per chat, read once per message | no concurrent machines, no shared history |
| HTML `parse_mode` | MarkdownV2 | MarkdownV2 needs 18 characters escaped, HTML needs 3, and `<pre>` content is never parsed | markup is built, not pasted |
| Catalogs read from the agent | a hard-coded model list | the list drifts the day the agent ships a provider | one extra probe per session |
| One unit per gateway | one unit for all bots | stopping or crashing one bot leaves the others alone | more units to look at |
| Tags plus git install | publishing to PyPI | the transport depends on a specific agent surface, so edge installs matter | the installing machine needs git |

## Sharp edges found while building it

- **`session/load` replays the transcript.** A resumed session re-emits earlier assistant
  messages as `session/update` frames, so a frontend with its update handler still attached
  dumps every old answer into the chat. `ChatSession.open` mutes updates while loading.
- **Permissions are option ids, not booleans.** `session/request_permission` returns options
  with a `kind` (`allow_once`, `reject_once`, ...) and the reply is
  `{outcome: {outcome: "selected", optionId}}`. Always pick from what the agent offered.
- **Model selection is a config option.** There is no model field in ACP; it is the
  `configOptions` entry with `id = "model"`, set through `session/set_config_option`.
- **A second prompt into a running turn cancels it.** Measured: `session/prompt` while a turn is
  running ends that turn with `stopReason: "cancelled"` (reproduced three times), and
  `_meta.steering.queue: "followUp"` does not help. `/queue` is implemented in the transport as
  a FIFO for that reason. `_session/steering` notifications do not cancel, but on this agent
  version the steering text never appeared in the reply in any of four payload shapes, so
  `/steer` stays opt-in and labelled experimental.
- **Keep the process warm.** Each turn carries a large baseline of input tokens (the agent's own
  prompt), mostly cache-served on later turns. Spawning per message throws that away.
- **`mcpServers` needs `env` as a list.** An entry whose `env` is an object, or missing, fails
  `session/new` with `Internal error / undefined is not an object`. Dict-shaped config is
  converted automatically, and a failure falls back to a session without MCP servers rather than
  blocking the chat.
- **One poller per token.** Two processes polling the same bot token make Telegram answer
  `409 Conflict` and drop updates. The per-gateway flock and the unit model both exist because
  of this.

## Gateway lifecycle

```
ompwire gateway add NAME     -> gateways.toml + .env (getMe verified)
ompwire gateway start NAME   -> ~/.config/systemd/user/ompwire-NAME.service
                               daemon-reload, enable --now
                               ExecStart: ompwire gateway _run NAME
ompwire gateway stop NAME    -> stop, disable
ompwire gateway remove NAME  -> stop, delete unit, delete definition
```

`_run` is the foreground entry point systemd supervises. It is deliberately absent from
`ompwire help`: a gateway runs under systemd or it does not run, so there is no user-facing
foreground mode to document.

The unit is generated, never checked in: it needs this machine's `ompwire` path, `.env` path and
working directory. `Restart=always`, `RestartSec=5`, `MemoryMax=4G` per gateway. It also carries
`Environment=PATH=` built from where this install found the agent and `ompwire`, because a systemd
user unit does not inherit an interactive shell's PATH: without it the poller connects and then
every turn fails with `ACP command not found`.

Because the unit is generated, `gateway restart` rewrites it before restarting. A plain
`systemctl restart` would bring back the same process with the same stale paths, which is exactly
the bug that made the restart useless the first time it happened. `doctor` compares each unit on
disk against what this version would write and names the ones that need a restart.

## What would have to change for multi-user

This is documented so nobody mistakes a personal tool for a platform. A public bot needs, at
minimum: a per-user credential store instead of one `.env`; per-user filesystem isolation,
because `cwd` and the shell are shared today; resource limits per conversation rather than per
gateway; and a real queue so one user's turn cannot exhaust the machine. None of that is a
small patch, and the current code assumes the opposite in `store.py`, `gateways.py` and
`telegram.py`.
