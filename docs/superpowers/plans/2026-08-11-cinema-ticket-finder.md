# Cinema Ticket Finder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an allow-listed Telegram service that reads BFI IMAX performances and seat maps, ranks every eligible adjacent-seat option, and monitors for new or improved tickets.

**Architecture:** A single Python asyncio process runs under `launchd`, using Telegram long polling, a shared `curl_cffi` Chrome-impersonating session for read-only BFI GETs, pure filtering/ranking functions, and SQLite repositories. Persistent scheduling, result snapshots, notification state, and the host-wide Cloudflare circuit breaker survive restarts.

**Tech Stack:** Python 3.12+, `python-telegram-bot`, `curl_cffi`, `lxml`, `aiosqlite`, SQLite, pytest, pytest-asyncio, Ruff, mypy, macOS `launchd`.

## Global Constraints

- Require Python 3.12 or later; invoke it as `python3.12` on the target Mac.
- Use one shared `curl_cffi.requests.AsyncSession` with `BFI_IMPERSONATE_PROFILE`, default `chrome`, for every BFI request.
- Send Tessitura parameter names containing `::` literally; percent-encode parameter values only.
- Accept only HTTPS BFI IMAX article URLs on exact host `whatson.bfi.org.uk` and reconstruct outbound URLs from the validated slug.
- Perform read-only GETs only. Never select or reserve seats, create a basket, automate checkout, solve a challenge, use a browser/CDP, or rotate proxies/IPs.
- Treat any presented Cloudflare challenge as a stop signal and open the persisted host circuit.
- Limit BFI traffic to two in-flight requests, starts at least one second apart, a 60-second coalescing/cache window, and recurring intervals of at least 15 minutes.
- Use `Europe/London` for user criteria and performance display; store instants in UTC.
- Support exactly one through eight adjacent ordinary seats; exclude wheelchair, companion, and other restricted access seats.
- Rank viewing position only; standard, premium, and VIP categories are display metadata.
- Restrict Telegram access to `TELEGRAM_ALLOWED_USER_IDS` and verify resource ownership on every command and callback.
- Use SQLite foreign keys and WAL mode. Retain snapshots for 24 hours except each watch's latest, check summaries for 30 days, drafts for 24 hours, and notification keys for the watch lifetime.
- Load secrets from a user-owned configuration file with mode `0600`; never log Telegram credentials, BFI bodies, or full `sToken` values.
- Normal automated tests must not contact BFI. Only the explicit manual smoke command may make the bounded live request chain.
- Use conventional commits and never add a `Co-authored-by` trailer.

---

## Planned File Layout

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, runtime/dev dependencies, pytest/Ruff/mypy configuration, CLI entry points |
| `.gitignore` | Ignore virtualenv, caches, local database, logs, and secret env files |
| `src/cinema_friend/config.py` | Parse and validate service configuration |
| `src/cinema_friend/clock.py` | Real clock and injectable clock protocol for deterministic timing tests |
| `src/cinema_friend/domain/state.py` | Enums for watch, check, seat, delivery, and circuit states |
| `src/cinema_friend/domain/watch.py` | Watch criteria and watch lifecycle records |
| `src/cinema_friend/domain/bfi.py` | Performance, price-zone, seat, and seat-map records |
| `src/cinema_friend/domain/results.py` | Seat blocks, rank vectors, options, snapshots, and notification decisions |
| `src/cinema_friend/domain/errors.py` | Typed input, transport, challenge, contract, persistence, and delivery errors |
| `src/cinema_friend/bfi/urls.py` | BFI URL validation and literal Tessitura query construction |
| `src/cinema_friend/bfi/article_context.py` | Extract/normalise `articleContext`, validate fields, and map performance rows |
| `src/cinema_friend/bfi/seat_map.py` | Parse SVG seats, price zones, access notes, and performance identity |
| `src/cinema_friend/bfi/transport.py` | Shared `curl_cffi` session, spacing, concurrency, retries, redirects, and challenge circuit |
| `src/cinema_friend/bfi/gateway.py` | Pagination, request coalescing, typed BFI reads, and cross-source checks |
| `src/cinema_friend/watches/criteria.py` | Date/time and performance eligibility predicates |
| `src/cinema_friend/watches/blocks.py` | Aisle-aware contiguous block generation |
| `src/cinema_friend/watches/ranking.py` | BFI view score and deterministic final ordering |
| `src/cinema_friend/storage/migrations/001_initial.sql` | Complete initial SQLite schema and indexes |
| `src/cinema_friend/storage/database.py` | Connections, WAL/foreign-key setup, migrations, and transaction context |
| `src/cinema_friend/storage/watch_repository.py` | Watch persistence and due-watch queries |
| `src/cinema_friend/storage/draft_repository.py` | Persisted Telegram wizard drafts |
| `src/cinema_friend/storage/result_repository.py` | Check runs, snapshots, ranked options, and retention |
| `src/cinema_friend/storage/notification_repository.py` | Surfaced option keys, notification state, and retryable deliveries |
| `src/cinema_friend/storage/circuit_repository.py` | Persisted BFI host circuit |
| `src/cinema_friend/services/notification_policy.py` | Pure new/improved/degraded/recovery notification decisions |
| `src/cinema_friend/services/watch_service.py` | Authorized watch lifecycle operations |
| `src/cinema_friend/services/check_service.py` | End-to-end check orchestration and atomic result persistence |
| `src/cinema_friend/services/scheduler.py` | Due-watch, pending-delivery, and retention loops |
| `src/cinema_friend/telegram/auth.py` | Allow-list and ownership guards |
| `src/cinema_friend/telegram/rendering.py` | Telegram result pages, watch lists, errors, and keyboards |
| `src/cinema_friend/telegram/wizard.py` | Persistent guided watch-creation state machine |
| `src/cinema_friend/telegram/commands.py` | `/watches`, `/check`, `/pause`, `/resume`, `/delete`, `/help` |
| `src/cinema_friend/telegram/callbacks.py` | Result pagination and inline action callbacks |
| `src/cinema_friend/telegram/bot.py` | Handler registration and pending-notification delivery |
| `src/cinema_friend/app.py` | Dependency wiring, lifecycle, signals, and graceful shutdown |
| `src/cinema_friend/__main__.py` | `cinema-friend` CLI entry point |
| `src/cinema_friend/smoke.py` | Explicit bounded live BFI contract check |
| `scripts/install_launch_agent.py` | Render/install/uninstall a path-correct user LaunchAgent |
| `.env.example` | Non-secret configuration shape |
| `README.md` | Setup, Telegram configuration, launchd operation, smoke test, and recovery |
| `tests/` | Unit and integration tests mirroring the package layout |

---

### Task 1: Bootstrap the package, configuration, and domain types

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/cinema_friend/__init__.py`
- Create: `src/cinema_friend/config.py`
- Create: `src/cinema_friend/clock.py`
- Create: `src/cinema_friend/domain/__init__.py`
- Create: `src/cinema_friend/domain/state.py`
- Create: `src/cinema_friend/domain/watch.py`
- Create: `src/cinema_friend/domain/bfi.py`
- Create: `src/cinema_friend/domain/results.py`
- Create: `src/cinema_friend/domain/errors.py`
- Create: `tests/test_config.py`
- Create: `tests/domain/test_watch.py`
- Create: `tests/domain/test_results.py`

**Interfaces:**
- Produces: `Settings.from_mapping(values: Mapping[str, str]) -> Settings`
- Produces: `Clock` protocol with `now()`, `monotonic()`, and async `sleep(seconds)`
- Produces: `WatchCriteria`, `Watch`, `Performance`, `PriceZone`, `Seat`, `SeatMap`, `SeatBlock`, `RankVector`, `RankedOption`, `CheckResult`, `ResultSnapshot`, `SnapshotPage`, `NotificationPayload`, `HostCircuit`
- Produces enums: `WatchMode`, `WatchStatus`, `CheckTrigger`, `CheckOutcome`, `SeatStatus`, `DeliveryStatus`, `CircuitState`
- Produces errors: `InputError`, `BfiNetworkError`, `BfiChallengeError`, `BfiContractError`, `CircuitOpenError`, `PersistenceError`, `DeliveryError`

- [ ] **Step 1: Create package metadata and the test toolchain**

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "cinema-friend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "aiosqlite>=0.20,<1",
  "curl_cffi>=0.16,<0.17",
  "lxml>=5.3,<7",
  "python-dotenv>=1.0,<2",
  "python-telegram-bot>=22,<23",
]

[project.optional-dependencies]
dev = [
  "lxml-stubs>=0.5,<1",
  "mypy>=1.14,<2",
  "pytest>=8,<9",
  "pytest-asyncio>=0.24,<2",
  "pytest-cov>=6,<8",
  "ruff>=0.9,<1",
]

[project.scripts]
cinema-friend = "cinema_friend.__main__:main"
cinema-friend-smoke = "cinema_friend.smoke:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["cinema_friend"]
```

