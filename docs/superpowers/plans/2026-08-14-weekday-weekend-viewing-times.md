# Weekday and Weekend Viewing Times Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let new watches use either one daily viewing window or separate Monday-Friday and Saturday-Sunday windows without changing existing watches.

**Architecture:** Keep `WatchCriteria.time_from` and `time_to` as the required default/weekday window and add an optional atomic weekend override. Centralize local-date selection in `domain/time_window.py`, preserve old criteria JSON and wizard drafts through optional fields and an explicit draft flow marker, and route both matching and preferred-time validation through the shared selector.

**Tech Stack:** Python 3.12, frozen dataclasses, `zoneinfo`, python-telegram-bot 22, aiosqlite, pytest, Ruff, mypy

## Global Constraints

- Monday through Friday use the default window; Saturday and Sunday use the weekend override when present.
- The performance's Europe/London start date selects the window, including for a window that crosses midnight.
- `weekend_time_from` and `weekend_time_to` must both be present or both be absent.
- Existing watches with no weekend JSON fields keep one window for every day.
- Existing drafts already in `AWAIT_TIME_RANGE` keep the historical one-window path.
- New split schedules require both a weekday and a weekend window.
- Do not add saved-watch editing, disabled day categories, multiple windows, holiday rules, dependencies, or a SQLite migration.
- Preserve inclusive time bounds, existing midnight wrapping, and UTC storage for preferred instants.

---

## File Map

- `src/cinema_friend/domain/time_window.py`: select a default or weekend window from a London-local date.
- `src/cinema_friend/domain/watch.py`: own weekend-pair validation and preferred-instant validation.
- `src/cinema_friend/watches/criteria.py`: match performances against the effective window.
- `src/cinema_friend/storage/watch_repository.py`: encode and backward-compatibly decode weekend fields.
- `src/cinema_friend/telegram/wizard.py`: add schedule-choice states, prompts, draft compatibility, review copy, and preferred-time handling.
- `src/cinema_friend/telegram/rendering.py`: summarize uniform and split schedules in `/watches`.
- `README.md`: document the optional split schedule in `/new`.
- `tests/domain/test_time_window.py`, `tests/domain/test_watch.py`, `tests/watches/test_criteria.py`: domain behavior.
- `tests/storage/test_watch_repository.py`: criteria JSON compatibility.
- `tests/telegram/test_wizard.py`: state-machine and restart behavior.
- `tests/telegram/test_rendering.py`: compact watch-list output.

---

### Task 1: Domain Schedule Selection and Matching

**Files:**
- Modify: `src/cinema_friend/domain/time_window.py:10-26`
- Modify: `src/cinema_friend/domain/watch.py:14-65`
- Modify: `src/cinema_friend/watches/criteria.py:24-45`
- Test: `tests/domain/test_time_window.py`
- Test: `tests/domain/test_watch.py`
- Test: `tests/watches/test_criteria.py`

**Interfaces:**
- Produces: `DailyTimeWindow = tuple[time, time]`
- Produces: `window_for_local_date(local_date: date, *, default_window: DailyTimeWindow, weekend_window: DailyTimeWindow | None) -> DailyTimeWindow`
- Produces: `WatchCriteria.weekend_time_from: time | None`
- Produces: `WatchCriteria.weekend_time_to: time | None`
- Produces: `WatchCriteria.time_window_for(local_date: date) -> DailyTimeWindow`
- Consumes: existing `within_daily_window(time_from: time, time_to: time, value: time) -> bool`

- [ ] **Step 1: Write failing shared-selector tests**

Add imports and tests to `tests/domain/test_time_window.py`:

```python
from datetime import date, time

from cinema_friend.domain.time_window import (
    window_for_local_date,
    within_daily_window,
)


def test_uniform_schedule_uses_default_window_every_day() -> None:
    default = (time(18, 0), time(23, 0))

    assert window_for_local_date(
        date(2026, 8, 28),
        default_window=default,
        weekend_window=None,
    ) == default
    assert window_for_local_date(
        date(2026, 8, 29),
        default_window=default,
        weekend_window=None,
    ) == default


def test_split_schedule_uses_weekend_override_on_saturday_and_sunday() -> None:
    default = (time(18, 0), time(23, 0))
    weekend = (time(12, 0), time(16, 0))

    assert window_for_local_date(
        date(2026, 8, 28),
        default_window=default,
        weekend_window=weekend,
    ) == default
    for local_date in (date(2026, 8, 29), date(2026, 8, 30)):
        assert window_for_local_date(
            local_date,
            default_window=default,
            weekend_window=weekend,
        ) == weekend
```

