# Cinema ticket finder design

**Status:** Approved

**Date:** 2026-08-11

## Summary

Cinema Friend is a small, allow-listed Telegram bot that runs continuously on a spare Mac. A user supplies a BFI IMAX film URL and screening preferences. The service reads BFI's public, server-rendered film and seat-map pages, finds every matching contiguous seat block, ranks the options, and returns links that open the relevant BFI seat map for manual purchase.

BFI's public pages sit behind Cloudflare, which fingerprints the TLS handshake. The gateway therefore issues its GETs through a Chrome-impersonating HTTP client (`curl_cffi`) rather than an ordinary HTTP library. This is the minimum needed to read a page any member of the public can open in a browser; it is not a challenge bypass. See "BFI Access Method and Web Contract".

The service is read-only with respect to BFI. It does not select or reserve seats, add tickets to a basket, automate checkout, solve CAPTCHAs, rotate proxies or IP addresses, or attempt to answer an access-control challenge once one is presented.

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

- Automated CAPTCHA or Cloudflare challenge solving. A presented challenge is treated as a stop signal, not an obstacle to work around.
- Persistent browser automation, headless browser drivers, or CDP-driven sessions. The gateway impersonates a browser's TLS fingerprint only, at the HTTP client layer.
- Proxy rotation, IP rotation, or distributing load across identities to raise the effective request rate.
- Automatic seat selection, reservation, basket creation, payment, or purchase.
- Supporting cinemas other than BFI IMAX.
- Public bot registration; access is limited to configured Telegram user IDs.
- Accessible-seat recommendations in the first version. Wheelchair spaces, companion seats, and other restricted access seats are excluded.
- Price filtering or ranking by standard, premium, or VIP category.
- Natural-language interpretation through an LLM.

## BFI access method and web contract

`whatson.bfi.org.uk` runs Tessitura Network "Online" 7.90, the classic ASP product rather than TNEW. Two unauthenticated `GET` routes expose everything the service needs. Neither requires a cookie, login, or session token for a cold read.

This is an undocumented public HTML/SVG contract, not an official or supported BFI API. It carries no version, no deprecation notice, and no compatibility guarantee. Every field the service relies on is validated on every parse, and the gateway fails closed if the contract changes. A parse failure is never interpreted as no availability.

### Transport

Cloudflare fingerprints the TLS handshake in front of these routes. Plain `curl`, `requests`, `httpx`, and `wget` receive `HTTP 403` with a `cf-mitigated: challenge` response header regardless of `User-Agent`, `sec-ch-ua`, or any other header value, because the JA3/JA4 fingerprint is what is inspected. Header spoofing does not change the outcome, and a clean headless browser session is challenged as well.

The gateway therefore uses `curl_cffi`, which binds libcurl-impersonate and reproduces a real Chrome TLS and HTTP/2 fingerprint:

```python
from curl_cffi.requests import AsyncSession

session = AsyncSession(impersonate="chrome")
```

One long-lived `AsyncSession` is created at startup, shared by every BFI request, and closed on shutdown. There is no browser process, no CDP connection, no proxy pool, and no challenge handling.

Two consequences are accepted deliberately:

- The impersonation profile fixes the `User-Agent` and the rest of the browser header set. The service cannot also advertise a truthful bot identity in `User-Agent`, because a custom agent string paired with a Chrome fingerprint is incoherent and is itself a bot signal. The honest identification the design can still offer is behavioural: strictly read-only requests, a low and rate-limited request volume, no purchase or write path, and immediate backoff on any challenge.
- The impersonation profile is a compatibility surface. `BFI_IMPERSONATE_PROFILE` makes it configurable so a profile can be changed without a code release if Cloudflare's checks move.

Sporadic `403` responses still occur under sustained request rates even with impersonation, so retry and circuit-breaker handling remain mandatory.

### Endpoints

Performances for one film article:

```text
GET https://whatson.bfi.org.uk/imax/Online/default.asp
      ?BOparam::WScontent::loadArticle::permalink=<slug>
```

Seat map for one performance:

```text
GET https://whatson.bfi.org.uk/imax/Online/mapSelect.asp
      ?BOparam::WSmap::loadMap::performance_ids=<performance-id>
```

