# Rules

How to change ompwire, for people and for coding agents. ARCHITECTURE.md explains why the code
is shaped this way; this file says what to do and what not to do.

## Scope

- ompwire is a transport. Agent behaviour (the loop, tools, memory, compaction, the system
  prompt) belongs in OMP. A change that needs agent state goes upstream as an issue.
- ompwire may surface and control what OMP owns (context numbers, the compaction threshold),
  never reimplement it.
- One person, one machine. Do not add multi-user machinery; ARCHITECTURE.md lists what that
  would really take.

## Verify every change

```bash
.venv/bin/python -m pytest -q -W error::ResourceWarning
.venv/bin/ruff check src tests scripts
```

- A bug fix comes with a test that fails on the old code. Check it by stashing the fix and
  running the test.
- A user-visible change is exercised for real: against the bot, `ompwire chat`, or the real
  `omp` binary. Passing tests are not proof that a chat works.
- A claim about the ACP wire is measured against `omp`, then written into ARCHITECTURE.md
  with the version it was measured on.

## Tests never touch the machine

`tests/conftest.py` points every writable path at a temporary directory: systemd units,
`gateways.toml`, `overrides.json`, locks, `sessions.db`, OMP's transcript directory and
OMP's `config.yml`. A test run once disabled a real unit, and a subagent once wiped live state.

- A new code path that reads or writes outside the repo is added to that fixture in the same
  change.
- Tests never call `ompwire gateway`, `update`, `uninstall`, `schedule` or `chat` for real.
- CI has no `omp`. A test that would spawn it fakes `ChatAgent.ensure` or the client.
- No network in tests.

## On a machine where a gateway runs

- Restart through a delayed timer, so the reply that announces the restart lands first:
  `systemd-run --user --on-active=35 --unit=ompwire-restart-<name> <script>`, where the script
  runs `ompwire gateway restart <name>`.
- Never delete `inflight` rows in `sessions.db`. Resuming a turn after a restart depends on
  them.
- An editable install follows the checkout. `ompwire update` without `--edge` checks out a
  release tag.

## Code

- Clean cutover: migrate every caller and delete the old path. No shims, aliases or
  compatibility re-exports.
- The engine (`engine.py`) speaks `Surface` and never a platform. Telegram details stay in
  `telegram.py` and `tg_html.py`.
- Markers (`<<options:>>`, `<<file:>>`, `<<schedule:>>`) count only at the start of a line.
- Comments explain what is not obvious from the code: a measured agent behaviour, a failure
  that happened. Plain voice, no narration of the change.
- Anything the person reads (token counts, times) goes through `format.py`: `58.6k`, WIB.

## Secrets

- No tokens, chat ids or credentials in code, tests, fixtures, logs or screenshots. Use
  synthetic values.
- The agent process gets the environment without the harness's secrets (`child_env`). A new
  secret variable is added to `HARNESS_SECRETS`.

## Commits and docs

- Conventional subject (`fix(telegram): ...`, `docs: ...`) and a body that says why and what
  broke. Release notes are generated from these commits.
- The maintainer commits straight to `main`; there is no dev branch. Contributors work on a
  branch, as CONTRIBUTING.md describes.
- Docs change with the code they describe: behaviour in README.md, a boundary or a wire fact in
  ARCHITECTURE.md, anything that widens what the bot can reach in SECURITY.md.
