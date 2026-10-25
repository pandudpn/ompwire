<h1 align="center">ompwire</h1>

<p align="center">
  <strong>Your coding agent, in the chat app already on your phone.</strong><br/>
  ompwire puts an <a href="https://omp.sh">OMP</a> agent behind a Telegram bot. You send a message, and
  the agent works on your own machine with your shell, your repos and your MCP servers.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="docs/guide.md">Guide</a> ·
  <a href="ARCHITECTURE.md">Architecture</a> ·
  <a href="#contributing">Contributing</a> ·
  <a href="https://github.com/pandudpn/ompwire/issues">Issues</a>
</p>

```
Telegram  ─┐
           ├─>  ompwire  <->  omp --mode acp  ->  tools, skills, MCP servers
terminal  ─┘
```

Ask from your phone, then approve the deploy when the agent asks. ompwire carries the
conversation between the chat and OMP, and nothing else: the agent loop, the tools, the
memory and compaction stay in OMP, so you get the same agent you run in a terminal.

## What you get

**Every step, as it happens.** The agent's text up to each tool call, then the call with its
output, each as its own message. You read what it ran and what came back, not only the final
answer.

**Talk while it works.** A message sent mid-turn is steered into the running turn, and the bot
quotes your message with `↪️ steered`. If it cannot be steered, it waits in line as
`📥 queued #2`.

**Questions with buttons.** When the agent needs a decision it offers choices. Tap one, type
your own, or skip. Anything else you send meanwhile waits (`⏸️ held #1`) instead of derailing
the question.

**Approvals that matter.** A shell command that can change the machine asks first: Allow
once, Always allow, Deny, Always deny. `git log`, `ls` and `rg` run without a tap, so you are not trained to
approve without reading.

**Sessions that survive.** Restart the gateway and the conversation is still there. A turn cut
by the restart is resumed when the gateway comes back.

**Context you can see.** `/usage` shows the whole session's tokens, the context window, where
OMP will compact it and when it last did. `/compaction 70` changes the threshold from the
chat.

**Files both ways.** Send a screenshot or a PDF to the agent. Ask for a chart and it arrives
as a photo.

**Scheduled turns.** "Every morning at 7, summarise last night's commits." The agent proposes
it, you confirm, and a systemd timer runs it and reports to the chat.

**A terminal too.** `ompwire chat` runs the same engine in your shell, no bot needed.

## Install

You need Linux with `systemd --user`, Python 3.11+, and a bot token from
[@BotFather](https://t.me/BotFather). The installer brings `omp` if it is missing.

```bash
curl -fsSL https://raw.githubusercontent.com/pandudpn/ompwire/main/install.sh | bash
```

Then:

```bash
ompwire setup                    # agent, model, thinking level, working directory
ompwire chat                     # try it in this terminal first

ompwire gateway add telegram     # bot token (checked with getMe) and your user id
ompwire gateway start telegram   # systemd unit, enabled and started
ompwire doctor                   # one line per check
```

Send `/start` to your bot. Only user ids on the allow list get an answer; everyone else gets
silence.

## Platforms

| Platform | Status |
| --- | --- |
| Telegram | ready: `ompwire gateway add telegram` |
| Terminal | ready: `ompwire chat` |
| WhatsApp | defined in the gateway list, not implemented yet |

The engine never talks to a platform directly, so a new one is a surface plus a gateway.
[Adding a transport](CONTRIBUTING.md#adding-a-transport) walks through it, and it is the most
useful contribution right now.

## Built for one person

ompwire runs your agent, with your credentials, on your machine. That shapes it:

- one allow list per bot; strangers get no reply at all
- long polling, so no port is opened and nothing listens
- the bot token never reaches the agent process, and logs mask it
- no telemetry: messages go to Telegram and to your model provider, nowhere else
- `~/.omp` stays OMP's; uninstall never touches it

The full threat model is in [SECURITY.md](SECURITY.md).

## Documentation

- [Guide](docs/guide.md): gateways, chat commands, approvals, schedules, the terminal,
  configuration, troubleshooting
- [Architecture](ARCHITECTURE.md): how a turn flows, the ACP surface used, decisions and
  their costs
- [Rules](RULES.md): how to change this repo without breaking a running install
- [Security](SECURITY.md): threat model and private reporting

## Contributing

Bug reports, ideas and pull requests are welcome.

- **Found a bug?** [Open an issue](https://github.com/pandudpn/ompwire/issues/new/choose). The
  bug template asks for `ompwire doctor` output, logs and versions, which is usually all that
  is needed.
- **Have an idea?** Open a feature request and say what you were trying to do. Ideas that need
  agent state belong in [OMP](https://github.com/can1357/oh-my-pi) itself, and that is fine to
  say in the issue.
- **Want to send code?** Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup and
  [RULES.md](RULES.md) for how changes are verified. Small fixes can go straight to a pull
  request; for anything larger, open an issue first so we agree on the shape.
- **Security problem?** Do not open a public issue. Report it privately as
  [SECURITY.md](SECURITY.md) describes.

```bash
git clone https://github.com/pandudpn/ompwire.git && cd ompwire
make install && make test && make lint
```

## License

MIT. See [LICENSE](LICENSE).
