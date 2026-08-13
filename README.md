# Cinema Friend

A Telegram bot that watches BFI IMAX seating and messages you when good seats appear. You
tell it a film, a date range, a time window and how many seats you need; it checks the
public BFI listing on a schedule and sends you a link to the seat map when a block of
adjacent seats that matches shows up. It never buys anything. You follow the link and
complete the purchase yourself.

It runs as a single-user background service on macOS under `launchd`.

---

## Requirements

- macOS with `launchd` (the deployment script targets user agents in `~/Library/LaunchAgents`)
- Python 3.12 or newer
- A Telegram account

---

## Install

```sh
git clone https://github.com/ncksol/cinema-friend.git cinema-friend
cd cinema-friend

python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

The editable install puts two commands in `.venv/bin`:

| Command | Purpose |
| --- | --- |
| `cinema-friend` | the service itself |
| `cinema-friend-smoke` | the one-off BFI contract check described below |

Everything else in this guide assumes you are in the checkout and calling those commands
by their full path (`.venv/bin/cinema-friend`), so nothing depends on an activated shell.

---

## Configure

### 1. Create a bot

Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and follow the
prompts. It replies with a token that looks like `123456789:AA...`. That token is a
credential: anyone who has it can act as your bot.

### 2. Find your user ID

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric ID. The bot
ignores every message from anyone not on this list, so add the IDs of everyone who should
be able to see and edit your watches, and nobody else.

### 3. Write the env file

```sh
mkdir -p ~/.config/cinema-friend
cp .env.example ~/.config/cinema-friend/cinema-friend.env
chmod 600 ~/.config/cinema-friend/cinema-friend.env
$EDITOR ~/.config/cinema-friend/cinema-friend.env
```

`chmod 600` is not optional. The service checks the file's ownership and permissions
before reading a byte of it, and refuses to start if any other account on the machine can
read it.

| Setting | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | comma-separated numeric IDs allowed to use the bot |
| `DATABASE_PATH` | SQLite file for watches, results and notification history; created on first run |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `BFI_IMPERSONATE_PROFILE` | `curl_cffi` browser profile; leave as `chrome` |

There is no `User-Agent` setting. The impersonation profile supplies a complete, coherent
browser header set, and overriding one header inside it would break the fingerprint it
exists to present.

---

## Check the BFI contract before you deploy

Cinema Friend reads a public page that BFI never promised to keep stable. Before you leave
it running unattended, confirm that the page still looks the way the parsers expect:

```sh
.venv/bin/cinema-friend-smoke \
  'https://whatson.bfi.org.uk/imax/Online/default.asp?BOparam::WScontent::loadArticle::permalink=dog-stars'
```

Substitute any current BFI IMAX film URL: `dog-stars` is only an example, and a film that
has finished its run will have no performances on sale.

This makes one film-page request, follows that film's pagination chain, and fetches the
seat map for a single on-sale performance. It does not scan the programme, does not poll,
does not touch your database, and writes nothing. Run it by hand when you want it; it is
not part of the test suite.

It prints the impersonation profile it used, how many documents it fetched, how many
performances and seats it parsed, and the seat statuses it saw:

```
profile: chrome
film: dog-stars
article pages fetched: 2
performances parsed: 37
eligible performances: 12
...
contract: OK
```

The last line is the verdict. Exit codes:

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | contract holds | deploy |
| `2` | the URL was not a BFI IMAX film page | check the URL you pasted |
| `3` | BFI presented a challenge, or refused repeatedly | wait and try again later; do not retry in a loop |
| `4` | the page parsed, but not the way the code expects | the contract has drifted; see below |
| `5` | the network failed | check your connection, try again |

Exit code 4 is the one that matters. It means BFI changed the page and the service's
reading of it is now wrong. The message says which check failed: a missing field, an
unrecognised seat status, an accessible space that no longer reads as restricted, or a
mismatch between the availability count BFI reports and the number of available seats in
the seat map. Do not deploy against a drifted contract. A parser that silently mis-reads a
seat map is worse than no bot, because it will confidently tell you seats exist when they
do not. The fix is a code change, not a configuration one.

Exit code 3 is not a defect. BFI puts a challenge in front of clients it does not like, and
this project does not answer, bypass or automate challenges. It backs off. If the smoke
test is challenged persistently rather than occasionally, the public route is no longer
usable and no amount of retrying will change that.

---

## Deploy

Install the LaunchAgent using the same virtual environment you installed the project into.
The script reads the interpreter you invoke it with and wires the agent to that
environment's `cinema-friend`, so run it with `.venv/bin/python`:

```sh
.venv/bin/python scripts/install_launch_agent.py install \
  --env-file ~/.config/cinema-friend/cinema-friend.env