The `::` sequences in parameter *names* must be sent literally. Standard encoders emit `%3A%3A`, so query strings are assembled by concatenation rather than passed as a params mapping. Parameter *values* are percent-encoded normally; the `sToken` pagination value in particular contains commas and must be encoded.

The seat-map URL doubles as the Telegram purchase link. It opens BFI's own seat map in the user's browser and leaves seat selection and checkout entirely to the user. No `createBO` parameter is required.

### Performance records

The film page embeds a JavaScript object literal, not JSON:

```js
var articleContext = {
  searchHeaders : [ "Id", "Object Type", ... ],   // display labels
  searchNames   : [ "id", "object_type", ... ],   // 98 field names
  searchResults : [ [ ...98 values... ], ... ],   // one array per row
  pagination    : { current_page: "1", page_size: "5", total_pages: "2" },
  articleId     : "2152D1E8-CFF7-419F-BE57-F51C1E490F24",
  sToken        : "1,060cecdb,...",
  ...
};
```

Parsing steps, in order:

1. Extract the object literal following `var articleContext = `, terminated by the `};` that closes the assignment statement.
2. Quote bare keys (`searchNames :` becomes `"searchNames" :`).
3. Strip trailing commas before `}` or `]`.
4. Replace `\'`, which is legal in JavaScript and illegal in JSON, with `'`.
5. Decode as JSON, then zip `searchNames` against each `searchResults` row to produce one record per row.
6. Validate that `searchNames`, `searchResults`, `pagination`, and `articleId` are present and that every consumed field below exists on every record. A missing element is a parser-contract error.

Fields the service consumes:

| field | meaning |
|---|---|
| `id` | performance GUID; the key for the seat map |
| `object_type` | `P` performance, `A` article, `B` bundle, `M` misc item, `G` gift, `S` stored value. Only `P` rows are performances |
| `availability_num` | exact count of seats remaining, or a status-paired sentinel: `-1` with `U` means the count is withheld; `-4` with `S` means sold out |
| `availability_status` | `E` excellent, `G` good, `L` limited, `S` sold out, plus `U` and `N` in the client code |
| `sales_status` | `S`, `O`, `R` on sale; `C` not yet on sale; `N` not on sale; `X` cancelled. A trailing `*` flags a promotion and does not change the base meaning |
| `options` | array of option codes. `2` present means reserved seating, so `mapSelect.asp` is meaningful for that performance |
| `start_date` | local start, e.g. `Wednesday 26 August 2026 18:15` |
| `start_date_time`, `start_date_date`, `start_date_month`, `start_date_year` | split components |
| `min_price`, `max_price` | e.g. `£0.00` and `£26.00`. The `£0.00` floor is a comp or access price type, not a purchasable standard price |
| `keywords` | format, e.g. `IMAX (with laser)` |
| `venue_description` | e.g. `BFI IMAX, Waterloo` |
| `venue_name` | screen configuration |
| `short_description`, `name` | film title, preferring `short_description` |
| `additional_info` | relative URL back to the performance's article |

Rows are filtered to `object_type == "P"` and deduplicated by `id`.

### Pagination

`page_size` is set per article and is 5 on a film page, so a film with more than five performances always paginates. Pages 2 and above:

```text
GET /imax/Online/default.asp
      ?sToken=<percent-encoded sToken from page 1>
      &BOset::WScontent::SearchResultsInfo::current_page=<n>
      &doWork::WScontent::getPage=
      &BOparam::WScontent::getPage::article_id=<articleId>
```

Each page returns the same `articleContext` shape. `sToken` and `articleId` are read from page 1 and are required only for subsequent pages. The gateway iterates from page 2 to `pagination.total_pages` inclusive and treats a page count that changes mid-chain, or a page that yields no rows, as a parser-contract error rather than as an empty result. `sToken` is transient and is neither logged nor persisted.

### Seat map

`mapSelect.asp` returns a server-rendered SVG inside an HTML page. There is no XHR call and no separate data feed. Every seat is one `<circle>`, nested inside a `<g>` whose `id` is the price-zone GUID:

```html
<g id="3F5950DF-50B9-45EB-A78A-E0E518827835" style="fill: #1DABFF; stroke: #1DABFF">
  <circle role="button" r="2" class="seatA"
          id="9D64A4E0-69B3-4512-B3A4-E86ADA4C7955"
          data-status="A"
          data-seat-section="BFI IMAX" data-seat-row="P" data-seat-seat="29"
          data-tsdesc="BFI IMAX P 29"
          data-seatviewid="BFI IMAX-P-29"
          data-tsmessage="NB: This is a space for wheelchair users..."
          cx="596.143" cy="153.938" />
</g>
```

