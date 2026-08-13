# Watch-Scoped Delivery Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope initial-empty confirmation barriers to one watch and keep pending initial-empty deliveries readable after rollback.

**Architecture:** The wizard prepares its deterministic watch ID before the confirmation handler runs, allowing the delivery worker to key barriers by `(recipient_user_id, watch_id)`. Initial-empty copy becomes an optional presentation flag on an ordinary `results` payload, so the current build renders keep-watching copy while the previous build ignores the flag and sends the generic no-match result.

**Tech Stack:** Python 3.12, asyncio, aiosqlite, python-telegram-bot 22, pytest, pytest-asyncio, Ruff, mypy

## Global Constraints

- Defer only the initial-empty delivery for the watch currently being confirmed.
- Keep another watch's pending initial-empty retry deliverable during a long creation check.
- Preserve confirmation-first ordering for the watch being confirmed.
- Persist every successful result delivery with `kind = "results"`.
- Persist `initial_recurring_empty` as an optional boolean payload field defaulting to `false`.
- A previous release must decode the row and send the generic no-match result.
- Existing rows without the field must decode as `false`.
- Do not add a SQL migration, dependency, held-delivery state, or new retry behavior.
- Do not change the pre-existing ordering of non-empty creation results.
- Keep unknown non-result notification kinds terminal-failed.

---

## File Structure

- `src/cinema_friend/domain/results.py`: define backward-compatible notification presentation metadata.
- `src/cinema_friend/services/notification_policy.py`: decide the initial-empty presentation independently from notification kind.
- `src/cinema_friend/services/check_service.py`: persist the presentation flag on ordinary result payloads.
- `src/cinema_friend/storage/notification_repository.py`: encode the flag and default missing legacy data to false.
- `src/cinema_friend/telegram/bot.py`: render the flag and key process-local barriers by recipient and watch.
- `src/cinema_friend/telegram/commands.py`: expose the watch-scoped barrier through the dispatcher protocol.
- `src/cinema_friend/telegram/wizard.py`: prepare the stable confirmation watch ID before the saga starts.
- `tests/services/test_notification_policy.py`: pin the policy's kind/presentation matrix.
- `tests/services/test_check_service.py`: prove new deliveries stay rollback-readable.
- `tests/storage/test_notification_repository.py`: prove new and legacy payload round trips.
- `tests/telegram/test_bot.py`: prove presentation rendering and watch-scoped coordination.
- `tests/telegram/test_wizard.py`: prove stable identity preparation and legacy-draft upgrade.
- `docs/superpowers/specs/2026-08-13-recurring-watch-initial-empty-notification-design.md`: align the original feature spec with the final compatibility design.

### Task 1: Replace the persisted kind with presentation metadata

**Files:**
- Modify: `src/cinema_friend/domain/results.py:129-138`
- Modify: `src/cinema_friend/services/notification_policy.py:16-120`
- Modify: `src/cinema_friend/services/check_service.py:251-288`
- Modify: `src/cinema_friend/storage/notification_repository.py:44-75`
- Modify: `src/cinema_friend/telegram/bot.py:51-81,312-550`
- Test: `tests/services/test_notification_policy.py`
- Test: `tests/services/test_check_service.py`
- Test: `tests/storage/test_notification_repository.py`
- Test: `tests/telegram/test_bot.py`

**Interfaces:**
- Produces: `NotificationPayload.initial_recurring_empty: bool = False`.
- Produces: `NotificationDecision.initial_recurring_empty: bool`.
- Preserves: `NotificationDecision.kind == "results"` for every successful result delivery.
- Preserves: result idempotency key `results:<watch_id>:<snapshot_id>`.
- Consumes downstream: `DeliveryWorker` reads the flag only for a `results` payload.

- [ ] **Step 1: Write failing policy and check-service tests**

Change the recurring creation policy test to require an ordinary kind plus presentation:

```python
def test_empty_recurring_creation_uses_results_with_initial_empty_presentation() -> None:
    decision = decide_result_notification(
        CheckTrigger.CREATION,
        WatchMode.RECURRING,
        (),
        known_keys=frozenset(),
        last_best=None,
        recipient_user_id=11,
    )

    assert decision.requires_snapshot is True
    assert decision.kind == "results"
    assert decision.initial_recurring_empty is True
```

For every ordinary result case already covered by the matrix, add:

```python
assert decision.initial_recurring_empty is False
```

Change the check-service integration assertion:

```python
async def test_empty_recurring_creation_queues_rollback_readable_results(
    harness: Harness,
) -> None:
    harness.gateway.performances = []
    watch = await harness.add_watch()

    await harness.service.check(watch.watch_id, CheckTrigger.CREATION)

    (delivery,) = await harness.deliveries()
    assert delivery.payload.kind == "results"
    assert delivery.payload.initial_recurring_empty is True
    assert delivery.idempotency_key.startswith(f"results:{watch.watch_id}:")
```

- [ ] **Step 2: Write failing payload compatibility tests**

Extend the storage-test payload helper:

```python
def payload(
    watch: Watch | None = None,
    *,
    kind: str = "new_options",
    recipient_user_id: int = 11,
    snapshot_id: UUID | None = None,
    new_option_count: int = 2,
    host: str | None = None,
    recovery_text: str | None = None,
    initial_recurring_empty: bool = False,
) -> NotificationPayload:
    return NotificationPayload(
        kind=kind,
        recipient_user_id=recipient_user_id,
        watch_id=None if watch is None else watch.watch_id,
        snapshot_id=snapshot_id,
        new_option_count=new_option_count,
        host=host,
        recovery_text=recovery_text,
        initial_recurring_empty=initial_recurring_empty,
    )
```

Add a new-payload round trip:

```python
async def test_initial_empty_presentation_round_trips(
    repository: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    original = payload(
        watch,
        kind="results",
        initial_recurring_empty=True,
    )

    created = await repository.create_delivery(conn, "key-initial-empty", original, NOW)

    assert created.payload == original
```

Add legacy JSON coverage by creating a normal delivery, removing the new JSON member
directly, and reloading it:

```python
async def test_legacy_payload_without_initial_empty_field_defaults_false(
    repository: NotificationRepository,
    conn: aiosqlite.Connection,
    make_watch: WatchFactory,
) -> None:
    watch = await make_watch()
    created = await repository.create_delivery(
        conn, "key-legacy", payload(watch, kind="results"), NOW
    )
    cursor = await conn.execute(
        "SELECT payload_json FROM notification_deliveries WHERE id = ?",
        (str(created.delivery_id),),
    )
    row = await cursor.fetchone()
    assert row is not None
    legacy = json.loads(row["payload_json"])
    legacy.pop("initial_recurring_empty")
    await conn.execute(
        "UPDATE notification_deliveries SET payload_json = ? WHERE id = ?",
        (json.dumps(legacy, sort_keys=True), str(created.delivery_id)),
    )

    restored = await repository.delivery(conn, created.delivery_id)

    assert restored is not None
    assert restored.payload.kind == "results"
    assert restored.payload.initial_recurring_empty is False
```

- [ ] **Step 3: Write failing worker tests**

Change `_results_payload` to accept the flag rather than a special kind:

```python
def _results_payload(
    snapshot_id: UUID,
    *,
    watch_id: UUID = WATCH_ID,
    user_id: int = USER_ID,
    new: int = 3,
    initial_recurring_empty: bool = False,
) -> NotificationPayload:
    return NotificationPayload(
        kind="results",
        recipient_user_id=user_id,
        watch_id=watch_id,
        snapshot_id=snapshot_id,
        new_option_count=new,
        host=None,
        recovery_text=None,
        initial_recurring_empty=initial_recurring_empty,
    )
```

Update the keep-watching delivery test to queue:

```python
_results_payload(
    snapshot_id,
    new=0,
    initial_recurring_empty=True,
)
```

