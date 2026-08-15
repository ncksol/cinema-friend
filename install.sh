#!/bin/bash

set -euo pipefail

LABEL="com.ncksol.cinema-friend"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="${CINEMA_FRIEND_VENV_DIR:-$REPO_ROOT/.venv}"
VENV_PYTHON="$VENV_DIR/bin/python"
ENV_FILE="${CINEMA_FRIEND_ENV_FILE:-$HOME/.config/cinema-friend/cinema-friend.env}"
LOG_DIR="$HOME/Library/Logs/cinema-friend"
INSTALL_AGENT="$REPO_ROOT/scripts/install_launch_agent.py"
UNAME_BIN="${CINEMA_FRIEND_UNAME:-/usr/bin/uname}"
LAUNCHCTL_BIN="${CINEMA_FRIEND_LAUNCHCTL:-/bin/launchctl}"
ID_BIN="${CINEMA_FRIEND_ID:-/usr/bin/id}"
SLEEP_BIN="${CINEMA_FRIEND_SLEEP:-/bin/sleep}"
TEMP_ENV_FILE=""


die() {
    printf 'install error: %s\n' "$*" >&2
    exit 1
}


stage() {
    printf '==> %s\n' "$*"
}


require_supported_platform() {
    if [[ "$("$UNAME_BIN" -s)" != "Darwin" ]]; then
        die "Cinema Friend installation is supported only on macOS"
    fi
    if [[ ! -x "$LAUNCHCTL_BIN" ]]; then
        die "launchctl is unavailable at $LAUNCHCTL_BIN"
    fi
}


require_project_files() {
    [[ -f "$REPO_ROOT/pyproject.toml" ]] || die "pyproject.toml is missing from $REPO_ROOT"
    [[ -f "$INSTALL_AGENT" ]] || die "LaunchAgent installer is missing: $INSTALL_AGENT"
}


python_is_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
}


find_base_python() {
    local name
    local path
    for name in python3.12 python3; do
        path="$(command -v "$name" 2>/dev/null || true)"
        if [[ -n "$path" ]] && python_is_supported "$path"; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    return 1
}


prepare_virtualenv() {
    local base_python
    if [[ -e "$VENV_DIR" || -L "$VENV_DIR" ]]; then
        [[ -x "$VENV_PYTHON" ]] || \
            die "$VENV_DIR exists but has no executable bin/python; repair or remove it"
        python_is_supported "$VENV_PYTHON" || \
            die "$VENV_PYTHON must be Python 3.12 or newer; repair or remove $VENV_DIR"
        stage "Reusing $VENV_DIR"
        return
    fi

    if ! base_python="$(find_base_python)"; then
        die "Python 3.12 or newer is required; install it and rerun ./install.sh"
    fi
    stage "Creating $VENV_DIR"
    "$base_python" -m venv "$VENV_DIR"
    [[ -x "$VENV_PYTHON" ]] || die "virtual environment did not create $VENV_PYTHON"
}


install_package() {
    stage "Upgrading pip"
    "$VENV_PYTHON" -m pip install --upgrade pip
    stage "Installing Cinema Friend runtime"
    (
        cd "$REPO_ROOT"
        "$VENV_PYTHON" -m pip install --upgrade .
    )
}


cleanup_temp_config() {
    if [[ -n "${TEMP_ENV_FILE:-}" && -e "$TEMP_ENV_FILE" ]]; then
        rm -f "$TEMP_ENV_FILE"
    fi
}


ensure_config() {
    local config_dir
    local token
    local allowed_ids

    if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
        stage "Reusing $ENV_FILE"
        return
    fi

    config_dir="$(dirname "$ENV_FILE")"
    mkdir -p "$config_dir"
    chmod 700 "$config_dir"
    umask 077
    TEMP_ENV_FILE="$(mktemp "$config_dir/.cinema-friend.env.XXXXXX")"

    printf 'Telegram bot token: ' >&2
    if ! IFS= read -r -s token; then
        printf '\n' >&2
        die "could not read the Telegram bot token"
    fi
    printf '\n' >&2
    [[ -n "$token" ]] || die "Telegram bot token must not be empty"

    printf 'Allowed Telegram user IDs (comma-separated): ' >&2
    IFS= read -r allowed_ids || die "could not read allowed Telegram user IDs"
    [[ -n "$allowed_ids" ]] || die "allowed Telegram user IDs must not be empty"

    {
        printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
        printf 'TELEGRAM_ALLOWED_USER_IDS=%s\n' "$allowed_ids"
        printf 'DATABASE_PATH=~/.local/state/cinema-friend/cinema-friend.db\n'
        printf 'LOG_LEVEL=INFO\n'
        printf 'BFI_IMPERSONATE_PROFILE=chrome\n'
    } > "$TEMP_ENV_FILE"
    chmod 600 "$TEMP_ENV_FILE"
    mv "$TEMP_ENV_FILE" "$ENV_FILE"
    TEMP_ENV_FILE=""
    stage "Created $ENV_FILE"
}


install_launch_agent() {
    stage "Installing and loading the LaunchAgent"
    "$VENV_PYTHON" "$INSTALL_AGENT" install --env-file "$ENV_FILE"
}


service_is_running() {
    local output
    local uid
    uid="$("$ID_BIN" -u)"
    if ! output="$("$LAUNCHCTL_BIN" print "gui/$uid/$LABEL" 2>/dev/null)"; then
        return 1
    fi
    grep -Eq '^[[:space:]]*state = running[[:space:]]*$' <<< "$output" || return 1
    grep -Eq '^[[:space:]]*pid = [1-9][0-9]*[[:space:]]*$' <<< "$output" || return 1
}


verify_service() {
    local attempt=0
    local uid
    while [[ "$attempt" -lt 10 ]]; do
        if service_is_running; then
            return
        fi
        attempt=$((attempt + 1))
        if [[ "$attempt" -lt 10 ]]; then
            "$SLEEP_BIN" 1
        fi
    done

    uid="$("$ID_BIN" -u)"
    die "service did not reach a running state; inspect: launchctl print \"gui/$uid/$LABEL\"; log: $LOG_DIR/cinema-friend.err.log"
}


main() {
    [[ "$#" -eq 0 ]] || die "usage: ./install.sh"
    trap cleanup_temp_config EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    require_supported_platform
    require_project_files
    prepare_virtualenv
    install_package
    ensure_config
    install_launch_agent
    verify_service

    cleanup_temp_config
    trap - EXIT HUP INT TERM
    printf '\nCinema Friend is running.\n'
    printf 'Configuration: %s\n' "$ENV_FILE"
    printf 'Logs: %s\n' "$LOG_DIR"
    printf 'Uninstall: "%s" "%s" uninstall\n' "$VENV_PYTHON" "$INSTALL_AGENT"
}


if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