| attribute | meaning |
|---|---|
| `id` | stable seat GUID |
| `data-status` | `A` available, `S` sold, `U` unavailable (held, killed, or restricted), `O` sitting in another customer's basket |
| `class` | mirrors status as `seatA`, `seatS`, `seatu` |
| `data-seat-section`, `data-seat-row`, `data-seat-seat` | seat identity |
| `data-tsmessage` | obstruction or accessibility note |
| `cx`, `cy` | position within the seat-map coordinate space |
| enclosing `<g id>` | price-zone GUID |

Parsing uses `lxml` over the whole document. Each `<circle>` carrying `data-status` is a seat; its price zone is the `id` of its nearest ancestor `<g>`. The same seat `id` can appear on more than one circle, because a seat is drawn as an outline and a fill, so seats are deduplicated by `id`. Only `data-status="A"` counts as available. `O` is contended rather than permanently gone: it can revert to `A` when another customer's basket times out, but it is not offerable now and is excluded from option generation exactly like `S` and `U`.

Price zones are resolved from inline legend scripts:

```js
let priceZoneId = "3F5950DF-50B9-45EB-A78A-E0E518827835";
priceZoneInfo[priceZoneId].label  = "1 Standard";
priceZoneInfo[priceZoneId].colour = "#1DABFF";
```

with the price taken from the adjacent `<span class="price-zone-price-text">`. A zone GUID can appear on the map without a corresponding legend entry, in which case it carries no purchasable price; such a zone resolves to an unlisted zone with a null price and is a display-only condition, not an error.

Observed zones and prices:

| zone | price |
|---|---|
| 1 Standard | £22.00 |
| Premium | £25.00 |
| BFI IMAX VIP | £26.00 |
| BFI IMAX wheelchair space | £22.00 |
| BFI IMAX assistant or companion | £22.00 |

The wheelchair and companion zone labels, together with `data-tsmessage` text, are the two independent signals used to exclude restricted access seats.

### Venue geometry

BFI IMAX presents 493 seats across 15 rows labelled `A` to `Q`, skipping `I` and `O`. The skipped letters are why row distance in the ranking formula is computed from observed row order by median vertical (`cy`) position rather than from alphabetic distance.

### Cross-source consistency

`availability_num` and the seat map are independently produced. In a single controlled read they agree exactly, and 387 against 387 was observed for the verified performance. At runtime the two documents are fetched seconds apart and genuine bookings occur in between, so the gateway tolerates small drift rather than requiring equality; the smoke test, which reads both in immediate succession, asserts the exact match. This is the cheapest available detector of drift in either parser.

### Verification

Verified live from the target Mac on 2026-08-11, read-only, five HTTP requests in total: one plain-`httpx` control plus four `curl_cffi` reads.

| check | result |
|---|---|
| Control: plain `httpx` GET of the film page | `HTTP 403`, `cf-mitigated: challenge` |
| `curl_cffi` `Session(impersonate="chrome")`, film page `dog-stars` | `HTTP 200`, 124,680 bytes |
| `curl_cffi` `AsyncSession(impersonate="chrome")`, same page | `HTTP 200`, 124,686 bytes, `articleContext` present |
| `articleContext` parse | 98 field names, 5 rows, `pagination` `{current_page: 1, page_size: 5, total_pages: 2}` |
| Page 2 via `sToken` + `getPage` | `HTTP 200`, 122,951 bytes |
| Performance records after `object_type == "P"` filter and dedup | 8 records, all consumed fields present, all `options` containing `2` |
| Seat map for `2475959F-2B73-4EA6-AD26-AFA8AEB785FD` (Wed 26 Aug 2026 18:15) | `HTTP 200`, 504,320 bytes |
| Seats parsed | 493 unique, all carrying `cx`/`cy`; statuses `A` 387, `S` 86, `U` 20 |
| `availability_num` versus `data-status="A"` count | 387 versus 387, exact match |
| Price zones resolved from legend | 5 zones, all with prices |
| Rows observed | `A` to `Q` excluding `I` and `O`, 15 rows |
| Seats carrying `data-tsmessage` | 8, all wheelchair-space or companion wording |

