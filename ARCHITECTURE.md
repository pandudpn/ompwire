# Architecture

ompwire is the transport between a chat platform and an OMP agent. Everything that decides
*what* to answer lives in the agent. This document is about *how the message gets there and
back*, and where the seams are.

```
+--------+        +---------+        +-------------------+        +----------------------+
| chat   | <----> | ompwire | <----> | omp --mode rpc-ui | <----> | tools, skills, MCP   |
| surface|        |         | stdio  | loop, memory      |        | providers, files     |
+--------+        +---------+        +-------------------+        +----------------------+
                       |
                  sessions.db
```

One `omp` subprocess per conversation, one `ompwire` process per gateway. Nothing else.

## Who owns what

| Layer | Owns | When it fails |
| --- | --- | --- |
| Engine (`engine.py`) | the turn itself: agent process lifecycle, update drain, pacing, approvals, plan mode, schedules, files. Speaks `Surface`, never a platform | a turn fails: recorded, the next gateway start resumes it |
| Transport (`telegram.py`, `tg_*.py`) | Telegram routing and buttons: handlers, `TelegramSurface` (posting, cards, approvals), `TelegramDelivery`, pickers, per-chat prefs, what addresses the bot in a group, the `tg:` key prefix | markup is rejected (resent as plain text) or a send fails (logged as a WARNING, the turn goes on) |
| Transport (`whatsapp.py`, `wa_*.py`) | WhatsApp: the linked device (neonize), what addresses the bot in a group, numbered answers in place of buttons, `WhatsAppSurface`, the outbox for scheduled reports, the `wa:` key prefix | WhatsApp ends the link (logged out, new QR asked): the gateway stops and says to relink; a send fails: logged as a WARNING, the turn goes on |
| Audience (`audience.py`) | who is answered where, for every platform: allowed people, listed groups and channels, each chat's mode (`mention`, `all`, `read`) | a chat or person it does not know: dropped without a reply, the reason in the journal |
| `sessions.db` (`store.py`) | chat key to session id, prefs, approval override, turns a restart left unfinished (read per key prefix) | unreadable: a new agent session, the chat key is kept |
| Agent client (`rpc.py`) | omp's RPC wire: framing, command ids, timeouts, translating events into updates | the subprocess dies mid-turn: respawn with `--resume` on the same session id |
| Agent (`omp`) | loop, tools, MCP, compaction, skills, memory, prompt | provider or tool error: reported to the chat as the agent's own text |
| Delivery (`delivery.py`) | how a scheduled run's report reaches a person: one `Delivery` per gateway kind, resolved from the gateway stored on the schedule | no implementation for that kind: the report fails, and the schedule status carries the reason |
| Catalogs (`catalog.py`) | model, mode, thinking, provider, skill lists | the agent will not answer a probe: an empty list, and the command says so instead of inventing options |
| Gateways (`gateways.py`) | one bot definition: platform, allow list, credential | unreadable file: refuse to start rather than guess |
| Service (`service.py`) | the systemd unit per gateway | no `systemd --user`: `start` refuses, nothing is left unsupervised |

## Modules

