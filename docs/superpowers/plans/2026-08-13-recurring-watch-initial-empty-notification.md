# Recurring Watch Initial Empty Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send one immediate keep-watching message after a recurring watch's first successful empty check, after the existing creation confirmation, while keeping later empty checks silent.

**Architecture:** The pure notification policy selects a distinct `initial_recurring_empty` delivery kind for `CREATION + RECURRING + zero options`. The existing durable result-delivery pipeline renders and retries that event. A recipient-scoped, process-local deferral prevents a background sweep from sending the event before the confirmation route has sent the existing watch-created message.

**Tech Stack:** Python 3.12, asyncio, aiosqlite, python-telegram-bot 22, pytest, pytest-asyncio, Ruff, mypy

## Global Constraints

- Emit the special notification only for `CheckTrigger.CREATION`, `WatchMode.RECURRING`, and zero ranked options.
- Send the existing watch-created confirmation first as a separate message.
- The follow-up copy is exactly: `I haven't found anything right now, but I'll keep watching.`
- Preserve the film title and London check time in the follow-up.
- Keep subsequent scheduled and recovery empty checks silent.
- Keep `/check`, one-off watch, non-empty result, and typed failure behavior unchanged.
- Keep the notification durable, idempotent, and retryable through the existing delivery queue.
- Do not add a database migration, change `NotificationPayload`, or add a dependency.

---

## File Structure

- `src/cinema_friend/services/notification_policy.py`: select and name the new notification event.
- `src/cinema_friend/services/check_service.py`: pass watch mode into the pure policy.
- `src/cinema_friend/telegram/rendering.py`: render the special empty-result copy while reusing the normal result header.
- `src/cinema_friend/telegram/bot.py`: recognize the event, defer it during confirmation, and flush it after confirmation.
- `src/cinema_friend/telegram/commands.py`: expose the delivery deferral through the existing dispatcher protocol.
- `tests/services/test_notification_policy.py`: pin the trigger/mode/options decision matrix.
- `tests/services/test_check_service.py`: prove durable first-empty queueing and later silence.
- `tests/telegram/test_rendering.py`: pin the title, time, and exact copy.
- `tests/telegram/test_bot.py`: prove worker rendering, scoped deferral, route ordering, and handler registration.
- `README.md`: document the one initial empty reply and later silence.

### Task 1: Select and persist the initial recurring empty event

**Files:**
- Modify: `src/cinema_friend/services/notification_policy.py:16-110`
- Modify: `src/cinema_friend/services/check_service.py:251-288`
- Test: `tests/services/test_notification_policy.py:80-207`
- Test: `tests/services/test_check_service.py:602-644`

**Interfaces:**
- Consumes: `CheckTrigger`, `WatchMode`, ranked options, known option keys, prior best rank, and recipient ID.
- Produces: `INITIAL_RECURRING_EMPTY_KIND = "initial_recurring_empty"` and `NotificationDecision.kind` set to that value only for an empty recurring creation check.
- Preserves: `NotificationPayload` and the existing `results:<watch_id>:<snapshot_id>` idempotency key.

- [ ] **Step 1: Write notification-policy tests for the decision matrix**

Replace the combined empty-trigger test with explicit mode-aware cases:

```python
def test_empty_recurring_creation_uses_initial_empty_kind() -> None:
    decision = decide_result_notification(
        CheckTrigger.CREATION,
        WatchMode.RECURRING,
        (),
        known_keys=frozenset(),
        last_best=None,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is True
    assert decision.kind == "initial_recurring_empty"
    assert decision.all_option_keys == frozenset()
    assert decision.best_rank is None


@pytest.mark.parametrize(
    ("trigger", "mode"),
    [
        (CheckTrigger.CREATION, WatchMode.ONE_OFF),
        (CheckTrigger.MANUAL, WatchMode.RECURRING),
        (CheckTrigger.MANUAL, WatchMode.ONE_OFF),
    ],
)
def test_other_owner_initiated_empty_checks_use_results_kind(
    trigger: CheckTrigger, mode: WatchMode
) -> None:
    decision = decide_result_notification(
        trigger,
        mode,
        (),
        known_keys=frozenset(),
        last_best=None,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is True
    assert decision.kind == "results"
```