- [ ] **Step 2: Write failing criteria-invariant and preferred-time tests**

Add to `tests/domain/test_watch.py`:

```python
@pytest.mark.parametrize(
    ("weekend_time_from", "weekend_time_to"),
    [(time(12, 0), None), (None, time(16, 0))],
)
def test_weekend_window_requires_both_bounds(
    weekend_time_from: time | None,
    weekend_time_to: time | None,
) -> None:
    with pytest.raises(InputError, match="weekend"):
        WatchCriteria(
            **_BASE,
            weekend_time_from=weekend_time_from,
            weekend_time_to=weekend_time_to,
        )


def test_preferred_instant_uses_the_window_for_its_london_date() -> None:
    split = {
        **_BASE,
        "weekend_time_from": time(12, 0),
        "weekend_time_to": time(16, 0),
    }

    WatchCriteria(
        **split,
        preferred_utc_instant=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(InputError, match="time"):
        WatchCriteria(
            **split,
            preferred_utc_instant=datetime(2026, 8, 29, 18, 0, tzinfo=UTC),
        )
```

`2026-08-29T12:00Z` is 13:00 Saturday in London and belongs to the weekend
window; `18:00Z` is 19:00 and does not.

- [ ] **Step 3: Write failing performance-matching tests**

Extend `criteria_for` in `tests/watches/test_criteria.py`:

```python
def criteria_for(
    *,
    date_from: date = date(2026, 8, 26),
    date_to: date = date(2026, 8, 27),
    time_from: time = time(0, 0),
    time_to: time = time(23, 59),
    weekend_time_from: time | None = None,
    weekend_time_to: time | None = None,
    quantity: int = 2,
) -> WatchCriteria:
    return WatchCriteria(
        source_url="https://whatson.bfi.org.uk/imax/Online/default.asp?doWork::WScontent::loadArticle=Load&BOparam::WScontent::loadArticle::article_id=2152D1E8-CFF7-419F-BE57-F51C1E490F24",
        slug=SLUG,
        date_from=date_from,
        date_to=date_to,
        time_from=time_from,
        time_to=time_to,
        quantity=quantity,
        mode=WatchMode.ONE_OFF,
        weekend_time_from=weekend_time_from,
        weekend_time_to=weekend_time_to,
    )
```

Add:

```python
def test_split_schedule_matches_each_day_category() -> None:
    criteria = criteria_for(
        date_from=date(2026, 8, 28),
        date_to=date(2026, 8, 30),
        time_from=time(18, 0),
        time_to=time(23, 0),
        weekend_time_from=time(12, 0),
        weekend_time_to=time(16, 0),
    )

    assert performance_matches(criteria, performance_at("2026-08-28T19:00:00+01:00"))
    assert performance_matches(criteria, performance_at("2026-08-29T13:00:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-29T19:00:00+01:00"))


def test_split_wrapping_schedule_is_selected_by_performance_start_day() -> None:
    criteria = criteria_for(
        date_from=date(2026, 8, 28),
        date_to=date(2026, 8, 29),
        time_from=time(22, 0),
        time_to=time(1, 0),
        weekend_time_from=time(12, 0),
        weekend_time_to=time(16, 0),
    )

    assert performance_matches(criteria, performance_at("2026-08-28T23:30:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-29T00:30:00+01:00"))
```

- [ ] **Step 4: Run the focused tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest \
  tests/domain/test_time_window.py \
  tests/domain/test_watch.py \
  tests/watches/test_criteria.py -q
```

Expected: failures for missing weekend fields, selector, and split matching.

- [ ] **Step 5: Implement the shared selector**

In `src/cinema_friend/domain/time_window.py`, import `date` and define:

```python
type DailyTimeWindow = tuple[time, time]


def window_for_local_date(
    local_date: date,
    *,
    default_window: DailyTimeWindow,
    weekend_window: DailyTimeWindow | None,
) -> DailyTimeWindow:
    if local_date.weekday() >= 5 and weekend_window is not None:
        return weekend_window
    return default_window
