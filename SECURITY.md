# Security

## Reporting

Do not open a public issue for a security problem.

Use GitHub's private report: **Security** tab, then **Report a vulnerability** on
[github.com/pandudpn/ompwire](https://github.com/pandudpn/ompwire/security/advisories/new). If
that is not possible, email `pandu@pandudpn.id` with `ompwire security` in the subject.

Expect an acknowledgement within a few days. This is a single-maintainer project with no
on-call rotation, so there is no promised fix window. You will get a straight answer about
whether the report is in scope, and credit in the advisory unless you ask otherwise.

## Supported versions

The newest release tag only. `install.sh` resolves that tag by default and older tags are not
patched. `main` is unreleased code.

## Threat model

ompwire exposes a personal agent on your own machine to the chat platforms you connect as
gateways, and to your own terminal through `ompwire chat`. It is single user on purpose, and the
boundaries follow from that. Every gateway gets the same rules below; Telegram and WhatsApp
are the ones implemented today.

**Who can talk to the agent**
- Every update is dropped unless the sender's user id is in the gateway's `allowed_users`
  (a Telegram user id, a WhatsApp phone number). Strangers get no reply, not a refusal.
- WhatsApp groups: only listed groups are read, only allowed numbers in them are answered, and
  `group_mode` limits it further (`mention` by default). Being in a group with the bot gives a
  stranger nothing.
- No inbound network. The Telegram gateway uses long polling and the WhatsApp gateway an
  outgoing websocket: no port, no webhook secret, nothing listening. A future gateway that
  needs a webhook is a change to this model and has to be documented here.
- `ompwire chat` is reachable only by whoever already has a shell as your user.

**What the agent may do without a person**
- `approval_mode = write`: shell commands that can change the machine need a button press.
- Read-only commands pass without asking. The command is tokenised, a pipeline passes only when
  every part is read-only, and a redirect only into `/dev/null`. Flags that make a reader write
  (`git --output`, `sort -o`, `rg --pre`, ...) are refused.
- An unanswered request is denied after `[approval] timeout`.
- Scheduled runs answer `cancelled` to every permission request unless the schedule opts into
  `yolo`.

**What leaves the machine**
- Outgoing files must resolve inside the agent's working directory, exist, fit `files.max_mb`,
  and at most five go out per reply. A model that read `~/.ssh` cannot post it by naming it.
- Nothing is sent anywhere except the connected chat platforms and the model provider OMP
  talks to. No telemetry.

**Secrets**
- Each gateway's token lives in its own `~/.config/ompwire/gateways/<name>/.env` (mode `0600`),
  loaded by that gateway's unit through `EnvironmentFile`. A gateway's process never holds
  another gateway's token, and tokens are never written into a unit, the config or the repo.
- The agent process starts without the harness's secrets: `child_env()` removes every bot token
  (`TELEGRAM_BOT_TOKEN`, every `OMPWIRE_GATEWAY_*_TOKEN`) and memory API keys before spawning
  `omp`, schedules included.
- A WhatsApp gateway has no token. Its credential is the linked-device session in
  `~/.local/state/ompwire/gateways/<name>/whatsapp.db`, created `0600` before whatsmeow opens it;
  nothing logs its contents, and the agent process never gets its path.
- Logs mask token-shaped strings before they are written; `ompwire scrub-logs` cleans older
  files.
- ompwire holds no provider keys. OMP authenticates itself.

**What ompwire touches in `~/.omp`**
- It reads OMP's session transcripts to compute `/usage` totals and compaction notices.
- `/compaction` edits only the `compaction:` block of `~/.omp/agent/config.yml`, atomically,
  after a one-time backup.
- `uninstall` never touches `~/.omp`.

## In scope

- A token, chat id or other secret written to a log, unit file, error message or crash trace.
- An allow-list bypass: any path where an update from a user not in `allowed_users` reaches the
  agent, including a WhatsApp group that is not listed or a member who is not allowed.
- A message the bot sends that mentions (tags) someone the agent did not name.
- An approval bypass: a command the read-only check lets through that can write, delete or
  run code.
- A `<<file:>>` path that escapes the working directory or the limits.
- A harness secret that reaches the agent process.
- Command injection or path traversal through configured values: `cwd`, `home_channel`, gateway
  names, MCP hub entries, schedule names.
- Markup the agent did not intend that renders as HTML or a link usable against the reader.
- A dependency or install-script flaw that gives remote code execution.

## Out of scope

- What the agent does when you ask it to. It runs with your shell, files and credentials by
  design; approvals and `yolo` are the control, and that control is yours.
- Anyone you put on the allow list. Adding a user id hands over the agent.
- Denial of service against your own machine: a runaway turn, a huge context, a model bill.
- Local access to a machine where the config and `.env` are readable.

## If a token leaks

Revoke first, then repair the local copy. For Telegram:

```bash
# 1. revoke in @BotFather, then
ompwire gateway start telegram           # answer yes to "enter a new one?"; checked with getMe
ompwire scrub-logs                       # clean any log file the old token reached
```

If the token reached a git history you push, treat the bot as compromised, not only the token:
whoever used it could read your conversations and act as you until it was revoked.

For WhatsApp the credential is the session file
`~/.local/state/ompwire/gateways/<name>/whatsapp.db`: whoever copies it acts as the linked
device. Remove the device on the phone (Settings > Linked devices), then
`ompwire gateway start <name> --relink`.