Keep the defensive non-empty test, but set the flag rather than the kind. Assert the
ordinary option list renders and the keep-watching sentence does not.

Add a rollback-shape assertion against the stored JSON:

```python
async def test_initial_empty_delivery_uses_rollback_readable_results_shape(
    harness: Harness,
) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 0)
    delivery_id = await _queue(
        harness,
        _results_payload(
            snapshot_id,
            new=0,
            initial_recurring_empty=True,
        ),
    )

    row = await _delivery_row(harness, delivery_id)
    encoded = json.loads(row["payload_json"])

    assert row["kind"] == "results"
    assert encoded["kind"] == "results"
    assert encoded["initial_recurring_empty"] is True
```

- [ ] **Step 4: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest \
  tests/services/test_notification_policy.py \
  tests/services/test_check_service.py \
  tests/storage/test_notification_repository.py \
  tests/telegram/test_bot.py -q
```

Expected: failures because neither domain type carries `initial_recurring_empty`, and
the policy still persists `initial_recurring_empty` as the kind.

- [ ] **Step 5: Implement backward-compatible presentation metadata**

Add the optional domain field last so all existing keyword constructors remain valid:

```python
@dataclass(frozen=True, slots=True)
class NotificationPayload:
    kind: str
    recipient_user_id: int
    watch_id: UUID | None
    snapshot_id: UUID | None
    new_option_count: int
    host: str | None
    recovery_text: str | None
    initial_recurring_empty: bool = False
```

Add the field to `NotificationDecision`:

```python
initial_recurring_empty: bool
```

Remove `INITIAL_RECURRING_EMPTY_KIND`. Compute presentation separately and always return
the ordinary kind:

```python
initial_recurring_empty = (
    trigger is CheckTrigger.CREATION
    and mode is WatchMode.RECURRING
    and not options
)
return NotificationDecision(
    kind=_RESULTS_KIND,
    recipient_user_id=recipient_user_id,
    new_option_keys=new_keys,
    all_option_keys=current_keys,
    best_rank=best_rank,
    requires_snapshot=requires_snapshot,
    initial_recurring_empty=initial_recurring_empty,
)
```

Persist the decision in `CheckService._persist_success`:

```python
NotificationPayload(
    kind=decision.kind,
    recipient_user_id=decision.recipient_user_id,
    watch_id=watch.watch_id,
    snapshot_id=snapshot.snapshot_id,
    new_option_count=len(decision.new_option_keys),
    host=None,
    recovery_text=None,
    initial_recurring_empty=decision.initial_recurring_empty,
)
```

Encode and decode the optional JSON member:

```python
"initial_recurring_empty": payload.initial_recurring_empty,
```

```python
initial_recurring_empty=bool(payload.get("initial_recurring_empty", False)),
```

In `DeliveryWorker._render`, remove the special-kind branch and pass presentation from
the ordinary result branch:

```python
if payload.kind == _RESULTS_KIND:
    return await self._render_results(
        payload.snapshot_id,
        initial_recurring_empty=payload.initial_recurring_empty,
    )
```

Update `_initial_empty_is_deferred` to identify the flag:

```python
return (
    delivery.payload.kind == _RESULTS_KIND
    and delivery.payload.initial_recurring_empty
    and ...
)
```

Keep the existing unknown-kind terminal-failure test unchanged.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/services/test_notification_policy.py \
  tests/services/test_check_service.py \
  tests/storage/test_notification_repository.py \
  tests/telegram/test_bot.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit payload compatibility**

```bash
git add \
  src/cinema_friend/domain/results.py \
  src/cinema_friend/services/notification_policy.py \
  src/cinema_friend/services/check_service.py \
  src/cinema_friend/storage/notification_repository.py \
  src/cinema_friend/telegram/bot.py \
  tests/services/test_notification_policy.py \
  tests/services/test_check_service.py \
  tests/storage/test_notification_repository.py \
  tests/telegram/test_bot.py