Create `.gitignore` entries for `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.db`, `*.db-*`, `*.log`, and `.env`.

- [ ] **Step 2: Create and populate the Python 3.12 environment**

Run:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

Expected: installation completes and `.venv/bin/python --version` reports Python 3.12.x.

- [ ] **Step 3: Write failing configuration tests**

```python
def test_settings_parse_allow_list_and_defaults(tmp_path):
    settings = Settings.from_mapping({
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_ALLOWED_USER_IDS": "11, 22",
        "DATABASE_PATH": str(tmp_path / "cinema.db"),
    })
    assert settings.allowed_user_ids == frozenset({11, 22})
    assert settings.bfi_impersonate_profile == "chrome"
    assert settings.minimum_watch_interval.total_seconds() == 900


def test_settings_reject_missing_token(tmp_path):
    with pytest.raises(InputError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_mapping({
            "TELEGRAM_ALLOWED_USER_IDS": "11",
            "DATABASE_PATH": str(tmp_path / "cinema.db"),
        })
```

- [ ] **Step 4: Run the configuration tests and observe the failure**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`

Expected: FAIL because `cinema_friend.config` does not exist.

- [ ] **Step 5: Implement immutable settings and a real/injectable clock**

```python
@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    database_path: Path
    log_level: str = "INFO"
    bfi_impersonate_profile: str = "chrome"
    minimum_watch_interval: timedelta = timedelta(minutes=15)
    bfi_max_concurrency: int = 2
    bfi_min_spacing_seconds: float = 1.0
    bfi_cache_seconds: float = 60.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "Settings":
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise InputError("TELEGRAM_BOT_TOKEN is required")
        raw_ids = values.get("TELEGRAM_ALLOWED_USER_IDS", "")
        try:
            ids = frozenset(int(value.strip()) for value in raw_ids.split(",") if value.strip())
        except ValueError as exc:
            raise InputError("TELEGRAM_ALLOWED_USER_IDS must contain integers") from exc
        if not ids:
            raise InputError("TELEGRAM_ALLOWED_USER_IDS must not be empty")
        database = values.get("DATABASE_PATH", "").strip()
        if not database:
            raise InputError("DATABASE_PATH is required")
        return cls(
            telegram_bot_token=token,
            allowed_user_ids=ids,
            database_path=Path(database).expanduser(),
            log_level=values.get("LOG_LEVEL", "INFO").upper(),
            bfi_impersonate_profile=values.get("BFI_IMPERSONATE_PROFILE", "chrome"),
        )
```

Define `Clock` as a protocol and `SystemClock` with UTC `datetime.now(timezone.utc)`, `time.monotonic()`, and `asyncio.sleep()`.

- [ ] **Step 6: Write failing domain-invariant and rank-vector tests**

```python
def test_recurring_criteria_require_interval():
    with pytest.raises(InputError, match="interval"):
        WatchCriteria(
            source_url="https://whatson.bfi.org.uk/imax/Online/article/dog-stars",
            slug="dog-stars",
            date_from=date(2026, 8, 26),
            date_to=date(2026, 8, 30),
            time_from=time(18, 0),
            time_to=time(23, 0),
            quantity=2,
            mode=WatchMode.RECURRING,
            interval=None,
        )


def test_rank_vector_uses_ascending_sort_key_for_better_option():
    better = RankVector(
        preferred_seat_overlap=2,
        preferred_row_match=1,
        view_score_band=19,
        preferred_time_distance_minutes=20,
        raw_view_score=98.0,
        performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        seat_label="L17-L18",
    )
    worse = RankVector(
        preferred_seat_overlap=0,
        preferred_row_match=0,
        view_score_band=18,
        preferred_time_distance_minutes=0,
        raw_view_score=94.0,
        performance_start=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
        seat_label="K1-K2",
    )
    assert better.sort_key() < worse.sort_key()
```

- [ ] **Step 7: Run the domain tests and observe the failure**

Run: `.venv/bin/python -m pytest tests/domain -q`

Expected: FAIL because the domain modules and types are missing.

- [ ] **Step 8: Implement the exact domain records**

Use frozen, slotted dataclasses. `WatchCriteria` must contain source URL, slug, inclusive dates/times, quantity, preferred/excluded seats and rows, optional preferred UTC instant, mode, and optional interval. Enforce quantity `1 <= quantity <= 8`, ordered dates, preferred instant satisfying the criteria, recurring interval at least 15 minutes, and no interval for one-off mode.

Define `RankVector` with the seven keyword fields used above. `CheckResult` contains check-run ID, watch ID, trigger, outcome, optional snapshot ID, performance/option counts, and optional typed error detail. `SnapshotPage` contains snapshot ID, checked-at time, options, page, total pages, total options, and total performances. `NotificationPayload` contains kind, recipient user ID, optional watch/snapshot IDs, new-option count, and optional host/recovery text. `HostCircuit` contains host, state, backoff step, generation, optional next probe, and updated-at time.

`RankVector.sort_key()` must return:

```python
return (
    -self.preferred_seat_overlap,
    -self.preferred_row_match,
    -self.view_score_band,
    self.preferred_time_distance_minutes,
    -self.raw_view_score,
    self.performance_start,
    self.seat_label,
)
```

Keep BFI transport fields raw enough for contract validation: `Performance` includes raw sales/availability codes, exact `availability_num`, `reserved_seating`, and canonical seat-map URL; `Seat` includes raw status code, zone metadata, note, and coordinates.

- [ ] **Step 9: Run formatting, type, and focused tests**

Run:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/test_config.py tests/domain -q
```

Expected: all commands exit 0.

- [ ] **Step 10: Commit the bootstrap**

```bash
git add pyproject.toml .gitignore src/cinema_friend tests/test_config.py tests/domain
git commit -m "chore: bootstrap cinema friend service"
```

---

### Task 2: Validate film URLs and build literal Tessitura queries

**Files:**
- Create: `src/cinema_friend/bfi/__init__.py`
- Create: `src/cinema_friend/bfi/urls.py`
- Create: `tests/bfi/test_urls.py`

**Interfaces:**
- Consumes: `InputError`
- Produces: `ArticleRef(slug: str, canonical_url: str)`
- Produces: `parse_article_url(raw_url: str) -> ArticleRef`
- Produces: `film_page_url(slug: str) -> str`
- Produces: `pagination_url(s_token: str, page: int, article_id: str) -> str`
- Produces: `seat_map_url(performance_id: str) -> str`
- Produces: `validate_redirect_target(url: str) -> str`

- [ ] **Step 1: Write URL-validation and query-construction tests**

```python
def test_parse_article_url_rebuilds_canonical_url():
    ref = parse_article_url(
        "https://whatson.bfi.org.uk/imax/Online/default.asp"
        "?utm_source=x&BOparam::WScontent::loadArticle::permalink=dog-stars"
    )
    assert ref.slug == "dog-stars"
    assert ref.canonical_url == (
        "https://whatson.bfi.org.uk/imax/Online/default.asp"
        "?BOparam::WScontent::loadArticle::permalink=dog-stars"
    )


def test_pagination_keeps_parameter_names_literal_and_encodes_token():
    url = pagination_url("1,a/b+=", 2, "2152D1E8-CFF7-419F-BE57-F51C1E490F24")
    assert "BOset::WScontent::SearchResultsInfo::current_page=2" in url
    assert "sToken=1%2Ca%2Fb%2B%3D" in url
    assert "%3A%3A" not in url


@pytest.mark.parametrize("url", [
    "http://whatson.bfi.org.uk/imax/Online/article/dog-stars",
    "https://evil.example/imax/Online/article/dog-stars",
    "https://whatson.bfi.org.uk:444/imax/Online/article/dog-stars",
    "https://user@whatson.bfi.org.uk/imax/Online/article/dog-stars",
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(InputError):
        parse_article_url(url)
```

- [ ] **Step 2: Run the test and observe the failure**

Run: `.venv/bin/python -m pytest tests/bfi/test_urls.py -q`