Update every existing `decide_result_notification` call in this test module to pass
`WatchMode.RECURRING` after the trigger. Keep the existing changed-result assertions.

- [ ] **Step 2: Run the policy tests and verify the new tests fail**

Run:

```bash
python -m pytest tests/services/test_notification_policy.py -q
```

Expected: failures because `decide_result_notification` does not yet accept `WatchMode`
and cannot return `initial_recurring_empty`.

- [ ] **Step 3: Implement mode-aware notification selection**

In `notification_policy.py`, import `WatchMode`, expose the event name, add `mode` to the
function signature, and select the kind independently from the existing send/no-send
decision:

```python
from cinema_friend.domain.state import CheckTrigger, WatchMode

INITIAL_RECURRING_EMPTY_KIND = "initial_recurring_empty"


def decide_result_notification(
    trigger: CheckTrigger,
    mode: WatchMode,
    options: Sequence[RankedOption],
    known_keys: frozenset[str],
    last_best: RankVector | None,
    recipient_user_id: int,
) -> NotificationDecision:
    current_keys = frozenset(option.key for option in options)
    new_keys = current_keys - known_keys
    best_rank = _best_rank(options)
    rank_improved = (
        best_rank is not None
        and last_best is not None
        and best_rank.sort_key() < last_best.sort_key()
    )
    requires_snapshot = trigger in _ALWAYS_NOTIFY_TRIGGERS or bool(new_keys) or rank_improved
    kind = (
        INITIAL_RECURRING_EMPTY_KIND
        if trigger is CheckTrigger.CREATION
        and mode is WatchMode.RECURRING
        and not options
        else _RESULTS_KIND
    )
    return NotificationDecision(
        kind=kind,
        recipient_user_id=recipient_user_id,
        new_option_keys=new_keys,
        all_option_keys=current_keys,
        best_rank=best_rank,
        requires_snapshot=requires_snapshot,
    )
```

Update the docstring to distinguish the special recurring creation response from ordinary
manual and one-off empty results.

In `CheckService._persist_success`, pass the mode from the observed watch:

```python
decision = decide_result_notification(
    trigger,
    watch.criteria.mode,
    options,
    known_keys,
    state.last_best_rank,
    watch.user_id,
)
```

- [ ] **Step 4: Add check-service integration tests**

Replace `test_owner_initiated_checks_always_queue_a_delivery` with explicit tests:

```python
async def test_empty_recurring_creation_queues_initial_empty_delivery(
    harness: Harness,
) -> None:
    harness.gateway.performances = []
    watch = await harness.add_watch()

    await harness.service.check(watch.watch_id, CheckTrigger.CREATION)

    deliveries = await harness.deliveries()
    assert [delivery.payload.kind for delivery in deliveries] == [
        "initial_recurring_empty"
    ]


@pytest.mark.parametrize(
    ("trigger", "watch_criteria"),
    [
        (CheckTrigger.MANUAL, criteria()),
        (CheckTrigger.CREATION, one_off_criteria()),
    ],
)
async def test_other_owner_initiated_empty_checks_queue_results(
    harness: Harness,
    trigger: CheckTrigger,
    watch_criteria: WatchCriteria,
) -> None:
    harness.gateway.performances = []
    watch = await harness.add_watch(watch_criteria=watch_criteria)

    await harness.service.check(watch.watch_id, trigger)

    deliveries = await harness.deliveries()
    assert [delivery.payload.kind for delivery in deliveries] == ["results"]


async def test_empty_recurring_creation_notifies_once_then_scheduled_empty_is_silent(
    harness: Harness,
) -> None:
    harness.gateway.performances = []
    watch = await harness.add_watch()
    await harness.service.check(watch.watch_id, CheckTrigger.CREATION)

    first_delivery = (await harness.deliveries())[0]
    async with harness.database.connection() as conn:
        await harness.notifications.mark_delivered(
            conn, first_delivery.delivery_id, (), None, NOW
        )

    await harness.service.check(watch.watch_id, CheckTrigger.SCHEDULED)

    assert await harness.deliveries() == ()
```