```

Keep `within_daily_window` unchanged.

- [ ] **Step 6: Extend `WatchCriteria` and preferred-time validation**

In `src/cinema_friend/domain/watch.py`, append these fields after
`preferred_utc_instant` to preserve the existing positional field order:

```python
weekend_time_from: time | None = None
weekend_time_to: time | None = None
```

Import `DailyTimeWindow` and `window_for_local_date`, then add:

```python
def time_window_for(self, local_date: date) -> DailyTimeWindow:
    weekend_window = (
        (self.weekend_time_from, self.weekend_time_to)
        if self.weekend_time_from is not None and self.weekend_time_to is not None
        else None
    )
    return window_for_local_date(
        local_date,
        default_window=(self.time_from, self.time_to),
        weekend_window=weekend_window,
    )
```

At the start of `__post_init__`, enforce:

```python
if (self.weekend_time_from is None) != (self.weekend_time_to is None):
    raise InputError("weekend time range requires both start and end")
```

Replace preferred-instant use of `self.time_from` and `self.time_to` with:

```python
time_from, time_to = self.time_window_for(local.date())
if not within_daily_window(time_from, time_to, local.time()):
    raise InputError("preferred_utc_instant time is outside the watch time range")
```

- [ ] **Step 7: Route performance matching through the criteria method**

In `src/cinema_friend/watches/criteria.py`, replace the direct field lookup with:

```python
time_from, time_to = criteria.time_window_for(local_start.date())
return _matches_date_window(criteria, local_start.date()) and within_daily_window(
    time_from,
    time_to,
    local_start.time(),
)
```

- [ ] **Step 8: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest \
  tests/domain/test_time_window.py \
  tests/domain/test_watch.py \
  tests/watches/test_criteria.py -q
```

Expected: all pass.

Commit:

```bash
git add src/cinema_friend/domain/time_window.py \
  src/cinema_friend/domain/watch.py \
  src/cinema_friend/watches/criteria.py \
  tests/domain/test_time_window.py \
  tests/domain/test_watch.py \
  tests/watches/test_criteria.py
git commit -m "feat: select viewing windows by day type"
```

---

### Task 2: Criteria JSON Compatibility

**Files:**
- Modify: `src/cinema_friend/storage/watch_repository.py:22-74`
- Test: `tests/storage/test_watch_repository.py`

**Interfaces:**
- Consumes: `WatchCriteria.weekend_time_from` and `weekend_time_to` from Task 1.
- Produces: criteria JSON keys `weekend_time_from` and `weekend_time_to`, each an ISO time string or `null`.
- Compatibility: missing keys decode as `None`; a partial pair reaches `WatchCriteria` and raises `InputError`.

- [ ] **Step 1: Write failing round-trip and compatibility tests**

Add to `tests/storage/test_watch_repository.py`:

```python
async def test_weekend_time_override_round_trips(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch(
        criteria=_criteria(
            weekend_time_from=time(12, 0),
            weekend_time_to=time(16, 0),
        )
    )

    await repo.create(conn, watch)

    assert await repo.get(conn, watch.watch_id) == watch


async def test_uniform_schedule_writes_null_weekend_bounds(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)

    cursor = await conn.execute(
        "SELECT criteria_json FROM watches WHERE id = ?",
        (str(watch.watch_id),),
    )
    row = await cursor.fetchone()

    assert row is not None
    payload = json.loads(row["criteria_json"])
    assert payload["weekend_time_from"] is None
    assert payload["weekend_time_to"] is None


async def test_legacy_criteria_without_weekend_bounds_keep_the_default_window(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)
    cursor = await conn.execute(
        "SELECT criteria_json FROM watches WHERE id = ?",
        (str(watch.watch_id),),
    )
    row = await cursor.fetchone()
    assert row is not None
    payload = json.loads(row["criteria_json"])
    payload.pop("weekend_time_from")
    payload.pop("weekend_time_to")
    await conn.execute(
        "UPDATE watches SET criteria_json = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), str(watch.watch_id)),
    )

    fetched = await repo.get(conn, watch.watch_id)

    assert fetched is not None
    assert fetched.criteria.weekend_time_from is None
    assert fetched.criteria.weekend_time_to is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/storage/test_watch_repository.py -q
```

Expected: failures because the repository neither writes nor reads weekend bounds.