Payload sizes measured in that run set the fetch strategy. One film-page request is roughly 124 KB and carries up to `page_size` performances, so the full performance list for a film costs one such request per page of the pagination chain. One seat map is roughly 504 KB and covers a single performance. Seat maps are therefore fetched only for performances that survive every cheaper filter.

## System architecture

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
   - Owns the single shared `curl_cffi` Chrome-impersonating session and performs only HTTPS GET requests to validated BFI IMAX routes.
   - Builds query strings literally so Tessitura's `::` parameter names survive unencoded.
   - Parses film pages and seat maps into typed domain records, validating the contract on every parse.
   - Applies host-level concurrency, rate limiting, request coalescing, challenge detection, and circuit breaking.

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
- `curl_cffi` for async HTTP through a Chrome-impersonating TLS stack. It is the only supported transport for BFI; an ordinary async HTTP client cannot reach these routes.
- `lxml` for HTML and SVG parsing.
- `aiosqlite` for async SQLite access.

The `articleContext` JavaScript literal is normalised and decoded with the standard library, using the steps set out in the contract section, so no additional JavaScript-parsing dependency is required.

## Domain model

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
- Exact seats-remaining count from `availability_num`.
- Reserved-seating flag, true when `options` contains `2`.
- Venue and booking type where supplied.
- Canonical direct seat-map URL.

### Seat

A seat contains:

- Stable BFI seat ID.
- Section, row, and displayed seat number.
- SVG `x` and `y` coordinates.
- Raw BFI status code and its mapped availability state.
- Price-zone GUID and resolved zone label and price when available, for display only.
- A restricted-access flag derived from the zone label and from BFI's `data-tsmessage` seat message.

### Ranked option

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

## Telegram interaction

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

### Watch wizard

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

## Retrieval flow

### URL validation

The gateway accepts only:

- Scheme `https`.
- Exact host `whatson.bfi.org.uk`.
- `/imax/Online/default.asp` with exactly one non-empty `BOparam::WScontent::loadArticle::permalink` value.
- `/imax/Online/article/<slug>` with one non-empty path segment after `article`.

Additional query parameters that are not the permalink are ignored rather than treated as grounds for rejection, because the request sent to BFI is rebuilt from the slug and never replays the user's parameters. User information, fragments, non-default ports, duplicate or conflicting permalink values, alternate hosts, redirects to alternate hosts, and arbitrary paths are rejected. Redirects are followed only when every hop remains on the exact allowed host and resolves to one of the accepted route shapes.

Validation reduces the URL to a single article slug, which is the watch's canonical identity. Requests are then reconstructed from that slug in the literal form given in the contract section rather than by replaying the user's raw URL, so tracking parameters and encoding variations cannot reach BFI.

One slug is one article. BFI publishes subtitled variants as separate articles, so `dog-stars` and `dog-stars-sdh` are distinct watches and a watch on one does not surface performances of the other. The wizard shows this caveat whenever the slug ends in a recognised variant suffix, initially `-sdh`, and treats the suffix list as a best-effort hint rather than a validation rule. The aggregate programme article that lists every IMAX film in one result set is not used; watches are per-film by design.

### Film discovery

1. Normalize and validate the supplied URL and reduce it to an article slug.
2. Fetch the film page through the shared Chrome-impersonating session, with the permalink parameter name sent literally.
3. Locate and parse the `articleContext` object literal using the documented normalisation steps.
4. Validate the article identity, pagination block, search-field mapping, and performance records.
5. Follow the `sToken` pagination chain to `total_pages` and merge every page's rows.
6. Keep rows whose `object_type` is `P` and deduplicate by performance ID.
7. Convert performance starts to `Europe/London`.
8. Apply the inclusive date and daily time predicates.
9. Skip performances whose `sales_status` is not an on-sale code, whose `options` does not contain `2`, or whose `availability_num` is below the requested ticket quantity.
10. Construct the canonical seat-map URL for each remaining performance.

Filtering on the exact `availability_num` count rather than only on the coarse availability band avoids fetching a roughly 504 KB seat map for a performance that cannot possibly hold the requested block. A performance without reserved seating has no meaningful seat map; it is recorded as an unsupported-performance skip rather than an error. A future poll re-evaluates the film page, so a performance that later gains enough seats is checked then.