- [ ] **Step 5: Run the service tests**

Run:

```bash
python -m pytest \
  tests/services/test_notification_policy.py \
  tests/services/test_check_service.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the policy and persistence change**

```bash
git add \
  src/cinema_friend/services/notification_policy.py \
  src/cinema_friend/services/check_service.py \
  tests/services/test_notification_policy.py \
  tests/services/test_check_service.py
git commit -m "feat: identify initial empty recurring results"
```

### Task 2: Render and deliver the special empty result

**Files:**
- Modify: `src/cinema_friend/telegram/rendering.py:42-44,179-203`
- Modify: `src/cinema_friend/telegram/bot.py:50-76,90-94,435-496`
- Test: `tests/telegram/test_rendering.py:462-475`
- Test: `tests/telegram/test_bot.py:234-259,270-347`

**Interfaces:**
- Consumes: `INITIAL_RECURRING_EMPTY_KIND` and an immutable `SnapshotPage`.
- Produces: `render_initial_recurring_empty_page(snapshot_page, *, max_chars=...)`.
- Preserves: ordinary `render_result_page` behavior and delivery bookkeeping.

- [ ] **Step 1: Write the rendering test**

Add:

```python
def test_initial_recurring_empty_page_keeps_context_and_promises_to_watch() -> None:
    page = _snapshot_page(
        options=(),
        total_options=0,
        total_performances=0,
        watch_title="Dog Stars",
    )

    rendered = render_initial_recurring_empty_page(page)

    assert "Dog Stars" in rendered.text
    assert "Checked:" in rendered.text
    assert "I haven't found anything right now, but I'll keep watching." in rendered.text
    assert "No matching seats" not in rendered.text
    assert rendered.reply_markup is None
```

Import `render_initial_recurring_empty_page` beside `render_result_page`.

- [ ] **Step 2: Run the rendering test and verify it fails**

Run:

```bash
python -m pytest \
  tests/telegram/test_rendering.py::test_initial_recurring_empty_page_keeps_context_and_promises_to_watch \
  -q
```

Expected: import failure because the special renderer does not exist.

- [ ] **Step 3: Implement the special renderer without duplicating page layout**

Add the copy and a private empty-message parameter:

```python
_NO_MATCH = "No matching seats were found for this check."
_INITIAL_RECURRING_EMPTY = (
    "I haven't found anything right now, but I'll keep watching."
)


def render_result_page(
    snapshot_page: SnapshotPage,
    *,
    max_chars: int = MAX_MESSAGE_CHARS,
    empty_message: str = _NO_MATCH,
) -> RenderedMessage:
    ...
    if not snapshot_page.options:
        return RenderedMessage(
            text="\n".join([*header, "", empty_message]),
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )
    ...


def render_initial_recurring_empty_page(
    snapshot_page: SnapshotPage, *, max_chars: int = MAX_MESSAGE_CHARS
) -> RenderedMessage:
    return render_result_page(
        snapshot_page,
        max_chars=max_chars,
        empty_message=_INITIAL_RECURRING_EMPTY,
    )
```

Keep `empty_message` keyword-only. Update the `render_result_page` docstring to state that
callers may supply context-specific copy for an empty snapshot.

- [ ] **Step 4: Write the delivery-worker test**

Allow `_results_payload` to accept a kind:

```python
def _results_payload(
    snapshot_id: UUID,
    *,
    watch_id: UUID = WATCH_ID,
    new: int = 3,
    kind: str = "results",
) -> NotificationPayload:
    return NotificationPayload(
        kind=kind,
        recipient_user_id=USER_ID,
        watch_id=watch_id,
        snapshot_id=snapshot_id,
        new_option_count=new,
        host=None,
        recovery_text=None,
    )
