#!/usr/bin/env bash
# Install ompwire (and omp, if it is missing) with one command.
#
#   curl -fsSL https://raw.githubusercontent.com/pandudpn/ompwire/main/install.sh | bash
#
# or run it from a clone:  ./install.sh
#
# Flags:
#   --ref <main|vX.Y.Z>   what to install (default: newest release tag)
#   --no-setup            skip the interactive `ompwire setup` at the end
#   --no-omp              do not offer to install the agent
#   --no-path             leave the shell config alone
#   -h, --help            this text
#
# Environment equivalents: OMPWIRE_REF, OMPWIRE_NO_SETUP, OMPWIRE_NO_OMP,
# OMPWIRE_NO_PATH_EDIT.
set -eu

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ref)      OMPWIRE_REF="${2:?--ref needs a value}"; shift 2 ;;
        --no-setup) OMPWIRE_NO_SETUP=1; shift ;;
        --no-omp)   OMPWIRE_NO_OMP=1; shift ;;
        --no-path)  OMPWIRE_NO_PATH_EDIT=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) printf 'unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

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

# 3. the command has to be reachable now and in every future shell
BIN_DIR="${HOME}/.local/bin"
case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) PATH="${BIN_DIR}:${PATH}"; export PATH ;;
esac

shell_rc() {
    # the file an interactive shell of this user will actually read
    shell_name=$(basename "${SHELL:-sh}")
    case "${shell_name}" in
        zsh)  printf '%s\n' "${ZDOTDIR:-${HOME}}/.zshrc" ;;
        bash) if [ -f "${HOME}/.bashrc" ]; then printf '%s\n' "${HOME}/.bashrc";
              else printf '%s\n' "${HOME}/.bash_profile"; fi ;;
        fish) printf '%s\n' "${HOME}/.config/fish/config.fish" ;;
        *)    printf '%s\n' "${HOME}/.profile" ;;
    esac
}

ensure_path() {
    [ "${OMPWIRE_NO_PATH_EDIT:-0}" = "1" ] && {
        say "--   OMPWIRE_NO_PATH_EDIT=1: not touching your shell config"
        return 0
    }
    rc=$(shell_rc)
    case "${rc}" in
        *config.fish) line='fish_add_path $HOME/.local/bin' ;;
        *)            line='export PATH="$HOME/.local/bin:$PATH"' ;;
    esac
    if [ -f "${rc}" ] && grep -qF '.local/bin' "${rc}" 2>/dev/null; then
        say "ok   PATH already set in ${rc}"
        return 0
    fi
    mkdir -p "$(dirname "${rc}")" 2>/dev/null || true
    {
        printf '\n# ompwire: put ~/.local/bin on PATH (added by the ompwire installer)\n'
        printf '%s\n' "${line}"
    } >> "${rc}" || {
        say "FAIL could not write ${rc}; add this line yourself:"
        say "     ${line}"
        return 1
    }
    say "ok   added ~/.local/bin to PATH in ${rc}"
}

ensure_path || true
if ! have ompwire; then
    say "FAIL ompwire was installed but the command is still not reachable."
    say "     open a new shell, then run:  ompwire setup"
    exit 1
fi

say "ok   ompwire $(ompwire -V 2>/dev/null | awk '{print $2}') at $(command -v ompwire)"
if [ "${OMPWIRE_NO_PATH_EDIT:-0}" != "1" ]; then
    say "     this shell picks it up after:  hash -r   (or open a new shell)"
fi

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
