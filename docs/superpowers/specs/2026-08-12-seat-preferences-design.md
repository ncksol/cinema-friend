# Simplified Seat Preferences

## Goal

Replace the cumbersome row-by-row and seat-by-seat setup with a simple preset flow for
users who only want seats in the centre of the auditorium, while retaining the existing
manual controls as an advanced option.

Simple presets are hard eligibility rules. Seats outside the selected preset are not
returned as lower-ranked alternatives.

## Scope

This change covers:

- The Telegram watch-creation wizard.
- Persisted watch criteria and interrupted wizard drafts.
- Seat-block eligibility during each check.
- Review text, tests, and user documentation.

It does not change seat-map parsing, adjacency, view scoring, performance-time preferences,
notification policy, or the current advanced controls.

## Seat Preference Strategy

`WatchCriteria` gains one seat preference strategy with three values:

- `advanced`
- `only_best`
- `best_and_good`

Advanced criteria continue to use the existing preferred and excluded row and seat sets.
Simple criteria use one preset and require those four sets to be empty, preventing manual
rules from conflicting with the preset.

The strategy is declarative. A simple watch stores the preset rather than seat labels or a
materialized exclusion list because the available seat map is not known when the watch is
created.

## Wizard Flow

After ticket quantity, a new watch asks how the user wants to choose seats:

- **Simple**
- **Advanced**

Advanced follows the existing sequence unchanged:

1. Preferred rows.
2. Preferred seats.
3. Excluded rows.
4. Excluded seats.

Simple asks for one preset and then skips all four manual prompts:

- **Only the best** — adjacent seats in the aisle-bounded centre bank, in row J or
  farther back.
- **Best and good** — adjacent seats in the same centre bank, in row C or farther back.

The prompt includes these explanations in its message; the buttons use the concise preset
names. After either path completes, the wizard rejoins the existing flow at the preferred
performance time.

The review message shows `Seat preference: Only the best` or
`Seat preference: Best and good` for simple watches. Advanced watches retain the current
preferred and excluded row and seat details.

## Centre-Bank Eligibility

Simple eligibility is evaluated independently for every fetched seat map. A physical row
is identified by its BFI section and row label; sections that reuse a label are evaluated
independently so their coordinates and geometry cannot affect each other.

For each physical section-row, using all parsed seats regardless of availability:

1. Sort its seats by displayed seat number.
2. Reuse the existing horizontal aisle-gap calculation and threshold used by adjacency.
3. Split the row into banks only at an oversized horizontal gap. A gap in displayed seat
   numbers is not evidence of an aisle.
4. Require at least three banks, so the row carries positive evidence of a bank bounded
   by an aisle on both sides.
5. Take the median horizontal coordinate of every physical seat in the row.
6. Select the bank when exactly one non-edge bank's horizontal span contains that median.

Everything else selects no bank: a single bank, two banks, a median that lands in an
aisle between banks, a median that lands in the first or last bank, more than one
candidate bank, and a row whose geometry does not provide enough evidence for the
existing aisle-gap calculation. These cases fail closed so simple mode cannot return a
seat outside its promise. The median is used instead of a minimum/maximum midpoint
because a detached side cluster can drag a midpoint far enough to select an outer bank.
A section-row with weak geometry does not invalidate another section-row that shares its
row label.

Rows must have a single ASCII letter from A through Z, normalized to uppercase.
`only_best` permits J and later row letters; `best_and_good` permits C and later row
letters. Both cutoffs are inclusive. Other row labels are ineligible in simple mode.

A candidate block is eligible only when every seat in the block belongs to the selected
centre bank and its row meets the preset cutoff. Displayed seat numbers must still be
consecutive within a purchasable block, so a numbering gap breaks adjacency after physical
bank selection without masquerading as an aisle. This filtering occurs before ranking.
Eligible blocks retain the current view-score and preferred-performance-time ordering.

Advanced criteria bypass the preset filter and continue through the current explicit
exclusion and preference logic.

## Components and Data Flow

The implementation remains within the existing boundaries:

1. The Telegram wizard records the selected strategy in its persisted draft.
2. Confirmation rebuilds and validates `WatchCriteria`.
3. The watch repository serializes the strategy in `criteria_json`.
4. Each check fetches a seat map as it does today.
5. Block generation applies the strategy's eligibility rule.
6. Ranking and notification receive only eligible blocks.

The aisle calculation and bank partitioning belong in a focused helper shared by
adjacency and simple eligibility. This avoids two definitions of an aisle drifting apart.

## Persistence and Compatibility

No SQL migration is required because watch criteria and wizard payloads are stored as
JSON.

New drafts carry a seat-flow version marker from the start. That marker distinguishes a
new draft that has not reached the seat question from a legacy draft that has no strategy
field.

- A missing strategy in stored watch criteria decodes as `advanced`.
- A legacy interrupted draft without the flow marker continues through the existing
  advanced prompts.
- A new draft with the marker receives the Simple or Advanced choice after quantity.
- New watches always serialize their explicit strategy.

This preserves every existing saved watch and interrupted setup without requiring user
action.

## Validation and Failure Behavior

The domain rejects a simple strategy combined with non-empty manual preferred or excluded
row or seat sets.

Invalid or stale seat-choice callbacks produce the existing recoverable input error and
leave the draft on its current step. A valid simple watch with no currently eligible block
uses the normal no-match result. Ambiguous row geometry or labels exclude only the affected
section-row and emit a diagnostic consistent with existing seat-map geometry diagnostics;
they do not broaden eligibility. Each diagnostic category is aggregated into one warning
per seat map, listing the affected section-rows, so a recurring check on a large auditorium
does not emit a warning per row on every poll.

## Testing

Tests cover:

- The Simple and Advanced wizard branches.
- The explanations shown before the simple preset buttons.
- Review text for each strategy.
- Recovery of legacy drafts from every affected wizard stage.
- Backward-compatible decoding of stored watches without a strategy.
- Criteria JSON round trips for all strategies.
- Rejection of mixed simple and manual criteria.
- Inclusive C and J boundaries.
- Exclusion of rows in front of each cutoff.
- Selection of the aisle-bounded centre bank and rejection of outer banks.
- Numbering gaps not creating physical banks while still breaking purchasable adjacency.
- Sections that reuse a row label being evaluated independently.
- A weak section-row not invalidating a well-formed section-row with the same label.
- Rejection of a single uninterrupted row and of a two-bank row.
- A detached side cluster leaving the centre-bank selection unchanged.
- Each simple-eligibility diagnostic being logged once per seat map.
- Multi-seat blocks remaining within one bank and section.
- Quantity-one behavior under the same simple eligibility rule.
- Ambiguous or insufficient geometry failing closed.
- Unchanged advanced exclusion, preference, and ranking behavior.

The README watch-creation steps document the new choice and both preset definitions.