| File | What it is |
| --- | --- |
| `engine.py` | the platform-neutral agent engine: ChatAgent, the turn renderer, the `Surface` contract, `ToolTrace`, `FloodWait`, resume planning |
| `telegram.py` | the Telegram transport: polling, routing (busy mode, held questions, ask answers), commands, callbacks, banners, group and channel addressing |
| `tg_surface.py` | how one Telegram chat shows a turn: `TelegramSurface`, `TelegramDelivery`, the steered/follow-up/queued/held acks |
| `tg_pickers.py`, `tg_login.py`, `tg_skills.py` | mixins of the transport: the model/mode/thinking pickers, provider login, `/skills` |
| `whatsapp.py` | the WhatsApp transport: routing, group addressing, numbered answers, commands, banners |
| `audience.py` | `Audience`: the one admission rule (private, group, channel) both transports ask before anything runs |
| `wa_surface.py` | how one WhatsApp chat shows a turn: `WhatsAppSurface`, cards and tool traces in WhatsApp markup, numbered lists |
| `wa_client.py` | the link: session file, QR or pairing-code linking, the linked number, markdown to WhatsApp markup |
| `wa_outbox.py` | `WhatsAppDelivery`: a scheduled report written for the running gateway to send |
| `__main__.py` | the CLI: one function per command, `argparse` wiring |
| `gateway_cli.py` | `ompwire gateway ...`: start, remove, list, status, logs, and `_run`, the unit's entry point |
| `setup.py` | the wizard, `.env` writing token normalisation, `bot_identity` verification |
| `update.py` | install mode detection, update, restart of running gateways |
| `rpc.py` | the client for `omp --mode rpc-ui` over stdio |
| `gateways.py` | gateway definitions, credentials per gateway, selection rules |
| `config.py` | layered config: defaults, `config.toml`, environment, redaction of dumps |
| `service.py` | systemd unit rendering, start, stop, remove, state |
| `catalog.py` | model, mode, thinking, provider and skill lists read from the agent |
| `cli.py` | `ompwire chat`: `TerminalSurface` and the REPL, both on the engine. A terminal has no rate limit, so its surface sets `streams` and prints the answer as it is written (markers held back until they close); Telegram posts one message per segment |
| `uninstall.py` | what to remove, and what to leave alone |
| `mdrender.py` | markdown to Telegram HTML |
| `status.py` | one screen of current state, no handshake |
| `login.py` | provider login on a pty, driven from chat |
| `store.py` | sqlite: chat key to session id, per-chat prefs, unfinished turns per transport prefix |
| `format.py` | neutral helpers: `uptime`, WIB clock, token counts (`58.6k`), the tool-trace clip |
| `omp_sessions.py` | session totals and compactions, read incrementally from omp's transcript JSONL |
| `omp_config.py` | omp's `compaction:` block in `config.yml`, edited in place |
| `tg_html.py` | Telegram HTML: cards, grids, tool blocks, help and run cards, keyboard paging |
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
   allow list. A message sent while a turn runs follows the chat's `/busy` mode (steered, an omp
   follow-up, or ompwire's queue), with a reply saying which; one sent while a question is open is
   held until the question is settled.
2. The chat key resolves to a session id in `sessions.db`. The agent process starts with
   `--resume <id>`, or with a new session when there is none or the transcript is gone.
3. Stored prefs (model, thinking) are replayed with `set_model` and `set_thinking_level`.
4. The message becomes a `prompt` command. Images travel as base64 image blocks on it.
5. Event frames stream back, translated into updates. Nothing is edited after it is sent: the prose written
   before each tool call is posted as a message when the tool call starts, each tool call is
   posted as its own message when it finishes (with its output), and whatever is left goes out
   when the turn ends. Every message's timestamp is when that step happened. Telegram allows
   about one message a second per chat, so one sender posts the outbox in order at least
   `stream.min_gap` apart; tool traces that finish while it waits (the gap, or a flood window)
   go out together as one message, and a trace dropped after flood control is named in the
   chat ("… N tool call(s) not shown") instead of disappearing.
   A message is cut to what the platform accepts before it is sent. Telegram counts the text
   after entities are parsed, in UTF-16 units (an emoji outside the BMP is two, `&lt;` is one),
   and refuses anything over 4096: `TELEGRAM_LIMIT` keeps 96 under it, markdown is fitted by
   that count before it is rendered, and a card rendered upstream is split on its line breaks,
   closing every tag still open and reopening it in the next message.
6. An approval arrives as an `extension_ui_request` `select` (`Approve` / `Deny`) and becomes an
   inline keyboard. The answer is the label omp offered, plus a persisted override so the same
   tool is not asked twice.
7. Every action lands in a rotating log with token-shaped strings redacted before the handler
   sees them.
