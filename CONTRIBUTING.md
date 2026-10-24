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
2. The transport module itself, following `telegram.py`: long polling or webhook in, one
   `ChatSession` per conversation, streaming edits out, approvals as interactive replies.
3. A credential variable per gateway; `gateways.Gateway.token_var()` derives it.
4. Tests next to the existing ones. No network in tests.

A transport that cannot be started is worse than no transport: `gateway add` refuses anything
that is not in `IMPLEMENTED`.

## Releases

Releases are tags, because the install path is a git install:

```bash
# bump pyproject.toml and src/ompwire/__init__.py together, then
git tag -a vX.Y.Z -m "ompwire X.Y.Z"
git push origin vX.Y.Z
```

The tag triggers `.github/workflows/release.yml`, which refuses to publish if the tag and the
two version strings disagree, runs the suite, builds `sdist` and wheel, and creates the GitHub
release with generated notes and the artifacts attached. `install.sh` resolves the newest
`vX.Y.Z` tag, so a bad tag is an install-time bug for everyone.

## Questions

Open a discussion or an issue. For anything that looks like a security problem, read
[SECURITY.md](SECURITY.md) first and do not open a public issue.
