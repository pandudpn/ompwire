#!/bin/sh
# Install ompwire (and omp, if it is missing) with one command.
#
#   curl -fsSL https://raw.githubusercontent.com/pandudpn/ompwire/main/scripts/install.sh | sh
#
# Environment:
#   OMPWIRE_REF=main|vX.Y.Z   what to install (default: newest release tag)
#   OMPWIRE_NO_SETUP=1        skip the interactive `ompwire setup` at the end
#   OMPWIRE_NO_OMP=1          do not offer to install the agent
set -eu

REPO="pandudpn/ompwire"
GIT_URL="https://github.com/${REPO}.git"
OMP_INSTALL="curl -fsSL https://omp.sh/install | sh"

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "ompwire installer"

# 1. the agent comes first: the transport has nothing to drive without it
if have omp; then
    say "ok   agent: $(command -v omp)"
elif [ "${OMPWIRE_NO_OMP:-0}" = "1" ]; then
    say "--   agent missing, and OMPWIRE_NO_OMP=1 was set: continuing anyway"
else
    say "--   agent missing: omp is required before ompwire can run"
    say "     official installer: ${OMP_INSTALL}"
    if [ -t 0 ]; then
        printf '     install omp now? [Y/n]: '
        read -r answer || answer=""
        case "${answer}" in
            ""|y|Y|yes|YES) ;;
            *) say "     skipping the agent. Install omp, then re-run this script."; exit 1 ;;
        esac
    fi
    sh -lc "${OMP_INSTALL}"
    if have omp || [ -x "${HOME}/.local/bin/omp" ]; then
        say "ok   agent installed"
    else
        say "--   the installer finished but omp is not on PATH yet; open a new shell and re-run"
        exit 1
    fi
fi

# 2. the package, from the newest release unless told otherwise
REF="${OMPWIRE_REF:-}"
if [ -z "${REF}" ]; then
    REF=$(git ls-remote --tags --refs "${GIT_URL}" 2>/dev/null \
        | sed 's#.*refs/tags/##' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
        | sort -V | tail -1)
    [ -n "${REF}" ] || REF="main"
fi
SPEC="git+${GIT_URL}@${REF}"

if have uv; then
    say "ok   installing ompwire ${REF} with uv"
    uv tool install --force --from "${SPEC}" ompwire
elif have pipx; then
    say "ok   installing ompwire ${REF} with pipx"
    pipx install --force "${SPEC}"
else
    say "FAIL neither uv nor pipx is available."
    say "     uv:   curl -LsSf https://astral.sh/uv/install.sh | sh"
    say "     pipx: python3 -m pip install --user pipx && python3 -m pipx ensurepath"
    exit 1
fi

# 3. the command has to be reachable for the rest of this script and afterwards
BIN_DIR="${HOME}/.local/bin"
case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) PATH="${BIN_DIR}:${PATH}"; export PATH ;;
esac
if ! have ompwire; then
    say "--   ompwire was installed but is not on PATH yet."
    say "     add this to your shell config:  export PATH=\"\$HOME/.local/bin:\$PATH\""
    say "     then open a new shell and run:  ompwire setup"
    exit 0
fi

say "ok   ompwire $(ompwire -V 2>/dev/null | awk '{print $2}') at $(command -v ompwire)"

if [ "${OMPWIRE_NO_SETUP:-0}" = "1" ]; then
    say ""
    say "next:  ompwire setup"
    exit 0
fi

say ""
if [ -t 0 ]; then
    exec ompwire setup
fi

say "next:  ompwire setup      # terminal prompts need an interactive shell"
