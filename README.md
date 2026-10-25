# ompwire

Chat front end for an [OMP](https://omp.sh) agent. Talk to your agent from a chat app or from a
terminal.

OMP owns the agent: the loop, the tools, the skills, the memory and context compaction.
ompwire carries messages between a chat and `omp --mode acp` and renders what comes back. One
chat is one OMP session and one `omp` process.

```
Telegram  ─┐
(gateways) ├─>  ompwire engine  <->  omp --mode acp  ->  tools, skills, MCP servers
terminal  ─┘
```

The engine knows nothing about any platform. A platform is a surface that renders messages,
buttons and files its own way; a chat app connects as a gateway with its own bot and systemd
unit, and the terminal needs neither. Adding a platform does not change how a turn runs.

| Platform | Status | Start it with |
| --- | --- | --- |
| Telegram | implemented | `ompwire gateway add telegram` |
| Terminal | implemented | `ompwire chat` |
| WhatsApp | defined, not implemented: `gateway add whatsapp` refuses and writes nothing | — |

The terminal is a streaming REPL with the core commands; the full command set is the Telegram
gateway's. [Adding a transport](CONTRIBUTING.md#adding-a-transport) lists what a new platform
implements.

It is built for one person on their own machine. The only state ompwire keeps is a SQLite file
that maps a chat to a session id. There is no server, no web UI and no second agent loop.

## What it does

- Every step is its own message: the text up to a tool call, then the tool call with its
  output. Nothing is edited after it is sent.
- A message sent while the agent works goes into the running turn (`↪️ steered`), or waits its
  turn (`📥 queued #2`).
- The agent can ask with buttons. While a question is open, other messages wait
  (`⏸️ held #1`) until you answer it or tap Skip.
- Shell commands that can change the machine ask for approval with buttons. Commands that only
  read (`git log`, `ls`, `rg`) run without asking.
- Sessions survive restarts, and a turn cut by a restart is resumed.
- `/usage` shows token totals for the whole session, the context window and when OMP last
  compacted it.
- Files travel both ways: photos reach the agent as images, documents are saved for it to
  read, and the agent sends files back by naming them.
- Scheduled turns run on systemd timers and report to the chat.
- `ompwire chat` runs the same engine in a terminal.

## Requirements

- Linux with `systemd --user` for gateways. A gateway only runs as a user unit.
- Python 3.11 or newer.
- `omp` on `PATH`.
- A credential for each platform you connect. For Telegram, a bot token from
  [@BotFather](https://t.me/BotFather). The terminal needs none.

`omp --mode acp` is not listed in `omp --help`. `tests/test_acp_contract.py` checks the wire
against the real binary, so an agent upgrade that changes it fails a test instead of a chat.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/pandudpn/ompwire/main/install.sh | bash
```

The script installs `omp` if it is missing, installs ompwire from the newest release tag with
`uv` (or `pipx`), puts `~/.local/bin` on your shell path and offers to run `ompwire setup`.
Flags: `--ref`, `--no-setup`, `--no-omp`, `--no-path`.

From a checkout, for working on ompwire itself:

```bash
uv venv && uv pip install -e '.[dev]'
ln -sfn "$PWD/.venv/bin/ompwire" ~/.local/bin/ompwire
```

## Quick start

```bash
ompwire setup                    # find the agent, pick model, mode, thinking, cwd
ompwire chat                     # talk to it from this terminal right away
```

To reach it from a chat app, add a gateway. With Telegram:

```bash
ompwire gateway add telegram     # token (checked with getMe), then your user id
ompwire gateway start telegram   # write the systemd unit, enable it, start it
ompwire doctor                   # agent, store and credentials, one line per check
```

Then send `/start` to the bot. Only user ids on the allow list get an answer. Everyone else is
ignored without a reply.

## Gateways

A gateway is one bot on one platform, with its own token, allow list and systemd unit. One
machine can run several, and stopping one leaves the others running.

| Command | What it does |
| --- | --- |
| `ompwire gateway list` | name, platform, bot identity, allow list, unit state |
| `ompwire gateway add NAME` | read the token, verify it, ask who may use it |
| `ompwire gateway start NAME` | write the unit from this machine's paths, enable and start it |
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
Each unit is capped at `MemoryMax=4G`. `gateway add` refuses a platform that is not
implemented yet, so nothing half-configured is ever written.

## In a chat app

The commands and behaviour below are the Telegram gateway's. A future chat platform would
follow the same rules; the terminal has its own smaller set, described under
[Terminal](#terminal).

| Command | What it does |
| --- | --- |
| `/new`, `/sessions`, `/resume <id>`, `/continue` | session lifecycle |
| `/fork` | branch the current session |
| `/model`, `/mode`, `/thinking` | switch with an inline keyboard |
| `/cwd <path>` | start a session in another directory |
| `/status`, `/usage`, `/context` | what is running, what the session cost, what fills the context |
| `/compact` | run OMP's compaction now |
| `/compaction [10-95\|off\|on]` | show or set OMP's auto-compaction threshold |
| `/queue <text>`, `/steer <text>` | queue a turn, or add to the running one |
| `/stop`, `/kill` | cancel the turn, or stop this chat's agent process |
| `/approve`, `/verbose`, `/yolo` | approval policy, tool trace, approvals off for this chat |
| `/reasoning [on\|off]` | show the agent's thinking above the answer |
| `/skills [query]`, `/commands` | what the agent can do |
| `/schedule [run\|rm <name>]` | list scheduled turns, run one now, remove one |
| `/login` | sign in to a model provider |
| `/help`, `/whoami` | help, ids |

Any other slash command is forwarded to the agent as a prompt.

### While the agent is working

A message sent mid-turn is steered into the running turn: the agent reads it at its next step,
and the reply says `↪️ steered`. When OMP declines it, or when `[telegram] busy_message =
"queue"` is set, the message is queued and the reply says its place (`📥 queued #2`). `/queue`
always queues. `/stop` cancels the turn.

### Questions

The agent asks by ending a reply with `<<options: a | b>>`. The line becomes buttons, plus
✏️ Other answer and ⏭️ Skip. While the question is open:

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

The agent runs with `approval_mode = write`. Reads and file writes are free; a shell command
that can change something sends a request with Allow once, Always allow, Deny and Always deny.
Commands that only read are let through: the command is tokenised, a pipeline counts as
read-only only when every part is, and a redirect is allowed only into `/dev/null`.

`write` is the one OMP mode that both lets the agent work and reaches a person. Under
`always-ask` OMP denies writes without sending a request, so no button ever appears. `yolo`
asks nothing; `/yolo` turns it on for one chat. An unanswered request is denied after
`[approval] timeout` (120 seconds by default).

The appended system prompt sets the scope: a question gets a read-only answer, a clone goes to
`/tmp` or `~/.cache`, and anything that touches production stops so the agent can say what it
would run.

## Schedules

A schedule runs one turn on a timer and exits. Nothing of ompwire stays alive between runs.

```bash
ompwire schedule add morning --cron "0 7 * * *" --prompt "summarise last night's repo activity"
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

## Thinking and subagents

`/reasoning on` shows the agent's thinking above the answer in a collapsed block.
`[stream] show_reasoning` sets the default. Subagents need no switch: OMP runs them through its
`task` tool, and the trace labels that call `subagent` with its name and progress.

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
| `~/.config/ompwire/config.toml` | agent settings |
| `~/.config/ompwire/gateways.toml` | one block per gateway: platform, allow list, home channel |
| `~/.config/ompwire/.env` | one credential per gateway (the bot token for Telegram), mode `0600` |

Precedence: defaults, then `config.toml`, then environment variables. Sections: `[acp]`,
`[agent]`, `[session]`, `[stream]`, `[approval]`, `[files]`, `[logging]`, `[mcp]`,
and one per platform (`[telegram]`). `ompwire config` prints the merged result with secrets masked.
`OMPWIRE_CONFIG_DIR` points everything at another directory.

`ompwire setup` can be run again: every prompt is pre-filled with what is on disk.

`[mcp] source = "mcp-hub"` reads a shared `servers.yaml`; `exclude` and
`[mcp.overrides.<name>]` adjust it per machine.

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

`uninstall` removes only what ompwire created: `~/.config/ompwire`, `~/.local/state/ompwire`,
`~/.local/share/ompwire` and the package. `~/.omp` is never touched.

## When it goes quiet

Start with `ompwire doctor`; it exits non-zero when something is broken. `ompwire status`
reports without a handshake.

| Symptom | Cause |
| --- | --- |
| The bot never answers, no error | your user id is not on the allow list (`gateway list` shows it) |
| `gateway add` says the token is rejected | Telegram: revoked in @BotFather; use `--force` with the new one |
| `409 Conflict` in the journal | Telegram: a second process polls the same token |
| `start` says systemd --user is unavailable | no user manager (container, CI) |
| An answer stops mid-turn | `journalctl --user -u ompwire-<name> -f` |

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): the seams, the ACP surface used, decisions and their costs.
- [RULES.md](RULES.md): how to change this repo without breaking a running install.
- [SECURITY.md](SECURITY.md): threat model and how to report a problem. Never a public issue.
- [CONTRIBUTING.md](CONTRIBUTING.md): setup, adding a transport, releases.

## License

MIT. See [LICENSE](LICENSE).