8. If the agent ends with `<<options: a | b>>`, the marker is stripped and turned into buttons,
   plus ✏️ Other answer and ⏭️ Skip. While the question is open, other typed messages are held
   (`⏸️ held #n`) and run after it is answered or skipped; after Other answer, the next message
   is the answer. The buttons are removed once the question is settled, so a late tap sends
   nothing.

## Attachments

omp's RPC has no way for the agent to attach a file and no general attachment block going the other way,
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
transport would have to guess at. Images keep their own path: they go to the agent as an image
block on the `prompt` command.

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
`TelegramDelivery` sends straight to the Bot API with the gateway's token, so a result still
arrives when that gateway's poller is stopped. Only the token has to be valid.
`WhatsAppDelivery` cannot do that: a WhatsApp number is one linked device with one connection,
and a second process opening the same session would knock the running gateway off. It writes
the report to `state/gateways/<name>/outbox/` (written aside, then renamed) and the running
gateway sends it within two seconds. A gateway that is not running fails the schedule with that
reason, and a report older than six hours is dropped rather than delivered out of context. The destination (the gateway's first allowed user) comes from the schedule
rather than being chosen at run time: an unattended turn that picks its own destination is a
mistake nobody is watching for.

The marker is a proposal. `<<schedule: 0 7 * * * | prompt>>` reaches the chat as a confirmation
card, because a model that misreads "every morning" as every minute would otherwise be discovered
when the machine fills up. The name, the gateway and the calendar are all validated before a button
appears, and `systemd-analyze calendar` checks the expression, so an installed schedule is one
systemd already agreed to run.

## Who stops the agent

An approval is the only place a human can intervene. omp asks through an `extension_ui_request`
`select` whose title names the tool (`Allow tool: bash\nCommand: …`, `Allow tool: write\nPath: …`)
and whose options are `Approve`, `Deny` and often `Approve <tool> for this session`. In `write`
mode omp asks before a shell command and lets its own write tool through; `yolo` asks nothing.
Plan mode respawns the process with `--approval-mode=always-ask` so that every tool asks, then
answers reads itself and denies the rest (see Plan mode below). omp's `ask` tool is a question,
not an approval: it reaches the chat as buttons whatever the approval policy (see Questions
below). Any other dialog (`input`, `editor`) is cancelled, since nobody in the chat is shown it.

A denial is the safe default: the request expires after `[approval] timeout`, ompwire answers
`Deny`, and omp fails that one tool call instead of the turn. That is why an approval left
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

omp's RPC events (`message_update`, `tool_execution_*`, `model_changed`, ...) are translated in
`rpc.py` into the update kinds below, so the renderer and its tests never see the wire. Each one
has a decision behind it:

| Kind | What the transport does |
| --- | --- |
| `agent_message_chunk` | the answer, buffered and posted as a new message at the next tool call or the end of the turn |
| `agent_thought_chunk` | the reasoning, shown only with `/reasoning on`, kept to its tail |
| `advisor` | omp's advisor card: a `message_end` whose message is `role: "custom"`, `customType: "advisor"`, notes in `details.notes` (`{note, severity}`); shown with `/advisor on` (the default) in a verbose chat, mid-turn in order, after the turn straight away |
| `tool_call`, `tool_call_update` | one message per call, posted when it completes or fails, when `stream.show_tool_calls` is on; headed by the tool's own name and an icon |
| `plan` | the agent's todo list, posted as a fresh checklist each time it changes; a repeat (omp's reminder) and the emptied list at the end are skipped |
| `available_commands_update` | fills `/commands` |
| `config_option_update` | from `model_changed` / `thinking_level_changed`; keeps the pickers current; a model change the chat did not ask for (a fallback) is posted as `⚙️ omp switched …`, while the echo of a chat's own `/model` stays silent |
| `retry` | `auto_retry_*` and `retry_fallback_*`: `🔁 retry 2/3 in 5s · <error>`, `↪️ fallback a → b` in a verbose chat |
| `notice` | omp's warning and error notices and extension errors, as `⚠️ …`; info notices (MCP mounts) are dropped |
| `compaction` | `auto_compaction_start` as `🗜️ compacting…`; the end only when it failed, since the transcript-based notice reports before → after |
| `btw` | `btw_delta` / `btw_record`, collected per record for `/btw`; never enters the turn |
| `subagent` | `subagent_lifecycle` / `subagent_progress`: the job cards (see Subagents); never enters the turn |