- [ ] **Step 3: Encode and decode optional weekend bounds**

Add to `_encode_criteria`:

```python
"weekend_time_from": (
    criteria.weekend_time_from.isoformat()
    if criteria.weekend_time_from is not None
    else None
),
"weekend_time_to": (
    criteria.weekend_time_to.isoformat()
    if criteria.weekend_time_to is not None
    else None
),
```

At the start of `_decode_criteria`, read:

```python
weekend_time_from = payload.get("weekend_time_from")
weekend_time_to = payload.get("weekend_time_to")
```

Pass to `WatchCriteria`:

```python
weekend_time_from=(
    time.fromisoformat(weekend_time_from)
    if weekend_time_from is not None
    else None
),
weekend_time_to=(
    time.fromisoformat(weekend_time_to)
    if weekend_time_to is not None
    else None
),
```

- [ ] **Step 4: Add a persisted-partial-pair test**

Import `InputError`, then add:

```python
async def test_partial_persisted_weekend_window_is_rejected(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)
    cursor = await conn.execute(
        "SELECT criteria_json FROM watches WHERE id = ?",
        (str(watch.watch_id),),
    )
    row = await cursor.fetchone()
    assert row is not None
    payload = json.loads(row["criteria_json"])
    payload["weekend_time_from"] = "12:00:00"
    payload["weekend_time_to"] = None
    await conn.execute(
        "UPDATE watches SET criteria_json = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), str(watch.watch_id)),
    )

    with pytest.raises(InputError, match="weekend"):
        await repo.get(conn, watch.watch_id)
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/storage/test_watch_repository.py -q
```

Expected: all pass.

Commit:

```bash
git add src/cinema_friend/storage/watch_repository.py \
  tests/storage/test_watch_repository.py
git commit -m "feat: persist weekend viewing windows"
```

---

### Task 3: Telegram Schedule Setup and Draft Recovery

**Files:**
- Modify: `src/cinema_friend/telegram/wizard.py:40-884`
- Test: `tests/telegram/test_wizard.py`

**Interfaces:**
- Consumes: `DailyTimeWindow` and `window_for_local_date` from Task 1.
- Consumes: optional `WatchCriteria` weekend fields from Task 1.
- Produces: `TimeSetupMode.SAME_EVERY_DAY = "same"` and `TimeSetupMode.WEEKDAY_WEEKEND = "split"`.
- Produces: callbacks `wizard:time-mode:same` and `wizard:time-mode:split`.
- Produces: draft key `time_flow_version` with integer value `2`.
- Produces: states `AWAIT_TIME_MODE`, `AWAIT_WEEKDAY_TIME_RANGE`, and `AWAIT_WEEKEND_TIME_RANGE`.
- Preserves: unversioned `AWAIT_TIME_RANGE` drafts advance directly to quantity.

- [ ] **Step 1: Add test helpers for the new callback step**

In `tests/telegram/test_wizard.py`, add:

```python
async def _choose_uniform_time(
    deps: WizardDeps,
    times: str = "18:00 to 23:00",
) -> None:
    reply = await handle_wizard_callback(
        _callback_update("wizard:time-mode:same"),
        deps,
    )
    assert reply is not None
    assert (await handle_wizard_text(_text_update(times), deps)) is not None


async def _drive_to_time_mode(deps: WizardDeps) -> RenderedMessage:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    reply = await handle_wizard_text(
        _text_update("2026-08-26 to 2026-08-30"),
        deps,
    )
    assert reply is not None
    return reply
```

Update every fresh-draft test/helper that currently sends a date range and then immediately
sends a time range to call `_choose_uniform_time`. Keep the legacy-draft test unversioned
and sending directly to `AWAIT_TIME_RANGE`.

- [ ] **Step 2: Write failing schedule-choice and branch tests**

Add:

```python
async def test_new_draft_asks_for_time_mode_after_dates(deps: WizardDeps) -> None:
    reply = await _drive_to_time_mode(deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_TIME_MODE.value
    assert _callback_data(reply) == {
        "wizard:time-mode:same",
        "wizard:time-mode:split",
    }


async def test_uniform_time_mode_uses_one_window(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)

    await _choose_uniform_time(deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value
    assert draft.payload["time_from"] == "18:00:00"
    assert draft.payload["time_to"] == "23:00:00"
    assert "weekend_time_from" not in draft.payload
    assert "weekend_time_to" not in draft.payload


async def test_split_time_mode_collects_both_windows(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        deps,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    reply = await handle_wizard_text(_text_update("12:00 to 16:00"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value
    assert draft.payload["time_from"] == "18:00:00"
    assert draft.payload["time_to"] == "23:00:00"
    assert draft.payload["weekend_time_from"] == "12:00:00"
    assert draft.payload["weekend_time_to"] == "16:00:00"
```

