# Cinema Ticket Finder Design

**Status:** Approved

**Date:** 2026-08-11

## Summary

Cinema Friend is a small, allow-listed Telegram bot that runs continuously on a spare Mac. A user supplies a BFI IMAX film URL and screening preferences. The service reads BFI's public, server-rendered film and seat-map pages, finds every matching contiguous seat block, ranks the options, and returns links that open the relevant BFI seat map for manual purchase.

The service is read-only with respect to BFI. It does not select or reserve seats, add tickets to a basket, automate checkout, solve CAPTCHAs, rotate proxies, or otherwise evade access controls.

## Goals

- Accept a BFI IMAX film URL through a guided Telegram flow.
- Filter performances by an inclusive date range and daily time range in `Europe/London`.
- Support one to eight adjacent ordinary seats.
- Rank every matching seat block by explicit preferences, viewing position, and preferred performance time.
- Return the complete ranked result set through ten-option Telegram pages.
- Check once immediately when a watch is created.
- Optionally repeat checks at a user-selected interval of at least 15 minutes.
- Notify users about new or improved options without repeating unchanged results.
- Survive process restarts without losing watches, wizard state, schedules, or notification state.
- Run as a native `launchd` service with no inbound network listener.

## Non-goals

- Automated CAPTCHA or Cloudflare challenge solving.
- Browser automation, browser fingerprint impersonation, or proxy rotation.
- Automatic seat selection, reservation, basket creation, payment, or purchase.
- Supporting cinemas other than BFI IMAX.
- Public bot registration; access is limited to configured Telegram user IDs.
- Accessible-seat recommendations in the first version. Wheelchair spaces, companion seats, and other restricted access seats are excluded.
- Price filtering or ranking by standard, premium, or VIP category.
- Natural-language interpretation through an LLM.

## Observed BFI Web Contract

The sample film page exposes performance data in a JavaScript object named `articleContext`. Its `searchResults` records include the performance ID, start date and time, sales status, availability status, venue, and article metadata.

The public seat-map route accepts a performance ID:

```text
https://whatson.bfi.org.uk/imax/Online/mapSelect.asp
  ?BOparam%3A%3AWSmap%3A%3AloadMap%3A%3Aperformance_ids=<performance-id>
  &createBO%3A%3AWSmap=1
```

The returned SVG marks seats with stable IDs and attributes including availability status, section, row, seat number, and coordinates. The same URL is suitable for the Telegram purchase link: it opens BFI's seat map and leaves all purchase actions to the user.

This is an undocumented public web contract, not a supported BFI API. The gateway therefore validates every required field and fails closed if the contract changes. A parse failure is never interpreted as no availability.

## System Architecture

A single Python asyncio process contains the following units:

1. **Telegram adapter**
   - Uses Telegram long polling; the Mac exposes no webhook or HTTP port.
   - Enforces the configured allow-list and watch ownership on every command and callback.
   - Implements the guided watch wizard, management commands, result rendering, and pagination.
   - Persists wizard progress so an interrupted process can resume safely.

2. **Application services**
   - `WatchService` validates and manages watch lifecycle operations.
   - `CheckService` coordinates retrieval, option generation, ranking, persistence, and notification decisions.
   - Domain services depend on interfaces rather than Telegram, HTTP, or SQLite details.

3. **BFI gateway**
   - Performs only HTTPS GET requests to validated BFI IMAX routes.
   - Parses film pages and seat maps into typed domain records.
   - Applies host-level concurrency, rate limiting, request coalescing, and circuit breaking.

4. **Ranking engine**
   - Is a pure, deterministic unit with no network, database, or Telegram dependencies.
   - Generates contiguous blocks and produces a fully ordered list with score explanations.

5. **SQLite repositories**
   - Persist drafts, watches, schedules, check outcomes, result snapshots, options, and notification state.
   - Apply versioned SQL migrations at startup.

6. **Scheduler and notification dispatcher**
   - Finds due watches from persistent state once per minute.
   - Runs checks with bounded concurrency.
   - Delivers Telegram digests and records notification state atomically.

The initial implementation uses Python 3.12 or later with:

- `python-telegram-bot` for async long polling and callback handling.
- `httpx` for async HTTP sessions.
- `chompjs` for parsing the embedded JavaScript object literal.
- `lxml` for HTML and SVG parsing.
- `aiosqlite` for async SQLite access.

## Domain Model

### Watch

A watch contains:

- Stable watch ID and owning Telegram user ID.
- Canonical source URL and parsed film title.
- Inclusive local start and end dates.
- Inclusive daily start and end times.
- Ticket quantity from one through eight.
- Optional preferred exact seat labels.
- Optional preferred row labels.
- Optional excluded rows, exact seats, or seat ranges.
- Optional preferred local performance date and time.
- Mode: one-off or recurring.
- Recurring interval of at least 15 minutes.
- State: `active`, `paused`, `backoff`, `completed`, `expired`, or `failed`.
- Next due time, last check time, and host backoff metadata.