Notices from these kinds go through the turn's own outbox mid-turn, so they land in order with
the traces around them, and a quiet chat sees none of them.

RPC sends no usage event. After each turn the engine reads `get_state.contextUsage`, which fills
`agent.context` and drives the ⚠️ warning at `stream.compaction_warn`; a report under 5% of the
last good one is held, since omp can anchor it on a few hundred tokens after a resume.

## Questions

omp's `ask` tool only exists when the process runs as `--mode rpc-ui`; under plain `--mode rpc`
the model is told it is unavailable (measured on 18.12.6). `rpc.py` starts omp in rpc-ui and sends
`set_ask_dialog {enabled: true}`, after which one `ask` request carries every question of the
call. ompwire shows them one at a time through the same options card as the `<<options>>`
marker, with the question as its heading. A tap answers with that label, typed text with
`customInput`; Skip and the `[approval] timeout` both answer `{cancelled, timedOut}`, which omp
resolves to the recommended or first option and carries on. A bare `{cancelled: true}` would abort
the whole turn (measured on 18.12.6), so it is never sent for an ask. rpc-ui also sends
presentation requests (`setWidget`, `notify`, ...); they are fire-and-forget and never answered.
The `<<options>>` marker stays as the fallback for questions the model writes into its reply.

omp names the tool on every event (`toolName`), MCP tools included, so the trace is headed by the
tool's own name; a call whose path is `xd://x` is named after the device instead of `read`. A
finished `todo` call is held for one update: the todo result carries the whole list
(`details.phases`), sent on as a `plan` update right after, and the checklist replaces the trace.
A todo call that produces no list still gets the trace.

omp runs a subagent through its `task` tool, so in the turn's trace a subagent shows up as a
`tool_call` named `task` whose `rawInput.tasks[]` names it. That name tells a reader little, so
the transport relabels it `subagent` and prints the name plus the
agent type. What the subagent is doing arrives on the same call as `tool_call_update` events whose
`content` text reads `Spawned agent X`, `Running agent X`, `Background task X complete`; those are
kept as a short trail, deduplicated and capped at four. The final `rawOutput` carries the
subagent's own result snapshot, which is where its findings become readable. A call whose only
content is "waiting for the result" is dropped: it is the absence of a step, not a step. Reasoning is not guaranteed either: omp only emits
`agent_thought_chunk` on turns where the model actually reasons, so an empty thinking block means
the turn had none, not that the transport dropped it. That is also why the display is tested
against a fake stream rather than the live model.

## Subagents

`rpc.py` subscribes with `set_subagent_subscription {level: "progress"}`, so omp also sends
`subagent_lifecycle` (started, completed, failed, aborted) and `subagent_progress` for every job.
A progress frame carries the agent type, the resolved model, the last intent, the recent tool
calls, the tool count and the duration. A verbose chat gets one summary card per `task` call and
one card per job, each update posted as a new message like the rest of the chat, never an edit:
at most once a minute while a job runs, plus one at a terminal status. omp sends a dozen or more
progress frames for even a short job, which is why the updates are throttled. A job waiting on a
long tool call sends none, so while any job is still running the engine also asks omp's registry
(`get_subagents`) once a minute and posts the cards from it; a job that left the registry without
an end frame is closed as done. The summary waits 1.5 s before its first post, since a call's
start frames arrive a moment apart and it should count every job. Background (`detached`) jobs
outlive the turn, so their cards are kept per chat and still post the final state after the turn
ends.

## Storage