git commit -m "fix: keep initial empty deliveries rollback readable"
```

### Task 2: Scope confirmation barriers to the prepared watch

**Files:**
- Modify: `src/cinema_friend/telegram/wizard.py:916-1000`
- Modify: `src/cinema_friend/telegram/commands.py:124-133`
- Modify: `src/cinema_friend/telegram/bot.py:263-352,581-640`
- Test: `tests/telegram/test_wizard.py`
- Test: `tests/telegram/test_bot.py`

**Interfaces:**
- Produces: `prepare_confirmation_watch_id(user_id: int, deps: WizardDeps) -> UUID | None`.
- Changes: `DeliveryDispatcher.defer_initial_recurring_empty(recipient_user_id: int, watch_id: UUID)`.
- Changes: `DeliveryWorker.defer_initial_recurring_empty(recipient_user_id: int, watch_id: UUID)`.
- Preserves: ref-counted nesting for the same `(recipient_user_id, watch_id)` key.

- [ ] **Step 1: Write failing wizard identity tests**

Import `prepare_confirmation_watch_id` in `tests/telegram/test_wizard.py`.

Add legacy-draft preparation:

```python
async def test_prepare_confirmation_adds_stable_identity_to_legacy_review_draft(
    deps: WizardDeps,
) -> None:
    await _drive_to_review(deps)
    before = await _draft(deps)
    assert before is not None
    assert "setup_id" not in before.payload

    first = await prepare_confirmation_watch_id(USER_ID, deps)
    second = await prepare_confirmation_watch_id(USER_ID, deps)
    prepared = await _draft(deps)

    assert first is not None
    assert second == first
    assert prepared is not None
    assert prepared.payload["setup_id"]
```

Prove the saga creates the prepared ID:

```python
async def test_confirm_creates_the_prepared_watch_id(deps: WizardDeps) -> None:
    await _drive_to_review(deps)
    prepared = await prepare_confirmation_watch_id(USER_ID, deps)
    assert prepared is not None

    await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    (watch,) = await deps.watches.list_for_owner(USER_ID)
    assert watch.watch_id == prepared
```

Prove an existing ID does not rewrite the draft:

```python
async def test_prepare_confirmation_does_not_rewrite_an_already_prepared_draft(
    deps: WizardDeps,
    fake_clock: FakeClock,
) -> None:
    await _drive_to_review(deps)
    first = await prepare_confirmation_watch_id(USER_ID, deps)
    before = await _draft(deps)
    assert first is not None
    assert before is not None
    fake_clock.current += timedelta(minutes=5)

    second = await prepare_confirmation_watch_id(USER_ID, deps)
    after = await _draft(deps)

    assert second == first
    assert after == before
```

Add parametrized missing/non-confirmable coverage using no draft and an
`AWAIT_TIME_RANGE` draft; both return `None`.

- [ ] **Step 2: Run wizard identity tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py -k "prepare_confirmation or prepared_watch" -q
```

Expected: import failure because `prepare_confirmation_watch_id` does not exist.

- [ ] **Step 3: Implement identity preparation**

Add beside `_watch_id_for`:

```python
async def prepare_confirmation_watch_id(
    user_id: int, deps: WizardDeps
) -> UUID | None:
    """Ensure a confirmable draft has the stable identity its saga will create."""
    async with deps.database.connection() as conn, deps.database.transaction(conn):
        draft = await deps.drafts.get(conn, user_id)
        if draft is None:
            return None
        state = WizardState(draft.state)
        if state not in (WizardState.REVIEW, WizardState.CONFIRMING):
            return None
        payload = dict(draft.payload)
        setup_id = payload.get("setup_id")
        if setup_id is None:
            if state is not WizardState.REVIEW:
                return None
            setup_id = str(uuid4())
            payload["setup_id"] = setup_id
            await deps.drafts.upsert(conn, user_id, state.value, payload, deps.clock.now())
    return _watch_id_for(setup_id)
```

Keep `_claim`'s existing `setdefault("setup_id", ...)` as a crash-safe invariant.