Dates and times are interpreted with `zoneinfo.ZoneInfo("Europe/London")` and stored as UTC instants where an instant is required. A daily time window whose start is later than its end wraps across midnight. The performance's London calendar date must still fall inside the selected date range. A preferred date and time must satisfy both the date predicate and the daily time predicate.

### Performance

A performance contains:

- BFI performance ID.
- Film title.
- London-local start instant.
- Sales and headline availability states.
- Venue and booking type where supplied.
- Canonical direct seat-map URL.

### Seat

A seat contains:

- Stable BFI seat ID.
- Section, row, and displayed seat number.
- SVG `x` and `y` coordinates.
- Availability state.
- Price-zone/category label when available, for display only.
- A restricted-access flag derived from BFI's seat messages and metadata.

### Ranked Option

A ranked option contains:

- Performance ID and start instant.
- Ordered seat IDs and labels.
- Seat-category labels for display.
- Explicit-preference match details.
- Raw view score and five-point quality band.
- Preferred-time distance.
- Deterministic rank and concise score explanation.
- Canonical BFI seat-map link.
- Availability observation timestamp.

The stable option key is the performance ID followed by the ordered stable seat IDs.

## Persistence

SQLite uses foreign keys and WAL mode. The schema contains:

- `schema_migrations`: applied migration versions.
- `conversation_drafts`: one persisted wizard state and validated payload per user.
- `watches`: watch criteria, lifecycle state, owner, schedule, and backoff fields.
- `check_runs`: outcome, timing, counts, and typed error for each attempted check.
- `result_snapshots`: immutable result-set metadata and fingerprint for a completed check.
- `result_options`: every ranked option belonging to a snapshot.
- `notified_options`: option keys already surfaced for a watch.
- `notification_state`: last notified best-ranking tuple and degradation/recovery flags.
- `notification_deliveries`: pending, delivered, or failed Telegram deliveries keyed for idempotent retry.
- `host_circuits`: persisted Cloudflare/rate-limit circuit state and next probe time.

Latest results remain available after restart. Result snapshots older than 24 hours are removed, except for the newest snapshot of each watch. Check-run summaries are retained for 30 days. Notification keys remain for the watch lifetime. Deleting a watch cascades to its results, checks, and notification state. A conversation draft belongs to a user rather than a watch and is removed by `/cancel`, confirmation, or a 24-hour expiry job.

Writes for a successful check use one transaction: insert the check result and complete snapshot, update the watch schedule, decide whether a notification is due, create its pending delivery record, and commit. Telegram delivery occurs after commit. A second transaction marks the delivery and its option keys as surfaced. A delivery failure leaves the pending record retryable rather than pretending the notification succeeded.

## Telegram Interaction

### Authorization

`TELEGRAM_ALLOWED_USER_IDS` contains a small comma-separated allow-list. Unauthorized users receive a generic denial and cannot create watches, inspect state, or invoke callbacks. Every callback verifies both the current user and the resource owner.

### Commands

- `/new`: start the guided watch wizard.
- `/watches`: list the user's watches and states.
- `/check`: run a selected watch immediately without changing its recurring schedule.
- `/pause`: pause a recurring watch.
- `/resume`: resume a paused watch and schedule an immediate check.
- `/delete`: confirm and delete a watch.
- `/cancel`: abandon the current wizard draft.
- `/help`: show concise usage help.

### Watch Wizard

The wizard gathers one item at a time:

1. BFI IMAX film URL.
2. Inclusive date range.
3. Inclusive daily time range.
4. Ticket quantity from one through eight.
5. Optional preferred rows.
6. Optional preferred exact seats or ranges.
7. Optional excluded rows, seats, or ranges.
8. Optional preferred performance date and time.
9. One-off or recurring mode.
10. Recurring interval, when applicable.
11. Review and confirmation.

Dates and times use explicit examples and are validated before advancing. Seat input accepts comma-separated row labels, exact labels, and same-row ranges, such as `L`, `L18`, or `L16-L22`. Invalid or contradictory entries remain on the current step with a specific error.

Confirmation saves the watch and runs an immediate check. A one-off watch becomes `completed` after a successful check. A recurring watch remains `active` and receives its next due time after completion.

### Results

An immediate check always replies with either:

- A ranked page of options and the total option/performance counts.
- A clear no-match result with the observation time.
- A typed retrieval or parsing error that does not imply no availability.

Each page contains up to ten options. Each option shows:

- Performance date and time.
- Exact contiguous seats.
- View score and short ranking rationale.
- Seat categories when present.
- Availability observation time.
- A button linking to the performance's BFI seat map.