| Path | Holds |
| --- | --- |
| `~/.config/ompwire/config.toml` | settings every gateway shares |
| `~/.config/ompwire/schedules.toml` | the schedules, one row each (no daemon state) |
| `~/.config/ompwire/gateways/<name>/gateway.toml` | one gateway's definition and its own settings |
| `~/.config/ompwire/gateways/<name>/.env` | that gateway's credential, mode `0600`, the unit's `EnvironmentFile` |
| `~/.local/state/ompwire/gateways/<name>/sessions.db` | chat key to session id, per-chat prefs (model, mode, thinking, yolo, `context_size`), unfinished turns |
| `~/.local/state/ompwire/gateways/<name>/overrides.json` | toggles set from that gateway's chat |
| `~/.local/state/ompwire/gateways/<name>/ompwire.log` | rotating log, redacted |
| `~/.local/state/ompwire/gateways/<name>/serve.lock` | flock, one process per gateway |
| `~/.local/state/ompwire/cli/sessions.db` | `ompwire chat`'s sessions, keyed `cli:<cwd>` |
| `~/.local/state/ompwire/files/<chat id>/` | documents sent from the chat, for the agent to read |

`~/.omp` is the agent's own store: sessions, provider credentials, MCP config. ompwire touches it
in three places, and uninstall never does:

| Path | What ompwire does |
| --- | --- |
| `~/.omp/agent/mcp.json` | written by setup when an MCP server is added; omp loads it on its own, ompwire injects nothing |
| `~/.omp/agent/sessions/<cwd>/<ts>_<id>.jsonl` | read incrementally for `/usage` totals and compaction notices |
| `~/.omp/agent/config.yml` | the `compaction:` block only, through `/compaction`, after a one-time `.ompwire-bak` |

## The RPC surface used

Verified against omp 18.12.6. RPC is newline-delimited JSON, not JSON-RPC: a command carries an
`id` and gets a `response` with the same id; everything else is an event frame. The handshake
negotiates protocol v2, which splits large frames into chunks that `rpc.py` reassembles.

| Command or event | Direction | Used for |
| --- | --- | --- |
| `--resume <id>` (argument) | to agent | reopen a session after a restart, a `/sessions` tap, a mode switch |
| `set_ask_dialog` | to agent | turn `ask` into one structured request (see Questions) |
| `get_state` | to agent | session id and file, model, thinking level, `contextUsage` |
| `prompt` | to agent | one turn; acknowledged on admission, ended by `prompt_result` |
| `steer`, `follow_up` | to agent | a message sent mid-turn, by the chat's `/busy` mode |
| `abort` | to agent | `/stop`; the turn ends with status `aborted` |
| `set_model`, `get_available_models` | to agent | `/model` |
| `set_thinking_level`, `get_available_thinking_levels` | to agent | `/thinking` |
| `new_session`, `fork`, `compact`, `handoff` | to agent | `/new`, `/fork`, `/compact`, `/handoff` |
| `get_session_stats`, `set_session_name`, `export_html` | to agent | `/stats`, `/name`, `/export` |
| `btw` | to agent | `/btw`, answered beside the session |
| `message_update`, `tool_execution_*` | from agent | prose, reasoning, tool calls and their output |
| `model_changed`, `thinking_level_changed` | from agent | a switch the chat did not ask for |
| `auto_retry_*`, `retry_fallback_*`, `notice`, `auto_compaction_*` | from agent | the notices above |
| `set_subagent_subscription`, `subagent_lifecycle`, `subagent_progress` | both | the job cards |
| `available_commands_update` | from agent | `/commands` |
| `extension_ui_request` | from agent | approvals (`select`) and questions (`ask`); other dialogs are cancelled |

