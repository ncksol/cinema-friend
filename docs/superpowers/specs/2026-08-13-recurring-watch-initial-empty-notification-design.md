# Recurring Watch Initial Empty Notification

## Summary

When a recurring watch's creation check succeeds but finds no matching seats, the bot
will send two messages in order:

1. The existing watch-created confirmation.
2. A result message with the film title, check time, and:
   "I haven't found anything right now, but I'll keep watching."

The empty result remains a durable, retryable notification. Later scheduled checks that
also find nothing remain silent.

## Goals

- Reply once after a recurring watch's first successful empty check.
- Preserve the existing watch-created confirmation as a separate first message.
- Send the empty follow-up immediately after that confirmation.
- Preserve the existing title and London check time in the result message.
- Keep transient delivery retries and idempotency guarantees.
- Keep subsequent scheduled empty checks silent.
- Leave manual checks and one-off watches unchanged.

## Non-goals

- Changing watch scheduling, ranking, or filtering.
- Treating retrieval, challenge, contract, or persistence failures as empty results.
- Changing notification behavior for successful checks that find options.
- Changing the generic empty response returned by `/check`.

## Notification Decision

`decide_result_notification` receives the watch mode in addition to the existing
trigger and result state. Every successful result delivery it decides on is persisted
with the ordinary `results` kind; whether the empty immediate check for a recurring
watch should read as "still looking" rather than a generic empty-results notice is a
separate, boolean `initial_recurring_empty` presentation decision layered onto that
same `results` kind. The decision sets that flag only when all three conditions hold:

- The trigger is `CheckTrigger.CREATION`.
- The watch mode is `WatchMode.RECURRING`.
- The ranked option sequence is empty.

The decision still requires a snapshot for every manual and creation check. A
one-off creation check and an empty manual check use the ordinary `results` kind with
the flag left at its default `False`. A scheduled or recovery check with no new or
improved options continues to require no delivery.

## Persistence and Idempotency

The check service persists the empty snapshot and its `results` delivery, carrying the
`initial_recurring_empty` presentation flag, in the existing successful-check
transaction. The delivery retains the current result idempotency key derived from the
watch and snapshot, so replaying the same decision cannot create a duplicate.

`NotificationPayload` gains an optional `initial_recurring_empty` boolean field,
defaulting to `False`. Encoding always writes it; decoding treats a missing field as
`False`, so rows persisted before this field existed remain readable without a schema
migration or any other payload-shape change. A build that predates the field ignores it
entirely: it decodes the row, sees `kind = "results"`, and renders the ordinary
no-match result. Delivery success continues to update notification state through
`NotificationRepository.mark_delivered`.

## Confirmation-first Delivery

The watch ID a confirmation creates is deterministic before the saga runs: the
wizard derives it from the draft's stable `setup_id`. The exact `wizard:confirm` route
first prepares that identity -- ensuring a `REVIEW` draft has a `setup_id`, without
rewriting one a draft already carries -- and then establishes a deferral for that
specific `(recipient_user_id, watch_id)` pair before it invokes the confirmation
handler. While that deferral is active, a concurrent background delivery sweep leaves
the matching initial-empty delivery pending without changing its attempt count or retry
time. Every other notification remains deliverable, including a different watch's own
pending initial-empty retry for the same recipient: the deferral is scoped to one watch,
not to the recipient as a whole.

The deferral is process-local delivery coordination, not persisted notification state.
The delivery itself remains durable. On restart, confirmation recovery finishes before
the scheduler starts, so no background sweep can overtake a recovered confirmation.

After the handler returns, the route will:

1. Send the existing watch-created confirmation.
2. Release the watch-scoped deferral.
3. Run the delivery worker immediately.

This prevents the keep-watching follow-up from overtaking the confirmation while
avoiding a global delivery lock during the BFI check. The worker's existing lock
continues to serialize the explicit flush with background sweeps.

If sending the creation confirmation fails, the route will not force the follow-up.
The flagged delivery remains pending and becomes eligible for the normal delivery path
after the deferral is released. This preserves the result instead of silently dropping
it. Existing startup confirmation recovery runs before the scheduler starts, so
recovered confirmations retain the same confirmation-before-follow-up ordering.

## Rendering

Every result delivery, special or ordinary, is rendered through the same `results`
branch: the delivery worker loads the referenced snapshot through the existing result
repository and reads the payload's `initial_recurring_empty` flag alongside it. The
result renderer uses the normal title and checked-at header, and replaces the generic
no-match line with the keep-watching copy only when that flag is true and the loaded
snapshot is empty:

> I haven't found anything right now, but I'll keep watching.

Every other `results` delivery -- the flag false, or the snapshot non-empty -- uses:

> No matching seats were found for this check.

The flag is set only for an empty snapshot at watch creation, so a non-empty snapshot
never renders the keep-watching copy even if the flag were somehow true. If the
referenced snapshot has expired, the worker retains the existing truthful
expired-results response regardless of the flag.

## Data Flow

1. The user confirms a recurring watch.
2. The confirmation route prepares the draft's stable `setup_id` and the deterministic
   watch ID it identifies, then defers the initial-empty delivery for that specific
   `(recipient_user_id, watch_id)` pair.
3. The wizard creates the same watch ID and runs its `CREATION` check.
4. The check succeeds with no ranked options.
5. The policy sets the `initial_recurring_empty` presentation flag on an ordinary
   `results` decision.
6. The check transaction stores the empty snapshot, reschedules the watch, and queues
   the `results` delivery carrying that flag.
7. The route sends the existing creation confirmation.
8. The route releases the watch-scoped deferral and immediately runs the delivery
   worker.
9. The worker reads the flag, renders the keep-watching copy, sends it, and marks the
   delivery sent.
10. Later scheduled empty checks store snapshots but queue no delivery. A pending
    initial-empty retry for a different watch owned by the same user is not deferred by
    this confirmation and is sent by the same sweep.

## Error Handling

- A BFI network, challenge, contract, or circuit failure follows its existing typed
  outcome and does not set the `initial_recurring_empty` presentation flag.
- A transient Telegram failure keeps the flagged delivery pending with the existing
  retry backoff.
- A permanent Telegram failure marks the delivery failed using the existing rules.
- A stale or missing snapshot uses the existing expired-results response regardless of
  the flag.
- Re-running a delivery decision reuses the existing idempotent row.
- If identity preparation finds no confirmable draft, the route installs no deferral and
  preserves the existing stale-action behavior.
- Unknown non-`results` notification kinds retain the existing terminal-failure policy;
  the presentation flag only ever changes rendering within the `results` kind.
- Rolling back to a release that predates the presentation flag does not fail a pending
  flagged delivery: the older decoder reads the row, sees `kind = "results"`, ignores the
  unrecognized field, and sends the generic no-match result from the referenced
  snapshot, marking the delivery sent through the existing bookkeeping.

## Testing

### Notification policy

- Recurring creation plus zero options sets the `initial_recurring_empty` flag on a
  `results` decision.
- One-off creation plus zero options produces a `results` decision with the flag false.
- Recurring manual plus zero options produces a `results` decision with the flag false.
- Recurring scheduled plus zero options requires no delivery.

### Check service

- The first empty recurring creation check stores one `results` delivery with the flag
  true.
- A subsequent empty scheduled check stores a snapshot but no additional delivery.
- Existing one-off, manual, and non-empty creation behavior remains unchanged.

### Payload compatibility

- Existing payload JSON without the field decodes with the flag false.
- A newly persisted initial-empty payload uses `kind = "results"` with the flag set to
  true.
- A decoder that ignores the extra field still renders the row as a generic result,
  matching the rollback path below.

### Telegram delivery

- The flagged delivery includes the watch title, London check time, and keep-watching
  copy.
- Ordinary `results` deliveries, and a flagged delivery over a non-empty snapshot,
  retain the generic no-match copy.
- Transient retry, permanent failure, and idempotent resend behavior apply to the
  flagged delivery exactly as they do to any other `results` delivery.

### Ordering

- A background sweep racing a confirmation cannot send that confirmation's own deferred
  initial-empty delivery.
- A pending initial-empty retry for a different watch owned by the same user is sent by
  a sweep that races an unrelated confirmation, rather than being deferred by it.
- The route sends the creation confirmation before its own watch's initial-empty
  follow-up.
- Other recipients and other watches are not blocked by one watch's deferral.
- Startup recovery sends a recovered confirmation before its queued initial-empty
  follow-up.

### Rollback

- A pending initial-empty delivery, read by a decoder that predates the presentation
  field, decodes successfully as an ordinary `results` row and sends the generic
  no-match result rather than failing.

## Acceptance Criteria

- Creating a recurring watch whose first successful check finds no matches produces
  exactly one keep-watching follow-up after that watch's creation confirmation.
- The follow-up contains the title and London check time.
- Repeated scheduled empty results produce no messages.
- `/check` and one-off watch behavior do not change.
- The follow-up survives transient Telegram delivery failures without duplicate queue
  entries.
- Confirming one recurring watch cannot delay a same-user, different-watch pending
  initial-empty retry: that retry is sent by a sweep that races the confirmation, not
  deferred by it.
- Rolling back to a release that predates the presentation flag sends the generic
  no-match result for a pending initial-empty delivery instead of failing it.