```

Then add:

```python
async def test_initial_recurring_empty_delivery_uses_keep_watching_copy(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 0)
    await _queue(
        harness,
        _results_payload(
            snapshot_id,
            new=0,
            kind="initial_recurring_empty",
        ),
    )

    await harness.worker.run_once()

    text = harness.bot.sent[0]["text"]
    assert "Dog Stars" in text
    assert "Checked:" in text
    assert "I haven't found anything right now, but I'll keep watching." in text
    assert "No matching seats" not in text
```

- [ ] **Step 5: Run the worker test and verify it fails**

Run:

```bash
python -m pytest \
  tests/telegram/test_bot.py::test_initial_recurring_empty_delivery_uses_keep_watching_copy \
  -q
```

Expected: the worker treats `initial_recurring_empty` as an unknown kind and fails the
delivery without sending.

- [ ] **Step 6: Teach the worker to render the new event**

Import the public event constant and special renderer:

```python
from cinema_friend.services.notification_policy import INITIAL_RECURRING_EMPTY_KIND
from cinema_friend.telegram.rendering import (
    RenderedMessage,
    render_initial_recurring_empty_page,
    render_result_page,
)
```

Dispatch the special kind through the existing snapshot loader:

```python
if payload.kind == _RESULTS_KIND:
    return await self._render_results(payload.snapshot_id)
if payload.kind == INITIAL_RECURRING_EMPTY_KIND:
    return await self._render_results(payload.snapshot_id, initial_recurring_empty=True)
```

Make the renderer choice explicit:

```python
async def _render_results(
    self,
    snapshot_id: UUID | None,
    *,
    initial_recurring_empty: bool = False,
) -> _Renderable:
    ...
    message = (
        render_initial_recurring_empty_page(page)
        if initial_recurring_empty
        else render_result_page(page)
    )
    return _Renderable(message, keys, best)
```

- [ ] **Step 7: Run rendering and worker tests**

Run:

```bash
python -m pytest \
  tests/telegram/test_rendering.py \
  tests/telegram/test_bot.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit rendering and delivery**

```bash
git add \
  src/cinema_friend/telegram/rendering.py \
  src/cinema_friend/telegram/bot.py \
  tests/telegram/test_rendering.py \
  tests/telegram/test_bot.py
git commit -m "feat: render initial empty recurring result"
```

### Task 3: Guarantee confirmation-first ordering

**Files:**
- Modify: `src/cinema_friend/telegram/commands.py:20-24,113-127`
- Modify: `src/cinema_friend/telegram/bot.py:20-27,257-310,517-572,642-652`
- Test: `tests/telegram/test_bot.py:796-1025`

**Interfaces:**
- Consumes: `DeliveryDispatcher.defer_initial_recurring_empty(recipient_user_id)`.
- Produces: an async context manager that suppresses only
  `initial_recurring_empty` deliveries for the specified recipient.
- Preserves: concurrent delivery of other kinds and recipients, callback authorization,
  callback acknowledgement, and normal background retry.

- [ ] **Step 1: Write worker deferral tests**

Add an empty special delivery and an ordinary result delivery for the same user. Verify
that the special row remains pending while the ordinary result sends:

```python
async def test_initial_empty_deferral_is_scoped_by_kind_and_recipient(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    empty_snapshot, _ = await _seed_snapshot(harness, watch.watch_id, 0)
    result_snapshot, _ = await _seed_snapshot(harness, watch.watch_id, 1)
    special_id = await _queue(
        harness,
        _results_payload(
            empty_snapshot,
            new=0,
            kind="initial_recurring_empty",
        ),
        key="special",
    )
    ordinary_id = await _queue(
        harness,
        _results_payload(result_snapshot),
        key="ordinary",
    )

    async with harness.worker.defer_initial_recurring_empty(USER_ID):
        run = await harness.worker.run_once()

    assert run.sent_ids == frozenset({ordinary_id})
    assert (await _delivery_row(harness, special_id))["status"] == "pending"
    assert len(harness.bot.sent) == 1
```

Add a second test that queues special deliveries for `USER_ID` and `USER_ID + 1`, defers
only `USER_ID`, and asserts the other recipient's special delivery sends. Extend
`_seed_watch` and `_results_payload` with a `user_id` parameter where needed.

