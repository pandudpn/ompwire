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
| Engine (`engine.py`) | the turn itself: ACP session lifecycle, update drain, pacing, permissions, schedules, files. Speaks `Surface`, never a platform | a turn fails: recorded, the next gateway start resumes it |
| Transport (`telegram.py`) | Telegram routing and buttons: handlers, `TelegramSurface` (posting, cards, approvals), `TelegramDelivery`, pickers, per-chat prefs, the `tg:` key prefix | markup is rejected (resent as plain text) or a send fails (logged as a WARNING, the turn goes on) |
| `sessions.db` (`store.py`) | chat key to session id, prefs, approval override, turns a restart left unfinished (read per key prefix) | unreadable: a new agent session, the chat key is kept |
| ACP client (`acp.py`) | stdio framing, request ids, timeouts | the subprocess dies mid-turn: respawn, then `session/load` on the same id |
| Agent (`omp`) | loop, tools, MCP, compaction, skills, memory, prompt | provider or tool error: reported to the chat as the agent's own text |
| Delivery (`delivery.py`) | how a scheduled run's report reaches a person: one `Delivery` per gateway kind, resolved from the gateway stored on the schedule | no implementation for that kind: the report fails, and the schedule status carries the reason |
| Catalogs (`catalog.py`) | model, mode, thinking, provider, skill lists | the agent will not answer a probe: an empty list, and the command says so instead of inventing options |
| Gateways (`gateways.py`) | one bot definition: platform, allow list, credential | unreadable file: refuse to start rather than guess |
| Service (`service.py`) | the systemd unit per gateway | no `systemd --user`: `start` refuses, nothing is left unsupervised |

## Modules

| File | What it is |
| --- | --- |
| `engine.py` | the platform-neutral agent engine: ChatAgent, the turn renderer, the `Surface` contract, `ToolTrace`, `FloodWait`, resume planning |
| `telegram.py` | the Telegram transport: handlers, pickers, `TelegramSurface`, `TelegramDelivery`, buttons, banners |
| `__main__.py` | the CLI: one function per command, `argparse` wiring |
| `setup.py` | the wizard, `.env` writing token normalisation, `bot_identity` verification |
| `update.py` | install mode detection, update, restart of running gateways |
| `acp.py` | the ACP client over stdio |
| `gateways.py` | gateway definitions, credentials per gateway, selection rules |
| `config.py` | layered config: defaults, `config.toml`, environment, redaction of dumps |
| `service.py` | systemd unit rendering, start, stop, remove, state |
| `catalog.py` | model, mode, thinking, provider and skill lists read from the agent |
| `cli.py` | `ompwire chat`: `TerminalSurface` and the REPL, both on the engine. A terminal has no rate limit, so its surface sets `streams` and prints the answer as it is written (markers held back until they close); Telegram posts one message per segment |
| `uninstall.py` | what to remove, and what to leave alone |
| `mdrender.py` | markdown to Telegram HTML |
| `status.py` | one screen of current state, no handshake |
| `mcp_hub.py` | `servers.yaml` to `session/new` `mcpServers` |
| `login.py` | provider login on a pty, driven from chat |
| `store.py` | sqlite: chat key to session id, per-chat prefs, unfinished turns per transport prefix |
| `format.py` | neutral helpers: `uptime`, WIB clock, token counts (`58.6k`), the tool-trace clip |
| `omp_sessions.py` | session totals and compactions, read incrementally from omp's transcript JSONL |
| `omp_config.py` | omp's `compaction:` block in `config.yml`, edited in place |
| `tg_html.py` | Telegram HTML: cards, grids, tool blocks, help and run cards |
| `lock.py` | one flock per gateway |
| `logs.py` | redaction filter |
| `allowlist.py` | which shell commands are read-only and skip the approval button |
| `questions.py` | the `<<options: a \| b>>` marker; held messages and Skip live in `telegram.py` |
| `attachments.py` | the `<<file: path>>` marker, its guard rails, and incoming documents |
| `schedules.py` | cron to `OnCalendar`, the units, and the `<<schedule: ...>>` proposal |
| `schedule_run.py` | one unattended turn: build, run, deliver, record, exit |
| `delivery.py` | the `Delivery` a scheduled run needs, one implementation per gateway kind |


