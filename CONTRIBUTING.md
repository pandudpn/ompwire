# Contributing

ompwire is small on purpose. The agent loop, the tools, the memory, context compaction and
the system prompt all live inside OMP; this repo only moves messages and renders them. Before
writing code, check that the change belongs to the transport. If it needs agent state, it
belongs upstream in [OMP](https://omp.sh).

## Setup

```bash
git clone https://github.com/pandudpn/ompwire.git
cd ompwire
make install              # uv venv + editable install + dev extras
ln -sfn "$PWD/.venv/bin/ompwire" ~/.local/bin/ompwire
```

You also need `omp` on `PATH`. The contract tests use the real binary and skip themselves when
it is absent, so a machine without an agent can still run everything else.

## Before you open a PR

```bash
make lint      # ruff
make test      # pytest
```

Both run in CI on Python 3.11, 3.12 and 3.13, plus a shell check on `install.sh` in both its
file and piped invocation styles. A red build usually means a real bug, not a formatting
argument: ruff is configured as a deliberate subset (bugs, imports, modernisation,
simplification), and rules that only argue about shape are off.

Three rules that matter more than style:

1. **Tests never touch the machine.** `tests/conftest.py` redirects every writable path (systemd
   units, `gateways.toml`, `overrides.json`, the lock, `sessions.db`) into a temporary
   directory. Without it, a test run once disabled a real unit and read a real chat override.
   If you add a code path that writes somewhere, add it to that fixture.
2. **No secrets in the tree.** Not in tests, not in fixtures, not in screenshots. Use synthetic
   ids and tokens.
3. **Explain the wire change.** If you touch `acp.py` or the agent surface, say what the agent
   returned. `tests/test_acp_contract.py` is there so drift shows up as a failing test instead of
   a broken chat.

## Commits and branches

- Branch off `main`, one concern per branch, descriptive name (`fix/gateway-start-lock`).
- Do not merge `main` into your branch to "update" it. Rebase or start over; the history stays
  readable and the diff stays yours.
- Commit subjects in the imperative, with a scope when it helps: `fix(service): ...`,
  `docs: ...`, `refactor(gateway): ...`. The body explains why, including what breaks otherwise.
- A PR that changes behaviour updates `README.md`. A PR that moves a boundary updates
  `ARCHITECTURE.md`.

## Adding a transport

A gateway is one bot on one platform with its own credential and its own systemd unit. The
seams are already there:

1. `gateways.KINDS` and `gateways.IMPLEMENTED` in `gateways.py`.
2. A `Surface` implementation in `engine.py`'s terms: `post`, `trace`/`reasoning`/`dropped`/`join`
   renderers, a `typing` context, and the button surfaces (`offer_options`, `ask_permission`,
   `offer_schedule`, `send_file`, `notice`). `telegram.TelegramSurface` is the reference; a rate
   limit raises `engine.FloodWait`, any other send failure returns None.
3. The transport module itself, following `telegram.py`: polling or webhook in, one
   `engine.ChatAgent` per conversation via a `chat_agent(config, chat_id, key, app)` factory,
   approvals and option answers routed back through the agent's `answer_permission` and
   `pending_options`.
4. A credential variable per gateway; `gateways.Gateway.token_var()` derives it.
5. Tests next to the existing ones. No network in tests.

A transport that cannot be started is worse than no transport: `gateway add` refuses anything
that is not in `IMPLEMENTED`.

## Releases

Nobody types a version three times. `scripts/release/version.py` owns it, and it is the only
place a version is written:

```bash
python -m scripts.release.version show            # what the files say
python -m scripts.release.version bump minor      # write 1.5.0 into both files
python -m scripts.release.version check v1.5.0    # the guard the workflow runs
```

Two ways to cut a release, both from the Actions tab or the terminal:

```bash
# 1. the button computes the tag for you
gh workflow run release -f bump=patch

# 2. or tag it yourself, after bumping
python -m scripts.release.version bump patch
git commit -am "chore(release): v$(python -m scripts.release.version show)"
git tag -a v1.5.0 -m "ompwire 1.5.0" && git push origin v1.5.0
```

The release body is not typed by hand either. `scripts/release/notes.py` reads the commits between
the previous tag and this one, parses the conventional subjects, and groups them into Breaking
changes, Features, Fixes, Performance, Refactoring, Documentation, Tests, Build and CI. Write
commits worth reading, because they become the release page:

```bash
python -m scripts.release.notes --to v1.5.0            # preview before tagging
```

`--generate-notes` is not used: it lists merged pull requests, and this repository commits
straight to main, so it would publish a body with nothing but a changelog link.

Either way one workflow run does the rest: refuse a tag that disagrees with `pyproject.toml` or
`src/ompwire/__init__.py`, run lint and the suite, install from the tag with `install.sh` and
compare the reported version, build `sdist` and wheel once, `twine check` them, install the wheel
in a clean venv, publish the checksums, and create the GitHub release with generated notes.
Artifacts are attached, so the release page has the exact files that were checked.

A version with a suffix (`v1.5.0-rc.1`) is published as a prerelease. `install.sh` only resolves
`^vX.Y.Z$`, so a prerelease is never what `curl … | bash` picks up.

`gh workflow run release -f bump=patch` commits the bump and the tag itself. That push does not
trigger a second run, which is why the same run also builds and publishes: the alternative is a
version bump sitting on `main` with no release behind it.

## Questions

Open a discussion or an issue. For anything that looks like a security problem, read
[SECURITY.md](SECURITY.md) first and do not open a public issue.