- [ ] **Step 3: Write failing validation, restart, legacy, and review tests**

Add:

```python
async def test_invalid_time_mode_leaves_draft_unchanged(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:time-mode:weekly"),
        deps,
    )

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_TIME_MODE.value
    assert "time_from" not in draft.payload


async def test_split_schedule_resumes_at_weekend_window_after_restart(
    db_path: Path,
    fake_clock: FakeClock,
    fake_checks: FakeCheckRunner,
) -> None:
    first_database = await _make_database(db_path)
    first = _deps(first_database, fake_clock, fake_checks)
    await _drive_to_time_mode(first)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        first,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), first)

    second = _deps(Database(db_path), fake_clock, fake_checks)
    draft = await _draft(second)

    assert draft is not None
    assert draft.state == WizardState.AWAIT_WEEKEND_TIME_RANGE.value
    assert draft.payload["time_from"] == "18:00:00"
    assert draft.payload["time_to"] == "23:00:00"


async def test_legacy_time_range_draft_keeps_uniform_flow(deps: WizardDeps) -> None:
    await _seed_state(
        deps,
        WizardState.AWAIT_TIME_RANGE,
        {
            "date_from": "2026-08-26",
            "date_to": "2026-08-30",
        },
    )

    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_QUANTITY.value
    assert "time_flow_version" not in draft.payload


async def test_split_schedule_review_labels_both_windows(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        deps,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)
    await handle_wizard_text(_text_update("12:00 to 16:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )
    await handle_wizard_callback(
        _callback_update("wizard:seat-preference:only_best"),
        deps,
    )
    await handle_wizard_text(_text_update("skip"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:mode:one_off"),
        deps,
    )

    assert reply is not None
    assert "Weekdays: 18:00 to 23:00" in reply.text
    assert "Weekends: 12:00 to 16:00" in reply.text
```

Add the split preferred-time helper and test:

```python
async def _drive_split_to_preferred_instant(deps: WizardDeps) -> None:
    await _drive_to_time_mode(deps)
    await handle_wizard_callback(
        _callback_update("wizard:time-mode:split"),
        deps,
    )
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)
    await handle_wizard_text(_text_update("12:00 to 16:00"), deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )
    await handle_wizard_callback(
        _callback_update("wizard:seat-preference:only_best"),
        deps,
    )


async def test_split_preferred_time_uses_the_weekend_window(
    deps: WizardDeps,
) -> None:
    await _drive_split_to_preferred_instant(deps)

    accepted = await handle_wizard_text(
        _text_update("2026-08-29 13:00"),
        deps,
    )

    assert accepted is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_MODE.value
    assert draft.payload["preferred_utc_instant"] == "2026-08-29T12:00:00+00:00"

    await _drive_split_to_preferred_instant(deps)
    rejected = await handle_wizard_text(
        _text_update("2026-08-29 19:00"),
        deps,
    )

    assert rejected is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value
    assert "preferred_utc_instant" not in draft.payload
```

- [ ] **Step 4: Run wizard tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py -q
```

Expected: failures for missing states, callbacks, marker, weekend payload, and review copy.

- [ ] **Step 5: Add time-flow types, marker, callbacks, and prompts**

In `src/cinema_friend/telegram/wizard.py`, define:

```python
_TIME_FLOW_VERSION = 2
_TIME_FLOW_VERSION_KEY = "time_flow_version"


class TimeSetupMode(str, Enum):
    SAME_EVERY_DAY = "same"
    WEEKDAY_WEEKEND = "split"
```

Add states:

```python
AWAIT_TIME_MODE = "await_time_mode"
AWAIT_WEEKDAY_TIME_RANGE = "await_weekday_time_range"
AWAIT_WEEKEND_TIME_RANGE = "await_weekend_time_range"
```

Add callback prefix and prompt:

```python
_TIME_MODE_PREFIX = "wizard:time-mode:"