```

That writes `~/Library/LaunchAgents/com.ncksol.cinema-friend.plist`, creates
`~/Library/Logs/cinema-friend/`, and loads the agent. The service starts immediately, starts
again at every login, and is restarted if it exits. Re-run the same command after upgrading
or moving the checkout; it replaces the existing agent rather than failing.

Options:

| Flag | Default |
| --- | --- |
| `--env-file` | required |
| `--working-directory` | your home directory |
| `--log-dir` | `~/Library/Logs/cinema-friend` |

The env file is validated before anything is written, so an installation that refuses
leaves nothing behind to clean up.

### Confirm it is running

```sh
launchctl print "gui/$(id -u)/com.ncksol.cinema-friend"
```

Look for `state = running` and a `pid`. A `last exit code` that keeps changing means the
service is crash-looping; the reason will be in the error log.

### One copy at a time

Only one process may run against a given `DATABASE_PATH`. On startup the service takes an
exclusive lock on `<DATABASE_PATH>.lock` and holds it until it exits. A second copy stops
immediately, before it polls Telegram or asks BFI for anything:

```
another cinema-friend process is already running against this database (pid 4821);
lock: /Users/you/.local/state/cinema-friend/cinema-friend.db.lock
```

That is worth enforcing because the failure is otherwise quiet: two processes sharing one
bot token split incoming commands unpredictably between them, double the request rate at a
site that is already rate-limiting us, and send you every notification twice.

The lock is held by the kernel, not written down, so it is released however the process
ends, including a crash or `kill -9`. There is nothing to clean up by hand and no stale
lock to clear; the empty `.lock` file left behind is expected. If you genuinely want two
instances, give each its own `DATABASE_PATH`, and a separate bot token.

### Logs

```sh
tail -f ~/Library/Logs/cinema-friend/cinema-friend.out.log
tail -f ~/Library/Logs/cinema-friend/cinema-friend.err.log
```

Logs are JSON, one object per line, and carry watch and check correlation IDs so a single
check can be followed end to end. The bot token is redacted before anything is written, and
so is every URL query: BFI's paging URLs carry a session token, so a logged URL is cut
back to its host and path. Failure messages name the document that failed, not its address.
BFI page bodies and seat-map SVGs are never logged or stored: only parsed records, statuses
and byte counts. The same holds for the contract check in the terminal.

To read them comfortably, pipe through `jq`:

```sh
tail -f ~/Library/Logs/cinema-friend/cinema-friend.out.log | jq -r '"\(.level) \(.message)"'
```

### Stop, start, remove

```sh
launchctl kickstart -k "gui/$(id -u)/com.ncksol.cinema-friend"   # restart now

.venv/bin/python scripts/install_launch_agent.py uninstall       # unload and remove
```

`uninstall` unloads the agent and deletes the plist. It is safe to run when nothing is
installed, and it leaves your database, logs and env file untouched.

---

## Using the bot

Message your bot on Telegram and send `/help`.

| Command | What it does |
| --- | --- |
| `/new` | set up a new watch |
| `/cancel` | abandon a setup in progress |
| `/watches` | list your watches, with buttons to pause, resume or delete |
| `/check <number>` | check one watch right now |
| `/pause <number>` | stop scheduled checks for a watch |
| `/resume <number>` | start them again |
| `/delete <number>` | delete a watch (it asks you to confirm) |
| `/help` | the command list |

### Creating a watch

`/new` asks one question at a time:

1. **Film URL**: the BFI page for the film, e.g.
   `https://whatson.bfi.org.uk/imax/Online/article/dog-stars`