`/sessions` has no command behind it: omp's transcripts are listed from disk for the chat's cwd.
`/plan` has no RPC handler either (omp's plan review is TUI-only), which is why plan mode is
ompwire's own.

## Plan mode

`/mode plan` is a preference, not an omp setting. It respawns the agent with
`--approval-mode=always-ask` on the same session and wraps the next message in a planning
instruction. While it is on, reads (`read`, `grep`, `find`, `lsp`, `web_search`, `todo`, and
read-only shell commands) are approved without a button and every other tool is denied, so the
agent can look but not change anything. The answer ends with two buttons: ✅ Approve plan switches
back to default mode and tells the agent to implement it; ✏️ Deny & revise asks what to change,
and the next message (typed straight away works too) goes back as feedback for a revised plan.

## Decisions and their costs

| Decision | Alternative rejected | Why | What it costs |
| --- | --- | --- | --- |
| Transport only | a chat client with its own loop and tools | OMP already owns the loop, the tools and compaction; a second copy is where frontends rot | features that need agent state must go upstream |
| omp's RPC mode over stdio | ACP; calling provider HTTP APIs here | RPC loads omp's own `mcp.json` lazily, names every tool, and costs a fraction of ACP's memory per chat (about 390 MB against 2 GB measured); the agent authenticates itself, so no provider keys live in this repo | RPC is not a versioned protocol: `rpc.py` is the contract, and plan review has to live here |
| One subprocess per conversation | one shared agent with multiplexed sessions | a stuck turn cannot block another chat, and `cwd` is per chat | memory per live conversation |
| SQLite for routing | a database server | one row per chat, read once per message | no concurrent machines, no shared history |
| HTML `parse_mode` | MarkdownV2 | MarkdownV2 needs 18 characters escaped, HTML needs 3, and `<pre>` content is never parsed | markup is built, not pasted |
| Catalogs read from the agent | a hard-coded model list | the list drifts the day the agent ships a provider | one extra probe per session |
| One unit per gateway | one unit for all bots | stopping or crashing one bot leaves the others alone | more units to look at |
| Tags plus git install | publishing to PyPI | the transport depends on a specific agent surface, so edge installs matter | the installing machine needs git |

## Sharp edges found while building it

- **A group is read by more than the person who asked.** The tool trace carries paths, command
  output and file contents. An agent started for a group or channel (`shared`) begins quiet:
  the answer only, no trace, checklist, reasoning, usage line or notices; `/verbose` and
  `/reasoning` are prefs of that chat, never the gateway's.
- **python-telegram-bot's message filters also match edits.** `filters.TEXT` is true for
  `edited_message` and `edited_channel_post`, so fixing a typo ran the prompt a second time.
  Every message handler is limited to `UpdateType.MESSAGE | UpdateType.CHANNEL_POST`.
- **A Telegram bot cannot list its chats.** The only way to learn a group's id is an update
  from it, and an unlisted group's updates are dropped. `my_chat_member` (the bot added to a
  chat) is the one update that tells the owner the id; polling asks for it with
  `allowed_updates=Update.ALL_TYPES`, since it is not in Telegram's default set.
- **`--resume` with an unknown id kills the process.** omp exits with `Session … not found`
  instead of starting fresh. `ChatAgent.ensure` catches that and starts a new session, so a
  transcript deleted by hand costs the history, not the chat.
- **Approvals are labels, not booleans.** The reply to a `select` is
  `{"type": "extension_ui_response", "id", "value": "Approve"}`, or `{"cancelled": true}`.
  Always answer with a label omp offered.
- **Model selection is provider plus id.** `set_model` takes `{provider, modelId}`; the chat
  shows and stores `provider/id`, so a stored value without the slash is skipped, not guessed.
- **A message mid-turn goes through `steer` or `follow_up`, never a second `prompt`.** Both are
  queued by omp and show up as a `queue_update`; nothing running answers `promptRequired`. The
  chat's `/busy` mode picks: `steer` (default) goes into the running turn, `followup` runs as the
  next turn inside the same `prompt` call, `queue` is ompwire's own FIFO. A legacy
  `[telegram] busy_message` still sets the default. Anything omp declines falls back to the
  queue, and the reply says which: `↪️ steered`, `📥 follow-up queued` or `📥 queued #n`.
