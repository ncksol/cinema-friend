# One-command Backend Installer Design

## Purpose

Cinema Friend currently requires separate commands to create a virtual environment,
install the package, write a private configuration file, and register the macOS
LaunchAgent. A root-level `install.sh` will turn an existing checkout into a running
backend with one terminal command:

```sh
./install.sh
```

The installer targets the existing single-user macOS deployment model. It does not change
the bot's runtime architecture.

## Goals

- Install and start the backend from an existing checkout with one command.
- Require no activated shell, administrator access, or manually assembled plist.
- Prompt securely for first-run Telegram configuration.
- Reuse an existing valid configuration without asking for secrets again.
- Make reruns safe for upgrades and interrupted installations.
- Report success only after `launchd` shows the service running.
- Keep configuration and LaunchAgent rules in their existing Python owners.

## Non-goals

- Cloning or updating the repository.
- Installing Python, Homebrew, or other system prerequisites.
- Supporting Linux, containers, cloud hosts, or system-wide services.
- Creating a Telegram bot or discovering a Telegram user ID.
- Running the live BFI contract smoke test.
- Replacing the existing uninstall command.
- Installing development and test dependencies.

## Interface and Responsibilities

`install.sh` lives at the repository root and supports the Bash version shipped with
macOS. It resolves the checkout from its own path, so its behavior does not depend on the
caller's current working directory.

The shell script owns only installation orchestration:

- platform and Python prerequisite checks;
- virtual-environment creation and package installation;
- first-run prompts and atomic env-file creation;
- calls into the existing LaunchAgent installer;
- post-install service verification and operator-facing messages.

Python remains authoritative for application behavior:

- `cinema_friend.app.load_settings` validates env-file ownership, permissions, syntax,
  and required settings;
- `scripts/install_launch_agent.py` resolves the installed executable, renders the plist,
  and performs `launchctl` operations.

The LaunchAgent installer will call `load_settings` before writing or loading a plist. It
will translate `InputError` into its existing concise `InstallError` surface. This keeps
configuration rules out of shell and ensures an invalid existing env file cannot disturb a
running service.

## Installation Flow

The script uses `set -euo pipefail`, quotes every path, and never enables command tracing.
It performs these steps in order:

1. Require Darwin and the standard macOS `launchctl`.
2. Resolve the repository root and required project files from the script location.
3. If `.venv/bin/python` exists, verify that it is Python 3.12 or newer. If `.venv` exists
   but is incomplete or incompatible, stop with repair instructions rather than deleting
   it.
4. If `.venv` does not exist, find `python3.12` or `python3`, verify its version, and create
   `.venv`.
5. Upgrade pip in the virtual environment and install the local project with runtime
   dependencies using a non-editable `pip install --upgrade .`. Rerunning the installer
   therefore deploys the checkout's current contents; later working-tree edits do not
   silently alter the running service.
6. Reuse `~/.config/cinema-friend/cinema-friend.env` when it already exists. Otherwise,
   create it through the first-run flow below.
7. Invoke `.venv/bin/python scripts/install_launch_agent.py install --env-file <path>`.
   The Python installer validates the complete configuration before changing `launchd`.
8. Poll `launchctl print` for up to ten seconds. Success requires a running state and a
   process ID. If the process never reaches that state, exit nonzero and print the
   `launchctl print` command and stderr log path needed to diagnose it.

The script prints each non-secret phase before performing it. Its final message identifies
the running service, configuration path, log directory, and existing uninstall command.

## First-run Configuration

The fixed configuration path is
`~/.config/cinema-friend/cinema-friend.env`. The script creates the parent directory with
mode `0700`, sets `umask 077`, and writes through a temporary file in that directory.

It prompts for:

- `TELEGRAM_BOT_TOKEN`, with terminal echo disabled;
- `TELEGRAM_ALLOWED_USER_IDS`, as a visible comma-separated list.

Empty answers are rejected immediately. The application loader performs the authoritative
numeric and required-value validation. The remaining values use the deployment defaults:

```dotenv
DATABASE_PATH=~/.local/state/cinema-friend/cinema-friend.db
LOG_LEVEL=INFO
BFI_IMPERSONATE_PROFILE=chrome
```

The completed temporary file is mode `0600` and is renamed atomically into place. A trap
removes an unfinished temporary file on error or interruption. The token is never passed as
a process argument, printed, or copied into the plist.

An existing env file is never edited or replaced. It is passed directly to the Python
installer, which rejects missing, non-regular, incorrectly owned, overly permissive, or
semantically invalid files before touching the LaunchAgent.

## Reruns and Failure Handling

Rerunning `./install.sh`:

- reuses a healthy `.venv`;
- reinstalls the checkout's current runtime package;
- validates and reuses the existing env file without prompting;
- replaces and reloads the existing LaunchAgent through the idempotent Python installer;
- repeats the running-state verification.

The installer does not silently delete or repair user state. A broken virtual environment,
invalid configuration, failed package install, `launchctl` error, or failed health check
produces a nonzero exit and a stage-specific message. Configuration is created atomically,
and all configuration validation completes before the LaunchAgent is modified. The
database, logs, env file, and existing uninstall behavior remain unchanged.

## Testing

No automated test invokes live Telegram, BFI, or the user's real `launchd` domain.

Pytest subprocess tests exercise `install.sh` with temporary homes, temporary checkout
fixtures, controlled stdin, and fake command boundaries. They cover:

- rejection on non-macOS systems and Python versions below 3.12;
- creation of a missing virtual environment and reuse of a valid one;
- refusal to delete or overwrite a malformed existing `.venv`;
- runtime-only, non-editable package installation;
- hidden token input and absence of the token from stdout and stderr;
- atomic creation and mode `0600` of a new env file;
- preservation of an existing env file without prompting;
- delegation to the Python LaunchAgent installer with the expected env path;
- successful running-state verification, bounded retries, and actionable failure output;
- idempotent reruns.

The existing LaunchAgent tests are extended to prove that malformed configuration is
rejected before plist creation or any `launchctl` call. `bash -n install.sh`, Ruff, mypy,
and the targeted pytest files provide the implementation checks.

## Documentation

The README installation path will tell users to obtain a BotFather token and Telegram user
ID, then run `./install.sh`. It will document the Python prerequisite, fixed paths,
idempotent rerun behavior, success output, logs, and the existing uninstall command.
Development setup remains separate and continues to install the `dev` extra explicitly.