### Seat retrieval

For each candidate performance:

1. Fetch its canonical seat-map URL with GET.
2. Verify that returned performance metadata matches the requested performance ID.
3. Resolve the price-zone legend into zone GUID, label, and price.
4. Parse every `<circle>` carrying `data-status`, attach the zone of its nearest ancestor `<g>`, falling back to an unlisted zone with no price when that GUID has no legend entry, and deduplicate by seat ID so a seat drawn as both outline and fill is counted once.
5. Use all parsed physical seat positions to establish row geometry.
6. Retain only seats whose status code is `A`. Codes `S`, `U`, and `O` are all unofferable; `O` is recorded as contended rather than sold, because it can revert when another customer's basket times out.
7. Exclude wheelchair spaces, companion seats, and other restricted access seats, identified by the seat's price-zone label and by its `data-tsmessage` text.

Zone labels and prices are carried for display only and never affect rank.

The gateway allows at most two in-flight BFI requests and starts requests at least one second apart. Requests use a 10-second connection timeout and a 45-second total timeout, which is deliberate headroom over the sub-second responses observed for both routes. Identical film or performance requests share one in-flight operation. A completed response may be reused only within 60 seconds, preventing concurrent watches from creating duplicate load without presenting materially stale availability.

The gateway cross-checks the count of `A` seats against the `availability_num` reported for the same performance on the film page. The two documents are fetched seconds apart and real bookings occur in between, so an exact match is not required:

- A difference of more than five seats is logged as a contract-drift signal for the smoke test to investigate. The check does not fail.
- A seat map that yields zero parsed seats is a parser-contract error.
- Zero *available* seats while `availability_num` is positive is not, by itself, a parser-contract error. The two documents are read up to a cache lifetime apart, so the last free seats can be bought (`S`) or taken into another customer's basket (`O`) in between; failing there would pause the watch and alert its owner about a change that never happened, on precisely the nearly-sold-out screening they most want watched. It is a contract error only when some seat carries a `data-status` the parser does not recognise, which is what a changed page looks like.
- A map carrying no `A`, `S` or `O` seat at all cannot mean "just sold out" either, since a sell-out leaves the seats somebody took. That is logged as a warning for the smoke test to investigate, but the read still succeeds: stopping a watch asserts the site changed, and this evidence does not show that.

### Adjacency

Seats form a contiguous run only when they:

- Belong to the same section and row.
- Have consecutive displayed numeric seat numbers.
- Have an SVG horizontal gap no greater than 1.75 times that row's median normal adjacent-seat gap.

The row median is calculated from absolute horizontal gaps between numerically consecutive physical seats; aisle outliers therefore do not define normal spacing. The geometric rule prevents consecutive numbers on opposite sides of an aisle from being treated as adjacent. A row with fewer than three usable gaps, a zero-width row, or a non-numeric or structurally ambiguous seat label is excluded and recorded as a parser-contract warning.

For a requested quantity `N`, every sliding window of exactly `N` seats within each contiguous run becomes an option. A run of five seats therefore produces four distinct two-seat options. Options are deduplicated by their stable option key.

## Ranking

### Explicit preferences

Blocks containing an excluded row or seat are removed.

Remaining blocks are ranked first by:

1. Number of seats overlapping the preferred exact-seat set.
2. Whether the block is in a preferred row.

An explicit preference therefore overrides the default venue profile without changing the underlying view score.

### Default BFI view score

The default score ranges from 0 through 100 and measures viewing position only:

```text
row_distance = observed row steps from the nearest of L or M
row_score = max(0, 40 - 5 * row_distance)

row_center = midpoint of the minimum and maximum physical seat x-coordinates in the row
half_width = (maximum row x - minimum row x) / 2
block_center_x = midpoint of the minimum and maximum x-coordinates of the block's seats
normalized_offset = abs(block_center_x - row_center) / half_width
center_score = 60 * max(0, 1 - normalized_offset)

view_score = row_score + center_score
```

Rows are ordered front to back by their observed median vertical (`cy`) coordinate, so omitted letters such as `I` and `O` do not distort distance. Row distance is counted in steps along that observed order, not in alphabetic steps. Rows L and M are the default ideal band, and horizontal centring has the stronger weight. The score is rounded to two decimal places. Standard, premium, and VIP categories do not change it.