Expected: FAIL because `cinema_friend.bfi.urls` does not exist.

- [ ] **Step 3: Implement strict parsing and literal query assembly**

```python
BASE = "https://whatson.bfi.org.uk/imax/Online/"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _literal_query(pairs: Sequence[tuple[str, str]]) -> str:
    return "&".join(f"{name}={quote(value, safe='')}" for name, value in pairs)


def film_page_url(slug: str) -> str:
    _validate_slug(slug)
    return BASE + "default.asp?" + _literal_query([
        ("BOparam::WScontent::loadArticle::permalink", slug),
    ])


def seat_map_url(performance_id: str) -> str:
    _validate_guid(performance_id)
    return BASE + "mapSelect.asp?" + _literal_query([
        ("BOparam::WSmap::loadMap::performance_ids", performance_id),
    ])
```

Parse either `/imax/Online/article/<slug>` or `/imax/Online/default.asp` with exactly one permalink value. Ignore other inbound query parameters, but never replay them. Reject credentials, fragments, non-default ports, duplicate permalinks, alternate hosts, and any redirect outside the accepted host/path shapes.

- [ ] **Step 4: Run focused tests and static checks**

Run:

```bash
.venv/bin/python -m pytest tests/bfi/test_urls.py -q
.venv/bin/python -m ruff check src/cinema_friend/bfi tests/bfi
.venv/bin/python -m mypy src/cinema_friend/bfi
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/cinema_friend/bfi tests/bfi/test_urls.py
git commit -m "feat: validate BFI article URLs"
```

---

### Task 3: Parse `articleContext` and performance pages

**Files:**
- Create: `src/cinema_friend/bfi/article_context.py`
- Create: `tests/factories/__init__.py`
- Create: `tests/factories/bfi_html.py`
- Create: `tests/bfi/test_article_context.py`

**Interfaces:**
- Consumes: `Performance`, `BfiContractError`, `film_page_url`, `seat_map_url`
- Produces: `ArticlePage(article_id, s_token, current_page, total_pages, rows)`
- Produces: `extract_article_context(html: str) -> Mapping[str, object]`
- Produces: `parse_article_page(html: str) -> ArticlePage`
- Produces: `performance_from_row(row: Mapping[str, object]) -> Performance`

- [ ] **Step 1: Create a minimal fixture builder and failing parser tests**

```python
def make_article_html(*, rows, current_page=1, total_pages=1, token="1,a/b+="):
    context = {
        "searchNames": REQUIRED_FIELDS,
        "searchResults": rows,
        "pagination": {
            "current_page": str(current_page),
            "page_size": "5",
            "total_pages": str(total_pages),
        },
        "articleId": "2152D1E8-CFF7-419F-BE57-F51C1E490F24",
        "sToken": token,
    }
    literal = json.dumps(context).replace('"searchNames":', "searchNames :")
    literal = literal[:-1] + ",}"
    literal = literal.replace("O'Brien", r"O\'Brien")
    return f"<script>var articleContext = {literal};\\n</script>"


def test_parses_page_and_maps_performance():
    html = make_article_html(rows=[performance_row()])
    page = parse_article_page(html)
    performance = performance_from_row(page.rows[0])
    assert page.total_pages == 1
    assert performance.performance_id == "2475959F-2B73-4EA6-AD26-AFA8AEB785FD"
    assert performance.start.tzinfo == ZoneInfo("Europe/London")
    assert performance.availability_num == 387
    assert performance.reserved_seating is True


def test_missing_consumed_field_is_contract_error():
    row = performance_mapping()
    row.pop("availability_num")
    with pytest.raises(BfiContractError, match="availability_num"):
        performance_from_row(row)
```

Ensure the builder emits the three JavaScript differences under test: bare keys, a trailing comma, and an escaped apostrophe.

- [ ] **Step 2: Run the tests and observe the failure**

Run: `.venv/bin/python -m pytest tests/bfi/test_article_context.py -q`

Expected: FAIL because parser functions are missing.

- [ ] **Step 3: Implement extraction and JavaScript-literal normalisation**

```python
CONTEXT_RE = re.compile(r"var\\s+articleContext\\s*=\\s*(\\{.*?\\});", re.DOTALL)


def extract_article_context(html: str) -> Mapping[str, object]:
    match = CONTEXT_RE.search(html)
    if match is None:
        raise BfiContractError("articleContext not found")
    literal = match.group(1)
    literal = re.sub(r"([{,]\\s*)([A-Za-z_]\\w*)\\s*:", r'\\1"\\2":', literal)
    literal = re.sub(r",(\\s*[}\\]])", r"\\1", literal)
    literal = literal.replace(r"\\'", "'")
    try:
        value = json.loads(literal)
    except json.JSONDecodeError as exc:
        raise BfiContractError("articleContext is not parseable") from exc
    if not isinstance(value, dict):
        raise BfiContractError("articleContext must be an object")
    return value
```

Validate list shapes and equal row width before zipping. Never use `eval`.

- [ ] **Step 4: Implement page and performance mapping**

Parse `start_date` using `%A %d %B %Y %H:%M` and attach `ZoneInfo("Europe/London")`. Strip a trailing `*` from sales status for base-code decisions while retaining the raw code. Normalise `availability_num` to a non-negative integer and `options` to a tuple of strings. Construct the canonical seat-map URL from the validated GUID.

- [ ] **Step 5: Add DST, duplicate-field, malformed-row, and non-performance tests**

Add concrete tests for:
- `Sunday 25 October 2026 01:30` retaining the London zone.
- Duplicate `searchNames` raising `BfiContractError`.
- A row shorter than `searchNames` raising `BfiContractError`.
- `object_type="A"` being parseable as a row but rejected by `performance_from_row`.
- `sales_status="S*"` retaining raw `S*` and base `S`.

- [ ] **Step 6: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/bfi/test_article_context.py -q
.venv/bin/python -m ruff check src/cinema_friend/bfi/article_context.py tests/factories tests/bfi/test_article_context.py
.venv/bin/python -m mypy src/cinema_friend/bfi/article_context.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/bfi/article_context.py tests/factories tests/bfi/test_article_context.py
git commit -m "feat: parse BFI performance pages"
```

---

### Task 4: Parse seat maps, price zones, and restricted seats

**Files:**
- Create: `src/cinema_friend/bfi/seat_map.py`
- Create: `tests/bfi/test_seat_map.py`
- Modify: `tests/factories/bfi_html.py`

**Interfaces:**
- Consumes: `Seat`, `SeatMap`, `PriceZone`, `SeatStatus`, `BfiContractError`
- Produces: `parse_seat_map(html: str, expected_performance_id: str) -> SeatMap`
- Produces: `is_restricted_access(zone_label: str | None, note: str) -> bool`

- [ ] **Step 1: Write a synthetic SVG fixture with duplicate, sold, unavailable, and access seats**

```python
def seat_map_html():
    return """
    <html><script>
    getPerformanceEcommerceObject({"item_id":"2475959F-2B73-4EA6-AD26-AFA8AEB785FD"})
    let priceZoneId = "3F5950DF-50B9-45EB-A78A-E0E518827835";
    priceZoneInfo[priceZoneId].label = "1 Standard";
    </script>
    <div class="zone-label">1 Standard</div>
    <div class="price-zone-price-text">- £22.00</div>
    <svg><g id="3F5950DF-50B9-45EB-A78A-E0E518827835">
      <circle id="seat-1" data-status="A" data-seat-section="BFI IMAX"
        data-seat-row="L" data-seat-seat="17" cx="340" cy="180"/>
      <circle id="seat-1" data-status="A" data-seat-section="BFI IMAX"
        data-seat-row="L" data-seat-seat="17" cx="340" cy="180"/>
      <circle id="seat-2" data-status="S" data-seat-section="BFI IMAX"
        data-seat-row="L" data-seat-seat="18" cx="354" cy="180"/>
    </g></svg></html>
    """
```

- [ ] **Step 2: Write failing parser tests**

```python
def test_parses_and_deduplicates_seats():
    seat_map = parse_seat_map(seat_map_html(), PERFORMANCE_ID)
    assert seat_map.performance_id == PERFORMANCE_ID
    assert len(seat_map.seats) == 2
    assert seat_map.seats[0].zone.label == "1 Standard"
    assert seat_map.seats[0].zone.price == Decimal("22.00")


