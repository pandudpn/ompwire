# ompwire

Telegram as a front end for an [OMP](https://omp.sh) agent.

OMP keeps the memory, the skills, the tools and the agent loop. ompwire is the transport: it
takes a message from Telegram, sends it to `omp --mode acp`, and streams the answer back
into the same message. One conversation, one session, one agent process.

Single user by design: no database server, no web UI, no second agent loop. The one thing
ompwire stores is a SQLite file, `~/.local/state/ompwire/sessions.db`, which maps a chat to a
session id.

```
Telegram  <->  ompwire  <->  omp --mode acp  ->  tools, skills, MCP servers
```

## What you get

- Answers stream as a native Telegram draft and land as one message; groups get the final text
  in one piece (`[stream] answer = "draft" | "final" | "edit"`)
- Tool calls go to their own trace message, so the answer is never clobbered
- Approvals arrive as buttons; `/approve` sets the policy, or run with `yolo` and skip them
- Sessions survive restarts; `/new` starts one, `/resume` picks an old one
- Model, mode and thinking level switch from chat
- Photos go straight to the agent; a PDF, zip or csv is saved and the agent gets the path
- The agent sends files back: a chart, a screenshot or a report arrives as a photo or a document
- One systemd unit per bot, so a second bot cannot take the first one down

## Requirements

- Linux with `systemd --user` (a gateway only runs as that unit)
- Python 3.11 or newer
- `omp` on `PATH`
- A bot token from [@BotFather](https://t.me/BotFather)

`omp --mode acp` is not in `omp --help`. It works, and `tests/test_acp_contract.py` asserts
the wire against the real binary, so an agent upgrade that changes something shows up as a
failing test instead of a broken chat.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/pandudpn/ompwire/main/install.sh | bash
```

The script installs `omp` if it is missing, installs ompwire from the newest release tag with
`uv` (or `pipx`), puts `~/.local/bin` on your shell path, then offers to run `ompwire setup`.
Flags: `--ref`, `--no-setup`, `--no-omp`, `--no-path`.

From a clone:

```bash
git clone https://github.com/pandudpn/ompwire.git
cd ompwire && ./install.sh
```

Working on ompwire itself, a checkout and a symlink is enough:

```bash
uv venv && uv pip install -e '.[dev]'
ln -sfn "$PWD/.venv/bin/ompwire" ~/.local/bin/ompwire
```

## Quick start

```bash
ompwire setup                    # find the agent, pick model, mode, thinking, cwd
ompwire gateway add telegram     # token (verified with getMe), then your user id
ompwire gateway start telegram   # write the systemd unit, enable it, start it
ompwire doctor                   # agent, store, credential, one line per check
```

`setup` configures the agent only. A bot connection is a gateway, and gateways have their own
command, so adding a second bot never means running the wizard again.

Then send `/start` to your bot. Only user ids on the allow list get an answer; everyone else
is ignored without a reply.

## Gateways

A gateway is one bot on one platform with its own credential, its own allow list and its own
systemd unit. One machine can run several.

```
$ ompwire gateway list
telegram   telegram  @vidura_omp_bot      users 749994884  active
telegram-2 telegram  credential rejected  users 42         no unit
```

| Command | What it does |
| --- | --- |
| `ompwire gateway list` | name, platform, bot identity, allow list, unit state |
| `ompwire gateway add NAME` | read the token, verify it, ask who may use it |
| `ompwire gateway start NAME` | write the unit from this machine's paths, enable and start it |
| `ompwire gateway stop NAME` | stop it and disable it, so it stays down after a reboot |
| `ompwire gateway restart NAME` | restart the unit |
| `ompwire gateway status [NAME]` | unit path, state, log command |
| `ompwire gateway logs NAME` | last 40 journal lines |
| `ompwire gateway remove NAME` | stop the unit, delete the unit, delete the definition |
| `ompwire gateway types` | which platforms exist and which are implemented |

There is no install step and no foreground mode: `start` is the install. It writes
`~/.config/systemd/user/ompwire-<name>.service`, runs `daemon-reload`, then `enable --now`.
Run it again after moving a checkout and the unit is rewritten to match.

The token is verified before anything is written, and `start` checks it again. A unit with
`Restart=always` on a revoked token restarts every five seconds, which looks like a crash
loop rather than the one-line fix it is.

WhatsApp is in the type list because the design is per platform, but that transport does not
exist yet: `gateway add whatsapp` refuses and writes nothing.

Several bots on one machine is cheap: each adds one poller process. What costs memory is
conversations, because every live chat owns its own `omp` subprocess. Each unit is capped at
`MemoryMax=4G` so one runaway conversation cannot take another bot down.

## Chat commands

| Command | What it does |
| --- | --- |
| `/new`, `/sessions`, `/resume <id>`, `/continue` | session lifecycle |
| `/fork` | branch the current session |
| `/model`, `/mode`, `/thinking` | switch, with an inline keyboard |
| `/cwd <path>` | start a session in another directory |
| `/status`, `/usage`, `/context` | what is running, what it cost, how full the context is |
| `/skills [query]`, `/commands` | what the agent can do |
| `/login` | authenticate a model provider |
| `/approve`, `/verbose`, `/yolo` | approval policy, tool trace, bypass approvals |
| `/queue <text>`, `/steer <text>` | queue the next turn, or steer the running one |
| `/schedule [run\|rm <name>]` | list scheduled turns, run one now, remove one |
| `/reasoning [on\|off]` | show the agent's thinking above the answer |
| `/stop`, `/kill` | cancel the turn, kill the agent process for this chat |
| `/help`, `/whoami` | what this thing is |

Anything else is forwarded to the agent as a prompt. `/queue` is a real queue: a second prompt
sent into a running turn cancels it on this agent version, so ompwire holds the text and sends
it after the turn ends.

## Schedules

A schedule runs one turn on a timer and exits. There is no daemon: between runs nothing of ompwire
is alive, and the cost of a schedule is a timer entry plus about a kilobyte of text.

```
ompwire schedule add morning --cron "0 7 * * *" --prompt "ringkas aktivitas repo semalam"
ompwire schedule run morning      # prove it now instead of waiting for 07:00
ompwire schedule list
ompwire schedule remove morning
```

The cron fields are read on `Asia/Jakarta` by default, because that is the clock the bot shows,
and the result goes to the gateway named in the schedule (default `telegram`). Each run reports as
a card: when it runs (in words, not cron), the model and thinking level, how long it took, which
tools it used, the tokens it cost, and the answer rendered as markdown. A failed run reports the
same way with the reason, so a 04:00 failure is visible in the chat instead of only in the journal. Each schedule is a
`oneshot` unit capped at 1 GB and 200% CPU with a 30 minute timeout, so one runaway turn cannot
disturb anything else on the machine. Runs are ephemeral and answer `cancelled` to any tool that
would need a human approval; `--yolo` per schedule opts out, deliberately.

You can also just ask in the chat ("every morning at 7, summarise the repo"). The agent proposes
it with a marker, the transport turns that into a confirmation card, and nothing is installed until
you press **Buat schedule**.

`--approval` decides what a run may do while nobody is looking, and the three names are measured
against omp rather than guessed: `ask` reads only (a write is refused), `write` also writes files
(an ordinary shell command is refused), `yolo` asks nothing. The default is `yolo`, because a
scheduled job that cannot run git or kubectl is a job that does nothing; use `--approval ask` for a
schedule that only reports.

## Approval

The agent runs with `approval_mode = write`: reads and file writes are free, and every shell
command sends a permission request into the chat with Allow once, Always allow, Deny and Always
deny. That is deliberate, and the mode names were measured rather than guessed. `always-ask` and
`always-ask-cached` deny a write without ever sending a request, so the button never appears and the
agent simply refuses; `write` is the only mode that both lets the agent work and reaches a human for
the part that can clone, delete, or deploy. `yolo` asks nothing at all, which is why it is off by
default and why `/yolo` is a per-chat decision, not a config default.

An unanswered request is denied after `[approval] timeout` (600s). `ompwire doctor` says which gate
is in force, and warns when yolo is on.

The appended system prompt carries the rest: a read/check/explain request is read-only, a clone goes
to `/tmp` or `~/.cache` and never the home directory, nothing outside the directory in question is
modified, and anything touching production stops the agent so it can say what it would run.

## Thinking

ACP carries the agent's reasoning as `agent_thought_chunk`. `/reasoning on` renders it above the
answer, in a collapsible block, keeping the last 1200 characters; `/reasoning off` (the default)
drops it. `ompwire setup` asks once and writes `[stream] show_reasoning`.

Subagents are visible without a switch: omp runs them through its `task` tool, so the trace labels
the call `subagent`, names it (`subagent: ReadNote (sonic)`), and shows what it is doing as the
progress text arrives. ACP has no event of its own for a subagent, which is why that is where they
appear. The call that only says it is waiting for the result is dropped: it adds a line and no
information.

## Configuration

| Path | Holds |
| --- | --- |
| `~/.config/ompwire/config.toml` | agent settings: model, mode, thinking, cwd, streaming, logging |
| `~/.config/ompwire/gateways.toml` | one block per gateway: platform, allow list, home channel |
| `~/.config/ompwire/.env` | secrets (`0600`): one bot token per gateway |

Precedence is defaults, then `config.toml`, then environment variables. Sections in
`config.toml`: `[acp]`, `[agent]`, `[stream]`, `[mcp]`, `[files]`, `[approval]`, `[logging]`.
`ompwire config` prints the merged result with secrets masked. `OMPWIRE_CONFIG_DIR` points
everything at another directory, for a second install or a throwaway experiment.

`ompwire setup` rewrites the file with your answers. Running it again pre-fills every prompt
with what is on disk, so pressing enter keeps a value and typing changes one thing.

MCP servers are not duplicated here. `[mcp] source = "mcp-hub"` reads a shared `servers.yaml`,
and `exclude` or `[mcp.overrides.<name>]` adjust it per machine without editing the shared file.

`[files]` sets the limits for attachments in both directions: `max_mb` (20 by default, applied to
every file either way) and `incoming_dir`, where documents sent into the chat are written
(`~/.local/state/ompwire/files` by default). There is no on/off switch: a screenshot, a PDF or a
file the agent produces travels without being configured first. The agent sends a file by naming it in the reply:

```
Here is the chart you asked for.
<<file: /home/you/report/chart.png>>
```

The line is removed before the message is shown. A path outside the agent's working directory, a
missing file, or one over the limit is refused, and the chat gets one short line saying which file
and why instead of silently dropping it.

## Update and remove

```bash
ompwire update              # newest release, restarts every active gateway
ompwire update --check      # say what would change, change nothing
ompwire update --edge       # track the tip of main
ompwire restart             # restart without updating
ompwire uninstall --dry-run # list what would be removed
```

`update` restarts the gateways it finds, because new code is worthless while the old process
is still polling. `--no-restart` leaves them alone.

`uninstall` removes only what ompwire created: `~/.config/ompwire`, `~/.local/state/ompwire`,
`~/.local/share/ompwire` and the installed package. `~/.omp` is never touched, so agent
sessions, credentials and MCP config survive.

## When it goes quiet

Start with `ompwire doctor`. It checks the agent, the store and each credential, and exits
non-zero when something is broken. `ompwire status` reports instead, with no handshake.

| Symptom | Cause |
| --- | --- |
| Bot never answers, no error | your user id is not on the allow list; `gateway list` shows who is |
| `gateway add` says the token is rejected | revoked in @BotFather, use `--force` |
| Two bots answer each other's messages | the same token in two gateways; one token is one poller |
| `409 Conflict` in the journal | a second process is polling the same token |
| `start` says systemd --user is unavailable | no user manager (container, CI); run `gateway _run NAME` under your own supervisor |
| An answer stops mid-turn | `journalctl --user -u ompwire-<name> -f` |

An empty allow list is the failure that looks like success: the bot starts, answers nobody and
logs nothing unusual.

## Development

```bash
make install   # venv plus dev extras
make test      # pytest
make lint      # ruff
make run       # start the telegram gateway
```

CI runs lint and tests on Python 3.11, 3.12 and 3.13, plus a shell check on `install.sh`.
Contract tests skip themselves when `omp` is absent, so CI stays meaningful without an agent.
`tests/conftest.py` redirects every writable path into a temporary directory, because a test
run once disabled a real systemd unit.

Architecture, boundaries and the reasoning behind them: [ARCHITECTURE.md](ARCHITECTURE.md), and
the same picture drawn with its failure modes at
[docs/ompwire-architecture.html](docs/ompwire-architecture.html).

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md),
never a public issue.

## License

MIT. See [LICENSE](LICENSE).