Pagination callbacks refer to an immutable snapshot and enforce ownership. If the snapshot has expired, the bot explains that the result is stale and offers a fresh check.

## Retrieval Flow

### URL Validation

The gateway accepts only:

- Scheme `https`.
- Exact host `whatson.bfi.org.uk`.
- `/imax/Online/default.asp` with exactly one non-empty `BOparam::WScontent::loadArticle::permalink` value, apart from known inert tracking parameters.
- `/imax/Online/article/<slug>` with one non-empty path segment after `article`.

User information, fragments, non-default ports, duplicate permalink values, alternate hosts, redirects to alternate hosts, and arbitrary paths are rejected. Redirects are followed only when every hop remains on the exact allowed host and resolves to one of the accepted route shapes.

### Film Discovery

1. Normalize and validate the supplied URL.
2. Fetch the film page through a persistent `httpx.AsyncClient` cookie jar.
3. Locate and parse the `articleContext` object.
4. Validate the article identity, title, search-field mapping, and performance records.
5. Convert performance starts to `Europe/London`.
6. Apply the inclusive date and daily time predicates.
7. Skip records that BFI marks as not yet on sale, unavailable, or sold out.
8. Construct the canonical seat-map URL for each remaining performance.

The headline availability filter avoids unnecessary seat-map requests. A future poll re-evaluates the film page, so a sold-out performance that gains availability is checked then.

### Seat Retrieval

For each candidate performance:

1. Fetch its canonical seat-map URL with GET.
2. Verify that returned performance metadata matches the requested performance ID.
3. Parse all physical seat positions to establish row geometry.
4. Retain seats whose BFI status is available.
5. Exclude wheelchair spaces, companion seats, and other restricted access seats identified by seat messages or metadata.
6. Parse category/price-zone labels for display without using them in rank.

The gateway allows at most two in-flight BFI requests and starts requests at least one second apart. Requests use a 10-second connection timeout and a 30-second total timeout. Identical film or performance requests share one in-flight operation. A completed response may be reused only within 60 seconds, preventing concurrent watches from creating duplicate load without presenting materially stale availability.

### Adjacency

Seats form a contiguous run only when they:

- Belong to the same section and row.
- Have consecutive displayed numeric seat numbers.
- Have an SVG horizontal gap no greater than 1.75 times that row's median normal adjacent-seat gap.

The row median is calculated from absolute horizontal gaps between numerically consecutive physical seats; aisle outliers therefore do not define normal spacing. The geometric rule prevents consecutive numbers on opposite sides of an aisle from being treated as adjacent. A row with fewer than three usable gaps, a zero-width row, or a non-numeric or structurally ambiguous seat label is excluded and recorded as a parser-contract warning.

For a requested quantity `N`, every sliding window of exactly `N` seats within each contiguous run becomes an option. A run of five seats therefore produces four distinct two-seat options. Options are deduplicated by their stable option key.

## Ranking

### Explicit Preferences

Blocks containing an excluded row or seat are removed.

Remaining blocks are ranked first by:

1. Number of seats overlapping the preferred exact-seat set.
2. Whether the block is in a preferred row.

An explicit preference therefore overrides the default venue profile without changing the underlying view score.

### Default BFI View Score

The default score ranges from 0 through 100 and measures viewing position only:

```text
row_distance = observed row steps from the nearest of L or M
row_score = max(0, 40 - 5 * row_distance)

row_center = midpoint of the minimum and maximum physical seat x-coordinates in the row
half_width = (maximum row x - minimum row x) / 2
normalized_offset = abs(block_center_x - row_center) / half_width
center_score = 60 * max(0, 1 - normalized_offset)

view_score = row_score + center_score
```

Rows are ordered by their observed median SVG position, so omitted letters do not distort distance. Rows L and M are the default ideal band, and horizontal centring has the stronger weight. The score is rounded to two decimal places. Standard, premium, and VIP categories do not change it.

### Final Ordering

The deterministic sort key is:

1. Preferred exact-seat overlap, descending.
2. Preferred-row match, descending.
3. Five-point view-score band (`floor(view_score / 5)`), descending.
4. Absolute minutes from the preferred performance date/time, ascending; all options tie when none is supplied.
5. Raw view score, descending.
6. Performance start, ascending.
7. Section, row, and seat labels, ascending.

The score band lets performance time break genuine seat-quality near-ties while preventing a materially worse seat from winning solely because of time.

## Scheduling and Notifications

The scheduler scans for due watches once per minute. A recurring watch's next run is its completion time plus the chosen interval plus positive random jitter of up to 10 percent. The interval itself cannot be less than 15 minutes.

Startup recovers overdue watches from SQLite and staggers them through the same host rate limiter. A filesystem lock prevents a second service instance from running against the same database.