def test_access_note_or_zone_marks_seat_restricted():
    assert is_restricted_access("BFI IMAX wheelchair space", "") is True
    assert is_restricted_access(None, "companion seat to be sold with a wheelchair space") is True
```

- [ ] **Step 3: Run the tests and observe the failure**

Run: `.venv/bin/python -m pytest tests/bfi/test_seat_map.py -q`

Expected: FAIL because `cinema_friend.bfi.seat_map` does not exist.

- [ ] **Step 4: Implement `lxml` seat parsing and GUID-scoped zone lookup**

Use `lxml.html.fromstring()`, select circles with `data-status`, find the nearest ancestor `<g>` whose `id` matches the GUID pattern, and deduplicate by seat ID. Map `A`, `S`, `U`, and `O` to distinct `SeatStatus` values. Parse `cx`/`cy` as finite floats; missing or invalid coordinates are contract errors.

Resolve labels from `priceZoneInfo[priceZoneId].label` assignments and prices from the matching legend entry. Unknown zone GUIDs produce `PriceZone(guid, "(unlisted zone)", None)`.

- [ ] **Step 5: Add contract and access-exclusion cases**

Test:
- Expected and returned performance IDs differ.
- No seat circles exist.
- A duplicate seat ID disagrees on row/status.
- An `O` seat maps to contended and is not available.
- Wheelchair/assistant/companion labels and notes are restricted.
- Ordinary standard/premium/VIP seats are not restricted.

- [ ] **Step 6: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/bfi/test_seat_map.py -q
.venv/bin/python -m ruff check src/cinema_friend/bfi/seat_map.py tests/bfi/test_seat_map.py
.venv/bin/python -m mypy src/cinema_friend/bfi/seat_map.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/bfi/seat_map.py tests/factories/bfi_html.py tests/bfi/test_seat_map.py
git commit -m "feat: parse BFI seat maps"
```

---

### Task 5: Implement the Chrome-impersonating transport and host circuit

**Files:**
- Create: `src/cinema_friend/bfi/transport.py`
- Create: `tests/fakes.py`
- Create: `tests/bfi/test_transport.py`

**Interfaces:**
- Consumes: `Settings`, `Clock`, `HostCircuit`, `CircuitState`, typed BFI errors, `validate_redirect_target`
- Produces protocol: `HostCircuitStore.load(host)`, `HostCircuitStore.save(circuit)`
- Produces protocol: `AsyncHttpSession.get(url, **kwargs)` and `close()`
- Produces: `FetchedDocument(url, status_code, headers, text, byte_count)`
- Produces: `BfiTransport.get(url: str, kind: DocumentKind) -> FetchedDocument`
- Produces: `BfiTransport.close() -> None`

- [ ] **Step 1: Build deterministic fake clock/session/store objects**

```python
class FakeClock:
    def __init__(self, now):
        self.current = now
        self.monotonic_value = 0.0
        self.sleeps = []

    def now(self):
        return self.current

    def monotonic(self):
        return self.monotonic_value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.monotonic_value += seconds
        self.current += timedelta(seconds=seconds)
```

`FakeSession` must queue responses/exceptions and record URLs and maximum simultaneous calls. `MemoryCircuitStore` must persist one `HostCircuit` in memory.

- [ ] **Step 2: Write failing transport tests**

Cover these exact cases:

```python
async def test_cf_mitigated_403_opens_circuit_without_immediate_retry():
    session = FakeSession([response(403, headers={"cf-mitigated": "challenge"})])
    transport = make_transport(session=session)
    with pytest.raises(BfiChallengeError):
        await transport.get(FILM_URL, DocumentKind.ARTICLE)
    assert len(session.calls) == 1
    assert (await transport.circuit_store.load(BFI_HOST)).state is CircuitState.OPEN


async def test_transient_403_retries_two_four_six_seconds():
    session = FakeSession([response(403), response(403), response(403), response(200, ARTICLE)])
    document = await make_transport(session=session).get(FILM_URL, DocumentKind.ARTICLE)
    assert document.status_code == 200
    assert fake_clock.sleeps == [2.0, 4.0, 6.0]


async def test_open_circuit_blocks_until_probe_time():
    store = MemoryCircuitStore(open_circuit(next_probe_at=fake_clock.now() + timedelta(minutes=15)))
    with pytest.raises(CircuitOpenError):
        await make_transport(circuit_store=store).get(FILM_URL, DocumentKind.ARTICLE)
```

Also test 5xx/network retries, 429, interstitial 200, redirect validation, two-call concurrency, and one-second start spacing.

- [ ] **Step 3: Run the tests and observe the failure**

Run: `.venv/bin/python -m pytest tests/bfi/test_transport.py -q`

Expected: FAIL because `cinema_friend.bfi.transport` does not exist.

- [ ] **Step 4: Implement a single shared `curl_cffi` session and response classification**

```python
class BfiTransport:
    def __init__(self, settings, circuit_store, clock, session=None):
        self._session = session or AsyncSession(
            impersonate=settings.bfi_impersonate_profile
        )
        self._semaphore = asyncio.Semaphore(settings.bfi_max_concurrency)
        self._spacing_lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._session.close()
```

Before every call, enforce the persisted circuit. Under `_spacing_lock`, sleep until one second has elapsed since the previous request start. Follow redirects manually with `allow_redirects=False`, at most five hops, and validate every target before requesting it.

Classify `429`, `cf-mitigated`, and interstitial HTML as a challenge. A challenged response immediately advances the circuit delays `[15m, 30m, 1h, 2h, 4h, 6h]`. Only one request under `_probe_lock` may probe an elapsed open circuit.

- [ ] **Step 5: Implement bounded retry matrices**

- Network exception or 5xx: initial call plus retries after 1s and 3s, each with injected positive jitter up to 20%.
- Unmarked 403: initial call plus retries after 2s, 4s, and 6s, then escalate to challenge.
- Challenge/429/interstitial: no immediate retry.
- Any other 4xx: raise `BfiNetworkError` without retry.

Inject the jitter source in tests so expected delays are exact.

- [ ] **Step 6: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/bfi/test_transport.py -q
.venv/bin/python -m ruff check src/cinema_friend/bfi/transport.py tests/fakes.py tests/bfi/test_transport.py
.venv/bin/python -m mypy src/cinema_friend/bfi/transport.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/bfi/transport.py tests/fakes.py tests/bfi/test_transport.py
git commit -m "feat: add resilient BFI transport"
```

---

### Task 6: Assemble pagination, coalescing, and seat-map reads

**Files:**
- Create: `src/cinema_friend/bfi/gateway.py`
- Create: `tests/bfi/test_gateway.py`

**Interfaces:**
- Consumes: `BfiTransport`, parsers, URL builders, `Clock`, `Performance`, `SeatMap`
- Produces: `BfiGateway.list_performances(slug: str) -> tuple[Performance, ...]`
- Produces: `BfiGateway.load_seat_map(performance: Performance) -> SeatMap`
- Produces: `AvailabilityDrift(reported: int, parsed: int, difference: int)`

- [ ] **Step 1: Write a mocked two-page gateway test**

```python
async def test_lists_every_paginated_performance_and_deduplicates():
    transport = FakeTransport({
        film_page_url("dog-stars"): article_page_1,
        pagination_url(TOKEN, 2, ARTICLE_ID): article_page_2,
    })
    performances = await BfiGateway(transport, fake_clock).list_performances("dog-stars")
    assert [item.performance_id for item in performances] == [PERF_1, PERF_2, PERF_3]
    assert transport.calls == [
        film_page_url("dog-stars"),
        pagination_url(TOKEN, 2, ARTICLE_ID),
    ]
```

Add tests for changing `total_pages`, empty page 2, duplicate IDs with conflicting data, and invalid row fields.

- [ ] **Step 2: Write coalescing and seat cross-check tests**

```python
async def test_concurrent_equal_reads_share_one_request():
    results = await asyncio.gather(
        gateway.list_performances("dog-stars"),
        gateway.list_performances("dog-stars"),
    )
    assert results[0] == results[1]
    assert fake_transport.call_count(FILM_URL) == 1


async def test_positive_page_count_with_zero_parsed_available_is_contract_error():
    performance = make_performance(availability_num=3)
    with pytest.raises(BfiContractError, match="zero available"):
        await gateway.load_seat_map(performance)