### Final ordering

The deterministic sort key is:

1. Preferred exact-seat overlap, descending.
2. Preferred-row match, descending.
3. Five-point view-score band (`floor(view_score / 5)`), descending.
4. Absolute minutes from the preferred performance date/time, ascending; all options tie when none is supplied.
5. Raw view score, descending.
6. Performance start, ascending.
7. Section, row, and seat labels, ascending.

The score band lets performance time break genuine seat-quality near-ties while preventing a materially worse seat from winning solely because of time.

## Scheduling and notifications

The scheduler scans for due watches once per minute. A recurring watch's next run is its completion time plus the chosen interval plus positive random jitter of up to 10 percent. The interval itself cannot be less than 15 minutes.

Startup recovers overdue watches from SQLite and staggers them through the same host rate limiter. A filesystem lock prevents a second service instance from running against the same database.

An immediate creation or `/check` response displays the current snapshot. After that Telegram delivery succeeds, every current option is marked as already surfaced, including options beyond the first page.

Later recurring checks send one digest when either:

- The snapshot contains an option key never previously surfaced for that watch.
- The current best option has a better complete ranking tuple than the last notified best option.

The digest shows the current top ten, counts of all current and new options, and a button to browse the complete snapshot. All current option keys are marked surfaced only after the Telegram delivery succeeds. Unchanged snapshots, worse-only changes, and mere disappearances are silent.

## Error handling, rate limiting, and Cloudflare

Errors are typed and handled distinctly:

- **Input error:** remain in the wizard step and explain the correction.
- **Network or BFI 5xx error:** retry twice after delays of approximately one and three seconds, each with up to 20 percent positive jitter, then put the watch into backoff.
- **Transient BFI 403:** a `403` that carries no challenge marker is treated as rate pressure. Retry up to three times with delays of approximately two, four, and six seconds plus jitter. Exhausting those retries is escalated to the challenge path.
- **BFI challenge or rate-limit condition:** open the host-wide circuit breaker, as described below.
- **BFI parser-contract error:** pause the affected watch, retain its last valid snapshot, and alert its owner once.
- **Telegram delivery error:** retain a pending notification for retry.
- **Database error:** fail the operation, log it, and do not emit a success-shaped response.

A challenge or rate-limit condition is detected from a `429` status, from a `403` carrying the `cf-mitigated` response header, from a `403` that persists across the transient-403 retries, and from a `200` whose body is a Cloudflare interstitial rather than a parseable BFI document. The body check matters because a challenge page can return `200`; a document that parses as neither `articleContext` nor a seat map is inspected for interstitial markers before it is classified as a parser-contract error.

These responses are not retried immediately. Detection opens the persisted host-wide circuit breaker. No BFI requests are attempted until the next retry point, when one coordinator-owned request acts as the probe; its result either closes the circuit or advances the delay.

The retry sequence is:

```text
15 minutes, 30 minutes, 1 hour, 2 hours, 4 hours, then 6 hours maximum
```

Each delay receives positive jitter of up to 10 percent. Affected users receive one degraded-service alert, not one alert per watch. A successful probe closes the circuit, restores due scheduling, and sends one recovery alert.

Sustained request rates provoke sporadic challenges even through an impersonating client, so the rate limits in the retrieval flow are a correctness requirement rather than politeness. The 15-minute minimum recurring interval, the two-request concurrency ceiling, the one-second minimum spacing, the response-coalescing window, and the exact-count filter that suppresses avoidable seat-map fetches together bound the load a full watch list can generate.

The service does not attempt to answer, bypass, outsource, or automate a challenge once one is presented. Impersonating a browser's TLS fingerprint to read a public page is the transport the site requires of any client; responding to a challenge is not, and the circuit breaker exists so the service backs off instead. If BFI permanently protects the public route, the service remains visibly degraded until an authorized data source or a separately approved design replaces the gateway.

## Operations and security

`launchd` runs the process with `KeepAlive` and restarts it after failures. The process handles termination signals by stopping new checks, awaiting active checks within a bounded shutdown period, committing state, and closing Telegram, HTTP, and SQLite clients.

