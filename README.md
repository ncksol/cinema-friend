# Cinema Friend

A Telegram bot that watches BFI IMAX seating and messages you when good seats appear. You
tell it a film, a date range, a time window and how many seats you need; it checks the
public BFI listing on a schedule and sends you a link to the seat map when a block of
adjacent seats that matches shows up. It never buys anything. You follow the link and
complete the purchase yourself.

It runs as a single-user background service on macOS under `launchd`.

---

## Requirements

- macOS with `launchd`
- Python 3.12 or newer
- A Telegram account

The installer checks these prerequisites but does not install system software.

---

## Before you install

### 1. Create a bot

Message [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts. It
replies with a token that looks like `123456789:AA...`. Anyone holding that token can act
as your bot.

### 2. Find your user ID

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric ID. Gather
the IDs of everyone who should be allowed to see and edit watches.

---

## Install

From an existing checkout:

```sh
./install.sh
```

On the first run, the installer asks for the bot token without echoing it and then asks for
the comma-separated allowed user IDs. It creates:

- `.venv` in the checkout, containing the runtime installation;
- `~/.config/cinema-friend/cinema-friend.env`, mode `0600`;
- `~/Library/LaunchAgents/com.ncksol.cinema-friend.plist`;
- `~/Library/Logs/cinema-friend/`.

It loads the LaunchAgent and exits successfully only after `launchd` reports a running
process. Rerun the same command after updating the checkout. It reuses `.venv` and the
existing configuration, reinstalls the checkout's current code, and reloads the service.

The installer never replaces an existing configuration. If that file is invalid, it stops
before changing the LaunchAgent and reports the validation error.

---

## Check the BFI contract

Cinema Friend reads a public page that BFI never promised to keep stable. After installation
and whenever BFI behavior is in doubt, confirm that the page still looks the way the parsers
expect:

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

## Installation details

`install.sh` invokes the existing LaunchAgent installer with the checkout's virtual
environment:

```sh
.venv/bin/python scripts/install_launch_agent.py install \
  --env-file ~/.config/cinema-friend/cinema-friend.env
```

That lower-level command remains available for service-only reinstalls. It validates the
complete configuration before writing the plist, creates the log directory, and replaces a
previously loaded copy rather than failing.

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
3. **Time schedule**: choose **Same every day** or **Weekday + weekend**.
4. **Time window(s)**: enter one daily window, or separate Monday-Friday and
   Saturday-Sunday windows, such as `18:00 to 22:30`. Times are interpreted in
   Europe/London and may cross midnight, such as `22:00 to 01:00`; the showing's
   local start day selects the weekday or weekend window.
5. **Seats**: `1` to `8`, chosen from buttons
6. **Seat selection**: Simple or Advanced
7. **Simple preference**: **Only the best** keeps the middle seating bank between the
   aisles from row J back; **Best and good** keeps the same middle bank from row C back.
   Seats outside the chosen preset are excluded.
8. **Advanced seat controls**: optional preferred and excluded rows and exact seats,
   using formats such as `L,M` and `L16-L22,M17`
9. **Preferred time**: a specific showing you would rather have, or `skip`
10. **One-off or recurring**: one-off checks until it finds something; recurring keeps
    checking on an interval
11. **Interval**: for recurring watches, at least 15 minutes

It shows you a summary and waits for you to confirm. Nothing is saved until you do, and a
half-finished setup survives a restart of the service.

### What you get back

When a check finds adjacent seats matching your criteria, you get one message listing the
options in preference order, each with a link straight to that performance's seat map. You
still choose seats and pay on the BFI site.

When a recurring watch's creation check succeeds, it replies. If it finds no matching
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

Production installation intentionally excludes development tools. For a development
checkout:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

Then run:

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
