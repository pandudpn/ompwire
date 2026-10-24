# Security

## Reporting

Do not open a public issue for a security problem.

Use GitHub's private report: **Security** tab, then **Report a vulnerability** on
[github.com/pandudpn/ompwire](https://github.com/pandudpn/ompwire/security/advisories/new). If
that is not possible, email `pandu@pandudpn.id` with `ompwire security` in the subject.

Expect an acknowledgement within a few days. This is a single maintainer project with no
on-call rotation, so please do not assume a fix window. You will get an honest answer about
whether the report is in scope, and credit in the advisory unless you ask otherwise.

## What is supported

The newest release tag only. `install.sh` resolves that tag by default, and older tags are not
patched. If you are running `main`, you are testing unreleased code.

## The threat model, as designed

ompwire runs a personal agent on your own machine and exposes it to one chat platform. It is
single user on purpose, and the boundaries follow from that:

- **One allow list per gateway.** Every update is dropped unless the sender's user id is in
  `allowed_users`; strangers get no reply, not a refusal.
- **The bot token lives in `~/.config/ompwire/.env`**, mode `0600`, loaded by systemd through
  `EnvironmentFile`. It is never written into the unit file, the config, or the repo.
- **Logs are redacted before they are written.** `logs.py` masks token-shaped strings on the
  way in; `ompwire scrub-logs` cleans files written before that filter existed.
- **No inbound network.** The Telegram transport uses long polling, so there is no port to
  forward, no webhook secret to hold, and nothing listening.
- **No telemetry.** Nothing is sent anywhere except to Telegram and to the model provider the
  agent talks to.
- **The agent holds the credentials.** ompwire has no provider API keys; OMP authenticates
  itself, and its store is outside this repo's reach.

## In scope

- A bot token, chat id or other secret written to a log, a unit file, an error message or a
  crash trace.
- An allow-list bypass: any path where an update from a user not in `allowed_users` reaches the
  agent.
- Command injection or path traversal through configured values: `cwd`, `home_channel`, gateway
  names, MCP hub entries.
- Content that renders as HTML or a link the agent did not intend, in a way that could be used
  against the reader.
- A dependency or install-script flaw that gives remote code execution.

## Out of scope

- What the agent does when you ask it to do it. It runs with your shell, your files and your
  credentials by design; approvals and `yolo` are the control, and that control is yours.
- Anyone you put on the allow list. Adding a user id is handing over the agent.
- Denial of service against your own machine (a runaway turn, a huge context, a model bill).
- Physical or local access to a machine where the config and `.env` are readable.

## If a token leaks

Revoke first, then repair the local copy. A revoked token is worthless in five seconds; a
`git filter-repo` marathon is not.

```bash
# 1. revoke in @BotFather, then
ompwire gateway add telegram --force     # verifies the new token with getMe
ompwire gateway start telegram
ompwire scrub-logs                       # clean any log file the old token reached
```

If the token reached a git history you push, treat the bot as compromised, not just the token:
whoever used it read your conversations and could act as you until you revoked it.