```

- [ ] **Step 3: Run the tests and observe the failure**

Run: `.venv/bin/python -m pytest tests/bfi/test_gateway.py -q`

Expected: FAIL because `cinema_friend.bfi.gateway` does not exist.

- [ ] **Step 4: Implement single-flight plus 60-second cache**

Maintain:

```python
self._inflight: dict[str, asyncio.Task[FetchedDocument]] = {}
self._cache: dict[str, tuple[float, FetchedDocument]] = {}
```

Protect dictionaries with one lock. Return a completed document only while `clock.monotonic() < expires_at`; always remove completed tasks from `_inflight`. Do not cache errors or challenge pages.

- [ ] **Step 5: Implement complete pagination and cross-source validation**

Fetch page 1, parse it, then pages `2..total_pages`. Validate stable `article_id` and `total_pages`; deduplicate `object_type=="P"` rows by ID and reject conflicting duplicates.

For a seat map, verify performance identity, parse all seats, and compute `AvailabilityDrift`. Log differences over five, but only fail when no seats parse or parsed available is zero while `availability_num` is positive.

- [ ] **Step 6: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/bfi/test_gateway.py -q
.venv/bin/python -m ruff check src/cinema_friend/bfi/gateway.py tests/bfi/test_gateway.py
.venv/bin/python -m mypy src/cinema_friend/bfi/gateway.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/bfi/gateway.py tests/bfi/test_gateway.py
git commit -m "feat: assemble BFI availability gateway"
```

---

### Task 7: Filter performances and rank every adjacent seat block

**Files:**
- Create: `src/cinema_friend/watches/__init__.py`
- Create: `src/cinema_friend/watches/criteria.py`
- Create: `src/cinema_friend/watches/blocks.py`
- Create: `src/cinema_friend/watches/ranking.py`
- Create: `tests/watches/test_criteria.py`
- Create: `tests/watches/test_blocks.py`
- Create: `tests/watches/test_ranking.py`

**Interfaces:**
- Produces: `performance_matches(criteria, performance) -> bool`
- Produces: `generate_blocks(seat_map, criteria) -> tuple[SeatBlock, ...]`
- Produces: `score_block(block, all_physical_seats) -> float`
- Produces: `rank_options(criteria, performance_maps: Sequence[tuple[Performance, SeatMap]]) -> tuple[RankedOption, ...]`

- [ ] **Step 1: Write inclusive and midnight-wrapping criteria tests**

```python
def test_midnight_window_uses_performance_local_date():
    criteria = criteria_for(
        date_from=date(2026, 8, 26),
        date_to=date(2026, 8, 27),
        time_from=time(22, 0),
        time_to=time(1, 0),
    )
    assert performance_matches(criteria, performance_at("2026-08-26T23:30:00+01:00"))
    assert performance_matches(criteria, performance_at("2026-08-27T00:30:00+01:00"))
    assert not performance_matches(criteria, performance_at("2026-08-27T14:00:00+01:00"))
```

Also assert on-sale base codes `S/O/R`, reserved seating, and `availability_num >= quantity`.

- [ ] **Step 2: Write aisle-aware sliding-window tests**

Build a row with x coordinates `100, 114, 128, 180, 194`; numbers remain consecutive. For quantity 2, assert blocks `1-2`, `2-3`, and `4-5`, but not `3-4`. Assert restricted, sold, unavailable, and excluded seats break a run. Assert a five-seat run yields four two-seat windows.

- [ ] **Step 3: Write exact score and sort tests**

```python
def test_dead_centre_row_l_scores_100():
    block = block_for("L", [17, 18], xs=[343, 357])
    all_seats = symmetric_row("L", start_x=70, end_x=630)
    assert score_block(block, all_seats) == 100.0


def test_time_breaks_only_within_same_five_point_band():
    ranked = rank_options(criteria_with_preferred_time(), maps_with_scores(99.0, 96.0, 94.0))
    assert [option.raw_view_score for option in ranked] == [96.0, 99.0, 94.0]
```

The first two scores share band 19, so time can order them; score 94 is band 18 and remains behind.

- [ ] **Step 4: Run all watch-algorithm tests and observe failure**

Run: `.venv/bin/python -m pytest tests/watches -q`

Expected: FAIL because the watch algorithm modules are missing.

- [ ] **Step 5: Implement predicates, row geometry, and block generation**

Compute each row's normal gap as the median absolute `cx` difference among numerically consecutive physical seats. Require at least three usable gaps and non-zero row width. A pair is adjacent only when same section/row, consecutive number, and gap `<= 1.75 * median_gap`.

Exclude a block when any member is not `A`, restricted, explicitly excluded, or separated by an aisle. Emit every exact-size sliding window.

- [ ] **Step 6: Implement the approved score and rank vector**

```python
row_score = max(0.0, 40.0 - 5.0 * row_distance)
normalized_offset = abs(block_center_x - row_center_x) / half_width
center_score = 60.0 * max(0.0, 1.0 - normalized_offset)
view_score = round(row_score + center_score, 2)
```

Order rows by median `cy`; distance is the number of observed-row steps to nearest `L` or `M`. Build `RankVector` exactly as defined in Task 1 and sort by `sort_key()`. Option keys are performance ID plus ordered stable seat IDs.