- [ ] **Step 2: Run the deferral tests and verify they fail**

Run:

```bash
python -m pytest \
  tests/telegram/test_bot.py::test_initial_empty_deferral_is_scoped_by_kind_and_recipient \
  -q
```

Expected: failure because `DeliveryWorker.defer_initial_recurring_empty` does not exist.

- [ ] **Step 3: Implement a nested-safe recipient deferral**

Add `asynccontextmanager` and `AsyncIterator` imports. Store reference counts so duplicate
confirmation callbacks for the same user cannot release each other's barrier:

```python
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence


class DeliveryWorker:
    def __init__(...):
        ...
        self._initial_empty_deferrals: dict[int, int] = {}

    @asynccontextmanager
    async def defer_initial_recurring_empty(
        self, recipient_user_id: int
    ) -> AsyncIterator[None]:
        self._initial_empty_deferrals[recipient_user_id] = (
            self._initial_empty_deferrals.get(recipient_user_id, 0) + 1
        )
        try:
            yield
        finally:
            remaining = self._initial_empty_deferrals[recipient_user_id] - 1
            if remaining:
                self._initial_empty_deferrals[recipient_user_id] = remaining
            else:
                del self._initial_empty_deferrals[recipient_user_id]

    def _initial_empty_is_deferred(self, delivery: NotificationDelivery) -> bool:
        return (
            delivery.payload.kind == INITIAL_RECURRING_EMPTY_KIND
            and delivery.payload.recipient_user_id in self._initial_empty_deferrals
        )
```

Skip only deferred rows before `_attempt`:

```python
for delivery in due:
    if self._initial_empty_is_deferred(delivery):
        continue
    try:
        attempts.append(await self._attempt(delivery, now))
    ...
```

Skipping must not reschedule, increment attempts, or mark the delivery.

- [ ] **Step 4: Extend the dispatcher protocol**

In `commands.py`, import `AbstractAsyncContextManager` and add:

```python
class DeliveryDispatcher(Protocol):
    """Flushes and temporarily defers queued notifications."""

    async def run_once(self) -> DeliveryOutcome: ...

    def defer_initial_recurring_empty(
        self, recipient_user_id: int
    ) -> AbstractAsyncContextManager[None]: ...
```

Direct command-handler tests only call `run_once`; their fake dispatchers do not need the
new method until they are used through the special confirmation route.

- [ ] **Step 5: Write a route-ordering test**

Import `_route` in `tests/telegram/test_bot.py`. Create a dispatcher and bot that append
to one event list:

```python
class _OrderedDispatcher:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def defer_initial_recurring_empty(self, recipient_user_id: int) -> AsyncIterator[None]:
        yield

    async def run_once(self) -> DeliveryRun:
        self.events.append("follow-up")
        return DeliveryRun()


async def test_confirm_route_sends_confirmation_before_flushing_delivery(
    harness: Harness,
) -> None:
    events: list[str] = []
    dispatcher = _OrderedDispatcher(events)
    deps = CommandDeps(
        wizard=WizardDeps(
            database=harness.database,
            drafts=DraftRepository(),
            watches=harness.watches,
            checks=_Checks(),
            clock=harness.clock,
        ),
        results=harness.results,
        deliveries=dispatcher,
        allowed_user_ids=frozenset({USER_ID}),
    )

    async def confirm(
        update: Update, command_deps: CommandDeps
    ) -> RenderedMessage:
        return RenderedMessage(
            text="Watch created",
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

    bot = _Bot()
    original_send = bot.send_message

    async def record_send(**kwargs: Any) -> object:
        events.append("confirmation")
        return await original_send(**kwargs)

    bot.send_message = record_send  # type: ignore[method-assign]
    callback = _route(
        confirm,
        deps,
        answers_callback=True,
        defer_initial_recurring_empty=True,
    )

    await callback(
        _callback_update("wizard:confirm", user_id=USER_ID),
        SimpleNamespace(bot=bot),
    )

    assert events == ["confirmation", "follow-up"]
```

