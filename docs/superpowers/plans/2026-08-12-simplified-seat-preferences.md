# Simplified Seat Preferences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add simple `Only the best` and `Best and good` seat presets that hard-filter BFI seat maps while preserving the current manual controls as Advanced.

**Architecture:** Persist one `SeatPreferenceStrategy` on `WatchCriteria`, defaulting missing legacy data to `advanced`. A pure seat-bank helper owns the existing aisle geometry and identifies the unique center bank; block generation applies the selected row cutoff and center-bank filter before the existing ranking path. The Telegram wizard branches after quantity, using a draft flow-version marker to distinguish new drafts from legacy drafts that must continue as Advanced.

**Tech Stack:** Python 3.12, `python-telegram-bot`, dataclasses and enums, SQLite JSON via `aiosqlite`, pytest, pytest-asyncio, Ruff, mypy.

## Global Constraints

- Strategy values are exactly `advanced`, `only_best`, and `best_and_good`.
- Simple presets are hard eligibility rules; seats outside the preset must never be returned as lower-ranked alternatives.
- `only_best` permits the aisle-bounded center bank in row J and later ASCII row letters, inclusively.
- `best_and_good` permits the same center bank in row C and later ASCII row letters, inclusively.
- Center-bank geometry uses all parsed physical seats regardless of current availability.
- An ambiguous center-bank tie, insufficient aisle geometry, or a non-A-Z row label fails closed for that row and emits a diagnostic.
- Advanced retains the existing preferred-row, preferred-seat, excluded-row, excluded-seat, and ranking behavior.
- Simple criteria cannot contain any of the four manual preferred or excluded sets.
- Existing stored watches and interrupted drafts with no new fields behave as Advanced without user action.
- New drafts carry `seat_flow_version = 2` from creation; no SQL migration is added.
- The simple-choice prompt must explain both presets before presenting the buttons.
- Normal automated tests must not contact BFI.
- Use conventional commits and never add a `Co-authored-by` trailer.

---

## Planned File Layout

| Path | Responsibility |
|---|---|
| `src/cinema_friend/domain/state.py` | Define the persisted `SeatPreferenceStrategy` enum |
| `src/cinema_friend/domain/watch.py` | Store the strategy and reject conflicting simple/manual criteria |
| `src/cinema_friend/storage/watch_repository.py` | Encode the strategy and default missing legacy JSON to Advanced |
| `src/cinema_friend/watches/seat_banks.py` | Partition physical rows with the shared aisle rule and select a unique center bank |
| `src/cinema_friend/watches/blocks.py` | Preserve Advanced block generation and hard-filter Simple blocks |
| `src/cinema_friend/telegram/wizard.py` | Branch the persisted wizard, explain presets, resume legacy drafts, and render review text |
| `tests/domain/test_watch.py` | Verify strategy defaults and domain invariants |
| `tests/storage/test_watch_repository.py` | Verify explicit persistence and legacy decoding |
| `tests/watches/test_seat_banks.py` | Verify aisle, section, center, tie, and insufficient-geometry behavior |
| `tests/watches/test_blocks.py` | Verify preset row cutoffs and hard filtering without Advanced regressions |
| `tests/telegram/test_wizard.py` | Verify both wizard branches, callbacks, review text, persistence, and legacy draft recovery |
| `README.md` | Document Simple, Advanced, and both preset meanings |

---

### Task 1: Add the Domain Strategy and Backward-Compatible Persistence

**Files:**
- Modify: `src/cinema_friend/domain/state.py:8-19`
- Modify: `src/cinema_friend/domain/watch.py:14-54`
- Modify: `src/cinema_friend/storage/watch_repository.py:17-70`
- Modify: `tests/domain/test_watch.py:8-66`
- Modify: `tests/storage/test_watch_repository.py:1-90`

**Interfaces:**
- Produces: `SeatPreferenceStrategy` with `ADVANCED`, `ONLY_BEST`, and `BEST_AND_GOOD`
- Produces: `WatchCriteria.seat_preference_strategy: SeatPreferenceStrategy`
- Persists: `criteria_json["seat_preference_strategy"]`
- Compatibility: missing `criteria_json["seat_preference_strategy"]` decodes as `SeatPreferenceStrategy.ADVANCED`

- [ ] **Step 1: Write failing domain tests for the default and simple/manual invariant**

In `tests/domain/test_watch.py`, import `SeatPreferenceStrategy` and add:

```python
from cinema_friend.domain.state import (
    SeatPreferenceStrategy,
    WatchMode,
    WatchStatus,
)


def test_seat_preference_strategy_defaults_to_advanced():
    criteria = WatchCriteria(**_BASE)

    assert criteria.seat_preference_strategy is SeatPreferenceStrategy.ADVANCED


@pytest.mark.parametrize(
    "strategy",
    [
        SeatPreferenceStrategy.ONLY_BEST,
        SeatPreferenceStrategy.BEST_AND_GOOD,
    ],
)
def test_simple_seat_preference_accepts_empty_manual_criteria(
    strategy: SeatPreferenceStrategy,
):
    criteria = WatchCriteria(**_BASE, seat_preference_strategy=strategy)

    assert criteria.seat_preference_strategy is strategy


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("preferred_rows", frozenset({"L"})),
        ("preferred_seats", frozenset({"L17"})),
        ("excluded_rows", frozenset({"A"})),
        ("excluded_seats", frozenset({"A1"})),
    ],
)
def test_simple_seat_preference_rejects_manual_criteria(
    field: str,
    value: frozenset[str],
):
    with pytest.raises(InputError, match="manual seat criteria"):
        WatchCriteria(
            **_BASE,
            seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
            **{field: value},
        )
```

- [ ] **Step 2: Run the domain tests and verify the new enum is missing**

Run:

```bash
.venv/bin/python -m pytest tests/domain/test_watch.py -q
```

Expected: collection fails because `SeatPreferenceStrategy` cannot be imported.

- [ ] **Step 3: Implement the strategy enum and `WatchCriteria` invariant**

Add to `src/cinema_friend/domain/state.py` before `WatchMode`:

```python
class SeatPreferenceStrategy(Enum):
    ADVANCED = "advanced"
    ONLY_BEST = "only_best"
    BEST_AND_GOOD = "best_and_good"
```

In `src/cinema_friend/domain/watch.py`, import the enum:

```python
from cinema_friend.domain.state import (
    SeatPreferenceStrategy,
    WatchMode,
    WatchStatus,
)
```

Add this field after `interval`:

```python
    seat_preference_strategy: SeatPreferenceStrategy = SeatPreferenceStrategy.ADVANCED
```

Add this check at the start of `WatchCriteria.__post_init__`, after quantity validation:

```python
        manual_seat_criteria = (
            self.preferred_seats
            or self.excluded_seats
            or self.preferred_rows
            or self.excluded_rows
        )
        if (
            self.seat_preference_strategy is not SeatPreferenceStrategy.ADVANCED
            and manual_seat_criteria
        ):
            raise InputError(
                "simple seat preferences cannot be combined with manual seat criteria"
            )
```

- [ ] **Step 4: Run the domain tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/domain/test_watch.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Write failing repository tests for explicit and legacy JSON**

In `tests/storage/test_watch_repository.py`, add `json` and
`SeatPreferenceStrategy` imports:

```python
import json

from cinema_friend.domain.state import (
    SeatPreferenceStrategy,
    WatchMode,
    WatchStatus,
)
```

Add:

```python
@pytest.mark.parametrize(
    "strategy",
    [
        SeatPreferenceStrategy.ONLY_BEST,
        SeatPreferenceStrategy.BEST_AND_GOOD,
    ],
)
async def test_simple_seat_preference_strategy_round_trips(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
    strategy: SeatPreferenceStrategy,
) -> None:
    watch = _watch(
        criteria=_criteria(
            seat_preference_strategy=strategy
        )
    )

    await repo.create(conn, watch)

    assert await repo.get(conn, watch.watch_id) == watch


async def test_new_criteria_json_writes_an_explicit_advanced_strategy(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    await repo.create(conn, _watch())

    cursor = await conn.execute("SELECT criteria_json FROM watches WHERE id = ?", (str(_uuid(1)),))
    row = await cursor.fetchone()

    assert row is not None
    assert json.loads(row["criteria_json"])["seat_preference_strategy"] == "advanced"


async def test_legacy_criteria_without_a_strategy_decode_as_advanced(
    conn: aiosqlite.Connection,
    repo: WatchRepository,
) -> None:
    watch = _watch()
    await repo.create(conn, watch)
    cursor = await conn.execute("SELECT criteria_json FROM watches WHERE id = ?", (str(watch.watch_id),))
    row = await cursor.fetchone()
    assert row is not None
    legacy_payload = json.loads(row["criteria_json"])
    legacy_payload.pop("seat_preference_strategy", None)
    await conn.execute(
        "UPDATE watches SET criteria_json = ? WHERE id = ?",
        (json.dumps(legacy_payload, sort_keys=True), str(watch.watch_id)),
    )

    fetched = await repo.get(conn, watch.watch_id)

    assert fetched is not None
    assert fetched.criteria.seat_preference_strategy is SeatPreferenceStrategy.ADVANCED
```

- [ ] **Step 6: Run the repository tests and verify persistence is incomplete**

Run:

```bash
.venv/bin/python -m pytest \
  tests/storage/test_watch_repository.py::test_simple_seat_preference_strategy_round_trips \
  tests/storage/test_watch_repository.py::test_new_criteria_json_writes_an_explicit_advanced_strategy \
  tests/storage/test_watch_repository.py::test_legacy_criteria_without_a_strategy_decode_as_advanced \
  -q
```

Expected: the explicit JSON assertion fails and the simple round trip decodes as Advanced.

- [ ] **Step 7: Encode the strategy and default legacy JSON**

In `src/cinema_friend/storage/watch_repository.py`, import
`SeatPreferenceStrategy`:

```python
from cinema_friend.domain.state import (
    SeatPreferenceStrategy,
    WatchMode,
    WatchStatus,
)
```

Add to `_encode_criteria`:

```python
        "seat_preference_strategy": criteria.seat_preference_strategy.value,
```

Add to the `WatchCriteria` constructor in `_decode_criteria`:

```python
        seat_preference_strategy=SeatPreferenceStrategy(
            payload.get(
                "seat_preference_strategy",
                SeatPreferenceStrategy.ADVANCED.value,
            )
        ),
```

- [ ] **Step 8: Run the focused domain and repository suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/domain/test_watch.py \
  tests/storage/test_watch_repository.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit the domain and persistence slice**

```bash
git add \
  src/cinema_friend/domain/state.py \
  src/cinema_friend/domain/watch.py \
  src/cinema_friend/storage/watch_repository.py \
  tests/domain/test_watch.py \
  tests/storage/test_watch_repository.py
git commit \
  -m "feat: persist seat preference strategies" \
  -m "Copilot-Session: eff05fd4-5274-496a-9140-639ed1bf56cf"
```

---

### Task 2: Share Aisle Geometry and Hard-Filter Simple Presets

**Files:**
- Create: `src/cinema_friend/watches/seat_banks.py`
- Create: `tests/watches/test_seat_banks.py`
- Modify: `src/cinema_friend/watches/blocks.py:1-107`
- Modify: `tests/watches/test_blocks.py:7-221`
- Verify: `tests/watches/test_ranking.py`

**Interfaces:**
- Produces: `partition_seat_banks(row_seats: Sequence[Seat]) -> tuple[tuple[Seat, ...], ...] | None`
- Produces: `center_seat_bank(row_seats: Sequence[Seat]) -> tuple[Seat, ...] | None`
- Consumes: `WatchCriteria.seat_preference_strategy`
- Preserves: `generate_blocks(seat_map: SeatMap, criteria: WatchCriteria) -> tuple[SeatBlock, ...]`

- [ ] **Step 1: Write failing unit tests for physical bank partitioning**

Create `tests/watches/test_seat_banks.py`:

```python
from __future__ import annotations

from cinema_friend.domain.bfi import PriceZone, Seat, SeatStatus
from cinema_friend.watches.seat_banks import (
    center_seat_bank,
    partition_seat_banks,
)

_ZONE = PriceZone(zone_id="z1", label="Premium", price=None)


def seat(
    column: int,
    x: float,
    *,
    section: str = "BFI IMAX",
    status: SeatStatus = SeatStatus.AVAILABLE,
) -> Seat:
    return Seat(
        seat_id=f"{section}-{column}",
        raw_status_code="A" if status is SeatStatus.AVAILABLE else "S",
        status=status,
        zone=_ZONE,
        note="",
        section=section,
        row="J",
        column=column,
        x=x,
        y=100.0,
    )


def test_partition_seat_banks_splits_on_oversized_aisle_gaps() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 70.0, 80.0, 90.0, 140.0, 150.0, 160.0],
            start=1,
        )
    ]

    banks = partition_seat_banks(row)

    assert banks is not None
    assert [[seat.column for seat in bank] for bank in banks] == [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
    ]
    center = center_seat_bank(row)
    assert center is not None
    assert [seat.column for seat in center] == [4, 5, 6]


def test_center_seat_bank_uses_unavailable_seats_to_hold_the_physical_layout() -> None:
    row = [
        seat(
            column,
            x,
            status=SeatStatus.SOLD if column == 5 else SeatStatus.AVAILABLE,
        )
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 70.0, 80.0, 90.0, 140.0, 150.0, 160.0],
            start=1,
        )
    ]

    center = center_seat_bank(row)

    assert center is not None
    assert [seat.column for seat in center] == [4, 5, 6]


def test_partition_seat_banks_splits_on_section_boundaries() -> None:
    row = [
        *[seat(index, x, section="Left") for index, x in enumerate([0.0, 10.0, 20.0, 30.0], 1)],
        *[seat(index, x, section="Center") for index, x in enumerate([70.0, 80.0, 90.0, 100.0], 1)],
        *[seat(index, x, section="Right") for index, x in enumerate([140.0, 150.0, 160.0, 170.0], 1)],
    ]

    center = center_seat_bank(row)

    assert center is not None
    assert {seat.section for seat in center} == {"Center"}


def test_center_seat_bank_rejects_an_exact_two_bank_tie() -> None:
    row = [
        seat(column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 30.0, 70.0, 80.0, 90.0, 100.0],
            start=1,
        )
    ]

    assert center_seat_bank(row) is None


def test_partition_seat_banks_rejects_insufficient_geometry() -> None:
    row = [seat(1, 0.0), seat(2, 10.0), seat(3, 20.0)]

    assert partition_seat_banks(row) is None
    assert center_seat_bank(row) is None
```

- [ ] **Step 2: Run the helper tests and verify the module is missing**

Run:

```bash
.venv/bin/python -m pytest tests/watches/test_seat_banks.py -q
```

Expected: collection fails because `cinema_friend.watches.seat_banks` does not exist.

- [ ] **Step 3: Implement the shared aisle and center-bank helper**

Create `src/cinema_friend/watches/seat_banks.py`:

```python
"""Physical seat-bank geometry shared by adjacency and preference filtering."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from itertools import pairwise

from cinema_friend.domain.bfi import Seat

_AISLE_THRESHOLD_MULTIPLIER = 1.75
_MIN_USABLE_GAPS = 3


def _normal_gap(section_seats: Sequence[Seat]) -> float | None:
    ordered = sorted(section_seats, key=lambda seat: seat.column)
    if (
        not ordered
        or max(seat.x for seat in ordered) - min(seat.x for seat in ordered)
        == 0
    ):
        return None
    gaps = [
        abs(right.x - left.x)
        for left, right in pairwise(ordered)
        if right.column == left.column + 1
    ]
    if len(gaps) < _MIN_USABLE_GAPS:
        return None
    return statistics.median(gaps)


def partition_seat_banks(
    row_seats: Sequence[Seat],
) -> tuple[tuple[Seat, ...], ...] | None:
    """Return aisle- and section-bounded physical banks, or None if geometry is weak."""
    if not row_seats:
        return None
    seats_by_section: dict[str, list[Seat]] = defaultdict(list)
    for seat in row_seats:
        seats_by_section[seat.section].append(seat)

    banks: list[tuple[Seat, ...]] = []
    for section in sorted(seats_by_section):
        ordered = sorted(seats_by_section[section], key=lambda seat: seat.column)
        normal_gap = _normal_gap(ordered)
        if normal_gap is None:
            return None
        threshold = _AISLE_THRESHOLD_MULTIPLIER * normal_gap
        current = [ordered[0]]
        for left, right in pairwise(ordered):
            if (
                right.column != left.column + 1
                or abs(right.x - left.x) > threshold
            ):
                banks.append(tuple(current))
                current = [right]
            else:
                current.append(right)
        banks.append(tuple(current))

    return tuple(
        sorted(
            banks,
            key=lambda bank: (
                min(seat.x for seat in bank),
                bank[0].section,
                bank[0].column,
            ),
        )
    )


def center_seat_bank(row_seats: Sequence[Seat]) -> tuple[Seat, ...] | None:
    """Return the unique bank nearest the physical row center, failing closed on ties."""
    banks = partition_seat_banks(row_seats)
    if not banks:
        return None
    row_center = (
        min(seat.x for seat in row_seats) + max(seat.x for seat in row_seats)
    ) / 2
    distances = [
        abs(
            (
                min(seat.x for seat in bank)
                + max(seat.x for seat in bank)
            )
            / 2
            - row_center
        )
        for bank in banks
    ]
    nearest = min(distances)
    winners = [
        bank
        for bank, distance in zip(banks, distances, strict=True)
        if distance == nearest
    ]
    return winners[0] if len(winners) == 1 else None
```

- [ ] **Step 4: Run the helper tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/watches/test_seat_banks.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Write failing block-generation tests for both presets**

In `tests/watches/test_blocks.py`, import `logging`, `pytest`, and
`SeatPreferenceStrategy`, and extend `criteria_for`:

```python
import logging

import pytest

from cinema_friend.domain.state import SeatPreferenceStrategy, WatchMode


def criteria_for(
    *,
    quantity: int = 2,
    excluded_seats: frozenset[str] = frozenset(),
    excluded_rows: frozenset[str] = frozenset(),
    seat_preference_strategy: SeatPreferenceStrategy = SeatPreferenceStrategy.ADVANCED,
) -> WatchCriteria:
    return WatchCriteria(
        source_url="https://whatson.bfi.org.uk/imax/Online/default.asp",
        slug="dog-stars",
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 27),
        time_from=time(0, 0),
        time_to=time(23, 59),
        quantity=quantity,
        mode=WatchMode.ONE_OFF,
        excluded_seats=excluded_seats,
        excluded_rows=excluded_rows,
        seat_preference_strategy=seat_preference_strategy,
    )
```

Add these helpers and tests:

```python
def _three_bank_row(row: str) -> list[Seat]:
    return [
        seat(row, column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 70.0, 80.0, 90.0, 140.0, 150.0, 160.0],
            start=1,
        )
    ]


@pytest.mark.parametrize(
    ("strategy", "front_row", "cutoff_row", "back_row"),
    [
        (SeatPreferenceStrategy.ONLY_BEST, "I", "J", "K"),
        (SeatPreferenceStrategy.BEST_AND_GOOD, "B", "C", "D"),
    ],
)
def test_simple_strategy_keeps_only_the_center_bank_at_and_behind_its_cutoff(
    strategy: SeatPreferenceStrategy,
    front_row: str,
    cutoff_row: str,
    back_row: str,
) -> None:
    seats = [
        *_three_bank_row(front_row),
        *_three_bank_row(cutoff_row),
        *_three_bank_row(back_row),
    ]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))

    blocks = generate_blocks(
        seat_map,
        criteria_for(quantity=2, seat_preference_strategy=strategy),
    )

    assert {(block.row, tuple(seat.column for seat in block.seats)) for block in blocks} == {
        (cutoff_row, (4, 5)),
        (cutoff_row, (5, 6)),
        (back_row, (4, 5)),
        (back_row, (5, 6)),
    }


def test_simple_quantity_one_still_requires_the_center_bank() -> None:
    seat_map = SeatMap(performance_id="p1", seats=tuple(_three_bank_row("J")))

    blocks = generate_blocks(
        seat_map,
        criteria_for(
            quantity=1,
            seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
        ),
    )

    assert {block.seats[0].label for block in blocks} == {"J4", "J5", "J6"}


def test_simple_strategy_fails_closed_and_logs_when_center_geometry_is_ambiguous(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seats = [
        seat("J", column, x)
        for column, x in enumerate(
            [0.0, 10.0, 20.0, 30.0, 70.0, 80.0, 90.0, 100.0],
            start=1,
        )
    ]
    seat_map = SeatMap(performance_id="p1", seats=tuple(seats))

    with caplog.at_level(logging.WARNING):
        blocks = generate_blocks(
            seat_map,
            criteria_for(
                quantity=1,
                seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
            ),
        )

    assert blocks == ()
    assert "could not identify a unique center bank" in caplog.text


def test_simple_strategy_fails_closed_and_logs_when_geometry_is_insufficient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seat_map = SeatMap(
        performance_id="p1",
        seats=(
            seat("J", 1, 0.0),
            seat("J", 2, 10.0),
            seat("J", 3, 20.0),
        ),
    )

    with caplog.at_level(logging.WARNING):
        blocks = generate_blocks(
            seat_map,
            criteria_for(
                quantity=1,
                seat_preference_strategy=SeatPreferenceStrategy.ONLY_BEST,
            ),
        )

    assert blocks == ()
    assert "could not identify a unique center bank" in caplog.text


def test_simple_strategy_fails_closed_and_logs_for_an_unsupported_row_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seat_map = SeatMap(performance_id="p1", seats=tuple(_three_bank_row("AA")))

    with caplog.at_level(logging.WARNING):
        blocks = generate_blocks(
            seat_map,
            criteria_for(
                quantity=1,
                seat_preference_strategy=SeatPreferenceStrategy.BEST_AND_GOOD,
            ),
        )

    assert blocks == ()
    assert "unsupported row label" in caplog.text
```

- [ ] **Step 6: Run the new block tests and verify Simple still admits outer seats**

Run:

```bash
.venv/bin/python -m pytest \
  tests/watches/test_blocks.py::test_simple_strategy_keeps_only_the_center_bank_at_and_behind_its_cutoff \
  tests/watches/test_blocks.py::test_simple_quantity_one_still_requires_the_center_bank \
  tests/watches/test_blocks.py::test_simple_strategy_fails_closed_and_logs_when_center_geometry_is_ambiguous \
  tests/watches/test_blocks.py::test_simple_strategy_fails_closed_and_logs_when_geometry_is_insufficient \
  tests/watches/test_blocks.py::test_simple_strategy_fails_closed_and_logs_for_an_unsupported_row_label \
  -q
```

Expected: the preset tests fail because `generate_blocks` does not inspect the strategy.

- [ ] **Step 7: Refactor Advanced adjacency onto the shared bank helper**

In `src/cinema_friend/watches/blocks.py`:

- Remove the `statistics` import and the two aisle constants.
- Add `import logging`.
- Import `SeatPreferenceStrategy`.
- Import `center_seat_bank` and `partition_seat_banks`.
- Add `logger = logging.getLogger(__name__)`.

Replace `_median_gap` and the current row-run implementation with:

```python
def _emit_windows(
    blocks: list[SeatBlock],
    row: str,
    run: list[Seat],
    quantity: int,
) -> None:
    for start in range(len(run) - quantity + 1):
        blocks.append(SeatBlock(row=row, seats=tuple(run[start : start + quantity])))


def _generate_bank_blocks(
    row: str,
    bank: tuple[Seat, ...],
    criteria: WatchCriteria,
) -> list[SeatBlock]:
    blocks: list[SeatBlock] = []
    run: list[Seat] = []
    for seat in bank:
        if not _is_purchasable(seat, criteria):
            _emit_windows(blocks, row, run, criteria.quantity)
            run = []
            continue
        run.append(seat)
    _emit_windows(blocks, row, run, criteria.quantity)
    return blocks


def _generate_advanced_row_blocks(
    row: str,
    row_seats: list[Seat],
    criteria: WatchCriteria,
) -> list[SeatBlock]:
    if criteria.quantity == 1:
        return [
            SeatBlock(row=row, seats=(seat,))
            for seat in sorted(row_seats, key=lambda seat: seat.column)
            if _is_purchasable(seat, criteria)
        ]
    banks = partition_seat_banks(row_seats)
    if banks is None:
        return []
    return [
        block
        for bank in banks
        for block in _generate_bank_blocks(row, bank, criteria)
    ]
```

Keep the Advanced branch grouped by `(section, row)` and call
`_generate_advanced_row_blocks`. This preserves quantity-one behavior and keeps section
identity mandatory.

- [ ] **Step 8: Add the Simple row cutoff and center-bank path**

Add to `src/cinema_friend/watches/blocks.py`:

```python
_MINIMUM_ROW = {
    SeatPreferenceStrategy.ONLY_BEST: "J",
    SeatPreferenceStrategy.BEST_AND_GOOD: "C",
}


def _simple_row_is_allowed(
    row: str,
    strategy: SeatPreferenceStrategy,
) -> bool | None:
    normalized = row.upper()
    if len(normalized) != 1 or not ("A" <= normalized <= "Z"):
        return None
    return normalized >= _MINIMUM_ROW[strategy]


def _generate_simple_blocks(
    seat_map: SeatMap,
    criteria: WatchCriteria,
) -> list[SeatBlock]:
    seats_by_row: dict[str, list[Seat]] = defaultdict(list)
    for seat in seat_map.seats:
        seats_by_row[seat.row].append(seat)

    blocks: list[SeatBlock] = []
    for row, row_seats in seats_by_row.items():
        row_allowed = _simple_row_is_allowed(
            row,
            criteria.seat_preference_strategy,
        )
        if row_allowed is None:
            logger.warning(
                "simple seat preference excluded unsupported row label",
                extra={
                    "performance_id": seat_map.performance_id,
                    "row": row,
                    "strategy": criteria.seat_preference_strategy.value,
                },
            )
            continue
        if not row_allowed:
            continue
        bank = center_seat_bank(row_seats)
        if bank is None:
            logger.warning(
                "simple seat preference could not identify a unique center bank",
                extra={
                    "performance_id": seat_map.performance_id,
                    "row": row,
                    "strategy": criteria.seat_preference_strategy.value,
                },
            )
            continue
        blocks.extend(_generate_bank_blocks(row, bank, criteria))
    return blocks
```

Replace `generate_blocks` with an explicit branch:

```python
def generate_blocks(
    seat_map: SeatMap,
    criteria: WatchCriteria,
) -> tuple[SeatBlock, ...]:
    if criteria.seat_preference_strategy is not SeatPreferenceStrategy.ADVANCED:
        return tuple(_generate_simple_blocks(seat_map, criteria))

    seats_by_section_row: dict[tuple[str, str], list[Seat]] = defaultdict(list)
    for seat in seat_map.seats:
        seats_by_section_row[(seat.section, seat.row)].append(seat)

    blocks: list[SeatBlock] = []
    for (_section, row), row_seats in seats_by_section_row.items():
        if row in criteria.excluded_rows:
            continue
        blocks.extend(_generate_advanced_row_blocks(row, row_seats, criteria))
    return tuple(blocks)
```

Update the docstring to state that Advanced keeps explicit exclusions while Simple admits
only the selected center bank and row range.

- [ ] **Step 9: Run helper, block, and ranking regression suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/watches/test_seat_banks.py \
  tests/watches/test_blocks.py \
  tests/watches/test_ranking.py \
  -q
```

Expected: all tests pass, including every pre-existing Advanced adjacency and ranking test.

- [ ] **Step 10: Commit the geometry and filtering slice**

```bash
git add \
  src/cinema_friend/watches/seat_banks.py \
  src/cinema_friend/watches/blocks.py \
  tests/watches/test_seat_banks.py \
  tests/watches/test_blocks.py
git commit \
  -m "feat: filter simple seat preference presets" \
  -m "Copilot-Session: eff05fd4-5274-496a-9140-639ed1bf56cf"
```

---

### Task 3: Branch the Telegram Wizard and Document the User Flow

**Files:**
- Modify: `src/cinema_friend/telegram/wizard.py:1-718`
- Modify: `tests/telegram/test_wizard.py:28-670`
- Modify: `README.md:245-263`

**Interfaces:**
- Produces wizard states: `AWAIT_SEAT_MODE`, `AWAIT_SIMPLE_SEAT_PREFERENCE`
- Produces callbacks: `wizard:seat-mode:simple`, `wizard:seat-mode:advanced`
- Produces callbacks: `wizard:seat-preference:only_best`, `wizard:seat-preference:best_and_good`
- Persists new draft marker: `seat_flow_version = 2`
- Persists draft strategy: `seat_preference_strategy`
- Compatibility: legacy drafts without `seat_flow_version` continue directly to the Advanced prompts

- [ ] **Step 1: Update shared wizard test drivers for the new Advanced branch**

In `tests/telegram/test_wizard.py`, import `SeatPreferenceStrategy`:

```python
from cinema_friend.domain.state import (
    CheckOutcome,
    CheckTrigger,
    SeatPreferenceStrategy,
)
```

After each quantity callback in `_drive_to_review`, `_drive_to_preferred_instant`, and
`_drive_recurring_to_interval`, add:

```python
    assert (
        await handle_wizard_callback(
            _callback_update("wizard:seat-mode:advanced"),
            deps,
        )
    ) is not None
```

Change new-draft payload assertions from `{}` to:

```python
    assert draft.payload == {"seat_flow_version": 2}