## A turn, end to end

1. Telegram delivers an update. The transport drops it unless the sender is on the gateway's
   allow list. A message sent while a turn runs is steered into it or queued, with a reply
   saying which; one sent while a question is open is held until the question is settled.
2. The chat key resolves to a session id in `sessions.db`, or a new ACP session is created.
3. Stored prefs (model, mode, thinking, yolo) are replayed onto the session with
   `session/set_config_option`.
4. The message becomes a `session/prompt`. Images arrive as ACP image content blocks.
5. `session/update` frames stream back. Nothing is edited after it is sent: the prose written
   before each tool call is posted as a message when the tool call starts, each tool call is
   posted as its own message when it finishes (with its output), and whatever is left goes out
   when the turn ends. Every message's timestamp is when that step happened. Telegram allows
   about one message a second per chat, so one sender posts the outbox in order at least
   `stream.min_gap` apart; tool traces that finish while it waits (the gap, or a flood window)
   go out together as one message, and a trace dropped after flood control is named in the
   chat ("… N tool call(s) not shown") instead of disappearing.
6. `session/request_permission` becomes an inline keyboard. The answer is an option id from
   what the agent offered, plus a persisted override so the same tool is not asked twice.
7. Every action lands in a rotating log with token-shaped strings redacted before the handler
   sees them.
8. If the agent ends with `<<options: a | b>>`, the marker is stripped and turned into buttons,
   plus ✏️ Other answer and ⏭️ Skip. While the question is open, other typed messages are held
   (`⏸️ held #n`) and run after it is answered or skipped; after Other answer, the next message
   is the answer. The buttons are removed once the question is settled, so a late tap sends
   nothing.

## Attachments

ACP has no way for the agent to attach a file and no general attachment block going the other way,
so the transport owns both directions.

Out: the agent names a path in its reply with `<<file: /abs/path>>`, one marker may carry several
paths separated by a pipe. The marker is stripped from everything the chat sees, and the files
are uploaded once the turn ends. The guard rails are the point:
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

## Scheduled turns

One schedule is one row in `schedules.toml`, one systemd timer and one `oneshot` service. systemd
owns the timing, so there is no scheduler process to keep alive and no idle memory cost: a timer is
an entry in PID 1's timer wheel, not a waiting process. The process exists only while the turn runs.

The runner (`schedule_run.py`) is where the unattended defaults live, and they all point one way:
nobody is watching.

- `--approval-mode=always-ask` with no permission handler installed, so every request is answered
  `cancelled` at once. omp has no `deny` mode; `cancelled` is the same outcome without a wait.
  `--yolo` per schedule is the deliberate exception.
- Ephemeral sessions (`--no-session`): a scheduled turn is not a conversation, and persisting it
  would grow the session store forever for nobody's benefit.
- `MemoryMax=1G`, `CPUQuota=200%`, `Nice=10`, `TimeoutStartSec` from the schedule: a run that
  misbehaves is contained by its own cgroup and killed there.
- `Environment=PATH=` on the unit. A user unit does not inherit the shell's PATH, and without this
  the timer fires, the process starts, and the turn dies hours later in a journal nobody reads.

The run report is a card rather than a line of text: `tg_html.run_card` takes the schedule in words
(`schedules.describe`), the outcome as a badge, a grid of what the run cost (model, thinking,
duration, tools, tokens) and the answer converted by the same markdown renderer the chat uses. The
first version printed a `schedule · name` line above raw text, which read like a log tail; a run
that happens while nobody is watching has to be worth reading in the morning. Failures use the same
card with the reason, because a scheduled run that dies silently is worse than one that reports.