def _time_mode_prompt() -> RenderedMessage:
    rows = [
        [
            InlineKeyboardButton(
                text="Same every day",
                callback_data=f"{_TIME_MODE_PREFIX}{TimeSetupMode.SAME_EVERY_DAY.value}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Weekday + weekend",
                callback_data=f"{_TIME_MODE_PREFIX}{TimeSetupMode.WEEKDAY_WEEKEND.value}",
            )
        ],
    ]
    return RenderedMessage(
        text="Use one viewing-time window every day, or separate weekday and weekend windows?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
```

Define distinct text prompts for daily, weekday, and weekend ranges, each reusing
`_TIME_RANGE_EXAMPLE`.

- [ ] **Step 6: Wire versioned state transitions**

Add `AWAIT_TIME_MODE` to `_CALLBACK_STATES` and the two split range states to
`_TEXT_STATES`. In `_prompt_for`, return the time-mode prompt and the corresponding text
prompts.

Start new drafts with both explicit markers:

```python
payload = {
    _SEAT_FLOW_VERSION_KEY: _SEAT_FLOW_VERSION,
    _TIME_FLOW_VERSION_KEY: _TIME_FLOW_VERSION,
}
```

Change the date transition:

```python
if payload.get(_TIME_FLOW_VERSION_KEY) == _TIME_FLOW_VERSION:
    return WizardState.AWAIT_TIME_MODE, payload
return WizardState.AWAIT_TIME_RANGE, payload
```

Handle `AWAIT_TIME_MODE` before quantity in `_apply_callback`:

```python
if state is WizardState.AWAIT_TIME_MODE:
    if not data.startswith(_TIME_MODE_PREFIX):
        raise InputError("choose one daily window or separate weekday and weekend windows")
    try:
        mode = TimeSetupMode(data[len(_TIME_MODE_PREFIX) :])
    except ValueError as exc:
        raise InputError(
            "choose one daily window or separate weekday and weekend windows"
        ) from exc
    if mode is TimeSetupMode.SAME_EVERY_DAY:
        payload.pop("weekend_time_from", None)
        payload.pop("weekend_time_to", None)
        return WizardState.AWAIT_TIME_RANGE, payload
    return WizardState.AWAIT_WEEKDAY_TIME_RANGE, payload
```

Handle text states:

```python
if state in (WizardState.AWAIT_TIME_RANGE, WizardState.AWAIT_WEEKDAY_TIME_RANGE):
    time_from, time_to = parse_time_range(text)
    payload["time_from"] = time_from.isoformat()
    payload["time_to"] = time_to.isoformat()
    if state is WizardState.AWAIT_WEEKDAY_TIME_RANGE:
        return WizardState.AWAIT_WEEKEND_TIME_RANGE, payload
    return WizardState.AWAIT_QUANTITY, payload
if state is WizardState.AWAIT_WEEKEND_TIME_RANGE:
    time_from, time_to = parse_time_range(text)
    payload["weekend_time_from"] = time_from.isoformat()
    payload["weekend_time_to"] = time_to.isoformat()
    return WizardState.AWAIT_QUANTITY, payload
```

Treat stale time-mode callbacks like stale seat-mode callbacks in
`handle_wizard_callback`, returning a retry prompt instead of silently ignoring them.

- [ ] **Step 7: Share weekend payload parsing with preferred-time validation**

Import `DailyTimeWindow` and `window_for_local_date`. Add:

```python
def _weekend_window(payload: Mapping[str, Any]) -> DailyTimeWindow | None:
    time_from_raw = payload.get("weekend_time_from")
    time_to_raw = payload.get("weekend_time_to")
    if (time_from_raw is None) != (time_to_raw is None):
        raise InputError("weekend time range requires both start and end")
    if time_from_raw is None or time_to_raw is None:
        return None
    return time.fromisoformat(time_from_raw), time.fromisoformat(time_to_raw)
```

In `_parse_preferred_instant`, select the effective pair:

```python
time_from, time_to = window_for_local_date(
    local.date(),
    default_window=(
        time.fromisoformat(payload["time_from"]),
        time.fromisoformat(payload["time_to"]),
    ),
    weekend_window=_weekend_window(payload),
)
```

In `_build_criteria`, call `_weekend_window(payload)` once and pass its two values, or
`None`, to the new `WatchCriteria` fields.

- [ ] **Step 8: Render the wizard review**

Replace the single time line in `_review_prompt` with:

```python
if criteria.weekend_time_from is None or criteria.weekend_time_to is None:
    lines.append(
        f"Times: {criteria.time_from.strftime('%H:%M')} "
        f"to {criteria.time_to.strftime('%H:%M')} daily"
    )
else:
    lines.extend(
        [
            (
                f"Weekdays: {criteria.time_from.strftime('%H:%M')} "
                f"to {criteria.time_to.strftime('%H:%M')}"
            ),
            (
                f"Weekends: {criteria.weekend_time_from.strftime('%H:%M')} "
                f"to {criteria.weekend_time_to.strftime('%H:%M')}"
            ),
        ]
    )
```

Update the module docstring's callback-state list to include the time schedule choice.

- [ ] **Step 9: Run wizard tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py -q
```

Expected: all pass.

Commit:

```bash
git add src/cinema_friend/telegram/wizard.py \
  tests/telegram/test_wizard.py
git commit -m "feat: add weekday and weekend time setup"
```

---

### Task 4: Watch-List Rendering, Documentation, and Full Validation

**Files:**
- Modify: `src/cinema_friend/telegram/rendering.py:260-273`
- Modify: `tests/telegram/test_rendering.py:108-143,728-735`
- Modify: `README.md:245-266`

**Interfaces:**
- Consumes: optional `WatchCriteria` weekend fields from Task 1.
- Produces: compact uniform summary `18:00-23:00`.
- Produces: compact split summary `Mon-Fri 18:00-23:00; Sat-Sun 12:00-16:00`.

- [ ] **Step 1: Write a failing split-summary test**

Extend the `_watch` helper in `tests/telegram/test_rendering.py` with:

```python
weekend_time_from: time | None = None,
weekend_time_to: time | None = None,
```

Pass both values to `WatchCriteria`, then add:

```python
def test_render_watch_list_labels_split_time_windows() -> None:
    rendered = render_watch_list(
        (
            _watch(
                weekend_time_from=time(12, 0),
                weekend_time_to=time(16, 0),
            ),
        )
    )

    assert "Mon-Fri 18:00-23:00" in rendered.text
    assert "Sat-Sun 12:00-16:00" in rendered.text
```

- [ ] **Step 2: Run the rendering test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest \
  tests/telegram/test_rendering.py::test_render_watch_list_labels_split_time_windows -q
```

Expected: failure because `_criteria_summary` shows only the default range.

- [ ] **Step 3: Render uniform and split schedules compactly**

In `_criteria_summary`, replace the direct time string construction with:

```python
time_from = criteria.time_from.strftime(_CLOCK_FORMAT)
time_to = criteria.time_to.strftime(_CLOCK_FORMAT)
if criteria.weekend_time_from is None or criteria.weekend_time_to is None:
    times = f"{time_from}-{time_to}"
else:
    weekend_from = criteria.weekend_time_from.strftime(_CLOCK_FORMAT)
    weekend_to = criteria.weekend_time_to.strftime(_CLOCK_FORMAT)
    times = (
        f"Mon-Fri {time_from}-{time_to}; "
        f"Sat-Sun {weekend_from}-{weekend_to}"
    )
```

Return `times` in the criteria summary instead of `time_from-time_to`.

- [ ] **Step 4: Update the README wizard sequence**

Replace items 3 through 10 with:

```markdown
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
```

- [ ] **Step 5: Run targeted tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/domain/test_time_window.py \
  tests/domain/test_watch.py \
  tests/watches/test_criteria.py \
  tests/storage/test_watch_repository.py \
  tests/telegram/test_wizard.py \
  tests/telegram/test_rendering.py -q
```

Expected: all pass.

- [ ] **Step 6: Run the complete quality gates**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy src scripts
```

Expected: all tests pass, Ruff reports no errors, and mypy reports success.

- [ ] **Step 7: Commit the final feature surface**

```bash
git add src/cinema_friend/telegram/rendering.py \
  tests/telegram/test_rendering.py \
  README.md
git commit -m "feat: show split viewing schedules"
```

- [ ] **Step 8: Confirm the branch is clean**

Run:

```bash
git status --short
git log -5 --oneline
```

Expected: no status output and four feature commits after the design and plan commits.