Also assert the deferral is entered before the handler by recording `"defer"`,
`"handler"`, `"confirmation"`, `"release"`, and `"follow-up"` in a focused variant.

- [ ] **Step 6: Implement confirmation-specific routing**

Add a keyword to `_route`:

```python
def _route(
    handler: _Handler,
    deps: CommandDeps,
    *,
    answers_callback: bool = False,
    defer_initial_recurring_empty: bool = False,
) -> Callable[[Update, _Context], Coroutine[Any, Any, None]]:
```

Capture the authorized user ID from the existing first authorization check. Factor the
current handler/error/send block into a nested coroutine returning `True` only when a
message was sent. For the special route:

```python
if defer_initial_recurring_empty:
    async with deps.deliveries.defer_initial_recurring_empty(user_id):
        sent = await invoke_and_send()
    if sent:
        await deps.deliveries.run_once()
    return
await invoke_and_send()
```

Do not flush after authorization denial, `None` responses, or a failed confirmation
send. Let send exceptions propagate as they do today; the context manager still releases
the process-local deferral in `finally`, leaving the durable notification pending.

Register an exact confirmation callback before the generic wizard callback:

```python
application.add_handler(
    CallbackQueryHandler(
        _route(
            handle_wizard_button,
            deps,
            answers_callback=True,
            defer_initial_recurring_empty=True,
        ),
        pattern="^wizard:confirm$",
    )
)
application.add_handler(
    CallbackQueryHandler(
        _route(handle_wizard_button, deps, answers_callback=True),
        pattern="^wizard:",
    )
)
```

python-telegram-bot processes the first matching handler in the group, so confirmation
uses the exact route and every other wizard callback uses the existing general route.

- [ ] **Step 7: Update registration tests**

Change the ordering assertion to require:

```python
assert patterns.index("^wizard:confirm$") < patterns.index("^wizard:")
assert patterns.index("^wizard:") < patterns.index("^v1:")
```

Keep denial coverage for both the general wizard route and add the exact confirmation
route to prove authorization still occurs before callback acknowledgement and deferral.

- [ ] **Step 8: Run Telegram tests**

Run:

```bash
python -m pytest tests/telegram/test_bot.py tests/telegram/test_commands.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit ordering coordination**

```bash
git add \
  src/cinema_friend/telegram/commands.py \
  src/cinema_friend/telegram/bot.py \
  tests/telegram/test_bot.py
git commit -m "fix: order recurring empty reply after confirmation"
```

### Task 4: Document and verify the complete behavior

**Files:**
- Modify: `README.md:268-276`

**Interfaces:**
- Consumes: the completed behavior from Tasks 1-3.
- Produces: user-facing documentation matching the implemented notification contract.

- [ ] **Step 1: Update the README behavior**

Replace the blanket empty-result paragraph with:

```markdown
The first successful check for a recurring watch always replies. If it finds no matching
seats, the bot says it found nothing and will keep watching. Later scheduled checks that
find nothing new stay silent.

After that first reply, you are messaged again only when something appears that is
genuinely better than what you were last told about, so a recurring watch on a quiet film
is not repetitive. Manual `/check` requests still return the current result, even when
empty.
```

- [ ] **Step 2: Run targeted behavioral tests together**

Run:

```bash
python -m pytest \
  tests/services/test_notification_policy.py \
  tests/services/test_check_service.py \
  tests/telegram/test_rendering.py \
  tests/telegram/test_bot.py \
  tests/telegram/test_commands.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run lint and type checking**

Run:

```bash
python -m ruff check .
python -m mypy
```

Expected: both commands exit successfully.

- [ ] **Step 4: Run the full test suite**

Run:

```bash
python -m pytest -q
```

Expected: the full suite passes.

- [ ] **Step 5: Inspect the final diff**

Run:

```bash
git diff --check
git status --short
git diff --stat HEAD~3
```

Expected: no whitespace errors; only the planned source, tests, README, spec, and plan are
present.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md
git commit -m "docs: explain recurring empty notifications"
```