- [ ] **Step 4: Write failing watch-scoped worker tests**

Update every fake dispatcher's context-manager signature to accept `watch_id`.

Replace the recipient-wide deferral test with two initial-empty deliveries for the same
user and different watches:

```python
async def test_initial_empty_deferral_is_scoped_to_one_watch(
    harness: Harness,
) -> None:
    target = await _seed_watch(
        harness,
        watch_id=UUID("00000000-0000-4000-8000-000000000001"),
    )
    unrelated = await _seed_watch(
        harness,
        watch_id=UUID("00000000-0000-4000-8000-000000000002"),
    )
    target_snapshot, _ = await _seed_snapshot(harness, target.watch_id, 0)
    unrelated_snapshot, _ = await _seed_snapshot(harness, unrelated.watch_id, 0)
    target_id = await _queue(
        harness,
        _results_payload(
            target_snapshot,
            watch_id=target.watch_id,
            new=0,
            initial_recurring_empty=True,
        ),
        key="target",
    )
    unrelated_id = await _queue(
        harness,
        _results_payload(
            unrelated_snapshot,
            watch_id=unrelated.watch_id,
            new=0,
            initial_recurring_empty=True,
        ),
        key="unrelated",
    )

    async with harness.worker.defer_initial_recurring_empty(USER_ID, target.watch_id):
        run = await harness.worker.run_once()

    assert run.sent_ids == frozenset({unrelated_id})
    assert (await _delivery_row(harness, target_id))["status"] == "pending"
```

Add nested-key coverage:

```python
async def test_nested_watch_deferrals_release_at_refcount_zero(harness: Harness) -> None:
    watch = await _seed_watch(harness)
    snapshot_id, _ = await _seed_snapshot(harness, watch.watch_id, 0)
    delivery_id = await _queue(
        harness,
        _results_payload(
            snapshot_id,
            new=0,
            initial_recurring_empty=True,
        ),
    )

    async with harness.worker.defer_initial_recurring_empty(USER_ID, watch.watch_id):
        async with harness.worker.defer_initial_recurring_empty(USER_ID, watch.watch_id):
            assert (await harness.worker.run_once()).attempted == 0
        assert (await harness.worker.run_once()).attempted == 0

    assert (await harness.worker.run_once()).sent_ids == frozenset({delivery_id})
```

- [ ] **Step 5: Write failing route tests**

Seed a review draft with a known `setup_id`, derive its expected UUID using
`uuid5(_WATCH_NAMESPACE, setup_id)` through the public preparation helper, and record the
watch ID passed to the fake dispatcher:

```python
class _RecordingDispatcher:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.deferred: list[tuple[int, UUID]] = []

    @asynccontextmanager
    async def defer_initial_recurring_empty(
        self, recipient_user_id: int, watch_id: UUID
    ) -> AsyncIterator[None]:
        self.deferred.append((recipient_user_id, watch_id))
        self.events.append("defer")
        try:
            yield
        finally:
            self.events.append("release")
```

Assert the exact route passes `(USER_ID, prepared_watch_id)`, enters before the handler,
sends the confirmation, releases, then flushes. Add a stale-draft case that installs no
barrier and does not flush when the handler returns `None`.

- [ ] **Step 6: Run worker and route tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_bot.py -k "deferral or confirm_route" -q
```

Expected: failures because the dispatcher and worker still accept only a recipient.

- [ ] **Step 7: Implement watch-scoped coordination**

Change the protocol:

```python
def defer_initial_recurring_empty(
    self, recipient_user_id: int, watch_id: UUID
) -> AbstractAsyncContextManager[None]: ...
```

In `DeliveryWorker`, key the counter by a tuple:

```python
self._initial_empty_deferrals: dict[tuple[int, UUID], int] = {}
```

```python
@asynccontextmanager
async def defer_initial_recurring_empty(
    self, recipient_user_id: int, watch_id: UUID
) -> AsyncIterator[None]:
    key = (recipient_user_id, watch_id)
    self._initial_empty_deferrals[key] = self._initial_empty_deferrals.get(key, 0) + 1
    try:
        yield
    finally:
        remaining = self._initial_empty_deferrals[key] - 1
        if remaining:
            self._initial_empty_deferrals[key] = remaining
        else:
            del self._initial_empty_deferrals[key]
