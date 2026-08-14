# Weekday and Weekend Viewing Times Design

## Purpose

Cinema Friend currently applies one daily performance-time window to every date in a
watch. A new watch may instead use one window from Monday through Friday and another on
Saturday and Sunday, while the existing one-window setup remains the default.

## Goals

- Let `/new` users choose between one daily window and separate weekday/weekend windows.
- Treat Monday through Friday as weekdays and Saturday and Sunday as weekends.
- Select the applicable window from the performance's Europe/London start date.
- Preserve inclusive bounds and windows that cross midnight.
- Keep existing watches and in-progress legacy drafts working without user action.
- Show the selected schedule clearly in review and watch-list output.

## Non-goals

- Editing an existing watch.
- Disabling all weekday or all weekend performances.
- Multiple windows for one day category.
- Arbitrary per-day schedules or configurable weekend days.
- Holiday-specific schedules.

## Domain Model

`WatchCriteria.time_from` and `WatchCriteria.time_to` remain the required default window.
For a split schedule, that pair is the Monday-Friday window. Two new optional fields,
`weekend_time_from` and `weekend_time_to`, hold the Saturday-Sunday override.

The weekend fields form an atomic pair:

- both absent means the default window applies every day;
- both present means the weekend override applies on Saturday and Sunday;
- only one present is invalid and raises `InputError`.

The shared time-window module exposes one operation that receives a default window, an
optional weekend window, and a London-local date, then returns the applicable pair.
`WatchCriteria`, performance matching, preferred-time validation, and wizard validation
use this operation rather than implementing weekday selection independently.

The selected window retains the existing `within_daily_window` semantics. Bounds are
inclusive. A start later than the end wraps across midnight. The performance's
London-local start date chooses the schedule category for the entire window; the date
does not change merely because the window extends into the following day.

## Matching and Preferred Time

Performance eligibility first applies the existing date-range predicate to the
performance's London-local start date. It then selects the effective window for that same
date and applies the existing daily-window predicate to the local start time. Sales,
reserved-seating, quantity, seat, and ranking behavior are unchanged.

An optional preferred performance instant must remain UTC-aware and inside the watch's
local date range. Its Europe/London date selects the effective weekday or weekend window,
and its London-local time must fall inside that window. The preferred instant remains a
ranking preference and does not otherwise change matching.

## Persistence and Compatibility

Watch criteria remain stored in `watches.criteria_json`; no SQLite schema migration is
required. New JSON writes include nullable `weekend_time_from` and `weekend_time_to`
properties. The decoder reads both with optional lookups:

- legacy JSON with neither property decodes as a same-every-day schedule;
- new JSON with both values decodes as a split schedule;
- malformed JSON with only one value fails domain validation instead of silently
  changing behavior.

An existing watch is not rewritten proactively. If another operation later updates it,
normal encoding may add the two properties as `null` without changing its meaning.

## Telegram Wizard

New drafts carry a time-flow version marker. After the date range, the wizard presents
two buttons:

- `Same every day`
- `Weekday + weekend`

The same-every-day branch asks the existing daily time-window question once and stores
only the default pair. The split branch asks for the Monday-Friday window and then the
Saturday-Sunday window. Every prompt reuses the existing time-range parser and explicitly
states that midnight-wrapping input is accepted.

The wizard adds distinct states for the schedule choice, weekday window, and weekend
window. Each valid transition persists its payload before returning the next prompt, so a
restart between the two split-window questions resumes at the correct state.

Drafts created before this feature have no time-flow marker. Their existing
`AWAIT_TIME_RANGE` state keeps its historical behavior: one valid range advances directly
to quantity and becomes a same-every-day schedule. New drafts take the schedule-choice
branch. This compatibility rule is explicit and does not infer draft age from whichever
payload keys happen to be present.

The preferred-time prompt validates against the effective window represented in the
draft. Confirmation rebuilds `WatchCriteria` and revalidates every invariant before
creating a watch, as it does today.

## Rendering

The confirmation review shows one of these forms:

```text
Times: 18:00 to 23:00 daily
```

```text
Weekdays: 18:00 to 23:00
Weekends: 13:00 to 23:00
```

The `/watches` compact criteria line uses the same distinction with shorter labels. Times
remain London-local. Other review and watch-list fields are unchanged.

## Error Handling

- An unknown or stale schedule-choice callback returns a choice-specific error and leaves
  the draft unchanged.
- A malformed time range returns the existing accepted-shape guidance and leaves the
  draft unchanged.
- A partial weekend override raises `InputError`.
- Corrupt persisted criteria surface as an error; decoding does not invent a fallback.
- Existing confirmation compensation and restart-recovery behavior remain unchanged.

## Testing

### Domain and matching

- A same-every-day schedule returns the default window on every day.
- A split schedule returns the default window Monday-Friday and the override on
  Saturday-Sunday.
- Friday and Saturday midnight-wrapping cases use the performance start day.
- Weekend fields are accepted only as a complete pair.
- Preferred instants are checked against the window for their London-local date.
- Existing inclusive, midnight-wrapping, and DST behavior remains covered.

### Persistence

- A split schedule round-trips through `criteria_json`.
- Newly written uniform schedules contain nullable weekend properties.
- Legacy JSON without weekend properties decodes unchanged.
- A persisted partial weekend pair fails validation.

### Wizard and rendering

- A new draft can choose the uniform branch and reach quantity after one time range.
- A new draft can choose the split branch and persist both ranges.
- Invalid choices and ranges do not advance or mutate the draft.
- Restarting between weekday and weekend prompts resumes at the weekend prompt.
- A legacy draft already awaiting a time range follows the uniform path.
- Preferred-time validation uses the selected day category.
- Review and `/watches` output distinguish uniform and split schedules.

No test performs a live BFI request.

## Documentation

The README watch-creation flow describes the schedule choice and the two split-window
prompts. It retains the existing explanation that all entered times are interpreted in
Europe/London and may cross midnight.
