# ompwire guide

Everything past the quick start: gateways, what the chat does, approvals, schedules, the
terminal, configuration and troubleshooting. Start with the [README](../README.md) if ompwire is
not installed yet.

- [Gateways](#gateways)
- [In a chat app](#in-a-chat-app)
- [Approval](#approval)
- [Schedules](#schedules)
- [Thinking, advisor and subagents](#thinking-advisor-and-subagents)
- [Terminal](#terminal)
- [Configuration](#configuration)
- [Update and remove](#update-and-remove)
- [When it goes quiet](#when-it-goes-quiet)

## Gateways

A gateway is one bot on one platform, with its own token, allow list and systemd unit. One
machine can run several, and stopping one leaves the others running.

### Platforms

| Platform | Status | Connect it with |
| --- | --- | --- |
| Telegram | ready | `ompwire gateway start --platform telegram`: a bot token from [@BotFather](https://t.me/BotFather), checked with `getMe`, then your user id |
| WhatsApp | ready | `ompwire gateway start --platform whatsapp`: scan a QR with the bot's number (Linked devices), then the phone numbers allowed to talk to it (see [WhatsApp](#whatsapp)) |
| Terminal | ready | `ompwire chat`: no gateway, no credential (see [Terminal](#terminal)) |

`ompwire gateway types` prints the same list for the installed version.

### Starting a gateway

`ompwire gateway start` is the one command for a first start, a restart and a credential
change. It asks only what it does not know:

1. The platform, from the ones this version implements.
2. Which profile, when that platform already has gateways: each is listed with its credential
   masked (`1234…alue`), plus "add a new profile". A platform can run several bots, each with
   its own name, credential and unit.
3. The name of a new gateway, the first one on a platform included. It names the systemd unit
   (`ompwire-<name>`), the state directory and the logs, so it is asked before anything is
   written; Enter keeps the suggestion (the platform, then `<platform>-2`, …).
4. The credential, when there is none or the platform rejects the stored one. Numbered steps
   for getting one on that platform (for Telegram: @BotFather, `/newbot`) are printed first. For an existing
   profile it shows the current one masked and asks whether to replace it. Nothing is saved
   until the platform accepts it, and a running gateway is stopped before its credential
   changes, so the old one never keeps polling next to the new one.
5. The allow list, when it is empty, again with the steps to find a user id (for Telegram:
   send any message to @userinfobot).

Then it writes the systemd unit and starts it, or restarts it when it is already running.
Running it again with nothing to change is a plain restart.

Without a terminal (a script, CI), every answer comes from flags and `--yes` fails instead of
asking:

```bash
ompwire gateway start work --platform telegram --token-stdin --allowed 42 --yes < token.txt
```

`--no-start` saves the gateway and its credential without starting it.

| Command | What it does |
| --- | --- |
| `ompwire gateway` | this command's help |
| `ompwire gateway list` | name, platform, bot identity, allow list, unit state |
| `ompwire gateway start [NAME]` | add a gateway or replace its credential, then run it (see above) |
| `ompwire gateway update [NAME]` | change its groups, group mode and own settings (see [Changing a gateway](#changing-a-gateway)) |
| `ompwire gateway stop NAME` | stop and disable it, so it stays down after a reboot |
| `ompwire gateway restart NAME` | rewrite the unit, then restart it |
| `ompwire gateway status [NAME]` | unit path, state, log command |
| `ompwire gateway logs NAME` | last 40 journal lines |
| `ompwire gateway remove NAME` | stop it, delete the unit and the definition |
| `ompwire gateway types` | which platforms exist and which are implemented |

`start` is the install: it writes `~/.config/systemd/user/ompwire-<name>.service`, reloads
systemd and runs `enable --now`. The token is checked before anything is written, because a
unit with `Restart=always` on a revoked token looks like a crash loop.

Each live chat owns its own `omp` process, so memory grows with conversations, not with bots.
Each unit is capped at `MemoryMax=4G`. Nothing is written for a gateway until its platform
accepts it (a token Telegram verifies, a WhatsApp device that finished linking).

### WhatsApp

A WhatsApp gateway is a linked device on a number of its own, the way WhatsApp Web is. There is
no token: `gateway start` shows a QR in the terminal, and you scan it on the bot's phone under
**Settings > Linked devices > Link a device**. No camera at hand: `--pair-phone <number>` asks
WhatsApp for an 8-character code to type on that phone instead.

```bash
ompwire gateway start --platform whatsapp --allowed 6281234567890 \
  --groups 120363000000000000@g.us --group-mode mention
```

- **Use a number of its own.** Anyone who messages it reaches the bot's gate, and WhatsApp may
  restrict a number that runs an unofficial client. The link goes through
  [neonize](https://github.com/krypton-byte/neonize) (whatsmeow), which speaks WhatsApp Web's
  protocol; it is not Meta's Cloud API.
- **Allowed numbers** are phone numbers with the country code, digits only. Only they get an
  answer, in a direct chat and in a group. Anyone else gets nothing back, not even "not
  allowed".
- **Groups** are opt-in. The running gateway records every group its number is in, and
  `gateway update` lists them by name to switch on. The modes are the same as on Telegram,
  see [Groups and channels](#groups-and-channels).
- **No buttons.** A question, an approval or `/model` arrives as a numbered list: reply with the
  number. A question also takes your own words, or `0` to skip.
- **Photos and files** work as on Telegram: a photo goes to the agent as an image, a document
  is saved and its path handed over. Voice notes and stickers are not supported yet.
- **The session file is the credential.** It lives in
  `~/.config/ompwire/gateways/<name>/whatsapp.db` (mode `0600`); whoever holds it acts as
  the linked device. `gateway remove --purge` deletes it; also remove the device on the phone.
- **Relink** with `gateway start <name> --relink`, for example after the phone logged the
  device out. The gateway stops itself when WhatsApp ends the link.
- **Schedules** reporting to a WhatsApp gateway are sent by that gateway while it runs: one
  number holds one connection, so the timer leaves the report in the gateway's outbox. A
  schedule whose gateway is down fails with that reason.

### Groups and channels

A gateway answers in a group or a Telegram channel only when that chat is listed. Unlisted
chats, and people not on the allow list, get no reply at all; the journal says why
(`update dropped … group not listed`).

```bash
ompwire gateway update work     # pick groups by name, then a mode for each
# or by flag: Telegram ids are negative; id:mode gives one chat its own mode
ompwire gateway update work --groups -1001234567890,-1009876543210:all --group-mode mention
```

| Mode | In a group | In a channel |
| --- | --- | --- |
| `mention` (default) | an @mention of the bot, a reply to one of its messages, or `/command@the_bot`, from an allowed member | nothing (a post cannot mention a bot) |
| `all` | every message from an allowed member | every post |
| `read` | nothing | nothing |

- **No id hunting.** Add the bot to the group or channel while the gateway runs. It records
  the chat with its title, and `gateway update` offers it by name; on Telegram it also tells
  you in a private message (or in `home_channel`). Telegram has no call that lists a bot's
  chats, so a chat the bot was added to before this version is recorded on its first message.
  In a listed chat `/whoami@the_bot` shows the id and mode.
- **Telegram privacy mode.** By default a bot in a group sees only replies to it and
  `/command@the_bot`. For a plain @mention, or for `all`, make the bot an admin, or run
  `/setprivacy` in @BotFather, pick the bot, choose Disable, then remove and re-add it.
- **Channels** need the bot as an admin. A post has no sender, so listing a channel trusts
  everyone who can post in it; list only channels you control.
- **One session per chat.** Everyone in a group shares it, and each prompt starts with who
  asked (`[Pandu] …`) with the bot's own @mention taken out. An edited message is not run
  again.
- **Quiet by default.** A group or channel gets the answer and nothing else: no tool trace,
  todo list, reasoning, usage line, context warning or "queued" reply. `/verbose` in that chat
  toggles the activity back on, for that chat only; private chats stay verbose.

## In a chat app

The commands and behaviour below are the same on Telegram and WhatsApp, except that WhatsApp
answers a choice with a typed number instead of a button, and has no `/skills` or `/login`.
The terminal has its own smaller set, described under [Terminal](#terminal).

| Command | What it does |
| --- | --- |
| `/new`, `/sessions` | start a session, or list this directory's sessions with a resume button each |
| `/fork`, `/handoff [note]` | branch the current session, or hand it to a fresh one with a summary |
| `/name <text>`, `/export` | rename the session, or get it as an HTML file |
| `/model`, `/mode`, `/thinking` | switch with an inline keyboard |
| `/cwd <path>` | start a session in another directory |
| `/status`, `/usage`, `/stats`, `/context` | what is running, what the session cost, message and tool counts, what fills the context |
| `/compact` | run OMP's compaction now |
| `/compaction [10-95\|off\|on]` | show or set OMP's auto-compaction threshold |
| `/busy [steer\|followup\|queue]` | what a message sent mid-turn does, in this chat |
| `/btw <question>` | a side question answered without touching the conversation |
| `/stop`, `/kill` | cancel the turn and its background jobs and subagents, or stop this chat's agent process |
| `/approve`, `/verbose`, `/yolo` | approval policy, activity in this chat (trace, todo list, notices), approvals off for this chat |
| `/reasoning [on\|off]` | show the agent's thinking above the answer, in this chat |
| `/advisor [on\|off]` | show omp's advisor notes on each turn, in this chat (on by default) |
| `/skills [query]`, `/commands` | what the agent can do |
| `/schedule [run\|rm <name>]` | list schedules (turns and commands), run one now, remove one |
| `/login` | sign in to a model provider |
| `/help`, `/whoami` | help, ids |

Any other slash command is forwarded to the agent as a prompt.

### While the agent is working

What a message sent mid-turn does is set per chat with `/busy`:

- `steer` (default): it goes into the running turn, which reads it at its next step. The reply
  says `↪️ steered`.
- `followup`: OMP queues it and runs it as the next turn, right after this one. The reply says
  `📥 follow-up queued`.
- `queue`: ompwire holds it and sends it as a new turn once this one ends (`📥 queued #2`).

When OMP declines a steer or a follow-up, the message falls back to the queue. `/stop` cancels
the turn, plus any bash job or subagent it left running in the background. `/btw` works while a
turn runs: the side question is answered on its own and never enters the conversation.

### Subagents

When the agent runs subagents through its `task` tool, a verbose chat gets one summary message
for the call and one card per job. The summary (`🤖 subagent (spawned 2 background agents)`)
lists each job with its type, status, age and tool count, the task it was given, and a totals
line (running, done, failed, tools, tokens). A job card shows the status line, then the task,
type, model with its thinking level, usage (requests, tokens, cost as omp reports them) and the
last action, in a code block. Each update is a new message, like the rest of the chat: until every
job has finished, a card is posted again at most once a minute, even while a job sits on one
long command, and once more when a job ends, so a background job that outlives the turn still
reports how it finished. A quiet chat (a group, or `/verbose` off) shows none of them.

### Questions

OMP's `ask` tool arrives as buttons, one question at a time, with the question above them. A
reply can also end with `<<options: a | b>>`, which becomes buttons the same way. Both add
✏️ Other answer and ⏭️ Skip. Skip on an `ask` lets the agent go on with the option OMP marked as
recommended (or the first one); the turn keeps running. An `ask` nobody answers within
`[approval] timeout` (120 seconds) is resolved the same way. While the question is open:

- a message about something else is held (`⏸️ held #1`) and runs after the question is settled;
- after ✏️ Other answer, the next message you type is the answer;
- ⏭️ Skip, or `/new`, closes the question and releases what was held.

Once the question is settled the buttons are removed, so a late tap sends nothing.

### Context and compaction

OMP compacts the context itself when it passes `compaction.thresholdPercent` in
`~/.omp/agent/config.yml`. ompwire shows it and lets you control it:

```
context   216k / 1.05M (20%)
compacts  at 70% (734k)
last      10:41 WIB · 735.1k → 68.2k
```

- `/usage` sums every model call in the session from OMP's transcript, so the totals survive a
  gateway restart and `/resume`.
- A ⚠️ notice arrives once when the context crosses `[stream] compaction_warn` (60%).
- When OMP compacts during a turn, a `🗜️` notice with the time and before → after follows the
  turn.
- `/compaction 70` edits only the `compaction:` block of OMP's config and keeps a one-time
  backup next to it. `/kill` makes the next process pick the new value up.

## Approval

The agent runs with `approval_mode = write`. Reads and OMP's own file writes go through; a shell
command that can change something sends a request with ✅ Allow, ✅ Allow for this session and
⛔ Deny. Commands that only read are let through: the command is tokenised, a pipeline counts as
read-only only when every part is, and a redirect is allowed only into `/dev/null`.

`yolo` asks nothing; `/yolo` turns it on for one chat. An unanswered request is denied after
`[approval] timeout` (120 seconds by default). A question OMP opens that is not an approval
(free text, an editor) is cancelled, because nobody in the chat sees it.

### Plan mode

`/mode plan` makes the agent explore and propose before it changes anything. Reads and read-only
commands run without a button; every other tool is refused. The plan arrives with two buttons:

- ✅ Approve plan: back to default mode, and the agent implements the plan.
- ✏️ Deny & revise: the bot asks what to change. Your next message (typing straight away works
  too) goes back as feedback, and a revised plan comes back for review.

Plan mode is ompwire's own, since OMP's plan review exists only in its terminal UI. `/mode
default` leaves it at any time.

The appended system prompt sets the scope: a question gets a read-only answer, a clone goes to
`/tmp` or `~/.cache`, and anything that touches production stops so the agent can say what it
would run.

## Schedules

A schedule runs one agent turn, or one shell command, on a timer and exits. Nothing of ompwire
stays alive between runs.

```bash
ompwire schedule add morning --cron "0 7 * * *" --prompt "summarise last night's repo activity"
ompwire schedule add disk --cron "30 3 * * *" --command "~/bin/cleanup.sh" --gateway ops
ompwire schedule run morning      # run it now instead of waiting for 07:00
ompwire schedule list
ompwire schedule remove morning
```

Cron fields are read in `Asia/Jakarta`. The result goes to the gateway named on the schedule as
a card: when it runs, model, duration, tools, tokens, and the answer. A failed run reports the
same way with the reason. Each run is a `oneshot` unit capped at 1 GB, 200% CPU and a 30 minute
timeout.

`--approval` decides what a run may do while nobody watches: `ask` reads only, `write` also
writes files, `yolo` (the default) asks nothing. You can also ask in the chat ("every morning
at 7, summarise the repo"); the agent proposes it and nothing is installed until you confirm.

A `--command` schedule runs the command with `bash -c` from your home directory, with no agent
and no model, so it costs nothing and still runs when the model provider is down. What it prints
is sent as written; a run that prints nothing sends nothing, which is how a watcher says "no
change". A non-zero exit or a timeout is reported with the reason and the tail of the output,
and the whole process group is stopped. The command does not see any bot token, and each run's
output is kept for 30 days under `~/.config/ompwire/schedule-logs/<name>/`.

## Thinking, advisor and subagents

`/reasoning on` shows the agent's thinking above the answer in a code block.
`[stream] show_reasoning` sets the default. When omp's advisor is enabled (`advisor.enabled` in
`~/.omp/agent/config.yml`), its notes on a turn show as `⚖️ advisor`, one code block per note
with its severity (🛑 blocker, ⚠️ concern, 💡 nit). `/advisor off` hides them in this chat;
`[stream] show_advisor` sets the default (on). Subagents need no switch: OMP runs them through
its `task` tool, and each job gets its own card (see Subagents). A skill the agent creates,
updates or deletes (`manage_skill`, `learn`) shows in the trace as `🎓` with the action and the
skill's name.

## Terminal

`ompwire chat` runs the same engine without a gateway: no bot, no token, no systemd unit. The
answer is printed as it is written, because a terminal has no rate limit.

```bash
ompwire chat                          # conversation for this directory
ompwire chat -q "summarise this repo" # one prompt, print the answer, exit
ompwire chat --cwd ~/proj             # run the agent in another directory
```

Each directory keeps its own conversation (`cli:<cwd>`), separate from any chat app. Inside:
`/new`, `/stop`, `/model <id>`, `/mode <default|plan>`, `/context`, `/compaction`, `/help` and
`/exit`. Questions are answered by number or by typing, and approvals are asked inline.

Without a terminal on stdin (`-q` from a script or a pipe) nobody can answer, so an approval
request is allowed instead of hanging. Run one-shot prompts only where that is acceptable.

## Configuration

| Path | Holds |
| --- | --- |
| `~/.config/ompwire/config.toml` | settings every gateway shares unless it sets its own |
| `~/.config/ompwire/schedules.toml` | the scheduled turns |
| `~/.config/ompwire/gateways/<name>/gateway.toml` | one gateway: platform, allow list, home channel, its own settings |
| `~/.config/ompwire/gateways/<name>/.env` | that gateway's credential only (the bot token for Telegram), mode `0600` |
| `~/.config/ompwire/gateways/<name>/` | also that gateway's sessions, chat toggles, log and lock |
| `~/.config/ompwire/cli/` | `ompwire chat`'s sessions |
| `~/.config/ompwire/files/`, `schedule-logs/` | documents sent into a chat, command schedules' output |

Everything ompwire keeps is in this one folder, and `OMPWIRE_CONFIG_DIR` moves all of it.
One gateway is one directory, so `gateway remove` with the full delete leaves nothing of it
behind. Back the folder up as you would a password store: besides the tokens it holds the
WhatsApp session file.

Precedence: defaults, then `config.toml`, then the gateway's own settings, then toggles set from
its chat, then environment variables. Sections: `[acp]` (the agent command, run as
`omp --mode rpc-ui`; an old `--mode acp` or `--mode rpc` is read as rpc-ui), `[agent]`,
`[session]`, `[stream]`,
`[approval]`, `[files]`, `[logging]`,
and one per platform (`[telegram]`). `ompwire config` prints the merged result with secrets masked.
`OMPWIRE_CONFIG_DIR` points everything at another directory.

### One gateway, its own settings

Every gateway shares the agent itself: the `omp` binary, its provider logins, skills, MCP
servers and memory in `~/.omp`. What it does not share:

- **Sessions.** Each gateway has its own session store, so the same person talking to two bots
  has two separate conversations, and `/new` in one leaves the other alone.
- **Chat toggles.** `/approve` and `/reasoning` persist to that gateway's own overrides.
  `/model`, `/thinking`, `/reasoning` and `/verbose` in one chat stay with that chat.
- **The log.** Each gateway writes its own.
- **Settings, if you want.** A gateway can set its own `[agent]`, `[stream]`, `[approval]`,
  `[files]` or `[session]` values over `config.toml`. `gateway start` asks when it creates a
  profile, `gateway update` changes them later (see below). By hand:

```toml
# ~/.config/ompwire/gateways/work/gateway.toml
[gateway.work]
type = "telegram"
allowed_users = [42]

[gateway.work.agent]
model = "anthropic/claude-sonnet"
cwd = "~/work"
```

### Persona (SOUL.md)

Each gateway can speak with its own persona, kept in `SOUL.md` in its folder:

```text
~/.config/ompwire/gateways/<name>/SOUL.md   one bot
~/.config/ompwire/cli/SOUL.md               ompwire chat
```

When `gateway start` creates a profile (and when `ompwire setup` runs), it asks for the persona
in your own words, has the chosen model draft `SOUL.md`, and prints the draft. Nothing is written
until you answer:

- `y` writes the draft
- `r` drafts again, optionally with what to change
- `n` writes an empty template instead

A draft that fails or times out also leaves the empty template, and the gateway is created anyway.
An existing `SOUL.md` is kept unless you confirm replacing it.

The agent receives the persona first, then `[agent] chat_format_hint` (the formatting and scope
rules), then the file/options marker protocol. Without a `SOUL.md`, `[agent] persona` from the
config is used. `/status` names the active source. The persona is passed when the agent process
starts, so an edit applies after `ompwire gateway restart <name>` (or once the idle timeout closes
the process), not on `/new`.

An older config with `append_system_prompt` still works: it is read as `chat_format_hint`, and
`ompwire chat` and each gateway's log warn until the key is renamed.

### Changing a gateway

`ompwire gateway update [NAME]` changes a gateway that exists, the same way on every
platform. Without flags it is a menu: pick a line, change it, repeat, then save. Each line
shows the value in force and whether it is the gateway's own or comes from `config.toml`.

| Setting | Flag | Values |
| --- | --- | --- |
| groups and channels | `--groups` | picked by name in the menu; ids by flag, `id:mode` for its own mode |
| group mode | `--group-mode` | `mention`, `all`, `read` |
| model | `--model` | `provider/id`; the menu lists what the agent offers |
| working directory | `--cwd` | an existing directory |
| approval mode | `--approval` | `write`, `yolo` |
| thinking level | `--thinking` | `off`, `auto`, `minimal`, `low`, `medium`, `high`, `xhigh` (the menu asks the agent) |
| reasoning in direct chats | `--reasoning` | `on`, `off` |
| activity in direct chats | `--verbose` | `on`, `off`: tool trace, todo list, notices |

`inherit` as a value drops the gateway's own setting, so `config.toml` decides again.
Reasoning and activity apply to direct chats; a group or channel stays quiet unless someone
there runs `/reasoning` or `/verbose`.

A chat's own `/model`, `/thinking`, `/reasoning` or `/verbose` beats the gateway's setting.
So when `update` changes one of those, it clears that choice in direct chats, and the new
value reaches them. A group keeps its own. A running gateway restarts once at the end to pick
the change up; `--no-start` saves without restarting.

```bash
ompwire gateway update work --thinking high --reasoning on --verbose off
ompwire gateway update work --model inherit
```

Scheduled turns run with the settings of the gateway they report to.

An install from before this layout (one `gateways.toml`, one `.env`, the first gateway's state at
the root of the state directory) is moved the first time each gateway starts: definitions
and credentials at once, and a gateway's sessions only while it is not running, so no
conversation or unfinished turn is lost. The old `gateways.toml` is kept as
`gateways.toml.migrated`.

`ompwire setup` can be run again: every prompt is pre-filled with what is on disk.

MCP servers are OMP's own: it reads `~/.omp/agent/mcp.json` and starts a server only when a turn
calls it. ompwire injects none, and an `[mcp]` section left in an old config is ignored.

`[files]` sets `max_mb` (20, both directions) and `incoming_dir` for documents sent into the
chat. The agent sends a file by naming it on its own line:

```
Here is the chart.
<<file: /home/you/report/chart.png>>
```

The path must be inside the agent's working directory, exist and fit the limit; at most five
files go out per reply. A refused file is named in the chat with the reason.

## Update and remove

```bash
ompwire update              # newest release, restarts every active gateway
ompwire update --check      # say what would change, change nothing
ompwire update --edge       # track the tip of main
ompwire restart             # restart without updating
ompwire uninstall --dry-run # list what would be removed
```

`uninstall` removes only what ompwire created: `~/.config/ompwire`, `~/.local/share/ompwire` and
the package, plus `~/.local/state/ompwire` when an older install left it behind. `~/.omp` is never touched.
`--keep-config` keeps `config.toml`, `schedules.toml` and each gateway's `gateway.toml` and `.env`,
and removes the sessions, logs and files beside them.

## When it goes quiet

Start with `ompwire doctor`; it exits non-zero when something is broken. `ompwire status`
reports without a handshake.

| Symptom | Cause |
| --- | --- |
| The bot never answers, no error | your user id is not on the allow list (`gateway list` shows it) |
| `gateway start` says the credential is rejected | Telegram: revoked in @BotFather; run `gateway start` again and give the new one |
| `409 Conflict` in the journal | Telegram: a second process polls the same token |
| WhatsApp bot silent for one person | their number is not on the allow list, or (in a group) the group is not listed or the mode is `mention` and nobody mentioned the bot; the journal says which (`message dropped … sender not allowed`) |
| Telegram bot silent in a group | the group is not listed, the mode is `mention` and the message neither mentions nor replies to the bot, or privacy mode keeps plain messages from reaching it (see [Groups and channels](#groups-and-channels)); the journal says which (`update dropped … not addressed`) |
| A group gets only the answer | groups and channels are quiet by default; `/verbose` in that chat turns the activity on |
| `whatsapp asked for a new QR` in the journal | the phone logged the device out; `gateway start <name> --relink` |
| `start` says systemd --user is unavailable | no user manager (container, CI) |
| An answer stops mid-turn | `journalctl --user -u ompwire-<name> -f` |
