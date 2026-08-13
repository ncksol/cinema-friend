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

`decide_result_notification` will receive the watch mode in addition to the existing
trigger and result state. It will select a new notification kind,
`initial_recurring_empty`, only when all three conditions hold:

- The trigger is `CheckTrigger.CREATION`.
- The watch mode is `WatchMode.RECURRING`.
- The ranked option sequence is empty.

The decision will still require a snapshot for every manual and creation check. A
one-off creation check and an empty manual check will continue to use the ordinary
`results` kind. A scheduled or recovery check with no new or improved options will
continue to require no delivery.

## Persistence and Idempotency

The check service will persist the empty snapshot and its
`initial_recurring_empty` delivery in the existing successful-check transaction. The
delivery will retain the current result idempotency key derived from the watch and
snapshot, so replaying the same decision cannot create a duplicate.

`NotificationPayload` already persists an open-ended kind string, so the new kind does
not require a schema migration or a payload-shape change. Delivery success will continue
to update notification state through `NotificationRepository.mark_delivered`.

## Confirmation-first Delivery

The exact `wizard:confirm` route will establish a recipient-scoped deferral for
`initial_recurring_empty` before it invokes the confirmation handler. While that
deferral is active, a concurrent background delivery sweep will leave matching
deliveries pending without changing their attempt count or retry time. Other
notifications remain deliverable.

The deferral is process-local delivery coordination, not persisted notification state.
The delivery itself remains durable. On restart, confirmation recovery finishes before
the scheduler starts, so no background sweep can overtake a recovered confirmation.

After the handler returns, the route will:

1. Send the existing watch-created confirmation.
2. Release the recipient-scoped deferral.
3. Run the delivery worker immediately.

This prevents the special follow-up from overtaking the confirmation while avoiding a
global delivery lock during the BFI check. The worker's existing lock continues to
serialize the explicit flush with background sweeps.

If sending the creation confirmation fails, the route will not force the follow-up.
The special delivery remains pending and becomes eligible for the normal delivery path
after the deferral is released. This preserves the result instead of silently dropping
it. Existing startup confirmation recovery runs before the scheduler starts, so
recovered confirmations retain the same confirmation-before-follow-up ordering.

## Rendering

The delivery worker will recognize `initial_recurring_empty` and load its referenced
snapshot through the existing result repository. The result renderer will use the
normal title and checked-at header, but replace the generic no-match line with:

> I haven't found anything right now, but I'll keep watching.

Ordinary `results` deliveries will continue to use:

> No matching seats were found for this check.

The special kind is only created for an empty snapshot. If the referenced snapshot has
expired, the worker will retain the existing truthful expired-results response.

## Data Flow

1. The user confirms a recurring watch.
2. The confirmation route defers that user's initial-empty delivery kind.
3. The wizard creates the watch and runs its `CREATION` check.
4. The check succeeds with no ranked options.
5. The policy selects `initial_recurring_empty`.
6. The check transaction stores the empty snapshot, reschedules the watch, and queues
   the delivery.
7. The route sends the existing creation confirmation.
8. The route releases the deferral and immediately runs the delivery worker.
9. The worker sends the special empty result and marks the delivery sent.
10. Later scheduled empty checks store snapshots but queue no delivery.

## Error Handling

- A BFI network, challenge, contract, or circuit failure follows its existing typed
  outcome and does not emit the special empty message.
- A transient Telegram failure keeps the special delivery pending with the existing
  retry backoff.
- A permanent Telegram failure marks the delivery failed using the existing rules.
- A stale or missing snapshot uses the existing expired-results response.
- Re-running a delivery decision reuses the existing idempotent row.

## Testing

### Notification policy

- Recurring creation plus zero options selects `initial_recurring_empty`.
- One-off creation plus zero options remains `results`.
- Recurring manual plus zero options remains `results`.
- Recurring scheduled plus zero options requires no delivery.

### Check service

- The first empty recurring creation check stores one special delivery.
- A subsequent empty scheduled check stores a snapshot but no additional delivery.
- Existing one-off, manual, and non-empty creation behavior remains unchanged.

### Telegram delivery

- The special delivery includes the watch title, London check time, and keep-watching
  copy.
- Ordinary empty results retain their current copy.
- Transient retry, permanent failure, and idempotent resend behavior apply to the new
  kind.

### Ordering

- A background sweep racing a confirmation cannot send the deferred special delivery.
- The route sends the creation confirmation before the special follow-up.
- Other recipients and notification kinds are not blocked by the deferral.
- Startup recovery sends a recovered confirmation before its queued special follow-up.

## Acceptance Criteria

- Creating a recurring watch whose first successful check finds no matches produces
  exactly one keep-watching follow-up after the creation confirmation.
- The follow-up contains the title and London check time.
- Repeated scheduled empty results produce no messages.
- `/check` and one-off watch behavior do not change.
- The follow-up survives transient Telegram delivery failures without duplicate queue
  entries.