- **Compaction is read from omp's transcript.** omp writes a `compaction` entry with
  `tokensBefore`/`tokensAfter` to the session JSONL, so after each turn a new entry gets one `🗜️`
  notice. Entries present when the process starts are not announced, and a `/compact` turn stays
  quiet because omp reports it itself (`command_output`). `/usage` sums the same file, so its
  totals survive restarts and a resume from `/sessions`.
- **An approval answer must not block the reader.** The reply to an `extension_ui_request` can
  take minutes; it is answered on its own thread so a steer sent during an approval returns.
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
- **Gateways share the agent, not the state.** Session keys are `tg:<chat id>`, and the same
  person talking to two bots has the same chat id in both. With one shared `sessions.db` the two
  bots resumed each other's session, and `/new` in one reset the other. `gateways.config_for()`
  gives each gateway its own store, overrides and log under `state/gateways/<name>/`, and layers
  its `[gateway.<name>.<section>]` settings over `config.toml`. The keys stay as they are; the
  file is what separates them.
- **A shared .env handed every bot's token to every agent.** Each unit loaded the one `.env`, so
  an agent with a shell could print another bot's token from its environment. Each gateway now
  has its own `.env`, `child_env()` strips every `OMPWIRE_GATEWAY_*_TOKEN` as well, and the shared
  `.env` is retired: nothing reads it, and a schedule unit loads no credential at all.
- **A transport reads its bot from `[gateway]`.** `apply()` used to copy every gateway's token and
  allow list into `[telegram]`, whatever the platform, so a second platform would have read its
  credential from the Telegram section. Identity is `[gateway]`; `[telegram]` is behaviour only.
- **Moving a live store is a restart, not a copy.** `gateways.migrate(state_for=name)` runs in
  `gateway _run` before the lock is taken, so the files move while no process has them open, and
  never while the old lock is held. The unfinished-turn row moves with `sessions.db`, which is how
  the turn that triggered the restart was resumed on the new layout.
- **One poller per token.** Two processes polling the same bot token make Telegram answer
  `409 Conflict` and drop updates. The per-gateway flock and the unit model both exist because
  of this.
- **neonize turns every "@digits" into a mention.** A reply that quoted an image caption
  containing the bot's own LID tagged a stranger whose phone number happened to match it. Plain
  sends go out as a bare conversation message (no context, no mentions), and a reply clears
  the mention list neonize derived from the text: agent output is full of ids, ports and emails.
- **A WhatsApp sender is a LID, not a phone number.** The phone arrives in `SenderAlt`, and the
  allow list, the direct-chat session key and the reply target all use it, so the same person
  maps to one session whichever form a message used.
- **Linking cannot rely on cleanup.** whatsmeow holds the thread in Go while it waits for the
  scan, so a closed terminal kills the process without unwinding. The link is made in a scratch
  directory and moved into the gateway's folder only once it is complete.
- **Importing neonize starts processes.** It probes `ldconfig` and `uname` at import time and
  never reaps them, so it is imported only where the link is actually used.

## Gateway lifecycle

```
ompwire gateway start [NAME] -> platform, profile, credential (verified first), allow list
                               gateways/NAME/{gateway.toml,.env} (stop first if the credential changes)
                               ~/.config/systemd/user/ompwire-NAME.service
                               daemon-reload, enable --now (or restart if already active)
                               ExecStart: ompwire gateway _run NAME
ompwire gateway stop NAME    -> stop, disable
ompwire gateway remove NAME  -> stop, delete unit, definition and credential;
                               full delete also removes state/gateways/NAME/
```

`_run` is the foreground entry point systemd supervises. It is deliberately absent from
`ompwire help`: a gateway runs under systemd or it does not run, so there is no user-facing
foreground mode to document.

The unit is generated, never checked in: it needs this machine's `ompwire` path, `.env` path and
working directory. `Restart=always`, `RestartSec=5`, `MemoryMax=4G` per gateway. It also carries
`Environment=PATH=` built from where this install found the agent and `ompwire`, because a systemd
user unit does not inherit an interactive shell's PATH: without it the poller connects and then
every turn fails with `agent command not found`.

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