An immediate creation or `/check` response displays the current snapshot. After that Telegram delivery succeeds, every current option is marked as already surfaced, including options beyond the first page.

Later recurring checks send one digest when either:

- The snapshot contains an option key never previously surfaced for that watch.
- The current best option has a better complete ranking tuple than the last notified best option.

The digest shows the current top ten, counts of all current and new options, and a button to browse the complete snapshot. All current option keys are marked surfaced only after the Telegram delivery succeeds. Unchanged snapshots, worse-only changes, and mere disappearances are silent.

## Error Handling and Cloudflare

Errors are typed and handled distinctly:

- **Input error:** remain in the wizard step and explain the correction.
- **Network or BFI 5xx error:** retry twice after delays of approximately one and three seconds, each with up to 20 percent positive jitter, then put the watch into backoff.
- **BFI parser-contract error:** pause the affected watch, retain its last valid snapshot, and alert its owner once.
- **Telegram delivery error:** retain a pending notification for retry.
- **Database error:** fail the operation, log it, and do not emit a success-shaped response.

A Cloudflare challenge or rate-limit condition is detected from 403/429 statuses, the `cf-mitigated` response header, and known interstitial markers. These responses are not retried immediately. Detection opens the persisted host-wide circuit breaker. No BFI requests are attempted until the next retry point, when one coordinator-owned request acts as the probe; its result either closes the circuit or advances the delay.

The retry sequence is:

```text
15 minutes, 30 minutes, 1 hour, 2 hours, 4 hours, then 6 hours maximum
```

Each delay receives positive jitter of up to 10 percent. Affected users receive one degraded-service alert, not one alert per watch. A successful probe closes the circuit, restores due scheduling, and sends one recovery alert.

The service does not attempt to answer, bypass, outsource, or automate the challenge. If BFI permanently protects the public route, the service remains visibly degraded until an authorized data source or a separately approved design replaces the gateway.

## Operations and Security

`launchd` runs the process with `KeepAlive` and restarts it after failures. The process handles termination signals by stopping new checks, awaiting active checks within a bounded shutdown period, committing state, and closing Telegram, HTTP, and SQLite clients.

Configuration is loaded from a user-owned file with mode `0600`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`
- `DATABASE_PATH`
- `LOG_LEVEL`
- `BFI_USER_AGENT`, containing a truthful service name and contact string

Secrets are never stored in SQLite or written to logs. Logs are structured, redact URL tokens and Telegram credentials, and include watch/check correlation IDs. BFI HTML and transient `sToken` values are neither logged nor persisted. The single-instance lock is `<DATABASE_PATH>.lock` and is held for the process lifetime.

## Testing Strategy

### Unit Tests

- URL normalization, host/path restrictions, and redirect validation.
- `articleContext` extraction and required-field validation.
- SVG seat parsing, access-seat exclusion, and category extraction.
- Row spacing and aisle-aware adjacency.
- Every sliding block for quantities one through eight.
- View-score formula, explicit preferences, quality bands, time tie-breaks, and deterministic final ordering.
- Date/time filtering across daylight-saving changes and midnight-wrapping time windows.
- Notification option keys, improvement detection, and repeat suppression.

### Integration Tests

- Telegram wizard and command handlers with fake updates and callback ownership checks.
- SQLite migrations, transactions, restart recovery, retention, and cascade deletion using temporary databases.
- Scheduler due-time and circuit-breaker behavior with a fake clock and deterministic jitter.
- HTTP-mocked end-to-end checks for available, no-match, sold-out, malformed, 5xx, 403, 429, and recovery responses.
- Telegram delivery failure followed by idempotent retry.

Fixtures are minimal synthetic HTML/SVG documents that preserve the required contract without committing full BFI pages.

### Manual Contract Smoke Test

A separately invoked smoke test performs one film-page GET and one seat-map GET for a supplied live performance. It verifies parsing and reports contract drift. It is not part of normal automated test runs and does not poll.

## Acceptance Criteria

1. Given a valid BFI IMAX film URL, date/time criteria, and quantity, the bot returns every distinct eligible contiguous block for every matching on-sale performance.
2. Results follow the approved preference, view-quality, and preferred-time ordering and remain deterministic across repeated runs over identical input.
3. Every result link opens the correct BFI performance seat map and requires the user to select seats and complete purchase manually.
4. Recurring watches run no more frequently than configured, survive service restarts, and expire after their date range.
5. A new or improved option sends one digest; an unchanged result sends none.
6. Unauthorized Telegram users cannot create, inspect, mutate, or page through watches.
7. Retrieval, parser, Cloudflare, and delivery failures are visible and never presented as no ticket availability.
8. The service never performs a BFI write action or automated access-control challenge handling.