2. **Date range**: `2026-08-26 to 2026-08-30`
3. **Time window**: `18:00 to 22:30`, applied to each day in the range
4. **Seats**: `1` to `8`, chosen from buttons
5. **Seat selection**: Simple or Advanced
6. **Simple preference**: **Only the best** keeps the middle seating bank between the
   aisles from row J back; **Best and good** keeps the same middle bank from row C back.
   Seats outside the chosen preset are excluded.
7. **Advanced seat controls**: optional preferred and excluded rows and exact seats,
   using formats such as `L,M` and `L16-L22,M17`
8. **Preferred time**: a specific showing you would rather have, or `skip`
9. **One-off or recurring**: one-off checks until it finds something; recurring keeps
   checking on an interval
10. **Interval**: for recurring watches, at least 15 minutes

It shows you a summary and waits for you to confirm. Nothing is saved until you do, and a
half-finished setup survives a restart of the service.

### What you get back

When a check finds adjacent seats matching your criteria, you get one message listing the
options in preference order, each with a link straight to that performance's seat map. You
still choose seats and pay on the BFI site.

The first successful check for a recurring watch always replies. If it finds no matching
seats, the bot says it found nothing and will keep watching. Later scheduled checks that
find nothing new stay silent.

After that first reply, you are messaged again only when something appears that is
genuinely better than what you were last told about, so a recurring watch on a quiet film
is not repetitive. Manual `/check` requests still return the current result, even when
empty.

---

## When BFI stops answering

The site rate-limits and occasionally challenges automated clients. When requests start
failing, the service stops checking that host, tells you once:

> I can't get reliable answers from `whatson.bfi.org.uk`, so I've paused checks against it.
> I'll keep retrying and tell you when it's back.

and keeps retrying at a widening interval in the background. When a probe succeeds it
resumes on its own and tells you once more. You get one message per outage, not one per
watch, and there is nothing for you to do in between.

This is deliberate. The service never tries to answer, bypass or automate a challenge.
Impersonating a browser's TLS fingerprint to read a public page is the transport the site
requires of any client; defeating a challenge is a different thing, and this project does
not do it. If BFI protects the route permanently, the bot stays visibly degraded until
there is a supported data source to move to. It will not quietly start guessing.

---

## Data and backups

Everything the bot knows lives in the SQLite file at `DATABASE_PATH`: your watches, the
results it has found, and which notifications it has already sent. No credentials are
stored there. Beside it sits an empty `<DATABASE_PATH>.lock`, which exists only to hold the
single-instance lock: there is nothing in it to back up.

To back it up, stop the service first so nothing is mid-write:

```sh
launchctl bootout "gui/$(id -u)/com.ncksol.cinema-friend"
cp ~/.local/state/cinema-friend/cinema-friend.db ~/backups/cinema-friend-$(date +%F).db
.venv/bin/python scripts/install_launch_agent.py install \
  --env-file ~/.config/cinema-friend/cinema-friend.env
```

Restore by putting the file back with the service stopped.

To remove Cinema Friend completely, uninstall the agent and then delete the database, its
`.lock` file, the log directory, and the env file.

---

## Development

```sh
.venv/bin/python -m pytest          # full suite; makes no network requests
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy src scripts
```

Tests use synthetic HTML and SVG fixtures. `cinema-friend-smoke` is the only thing in the
repository that talks to BFI, and it is never run automatically.

---

## A caveat worth reading

This reads a public web page that has no API, no versioning and no compatibility promise.
BFI can change the markup at any time, and when they do this bot will stop working,
ideally loudly, via the smoke test's exit code 4 or a degraded-service message, but the
possibility of a subtle mis-read is real. Treat what it tells you as a prompt to go and
look at the BFI site, not as an authority on what is available.

It is also, unavoidably, an automated client on someone else's site. The request rates
here are deliberately low: at most two requests at a time, at least a second apart, a
15-minute floor on recurring checks, and no scanning of the wider programme, because
that restraint is the only thing that makes running it defensible. Do not raise those
limits.
