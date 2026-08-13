# Watch-Scoped Delivery Compatibility

## Summary

The confirmation barrier for an initial empty recurring-watch result will apply only to
the watch being confirmed. A pending retry for another watch owned by the same user will
remain deliverable while the new watch's availability check runs.

The keep-watching copy will be represented as presentation metadata on an ordinary
`results` notification rather than as a new notification kind. If the service is rolled
back while such a delivery is pending, the older build will send the generic no-match
result instead of terminal-failing the row.

## Goals

- Defer only the initial-empty delivery for the watch currently being confirmed.
- Keep unrelated deliveries, including another watch's pending initial-empty retry,
  deliverable during a long creation check.
- Preserve confirmation-first ordering for the watch being confirmed.
- Make pending initial-empty deliveries readable by the previous release.
- Degrade the copy on rollback from keep-watching to the generic no-match result rather
  than dropping the message.
- Preserve the existing retry, idempotency, and delivery-state guarantees.
- Support drafts created before this change.

## Non-goals

- Changing the pre-existing ordering of non-empty creation results.
- Guaranteeing the keep-watching wording when an older release sends the delivery.
- Adding a held-delivery state or a database migration.
- Changing scheduling, ranking, result identity, or Telegram retry policy.

## Stable Watch Identity Before Confirmation

The watch ID is already deterministic: the wizard derives it from the draft's stable
`setup_id`. The exact confirmation route needs that ID before it invokes the saga so it
can install the barrier before the creation check queues a delivery.

A wizard helper will transactionally prepare the confirmation identity:

- Load the caller's draft.
- Return no watch ID when the draft is absent or is not confirmable.
- Ensure a `REVIEW` draft has a stable `setup_id`, adding one only when missing.
- Reuse the `setup_id` already present on a `CONFIRMING` draft.
- Derive and return the same deterministic watch ID used by the saga.
- Avoid rewriting a draft that already has its setup ID, so duplicate taps do not renew
  a confirmation claim lease.

This lazily upgrades review drafts persisted by the current release, which assigns the
setup ID only when claiming confirmation. The saga will continue to use `setdefault`, so
the prepared identity and the created watch cannot diverge.

## Watch-Scoped Deferral

The delivery worker's process-local deferral key will change from a recipient ID to:

```text
(recipient_user_id, watch_id)
```

The exact `wizard:confirm` route will:

1. Authorize the caller.
2. Prepare the deterministic watch ID from the draft.
3. Enter a ref-counted deferral for that user and watch when an ID is available.
4. Run the existing confirmation handler and creation check.
5. Send the existing watch-created confirmation.
6. Release the watch-scoped deferral.
7. Flush the delivery worker immediately.

The worker will skip a delivery only when it is the initial-empty result for the exact
recipient and watch in an active barrier. An older pending retry for another watch owned
by that user will continue through the same sweep.

Nested barriers for the same user and watch remain ref-counted. A stale confirmation
without a confirmable draft installs no barrier and follows the existing handler path.
Skipped rows remain pending without consuming an attempt or changing their retry time.

## Backward-Compatible Presentation Metadata

`initial_recurring_empty` will no longer be persisted as
`NotificationPayload.kind`. All successful result deliveries will persist:

```text
kind = "results"
```

`NotificationPayload` will gain an optional boolean presentation field:

```text
initial_recurring_empty = true | false
```

The field defaults to `false`. Payload encoding will write it, and decoding will treat a
missing field as `false`, so existing rows remain readable.

The notification policy will continue to decide whether a successful check is the
initial empty recurring case, but it will carry that decision separately from `kind`.
The check service will store an ordinary `results` payload with the presentation flag
set only when:

- The trigger is `CREATION`.
- The watch mode is `RECURRING`.
- The ranked option set is empty.

The delivery worker will render the keep-watching copy only when the flag is true and
the referenced snapshot is empty. Otherwise it will use ordinary result rendering.

## Rollback Behavior

The current main-branch payload decoder reads the known JSON keys explicitly and ignores
additional keys. Therefore, after rollback:

- The older build decodes the row successfully.
- It sees `kind = "results"`.
- It ignores `initial_recurring_empty`.
- It sends the generic no-match result from the referenced snapshot.
- It marks the delivery sent through the existing result bookkeeping.

This is intentional graceful degradation. Exact presentation compatibility across a
rollback is not required; delivery continuity is.

## Data Flow

1. The user taps Confirm on a recurring watch draft.
2. The route prepares the draft's stable setup ID and deterministic watch ID.
3. The worker installs a barrier for that recipient and watch.
4. The saga creates the same watch ID and runs the creation check.
5. An empty successful check stores a snapshot and a `results` delivery carrying
   `initial_recurring_empty = true`.
6. Background sweeps skip that exact delivery but may send another watch's pending
   delivery.
7. The route sends the watch-created confirmation.
8. The barrier is released and the worker is flushed.
9. The new build sends the keep-watching result; a rolled-back build sends the generic
   no-match result.

## Error Handling

- If identity preparation finds no confirmable draft, the route installs no barrier and
  preserves the existing stale-action behavior.
- If confirmation sending fails, the barrier is released by the context manager and the
  durable result remains pending for the normal worker.
- If the check fails, no initial-empty presentation flag is produced; existing typed
  failure behavior applies.
- If the snapshot is missing, the existing expired-results response remains authoritative.
- If the presentation flag is true on a non-empty snapshot, normal result rendering wins
  defensively.
- Unknown non-result notification kinds retain the existing terminal-failure policy.

## Testing

### Wizard identity preparation

- A legacy `REVIEW` draft without `setup_id` receives one and returns its deterministic
  watch ID.
- Repeating preparation returns the same ID.
- A prepared confirmation creates that exact watch ID.
- A draft that already has `setup_id` is not rewritten.
- Missing and non-confirmable drafts return no watch ID.

### Watch-scoped deferral

- The target watch's initial-empty delivery stays pending during its barrier.
- Another watch's initial-empty retry for the same recipient is sent.
- Another recipient remains unaffected.
- Ordinary result and host-alert deliveries remain unaffected.
- Nested barriers for the same recipient and watch release only at refcount zero.
- The exact confirmation route installs the barrier before invoking the handler and
  releases it after sending the confirmation.

### Payload compatibility

- Existing payload JSON without the flag decodes as `false`.
- New initial-empty payload JSON uses `kind = "results"` and the flag set to `true`.
- An older-decoder-equivalent path can ignore the extra field and render the row as a
  generic result.
- Ordinary results, one-off creation checks, and manual checks keep the flag false.
- Scheduled and recovery empty checks remain silent.
- Unknown non-result kinds still fail terminally.

## Acceptance Criteria

- Confirming watch B cannot delay a pending initial-empty retry for watch A owned by the
  same user.
- Watch B's own initial-empty result cannot overtake its watch-created confirmation.
- Every newly persisted initial-empty delivery has `kind = "results"`.
- Rolling back to the previous release sends a generic no-match result for a pending
  initial-empty delivery instead of failing it.
- Existing delivery, retry, idempotency, and typed-failure tests continue to pass.