- [ ] **Step 7: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/watches -q
.venv/bin/python -m ruff check src/cinema_friend/watches tests/watches
.venv/bin/python -m mypy src/cinema_friend/watches
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/cinema_friend/watches tests/watches
git commit -m "feat: rank adjacent BFI seat options"
```

---

### Task 8: Create SQLite schema, migrations, watch, and draft persistence

**Files:**
- Create: `src/cinema_friend/storage/__init__.py`
- Create: `src/cinema_friend/storage/migrations/001_initial.sql`
- Create: `src/cinema_friend/storage/database.py`
- Create: `src/cinema_friend/storage/watch_repository.py`
- Create: `src/cinema_friend/storage/draft_repository.py`
- Create: `tests/storage/test_database.py`
- Create: `tests/storage/test_watch_repository.py`
- Create: `tests/storage/test_draft_repository.py`

**Interfaces:**
- Produces: `Database.connect()`, `Database.transaction()`, `Database.migrate()`
- Produces: `WatchRepository.create/get/list_for_owner/list_due/list_active_owner_ids/update/delete`
- Produces: `DraftRepository.get/upsert/delete/delete_expired`

- [ ] **Step 1: Write the complete initial migration**

The migration must create all tables required by later tasks so production never depends on test order:

```sql
CREATE TABLE conversation_drafts (
  user_id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE watches (
  id TEXT PRIMARY KEY,
  owner_user_id INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  slug TEXT NOT NULL,
  title TEXT,
  criteria_json TEXT NOT NULL,
  mode TEXT NOT NULL,
  interval_seconds INTEGER,
  status TEXT NOT NULL,
  next_run_at TEXT,
  last_check_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX watches_due_idx ON watches(status, next_run_at);
CREATE INDEX watches_owner_idx ON watches(owner_user_id, created_at);

CREATE TABLE check_runs (
  id TEXT PRIMARY KEY,
  watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL,
  outcome TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  performance_count INTEGER NOT NULL DEFAULT 0,
  option_count INTEGER NOT NULL DEFAULT 0,
  error_kind TEXT,
  error_message TEXT
);

CREATE TABLE result_snapshots (
  id TEXT PRIMARY KEY,
  watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  check_run_id TEXT UNIQUE REFERENCES check_runs(id) ON DELETE SET NULL,
  checked_at TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  is_latest INTEGER NOT NULL CHECK (is_latest IN (0, 1))
);
CREATE UNIQUE INDEX one_latest_snapshot_idx
  ON result_snapshots(watch_id) WHERE is_latest = 1;

CREATE TABLE result_options (
  snapshot_id TEXT NOT NULL REFERENCES result_snapshots(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  option_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, rank),
  UNIQUE(snapshot_id, option_key)
);

CREATE TABLE notified_options (
  watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  option_key TEXT NOT NULL,
  first_notified_at TEXT NOT NULL,
  PRIMARY KEY(watch_id, option_key)
);

CREATE TABLE notification_state (
  watch_id TEXT PRIMARY KEY REFERENCES watches(id) ON DELETE CASCADE,
  last_best_rank_json TEXT,
  degradation_notified INTEGER NOT NULL DEFAULT 0 CHECK (degradation_notified IN (0, 1)),
  recovery_pending INTEGER NOT NULL DEFAULT 0 CHECK (recovery_pending IN (0, 1)),
  updated_at TEXT NOT NULL
);

CREATE TABLE notification_deliveries (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  recipient_user_id INTEGER NOT NULL,
  watch_id TEXT REFERENCES watches(id) ON DELETE CASCADE,
  snapshot_id TEXT REFERENCES result_snapshots(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE INDEX notification_due_idx ON notification_deliveries(status, next_attempt_at);

CREATE TABLE host_circuits (
  host TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  step INTEGER NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  next_probe_at TEXT,
  updated_at TEXT NOT NULL
);
```

- [ ] **Step 2: Write failing migration and transaction tests**

Assert WAL mode, foreign keys, migration idempotence, rollback on exception, and cascade deletion. Use a temporary database for every test.

- [ ] **Step 3: Implement database contexts and migration discovery**

`Database.connect()` opens a fresh `aiosqlite` connection, sets row factory, `PRAGMA foreign_keys=ON`, and `PRAGMA journal_mode=WAL`. `Database.transaction()` executes `BEGIN IMMEDIATE`, commits on success, and rolls back on exception. Before inspecting migration versions, `migrate()` bootstraps this metadata table outside the numbered files:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
```

It then reads numbered SQL resources in lexical order and records each version atomically.

- [ ] **Step 4: Write failing watch/draft repository tests**

Test watch round-trip, owner filtering, due filtering, paused exclusion, update timestamps, delete cascade, draft upsert, and 24-hour draft expiry.

- [ ] **Step 5: Implement JSON codecs and repositories**

Store enum values as strings and datetimes as UTC ISO 8601 ending in `+00:00`. Sort sets before JSON encoding so fingerprints and round trips are deterministic. Repository methods accept an existing connection so callers can compose atomic transactions. `list_active_owner_ids()` returns distinct owners of active/backoff watches for one host, enabling one host alert per user rather than per watch.

- [ ] **Step 6: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/storage/test_database.py tests/storage/test_watch_repository.py tests/storage/test_draft_repository.py -q
.venv/bin/python -m ruff check src/cinema_friend/storage tests/storage
.venv/bin/python -m mypy src/cinema_friend/storage
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/storage tests/storage
git commit -m "feat: persist watches and wizard drafts"
```

---

### Task 9: Persist results, notification deliveries, circuits, and retention

**Files:**
- Create: `src/cinema_friend/storage/result_repository.py`
- Create: `src/cinema_friend/storage/notification_repository.py`
- Create: `src/cinema_friend/storage/circuit_repository.py`
- Create: `src/cinema_friend/storage/retention.py`
- Create: `tests/storage/test_result_repository.py`
- Create: `tests/storage/test_notification_repository.py`
- Create: `tests/storage/test_circuit_repository.py`
- Create: `tests/storage/test_retention.py`

**Interfaces:**
- Produces: `ResultRepository.start_check`, `complete_with_snapshot`, `fail_check`, `latest_snapshot`, `snapshot_page`
- Produces: `NotificationRepository.state`, `known_keys`, `create_delivery`, `due_deliveries`, `mark_delivered`, `reschedule`
- Produces concrete `SqliteCircuitStore` implementing Task 5's protocol
- Produces: `RetentionService.run(now) -> RetentionCounts`

- [ ] **Step 1: Write failing atomic snapshot tests**

```python
async def test_completing_check_replaces_latest_snapshot_atomically(db):
    first = await save_snapshot(db, watch, options=[option("A")])
    second = await save_snapshot(db, watch, options=[option("B")])
    assert (await latest_snapshot(db, watch.id)).id == second.id
    assert await snapshot_options(db, first.id) == [option("A")]
```

Also force an insert failure halfway through `complete_with_snapshot` and assert the previous latest snapshot remains unchanged.

- [ ] **Step 2: Implement deterministic snapshot persistence and paging**

Compute the fingerprint as SHA-256 over ordered option keys. Set the old latest row to `0`, insert snapshot/options, and set the new row to `1` in one transaction. `snapshot_page(snapshot_id, page, page_size=10)` returns total count/pages and rejects page values outside `1..total_pages`.

- [ ] **Step 3: Write failing notification and circuit tests**

Test idempotency-key uniqueness, pending-delivery retry, marking every current option known only after delivery, last-best rank persistence, degradation/recovery flags, and host-circuit round-trip.

- [ ] **Step 4: Implement notification and circuit repositories**

`mark_delivered()` must update delivery status, insert every supplied option key with `INSERT OR IGNORE`, update last-best rank, and clear the relevant pending flag in one transaction. Delivery rows always carry `recipient_user_id`; host degradation/recovery rows may have null watch/snapshot IDs and use idempotency keys containing host, circuit generation, kind, and recipient.

`SqliteCircuitStore.load()` returns a closed step-zero, generation-zero circuit when no row exists. `save()` increments generation only when a closed circuit opens, then upserts state, step, generation, next probe, and timestamp.

- [ ] **Step 5: Write and implement retention tests**

At a fixed clock instant, assert:
- Old snapshots are removed except each watch's latest.
- A snapshot referenced by a pending delivery is retained until that delivery resolves.
- Check runs older than 30 days are removed.
- Drafts older than 24 hours are removed.
- Notification option keys remain.

- [ ] **Step 6: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/storage -q
.venv/bin/python -m ruff check src/cinema_friend/storage tests/storage
.venv/bin/python -m mypy src/cinema_friend/storage
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/storage tests/storage
git commit -m "feat: persist ticket results and delivery state"
```

---

### Task 10: Implement watch lifecycle and pure notification policy

**Files:**
- Create: `src/cinema_friend/services/__init__.py`
- Create: `src/cinema_friend/services/watch_service.py`
- Create: `src/cinema_friend/services/notification_policy.py`
- Create: `tests/services/test_watch_service.py`
- Create: `tests/services/test_notification_policy.py`

**Interfaces:**
- Consumes: watch/draft/result repositories, `WatchCriteria`, `RankedOption`
- Produces: `WatchService.create/list_for_owner/get_owned/pause/resume/delete`
- Produces: `decide_result_notification(trigger, options, known_keys, last_best) -> NotificationDecision`
- Produces: `decide_degradation_notification(circuit, owner_user_ids)`, `decide_recovery_notification(circuit, owner_user_ids)`

- [ ] **Step 1: Write ownership and lifecycle tests**

```python
async def test_pause_rejects_different_owner(service):
    watch = await service.create(11, criteria())
    with pytest.raises(InputError, match="watch not found"):
        await service.pause(owner_user_id=22, watch_id=watch.id)


async def test_resume_schedules_immediate_check(service, fake_clock):
    watch = await service.create(11, recurring_criteria())
    await service.pause(11, watch.id)
    resumed = await service.resume(11, watch.id)
    assert resumed.status is WatchStatus.ACTIVE
    assert resumed.next_run_at == fake_clock.now()
```

- [ ] **Step 2: Implement lifecycle operations with generic not-found responses**

Never reveal another user's watch. Delete uses one transaction and relies on foreign-key cascades. Creating a watch stores a canonical URL/slug and schedules immediate check; initial title may be null until the first successful BFI parse.

- [ ] **Step 3: Write notification-decision tests**

Assert:
- Creation/manual response always produces an immediate response.
- Recurring unchanged options produce no delivery.
- A never-surfaced lower-ranked option still produces one digest.
- A known option with a better complete rank vector produces one digest.
- Mere disappearance and worse-only changes are silent.
- Degraded and recovered alerts emit once per user and host-circuit generation, even when that user owns several watches.

- [ ] **Step 4: Implement pure decisions without database or Telegram imports**

Return a dataclass carrying kind, recipient, new keys, all current keys, current best rank, and snapshot requirement. Compare `RankVector.sort_key()` values; smaller is better. Host decisions return one `NotificationPayload` per distinct owner and deterministic idempotency keys derived from host, circuit generation, kind, and owner.

- [ ] **Step 5: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/services/test_watch_service.py tests/services/test_notification_policy.py -q
.venv/bin/python -m ruff check src/cinema_friend/services tests/services
.venv/bin/python -m mypy src/cinema_friend/services
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/cinema_friend/services tests/services
git commit -m "feat: manage ticket watch lifecycle"
```

---

### Task 11: Orchestrate checks and persist outcomes atomically

**Files:**
- Create: `src/cinema_friend/services/check_service.py`
- Create: `tests/services/test_check_service.py`

**Interfaces:**
- Consumes: `BfiGateway`, criteria/block/ranking functions, repositories, notification policy, clock/jitter
- Produces: `CheckService.check(watch_id: UUID, trigger: CheckTrigger) -> CheckResult`

- [ ] **Step 1: Write a successful recurring-check integration test with fakes**

```python
async def test_recurring_check_saves_ranked_snapshot_and_pending_digest(harness):
    harness.gateway.performances = [matching_performance()]
    harness.gateway.maps[PERF_ID] = seat_map_with_centre_pair()
    result = await harness.service.check(WATCH_ID, CheckTrigger.SCHEDULED)
    assert result.outcome is CheckOutcome.SUCCESS
    snapshot = await harness.results.latest_snapshot(WATCH_ID)
    assert [item.seat_label for item in snapshot.options] == ["L17-L18"]
    assert (await harness.notifications.due_deliveries(harness.clock.now()))[0].kind == "results"
```

- [ ] **Step 2: Add success-path behavior cases**

Test:
- Full pagination is consumed through gateway.
- Date/time, on-sale, reserved, and exact-count filters run before map fetches.
- Every surviving map contributes all blocks.
- No-match writes an empty valid snapshot.
- One-off success becomes `completed`.
- Recurring creation check sets next run to completion + interval + 0–10% positive jitter.
- Manual `/check` preserves an existing recurring `next_run_at`.

- [ ] **Step 3: Add typed failure cases**

Test:
- Contract error pauses the watch and retains latest snapshot.
- Challenge sets watch `backoff` until persisted host probe time, queries distinct active owners, and creates at most one degradation delivery per owner and circuit generation.
- Exhausted network/5xx retries set recurring watch `backoff` until its normal interval; one-off becomes `failed`.
- Database failure rolls back check/snapshot/watch changes.

- [ ] **Step 4: Run the tests and observe the failure**

Run: `.venv/bin/python -m pytest tests/services/test_check_service.py -q`

Expected: FAIL because `CheckService` does not exist.

- [ ] **Step 5: Implement the orchestration in explicit phases**

```python
async def check(self, watch_id: UUID, trigger: CheckTrigger) -> CheckResult:
    watch = await self._watches.get(watch_id)
    started_at = self._clock.now()
    performances = await self._gateway.list_performances(watch.criteria.slug)
    candidates = tuple(p for p in performances if performance_matches(watch.criteria, p))
    maps = await asyncio.gather(*(self._gateway.load_seat_map(p) for p in candidates))
    options = rank_options(watch.criteria, tuple(zip(candidates, maps, strict=True)))
    return await self._persist_success(watch, trigger, started_at, options)
```

Wrap each typed error separately; do not use a broad catch that turns unexpected defects into expected failures. Persist a pending delivery in the same transaction as the snapshot and watch schedule.

- [ ] **Step 6: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/services/test_check_service.py -q
.venv/bin/python -m ruff check src/cinema_friend/services/check_service.py tests/services/test_check_service.py
.venv/bin/python -m mypy src/cinema_friend/services/check_service.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/services/check_service.py tests/services/test_check_service.py
git commit -m "feat: orchestrate cinema ticket checks"
```

---

### Task 12: Build Telegram authorization, callback encoding, and rendering

**Files:**
- Create: `src/cinema_friend/telegram/__init__.py`
- Create: `src/cinema_friend/telegram/auth.py`
- Create: `src/cinema_friend/telegram/rendering.py`
- Create: `src/cinema_friend/telegram/callbacks.py`
- Create: `tests/telegram/test_auth.py`
- Create: `tests/telegram/test_rendering.py`
- Create: `tests/telegram/test_callbacks.py`

**Interfaces:**
- Produces: `authorized_user_id(update, allowed_ids) -> int`
- Produces: `require_owned_watch(update, watch_service, watch_id) -> Watch`
- Produces: `encode_callback(action, resource_id, page=None) -> str`
- Produces: `decode_callback(data) -> CallbackAction`
- Produces: `render_result_page(snapshot_page) -> RenderedMessage`
- Produces: `render_watch_list(watches) -> RenderedMessage`

- [ ] **Step 1: Write authorization and callback tests**

Assert generic denial for unauthorized/missing users, ownership enforcement, round-trip callback data, malformed callback rejection, and encoded length at most Telegram's 64-byte limit.

- [ ] **Step 2: Implement compact callback data**

Use versioned values:

```text
v1:r:<snapshot-uuid>:<page>
v1:w:<action>:<watch-uuid>
```

UUID text plus fixed fields stays below 64 bytes. Reject unknown versions/actions and non-positive pages.

- [ ] **Step 3: Write result-page rendering tests**

Assert ten options maximum, title/time/seats/view rationale/checked time, direct BFI URL buttons, total option/performance counts, and previous/next callbacks. Escape Telegram HTML and keep message text below 4096 characters.

- [ ] **Step 4: Implement rendering as pure dataclass output**

`RenderedMessage` carries `text`, `parse_mode`, and `InlineKeyboardMarkup`. Rendering must not call Telegram or repositories. Task 14 converts the persisted domain `NotificationPayload` into this Telegram-specific type at delivery time.

- [ ] **Step 5: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_auth.py tests/telegram/test_callbacks.py tests/telegram/test_rendering.py -q
.venv/bin/python -m ruff check src/cinema_friend/telegram tests/telegram
.venv/bin/python -m mypy src/cinema_friend/telegram
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/cinema_friend/telegram tests/telegram
git commit -m "feat: render authorized Telegram results"
```

---

### Task 13: Implement the persistent guided watch wizard

**Files:**
- Create: `src/cinema_friend/telegram/wizard.py`
- Create: `tests/telegram/test_wizard.py`

**Interfaces:**
- Consumes: `DraftRepository`, `WatchService`, `CheckService`, URL parser
- Produces: handlers `start_new`, `cancel`, `handle_wizard_text`, `handle_wizard_callback`
- Produces: `WizardState` values for URL, dates, times, quantity, preferred rows/seats, exclusions, preferred instant, mode, interval, review
- Produces parsers: `parse_date_range`, `parse_time_range`, `parse_seat_selectors`, `parse_interval`

- [ ] **Step 1: Write parser tests for every accepted input shape**

Test:
- `2026-08-26 to 2026-08-30`.
- `18:00 to 23:00` and wrapping `22:00 to 01:00`.
- Rows `L,M`.
- Seats/ranges `L16-L22,M17`.
- Skip values.
- Quantity buttons 1–8.
- Interval minutes rejecting values below 15.
- Preferred date/time required to satisfy both predicates.

- [ ] **Step 2: Implement strict, specific parsers**

Return normalized uppercase row/seat labels. Expand same-row ranges inclusively and cap expansion at the known BFI row width of 40 to reject abusive input. Return `InputError` messages that tell the user the accepted example.

- [ ] **Step 3: Write an end-to-end persisted draft test**

Drive `/new` through every state with fake updates, recreate the handler between two steps, and assert the draft resumes from SQLite. At confirmation, assert one watch is created, draft is deleted, and `CheckService.check(..., CREATION)` is called once.

- [ ] **Step 4: Implement state transitions and review screen**

Every input transaction reads the current draft, validates exactly one field, updates payload/state, and writes it back. Invalid input leaves state/payload unchanged. `/cancel` deletes the draft. Confirmation revalidates the complete `WatchCriteria` before creating the watch.

- [ ] **Step 5: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/telegram/test_wizard.py -q
.venv/bin/python -m ruff check src/cinema_friend/telegram/wizard.py tests/telegram/test_wizard.py
.venv/bin/python -m mypy src/cinema_friend/telegram/wizard.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/cinema_friend/telegram/wizard.py tests/telegram/test_wizard.py
git commit -m "feat: add Telegram watch wizard"
```

---

### Task 14: Add management commands, pagination, and delivery retries

**Files:**
- Create: `src/cinema_friend/telegram/commands.py`
- Create: `src/cinema_friend/telegram/bot.py`
- Modify: `src/cinema_friend/telegram/callbacks.py`
- Create: `tests/telegram/test_commands.py`
- Create: `tests/telegram/test_bot.py`

**Interfaces:**
- Consumes: watch/check services, result/notification repositories, rendering functions
- Produces handlers for `/watches`, `/check`, `/pause`, `/resume`, `/delete`, `/help`
- Produces: `handle_callback(update, context)`
- Produces: `DeliveryWorker.run_once() -> DeliveryRun`
- Produces: `build_telegram_application(settings, dependencies) -> Application`

- [ ] **Step 1: Write command and callback tests**

Cover owner-only list/pause/resume/delete/check, delete confirmation, manual check preserving recurring schedule, result pagination from immutable snapshot, expired snapshot message, and unauthorized callback rejection.

- [ ] **Step 2: Implement commands using inline watch action buttons**

`/watches` shows status, criteria summary, next check, and action buttons. `/delete` requires a second callback. `/check` returns the immediate snapshot and marks all current option keys surfaced only after Telegram send succeeds.

- [ ] **Step 3: Write delivery retry tests**

```python
async def test_delivery_failure_reschedules_without_marking_options_known(worker):
    worker.bot.send_message.side_effect = TimedOut()
    await worker.run_once()
    assert await notifications.known_keys(WATCH_ID) == frozenset()
    delivery = await notifications.get(DELIVERY_ID)
    assert delivery.status is DeliveryStatus.PENDING
    assert delivery.attempt_count == 1
```

Also test successful atomic marking and retry delays 1 minute, 5 minutes, 15 minutes, then 1 hour maximum.

- [ ] **Step 4: Implement delivery worker and Telegram wiring**

Deserialize the stored `NotificationPayload`, load its referenced snapshot when present, render a `RenderedMessage`, send it to `recipient_user_id`, then call repository `mark_delivered()` with all current keys and current best rank. Retention keeps snapshots referenced by pending deliveries. On Telegram timeout/network errors, reschedule; on permanent bad-chat/forbidden errors, mark failed and log without swallowing.

Register command, text, and callback handlers in deterministic order so active drafts consume wizard input before generic commands.

- [ ] **Step 5: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest tests/telegram -q
.venv/bin/python -m ruff check src/cinema_friend/telegram tests/telegram
.venv/bin/python -m mypy src/cinema_friend/telegram
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/cinema_friend/telegram tests/telegram
git commit -m "feat: manage watches through Telegram"
```

---

### Task 15: Run persistent scheduling and application lifecycle

**Files:**
- Create: `src/cinema_friend/services/scheduler.py`
- Create: `src/cinema_friend/app.py`
- Create: `src/cinema_friend/__main__.py`
- Create: `tests/services/test_scheduler.py`
- Create: `tests/test_app.py`

**Interfaces:**
- Produces: `Scheduler.run(stop_event)`, `run_due_once()`, `run_retention_once()`
- Produces: `CinemaFriendApp.start()`, `wait()`, `stop()`
- Produces CLI: `cinema-friend --env-file PATH`

- [ ] **Step 1: Write fake-clock scheduler tests**

Test a once-per-minute due scan, overdue recovery after restart, bounded check concurrency, paused/expired exclusion, delivery scans, and daily retention. Assert a recurring watch expires after its London `date_to` day.

- [ ] **Step 2: Implement independent scheduler loops**

Use one loop each for due watches (60s), pending deliveries (10s), and retention (24h). Replace arbitrary sleeps with:

```python
await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
```

catching `TimeoutError` to continue. A stop event terminates promptly.

- [ ] **Step 3: Write lifecycle tests**

Use fakes to assert startup order: migrate database, construct one `AsyncSession`, initialize/start Telegram, start workers. Assert shutdown stops new work, waits up to 30 seconds for active checks, stops/shuts down Telegram, closes transport, and closes resources even when one close step fails.

- [ ] **Step 4: Implement explicit PTB async lifecycle**

Do not call blocking `Application.run_polling()`. Use `initialize()`, `start()`, `updater.start_polling()`, wait for SIGINT/SIGTERM, then `updater.stop()`, `stop()`, and `shutdown()` in reverse order.

Load `.env` via `dotenv_values(env_file)`, verify the file exists, is owned by the current user, and has no group/other permission bits before calling `Settings.from_mapping`.

- [ ] **Step 5: Add structured redacted logging**

Use standard-library JSON logging with event, level, timestamp, watch/check IDs, statuses, and byte counts. Add a filter that removes keys matching token, secret, `sToken`, body, html, or svg. Test that a representative secret never appears in captured output.

- [ ] **Step 6: Run focused and full automated verification**

Run:

```bash
.venv/bin/python -m pytest tests/services/test_scheduler.py tests/test_app.py -q
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/cinema_friend/services/scheduler.py src/cinema_friend/app.py src/cinema_friend/__main__.py tests/services/test_scheduler.py tests/test_app.py
git commit -m "feat: run persistent ticket monitoring"
```

---

### Task 16: Add bounded smoke verification and macOS deployment

**Files:**
- Create: `src/cinema_friend/smoke.py`
- Create: `scripts/install_launch_agent.py`
- Create: `.env.example`
- Create: `README.md`
- Create: `tests/test_smoke.py`
- Create: `tests/test_launch_agent.py`

**Interfaces:**
- Produces CLI: `cinema-friend-smoke FILM_URL --profile chrome`
- Produces CLI: `python scripts/install_launch_agent.py install --env-file PATH`
- Produces CLI: `python scripts/install_launch_agent.py uninstall`

- [ ] **Step 1: Write a mocked smoke-test contract test**

Assert the smoke command:
- Parses and validates the supplied film URL.
- Fetches page 1 and every pagination page.
- Chooses the first on-sale reserved performance with positive availability.
- Fetches exactly one seat map.
- Requires HTTP 200 and complete fields.
- Requires exact `availability_num == A-seat count`.
- Prints profile, page/performance/seat counts, but no full token or body.

- [ ] **Step 2: Implement the bounded smoke command**

Exit codes:
- `0`: contract passes.
- `2`: input invalid or no eligible performance exists.
- `3`: challenge/circuit condition.
- `4`: contract mismatch.
- `5`: network failure.

Always close the session in `finally`. Never scan the aggregate programme.

- [ ] **Step 3: Write LaunchAgent rendering tests**

Generate a plist into a temporary directory and assert:
- `Label` is `com.ncksol.cinema-friend`.
- `ProgramArguments` uses the current venv's absolute `cinema-friend` executable and absolute env-file path.
- `RunAtLoad` and `KeepAlive` are true.
- Working directory and stdout/stderr paths are absolute.
- The debug port, browser, or proxy arguments never appear.

- [ ] **Step 4: Implement install/uninstall without hard-coded checkout paths**

Resolve `sys.executable`, locate sibling `cinema-friend`, and render `~/Library/LaunchAgents/com.ncksol.cinema-friend.plist`. Validate env-file mode `0600` before writing. Use `launchctl bootstrap gui/<uid>` and `bootout gui/<uid>` through `subprocess.run(..., check=True)` with argument arrays, never shell strings.

- [ ] **Step 5: Write deployment and recovery documentation**

Document:
- `python3.12 -m venv`, editable install, BotFather token, Telegram user ID allow-list, and `.env` mode.
- One-time smoke command.
- LaunchAgent install/status/log commands.
- `/new` and management command examples.
- Challenge degradation/recovery behavior.
- Database/log locations, backup by stopping service and copying the SQLite file, and uninstall.
- The undocumented-contract and BFI-acceptability risk.

- [ ] **Step 6: Run all automated checks**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy src scripts
```

Expected: all commands exit 0.

- [ ] **Step 7: Run one live read-only contract smoke test**

Run:

```bash
.venv/bin/cinema-friend-smoke \
  'https://whatson.bfi.org.uk/imax/Online/default.asp?BOparam::WScontent::loadArticle::permalink=dog-stars' \
  --profile chrome
```

Expected: exit 0; output reports a positive parsed-performance count, one completely parsed seat map, and exact agreement between reported and parsed available seats. Performance and availability counts may change as sales move; equality and complete parsing are the required invariants.

- [ ] **Step 8: Commit**

```bash
git add src/cinema_friend/smoke.py scripts/install_launch_agent.py .env.example README.md tests/test_smoke.py tests/test_launch_agent.py
git commit -m "feat: add BFI smoke test and launchd deployment"
```

---

## Final Verification

- [ ] Run the complete suite: `.venv/bin/python -m pytest -q`
- [ ] Run lint: `.venv/bin/python -m ruff check src tests scripts`
- [ ] Run type checking: `.venv/bin/python -m mypy src scripts`
- [ ] Run the bounded live smoke command once and confirm no challenge, parser drift, or availability mismatch.
- [ ] Inspect `git status --short` and confirm only intended files are present.
- [ ] Inspect `git log --format='%h %s%n%b'` and confirm no commit contains a `Co-authored-by` trailer.
- [ ] Start the LaunchAgent, create one one-off Telegram watch, page through more than ten options, and confirm every button opens the matching BFI seat map without selecting or reserving a seat.