```

Match the exact payload identity:

```python
return (
    delivery.payload.kind == _RESULTS_KIND
    and delivery.payload.initial_recurring_empty
    and delivery.payload.watch_id is not None
    and (
        delivery.payload.recipient_user_id,
        delivery.payload.watch_id,
    )
    in self._initial_empty_deferrals
)
```

In `_route`, prepare identity only for the special exact-confirm route:

```python
if defer_initial_recurring_empty:
    watch_id = await prepare_confirmation_watch_id(user_id, deps.wizard)
    if watch_id is None:
        await invoke_and_send()
        return
    async with deps.deliveries.defer_initial_recurring_empty(user_id, watch_id):
        sent = await invoke_and_send()
    if sent:
        await deps.deliveries.run_once()
    return
```

Import `prepare_confirmation_watch_id` from `telegram.wizard`.

- [ ] **Step 8: Run Telegram tests**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py tests/telegram/test_bot.py tests/telegram/test_commands.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit watch-scoped coordination**

```bash
git add \
  src/cinema_friend/telegram/wizard.py \
  src/cinema_friend/telegram/commands.py \
  src/cinema_friend/telegram/bot.py \
  tests/telegram/test_wizard.py \
  tests/telegram/test_bot.py \
  tests/telegram/test_commands.py
git commit -m "fix: scope initial empty deferrals to one watch"
```

### Task 3: Align documentation and verify the branch

**Files:**
- Modify: `docs/superpowers/specs/2026-08-13-recurring-watch-initial-empty-notification-design.md`

**Interfaces:**
- Consumes: the final payload and coordination contracts from Tasks 1 and 2.
- Produces: one internally consistent set of committed specifications.

- [ ] **Step 1: Align the original feature specification**

Update these sections in the original design without adding a change log:

- `Notification Decision`: `kind` remains `results`; initial empty is a boolean
  presentation decision.
- `Persistence and Idempotency`: payload JSON gains an optional field; no SQL migration.
- `Confirmation-first Delivery`: barrier is `(recipient_user_id, watch_id)` and the route
  prepares the deterministic ID before invoking the saga.
- `Rendering`: the worker reads the presentation flag from a result payload.
- `Testing` and `Acceptance Criteria`: include same-user unrelated-retry delivery and
  rollback generic rendering.

Keep the exact user-facing copy unchanged.

- [ ] **Step 2: Run targeted tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/services/test_notification_policy.py \
  tests/services/test_check_service.py \
  tests/storage/test_notification_repository.py \
  tests/telegram/test_wizard.py \
  tests/telegram/test_bot.py \
  tests/telegram/test_commands.py \
  tests/telegram/test_rendering.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run static checks**

Run:

```bash
.venv/bin/python -m ruff check .
MYPYPATH=src .venv/bin/python -m mypy
```

Expected: Ruff passes and mypy reports no issues in 47 source files.

- [ ] **Step 4: Run the full suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: the full suite passes.

- [ ] **Step 5: Inspect final branch state**

Run:

```bash
git diff --check
git status --short
git diff --stat origin/ncksol-recurring-watch-empty-reply...HEAD
```

Expected: no whitespace errors; only the planned compatibility, coordination, tests, and
specification files are changed relative to the current PR branch.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/superpowers/specs/2026-08-13-recurring-watch-initial-empty-notification-design.md
git commit -m "docs: align recurring watch delivery design"
```

- [ ] **Step 7: Report integration details**

Report:

- The three commit SHAs and subjects.
- The exact test, Ruff, and mypy results.
- Whether the branch is clean.
- Any review concern that remains unresolved.
- Do not open a separate pull request; the coordinator will integrate these commits into
  PR #3.