Delivery is per gateway kind: `schedule_run` resolves the gateway stored on the schedule, asks
`delivery.for_gateway` for that kind's `Delivery`, and hands over neutral fields (title, the
schedule in words, outcome, rows, answer markdown, error, footer, files, skipped files).
`TelegramDelivery` is the only implementation; it sends straight to the Bot API with the
gateway's token, so a result still arrives when that gateway's poller is stopped. Only the token
has to be valid. The destination (the gateway's first allowed user) comes from the schedule
rather than being chosen at run time: an unattended turn that picks its own destination is a
mistake nobody is watching for.

The marker is a proposal. `<<schedule: 0 7 * * * | prompt>>` reaches the chat as a confirmation
card, because a model that misreads "every morning" as every minute would otherwise be discovered
when the machine fills up. The name, the gateway and the calendar are all validated before a button
appears, and `systemd-analyze calendar` checks the expression, so an installed schedule is one
systemd already agreed to run.

## Who stops the agent

`session/request_permission` is the only place a human can intervene, and omp only sends it in
`write` and `yolo` (which sends none, since nothing is asked). Under `always-ask` and
`always-ask-cached` a write is denied inside omp, with no request and therefore no button: the
transport cannot ask for something it is never told about. `write` is the working gate.

A denial is the safe default: the request expires after `[approval] timeout`, the slot reports
`cancelled`, and omp fails that one tool call instead of the turn. That is why an approval left
unanswered at 04:00 cannot silently approve itself.

Not every shell command reaches a person. `allowlist.py` names the commands that cannot change
the machine (`ls`, `rg`, `git log`, `gh pr view`, ...) and answers those itself, because a button
on every `git status` trains the person to tap Allow without reading. The command is tokenised
rather than pattern-matched: a pipeline or chain passes only when every part is read-only, a
redirect only into `/dev/null`, and per-command flags that make a reader write (`git --output`,
`sort -o`, `rg --pre`, `journalctl --vacuum-*`) send it back to the button.

What this gate does not cover is a file the agent writes with its own write tool, which `write` mode
allows by design. The appended prompt is what keeps that honest, and `yolo` removes both.

## What the stream carries

`session/update` notifications arrive with these `sessionUpdate` values, and each one has a
decision behind it:

| Kind | What the transport does |
| --- | --- |
| `agent_message_chunk` | the answer, buffered and posted as a new message at the next tool call or the end of the turn |
| `agent_thought_chunk` | the reasoning, shown only with `/reasoning on`, kept to its tail |
| `tool_call`, `tool_call_update` | one message per call, posted when it completes or fails, when `stream.show_tool_calls` is on |
| `usage_update` | fills `agent.context` and drives the ⚠️ warning at `stream.compaction_warn`; a report under 5% of the last good one is held, since omp can anchor it on a few hundred tokens after `session/load`; never enters the turn queue |
| `session_info_update` | read for accounting, never rendered |
| `available_commands_update` | fills `/commands` |
| `config_option_update`, `current_mode_update` | kept so the pickers show current values |

A subagent has no event type of its own. omp runs one through its `task` tool, so a subagent shows
up as a `tool_call` whose `rawInput.tasks[]` names it. The ACP `kind` for that call is `other`,
which tells a reader nothing, so the transport relabels it `subagent` and prints the name plus the
agent type. What the subagent is doing arrives on the same call as `tool_call_update` events whose
`content` text reads `Spawned agent X`, `Running agent X`, `Background task X complete`; those are
kept as a short trail, deduplicated and capped at four. The final `rawOutput` carries the
subagent's own result snapshot, which is where its findings become readable. A call whose only
content is "waiting for the result" is dropped: it is the absence of a step, not a step. Reasoning is not guaranteed either: omp only emits
`agent_thought_chunk` on turns where the model actually reasons, so an empty thinking block means
the turn had none, not that the transport dropped it. That is also why the display is tested
against a fake stream rather than the live model.

## Storage

| Path | Holds |
| --- | --- |
| `~/.config/ompwire/config.toml` | agent settings |
| `~/.config/ompwire/gateways.toml` | gateway definitions |
| `~/.config/ompwire/.env` | one credential per gateway, mode `0600` |
| `~/.local/state/ompwire/sessions.db` | chat key to session id, per-chat prefs (model, mode, thinking, yolo, `context_size`), unfinished turns |
| `~/.local/state/ompwire/overrides.json` | approval overrides set from chat |
| `~/.local/state/ompwire/files/<chat id>/` | documents sent from the chat, for the agent to read |
| `~/.config/ompwire/schedules.toml` | the schedules, one row each (no daemon state) |
| `~/.local/state/ompwire/serve-<gateway>.lock` | flock, one process per gateway |
| `~/.local/state/ompwire/ompwire.log` | rotating log, redacted |

`~/.omp` is the agent's own store: sessions, provider credentials, MCP config. ompwire touches it
in three places, and uninstall never does:

| Path | What ompwire does |
| --- | --- |
| `~/.omp/agent/mcp.json` | written once by setup with the MCP servers |
| `~/.omp/agent/sessions/<cwd>/<ts>_<id>.jsonl` | read incrementally for `/usage` totals and compaction notices |
| `~/.omp/agent/config.yml` | the `compaction:` block only, through `/compaction`, after a one-time `.ompwire-bak` |

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
| `_session/steering` | to agent | a message sent mid-turn, and `/steer` (request, not notification) |
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
  dumps every old answer into the chat. `ChatAgent.ensure` mutes updates while loading.
- **Permissions are option ids, not booleans.** `session/request_permission` returns options
  with a `kind` (`allow_once`, `reject_once`, ...) and the reply is
  `{outcome: {outcome: "selected", optionId}}`. Always pick from what the agent offered.
- **Model selection is a config option.** There is no model field in ACP; it is the
  `configOptions` entry with `id = "model"`, set through `session/set_config_option`.
- **A second prompt into a running turn cancels it; steering is a request, not a notification.**
  Measured: `session/prompt` while a turn is running ends that turn with `stopReason:
  "cancelled"`. `_session/steering` works, but only as a JSON-RPC *request*
  (`{sessionId, prompt: [content blocks]}`): omp's `extNotification` handler is empty, which is
  why the earlier notification-based `/steer` never showed up in a reply. The request answers
  `injected` (the running turn reads it, verified on omp 18.12.6), `promptRequired` (nothing was
  running) or `failed`. A message sent while a turn runs is steered by default
  (`[telegram] busy_message = "queue"` restores the FIFO); anything not injected falls back to
  the queue, and `/queue` always queues. The message gets a reply saying which: `↪️ steered` or
  `📥 queued #n`.
- **Compaction is read from omp's transcript, not inferred from usage.** No compaction event
  crosses ACP in omp 18.12.6 (`auto_compaction_*` reach RPC mode only; asked upstream in
  can1357/oh-my-pi#16969). omp writes a `compaction` entry with `tokensBefore`/`tokensAfter` to
  the session JSONL, so after each turn a new entry gets one `🗜️` notice. Entries present when
  the process starts are not announced, and a `/compact` turn stays quiet because omp reports
  it itself. `/usage` sums the same file, so its totals survive restarts and `/resume`.
- **A permission answer must not block the reader.** The reply to `session/request_permission`
  can take minutes; it is answered on its own thread so a steer sent during an approval returns.
- **A turn cut by a restart is resumed, per transport.** `ask()` writes an `inflight` row before
  the prompt and deletes it when the turn ends, unless it ended `cancelled` without `/stop`,
  `/kill` or `/new`: that is systemd signalling the agent along with the gateway. A transport
  owns its store keys by prefix (`telegram.KEY_PREFIX = "tg:"`, the CLI uses `cli:<cwd>`), and
  `engine.resume_plan` returns only the rows under that prefix, split into worth-resuming and
  exhausted. Each resumed row becomes a prompt that tells the agent to check its session history
  and finish (at most `RESUME_LIMIT` tries). Polling also starts with
  `drop_pending_updates=False`, so a message sent during the restart window is answered instead
  of discarded.
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
ompwire gateway start [NAME] -> platform, profile, credential (verified first), allow list
                               gateways.toml + .env (stop first if the credential changes)
                               ~/.config/systemd/user/ompwire-NAME.service
                               daemon-reload, enable --now (or restart if already active)
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