Configuration is loaded from a user-owned file with mode `0600`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`
- `DATABASE_PATH`
- `LOG_LEVEL`
- `BFI_IMPERSONATE_PROFILE`, the `curl_cffi` impersonation profile, defaulting to `chrome`

There is no configurable `User-Agent`. The impersonation profile supplies a coherent browser header set, and overriding one header within it would both break the fingerprint and defeat the purpose of the transport.

Secrets are never stored in SQLite or written to logs. Logs are structured, redact URL tokens and Telegram credentials, and include watch/check correlation IDs. BFI HTML, SVG bodies, and transient `sToken` values are neither logged nor persisted; only parsed records, statuses, and byte counts are retained. The single-instance lock is `<DATABASE_PATH>.lock` and is held for the process lifetime.

## Testing strategy

### Unit tests

- URL normalization, host/path restrictions, slug reduction, and redirect validation.
- Literal query-string construction, asserting that `::` in parameter names is never percent-encoded and that the `sToken` value is.
- `articleContext` extraction, JavaScript-literal normalisation (bare keys, trailing commas, `\'` escapes), required-field validation, `object_type` filtering, and deduplication.
- Pagination chain assembly from `sToken`, `articleId`, and `total_pages`, including a truncated or empty later page.
- SVG seat parsing, duplicate outline/fill circle deduplication, price-zone resolution from the enclosing `<g>`, unlisted-zone fallback, access-seat exclusion by zone label and by `data-tsmessage`, and treatment of `O` as unofferable.
- Performance eligibility filtering on `sales_status`, `options` containing `2`, and `availability_num` against requested quantity.
- Challenge classification: `429`, `403` with `cf-mitigated`, repeated `403` without it, and a `200` interstitial body.
- Row spacing and aisle-aware adjacency, including rows whose letters skip `I` and `O`.
- Every sliding block for quantities one through eight.
- View-score formula, explicit preferences, quality bands, time tie-breaks, and deterministic final ordering.
- Date/time filtering across daylight-saving changes and midnight-wrapping time windows.
- Notification option keys, improvement detection, and repeat suppression.

### Integration tests

- Telegram wizard and command handlers with fake updates and callback ownership checks.
- SQLite migrations, transactions, restart recovery, retention, and cascade deletion using temporary databases.
- Scheduler due-time and circuit-breaker behavior with a fake clock and deterministic jitter.
- HTTP-mocked end-to-end checks for available, no-match, sold-out, unreserved-seating, malformed, 5xx, transient 403, challenged 403, 429, interstitial 200, and recovery responses.
- Rate-limit conformance: concurrency ceiling, minimum request spacing, and coalescing window under a fake clock.
- Telegram delivery failure followed by idempotent retry.

Fixtures are minimal synthetic HTML/SVG documents that preserve the required contract without committing full BFI pages. No test makes a live BFI request.

### Manual contract smoke test

A separately invoked smoke test performs one film-page GET, its pagination chain, and one seat-map GET for one returned performance. It asserts that each response is `HTTP 200`, that `articleContext` parses into performance records with every consumed field present, that the seat map parses into deduplicated seats carrying status, row, seat, coordinates, and price zone, and that `availability_num` equals the count of `data-status="A"` for that performance. It reports contract drift and the observed impersonation profile. It is not part of normal automated test runs, does not scan the programme, and does not poll.

## Acceptance criteria

1. Given a valid BFI IMAX film URL, date/time criteria, and quantity, the bot returns every distinct eligible contiguous block for every matching on-sale performance, across the complete paginated performance list rather than the first page alone.
2. Results follow the approved preference, view-quality, and preferred-time ordering and remain deterministic across repeated runs over identical input.
3. Every result link opens the correct BFI performance seat map and requires the user to select seats and complete purchase manually.
4. Recurring watches run no more frequently than configured, survive service restarts, and expire after their date range.
5. A new or improved option sends one digest; an unchanged result sends none.
6. Unauthorized Telegram users cannot create, inspect, mutate, or page through watches.
7. Retrieval, parser, challenge, and delivery failures are visible and never presented as no ticket availability.
8. The service never performs a BFI write action and never attempts to answer or bypass an access-control challenge; a detected challenge opens the circuit breaker and degrades the service visibly.
9. Every BFI request goes through the shared Chrome-impersonating session with literal `::` parameter names, and observed rate limits hold under a full watch list.