```

Change the invalid-URL unchanged-payload assertion to the same value. Change quantity
success assertions to expect `WizardState.AWAIT_SEAT_MODE`.

- [ ] **Step 2: Write failing tests for Simple, Advanced, explanations, and review text**

Add this test helper:

```python
async def _drive_to_quantity(deps: WizardDeps) -> None:
    await start_new(_text_update("/new"), deps)
    await handle_wizard_text(_text_update(FILM_URL), deps)
    await handle_wizard_text(_text_update("2026-08-26 to 2026-08-30"), deps)
    await handle_wizard_text(_text_update("18:00 to 23:00"), deps)


def _callback_data(message: object) -> set[str]:
    reply_markup = getattr(message, "reply_markup")
    assert reply_markup is not None
    return {
        button.callback_data
        for row in reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }
```

Add:

```python
async def test_new_quantity_choice_prompts_for_simple_or_advanced(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:qty:2"),
        deps,
    )

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_SEAT_MODE.value
    assert _callback_data(reply) == {
        "wizard:seat-mode:simple",
        "wizard:seat-mode:advanced",
    }


async def test_simple_mode_explains_both_presets_before_the_buttons(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    assert reply is not None
    assert "Only the best" in reply.text
    assert "between the aisles" in reply.text
    assert "row J" in reply.text
    assert "Best and good" in reply.text
    assert "row C" in reply.text
    assert _callback_data(reply) == {
        "wizard:seat-preference:only_best",
        "wizard:seat-preference:best_and_good",
    }


@pytest.mark.parametrize(
    ("callback", "strategy"),
    [
        (
            "wizard:seat-preference:only_best",
            SeatPreferenceStrategy.ONLY_BEST,
        ),
        (
            "wizard:seat-preference:best_and_good",
            SeatPreferenceStrategy.BEST_AND_GOOD,
        ),
    ],
)
async def test_simple_preset_skips_all_manual_seat_prompts(
    deps: WizardDeps,
    callback: str,
    strategy: SeatPreferenceStrategy,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    await handle_wizard_callback(_callback_update(callback), deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_INSTANT.value
    assert draft.payload["seat_preference_strategy"] == strategy.value
    assert {
        "preferred_rows",
        "preferred_seats",
        "excluded_rows",
        "excluded_seats",
    }.isdisjoint(draft.payload)


async def test_advanced_mode_keeps_the_existing_manual_flow(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)

    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:advanced"),
        deps,
    )

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_ROWS.value
    assert draft.payload["seat_preference_strategy"] == "advanced"


@pytest.mark.parametrize(
    ("callback", "label"),
    [
        ("wizard:seat-preference:only_best", "Only the best"),
        ("wizard:seat-preference:best_and_good", "Best and good"),
    ],
)
async def test_simple_review_names_the_selected_preset(
    deps: WizardDeps,
    callback: str,
    label: str,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )
    await handle_wizard_callback(
        _callback_update(callback),
        deps,
    )
    await handle_wizard_text(_text_update("skip"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:mode:one_off"),
        deps,
    )

    assert reply is not None
    assert f"Seat preference: {label}" in reply.text
    assert "Preferred rows:" not in reply.text
    assert "Excluded seats:" not in reply.text


async def test_advanced_review_keeps_manual_seat_details(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:advanced"),
        deps,
    )
    await handle_wizard_text(_text_update("L"), deps)
    await handle_wizard_text(_text_update("L17"), deps)
    await handle_wizard_text(_text_update("A"), deps)
    await handle_wizard_text(_text_update("A1"), deps)
    await handle_wizard_text(_text_update("skip"), deps)

    reply = await handle_wizard_callback(
        _callback_update("wizard:mode:one_off"),
        deps,
    )

    assert reply is not None
    assert "Preferred rows: L" in reply.text
    assert "Preferred seats: L17" in reply.text
    assert "Excluded rows: A" in reply.text
    assert "Excluded seats: A1" in reply.text
```

- [ ] **Step 3: Write failing tests for invalid callbacks and legacy drafts**

Add:

```python
async def test_invalid_simple_preset_callback_leaves_the_draft_unchanged(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    reply = await handle_wizard_callback(
        _callback_update("wizard:seat-preference:advanced"),
        deps,
    )

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE.value
    assert "seat_preference_strategy" not in draft.payload


async def test_stale_seat_callback_leaves_the_current_step_unchanged(
    deps: WizardDeps,
) -> None:
    await _drive_to_quantity(deps)
    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)
    await handle_wizard_callback(
        _callback_update("wizard:seat-mode:advanced"),
        deps,
    )

    reply = await handle_wizard_callback(
        _callback_update("wizard:seat-mode:simple"),
        deps,
    )

    assert reply is not None
    assert "no longer active" in reply.text
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_ROWS.value
    assert draft.payload["seat_preference_strategy"] == "advanced"


async def test_legacy_quantity_draft_continues_as_advanced(
    deps: WizardDeps,
) -> None:
    await _seed_state(deps, WizardState.AWAIT_QUANTITY, {})

    await handle_wizard_callback(_callback_update("wizard:qty:2"), deps)

    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == WizardState.AWAIT_PREFERRED_ROWS.value
    assert draft.payload["seat_preference_strategy"] == "advanced"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (WizardState.AWAIT_PREFERRED_ROWS, WizardState.AWAIT_PREFERRED_SEATS),
        (WizardState.AWAIT_PREFERRED_SEATS, WizardState.AWAIT_EXCLUDED_ROWS),
        (WizardState.AWAIT_EXCLUDED_ROWS, WizardState.AWAIT_EXCLUDED_SEATS),
        (WizardState.AWAIT_EXCLUDED_SEATS, WizardState.AWAIT_PREFERRED_INSTANT),
        (WizardState.AWAIT_PREFERRED_INSTANT, WizardState.AWAIT_MODE),
    ],
)
async def test_legacy_manual_draft_resumes_without_a_flow_marker(
    deps: WizardDeps,
    state: WizardState,
    expected: WizardState,
) -> None:
    await _seed_state(deps, state, {})

    reply = await handle_wizard_text(_text_update("skip"), deps)

    assert reply is not None
    draft = await _draft(deps)
    assert draft is not None
    assert draft.state == expected.value
    assert "seat_flow_version" not in draft.payload


async def test_legacy_review_draft_confirms_as_advanced(
    deps: WizardDeps,
) -> None:
    await _drive_to_review(deps)
    draft = await _draft(deps)
    assert draft is not None
    legacy_payload = dict(draft.payload)
    legacy_payload.pop("seat_flow_version")
    legacy_payload.pop("seat_preference_strategy")
    await _seed_state(deps, WizardState.REVIEW, legacy_payload)

    await handle_wizard_callback(_callback_update("wizard:confirm"), deps)

    (watch,) = await deps.watches.list_for_owner(USER_ID)
    assert (
        watch.criteria.seat_preference_strategy
        is SeatPreferenceStrategy.ADVANCED
    )
```

- [ ] **Step 4: Run the focused wizard tests and verify the states are missing**

Run:

```bash
.venv/bin/python -m pytest \
  tests/telegram/test_wizard.py::test_new_quantity_choice_prompts_for_simple_or_advanced \
  tests/telegram/test_wizard.py::test_simple_mode_explains_both_presets_before_the_buttons \
  tests/telegram/test_wizard.py::test_simple_preset_skips_all_manual_seat_prompts \
  tests/telegram/test_wizard.py::test_advanced_mode_keeps_the_existing_manual_flow \
  tests/telegram/test_wizard.py::test_simple_review_names_the_selected_preset \
  tests/telegram/test_wizard.py::test_advanced_review_keeps_manual_seat_details \
  tests/telegram/test_wizard.py::test_invalid_simple_preset_callback_leaves_the_draft_unchanged \
  tests/telegram/test_wizard.py::test_stale_seat_callback_leaves_the_current_step_unchanged \
  tests/telegram/test_wizard.py::test_legacy_quantity_draft_continues_as_advanced \
  tests/telegram/test_wizard.py::test_legacy_manual_draft_resumes_without_a_flow_marker \
  tests/telegram/test_wizard.py::test_legacy_review_draft_confirms_as_advanced \
  -q
```

Expected: collection or assertions fail because the new wizard states and callbacks do not
exist.

- [ ] **Step 5: Add the persisted wizard states, callback prefixes, and prompts**

In `src/cinema_friend/telegram/wizard.py`:

1. Import `SeatPreferenceStrategy` with `WatchMode`.
2. Add:

```python
_SEAT_FLOW_VERSION = 2
_SEAT_FLOW_VERSION_KEY = "seat_flow_version"
_SEAT_PREFERENCE_KEY = "seat_preference_strategy"


class SeatSetupMode(str, Enum):
    SIMPLE = "simple"
    ADVANCED = "advanced"
```

3. Add to `WizardState` after `AWAIT_QUANTITY`:

```python
    AWAIT_SEAT_MODE = "await_seat_mode"
    AWAIT_SIMPLE_SEAT_PREFERENCE = "await_simple_seat_preference"
```

4. Add callback prefixes:

```python
_SEAT_MODE_PREFIX = "wizard:seat-mode:"
_SEAT_PREFERENCE_PREFIX = "wizard:seat-preference:"
```

5. Add prompt functions:

```python
def _seat_mode_prompt() -> RenderedMessage:
    rows = [
        [
            InlineKeyboardButton(
                text="Simple",
                callback_data=f"{_SEAT_MODE_PREFIX}{SeatSetupMode.SIMPLE.value}",
            ),
            InlineKeyboardButton(
                text="Advanced",
                callback_data=f"{_SEAT_MODE_PREFIX}{SeatSetupMode.ADVANCED.value}",
            ),
        ]
    ]
    return RenderedMessage(
        text="How would you like to choose acceptable seats?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _simple_seat_preference_prompt() -> RenderedMessage:
    rows = [
        [
            InlineKeyboardButton(
                text="Only the best",
                callback_data=(
                    f"{_SEAT_PREFERENCE_PREFIX}"
                    f"{SeatPreferenceStrategy.ONLY_BEST.value}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                text="Best and good",
                callback_data=(
                    f"{_SEAT_PREFERENCE_PREFIX}"
                    f"{SeatPreferenceStrategy.BEST_AND_GOOD.value}"
                ),
            )
        ],
    ]
    return RenderedMessage(
        text=(
            "Choose a simple seat preference:\n\n"
            "<b>Only the best</b>: middle seating bank between the aisles, "
            "row J or farther back.\n"
            "<b>Best and good</b>: the same middle bank, row C or farther back."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
```

6. Return these prompts from `_prompt_for`:

```python
    if state is WizardState.AWAIT_SEAT_MODE:
        return _seat_mode_prompt()
    if state is WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE:
        return _simple_seat_preference_prompt()
```

7. Replace `_CALLBACK_STATES` with:

```python
_CALLBACK_STATES = frozenset(
    {
        WizardState.AWAIT_QUANTITY,
        WizardState.AWAIT_SEAT_MODE,
        WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE,
        WizardState.AWAIT_MODE,
    }
)
```

8. Update the module docstring's callback-state description to include the new button
   states.

- [ ] **Step 6: Parse the draft strategy and render the review**

Add:

```python
def _seat_preference_strategy(
    payload: Mapping[str, Any],
) -> SeatPreferenceStrategy:
    raw = payload.get(
        _SEAT_PREFERENCE_KEY,
        SeatPreferenceStrategy.ADVANCED.value,
    )
    try:
        return SeatPreferenceStrategy(raw)
    except (TypeError, ValueError) as exc:
        raise InputError("seat preference strategy is invalid") from exc
```

Pass this value into `_build_criteria`:

```python
        seat_preference_strategy=_seat_preference_strategy(payload),
```

In `_review_prompt`, branch before the four existing manual lines:

```python
    if criteria.seat_preference_strategy is SeatPreferenceStrategy.ONLY_BEST:
        lines.append("Seat preference: Only the best")
    elif criteria.seat_preference_strategy is SeatPreferenceStrategy.BEST_AND_GOOD:
        lines.append("Seat preference: Best and good")
    else:
        if criteria.preferred_rows:
            lines.append(f"Preferred rows: {', '.join(sorted(criteria.preferred_rows))}")
        if criteria.preferred_seats:
            lines.append(f"Preferred seats: {', '.join(sorted(criteria.preferred_seats))}")
        if criteria.excluded_rows:
            lines.append(f"Excluded rows: {', '.join(sorted(criteria.excluded_rows))}")
        if criteria.excluded_seats:
            lines.append(f"Excluded seats: {', '.join(sorted(criteria.excluded_seats))}")
```

Remove the old unconditional manual-detail block.

- [ ] **Step 7: Implement new and legacy callback transitions**

Replace `_apply_callback` with the same existing quantity and watch-mode validation plus
these explicit branches:

```python
def _apply_callback(
    state: WizardState,
    payload: dict[str, Any],
    data: str,
) -> tuple[WizardState, dict[str, Any]]:
    if state is WizardState.AWAIT_QUANTITY:
        if not data.startswith(_QTY_PREFIX):
            raise InputError("choose a quantity using the buttons above")
        try:
            quantity = int(data[len(_QTY_PREFIX) :])
        except ValueError as exc:
            raise InputError("choose a quantity using the buttons above") from exc
        if not (1 <= quantity <= 8):
            raise InputError("choose a quantity between 1 and 8")
        payload["quantity"] = quantity
        if payload.get(_SEAT_FLOW_VERSION_KEY) == _SEAT_FLOW_VERSION:
            return WizardState.AWAIT_SEAT_MODE, payload
        payload[_SEAT_PREFERENCE_KEY] = SeatPreferenceStrategy.ADVANCED.value
        return WizardState.AWAIT_PREFERRED_ROWS, payload

    if state is WizardState.AWAIT_SEAT_MODE:
        if not data.startswith(_SEAT_MODE_PREFIX):
            raise InputError("choose Simple or Advanced using the buttons above")
        try:
            seat_mode = SeatSetupMode(data[len(_SEAT_MODE_PREFIX) :])
        except ValueError as exc:
            raise InputError(
                "choose Simple or Advanced using the buttons above"
            ) from exc
        if seat_mode is SeatSetupMode.ADVANCED:
            payload[_SEAT_PREFERENCE_KEY] = SeatPreferenceStrategy.ADVANCED.value
            return WizardState.AWAIT_PREFERRED_ROWS, payload
        return WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE, payload

    if state is WizardState.AWAIT_SIMPLE_SEAT_PREFERENCE:
        if not data.startswith(_SEAT_PREFERENCE_PREFIX):
            raise InputError("choose one of the seat preferences above")
        try:
            strategy = SeatPreferenceStrategy(
                data[len(_SEAT_PREFERENCE_PREFIX) :]
            )
        except ValueError as exc:
            raise InputError("choose one of the seat preferences above") from exc
        if strategy is SeatPreferenceStrategy.ADVANCED:
            raise InputError("choose one of the seat preferences above")
        payload[_SEAT_PREFERENCE_KEY] = strategy.value
        for key in (
            "preferred_rows",
            "preferred_seats",
            "excluded_rows",
            "excluded_seats",
        ):
            payload.pop(key, None)
        return WizardState.AWAIT_PREFERRED_INSTANT, payload

    if not data.startswith(_MODE_PREFIX):
        raise InputError("choose one-off or recurring using the buttons above")
    mode_value = data[len(_MODE_PREFIX) :]
    try:
        mode = WatchMode(mode_value)
    except ValueError as exc:
        raise InputError("choose one-off or recurring using the buttons above") from exc
    payload["mode"] = mode.value
    if mode is WatchMode.RECURRING:
        return WizardState.AWAIT_INTERVAL, payload
    payload["interval_minutes"] = None
    return WizardState.REVIEW, payload
```

In `start_new`, replace the empty payload with:

```python
    payload = {_SEAT_FLOW_VERSION_KEY: _SEAT_FLOW_VERSION}
    async with deps.database.connection() as conn:
        await deps.drafts.upsert(
            conn,
            user_id,
            WizardState.AWAIT_URL.value,
            payload,
            deps.clock.now(),
        )
    return _prompt_for(WizardState.AWAIT_URL, payload)
```

In `handle_wizard_callback`, replace the existing non-callback-state guard with:

```python
    if state not in _CALLBACK_STATES:
        if data.startswith((_SEAT_MODE_PREFIX, _SEAT_PREFERENCE_PREFIX)):
            return _retry_prompt(
                "that seat choice is no longer active; use the current prompt"
            )
        return None
```

- [ ] **Step 8: Run the full wizard suite**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py -q
```

Expected: all tests pass, including confirmation recovery and persisted restart tests.

- [ ] **Step 9: Update the README wizard contract**

Replace the seat-related watch-creation steps in `README.md` with:

```markdown
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
```

- [ ] **Step 10: Run focused quality checks**

Run:

```bash
.venv/bin/python -m pytest \
  tests/domain/test_watch.py \
  tests/storage/test_watch_repository.py \
  tests/watches/test_seat_banks.py \
  tests/watches/test_blocks.py \
  tests/watches/test_ranking.py \
  tests/telegram/test_wizard.py \
  -q
.venv/bin/python -m ruff check \
  src/cinema_friend/domain/state.py \
  src/cinema_friend/domain/watch.py \
  src/cinema_friend/storage/watch_repository.py \
  src/cinema_friend/watches/seat_banks.py \
  src/cinema_friend/watches/blocks.py \
  src/cinema_friend/telegram/wizard.py \
  tests/domain/test_watch.py \
  tests/storage/test_watch_repository.py \
  tests/watches/test_seat_banks.py \
  tests/watches/test_blocks.py \
  tests/telegram/test_wizard.py
.venv/bin/python -m mypy
```

Expected: all focused tests, Ruff, and mypy pass.

- [ ] **Step 11: Run the full offline test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: the complete suite passes without contacting BFI.

- [ ] **Step 12: Commit the wizard and documentation slice**

```bash
git add \
  src/cinema_friend/telegram/wizard.py \
  tests/telegram/test_wizard.py \
  README.md
git commit \
  -m "feat: add simple seat preference wizard" \
  -m "Copilot-Session: eff05fd4-5274-496a-9140-639ed1bf56cf"
```
