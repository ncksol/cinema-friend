"""Parse BFI seat-map HTML pages."""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any, cast

import lxml.html

from cinema_friend.domain.bfi import PriceZone, Seat, SeatMap, SeatStatus
from cinema_friend.domain.errors import BfiContractError

_GUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_ZONE_ID_RE = re.compile(r'let\s+priceZoneId\s*=\s*"([^"]+)"')
_ZONE_LABEL_RE = re.compile(r'priceZoneInfo\[priceZoneId\]\.label\s*=\s*"([^"]+)"')
_PERF_ID_RE = re.compile(
    r'getPerformanceEcommerceObject\(\s*\{[^}]*"item_id"\s*:\s*"([^"]+)"'
)
_PRICE_RE = re.compile(r"£\s*([\d,]+\.?\d*)")

_STATUS_MAP: dict[str, SeatStatus] = {
    "A": SeatStatus.AVAILABLE,
    "S": SeatStatus.SOLD,
    "U": SeatStatus.UNAVAILABLE,
    "O": SeatStatus.CONTENDED,
}

_RESTRICTED_KEYWORDS = ("wheelchair", "companion", "assistant")


def is_restricted_access(zone_label: str | None, note: str) -> bool:
    """Return True if *zone_label* or *note* indicates a restricted-access seat."""
    combined = ((zone_label or "") + " " + note).lower()
    return any(kw in combined for kw in _RESTRICTED_KEYWORDS)


def parse_seat_map(html: str, expected_performance_id: str) -> SeatMap:
    """Parse a BFI seat-map HTML page and return a :class:`SeatMap`.

    Raises :class:`BfiContractError` on any contract violation:
    - Performance ID absent or mismatched.
    - No seat circles found.
    - Duplicate seat IDs that disagree on row or status.
    - Missing or non-finite seat coordinates.
    """
    perf_match = _PERF_ID_RE.search(html)
    if perf_match is None:
        raise BfiContractError("performance ID not found in seat-map page")
    actual_id = perf_match.group(1)
    if actual_id.upper() != expected_performance_id.upper():
        raise BfiContractError(
            f"performance ID mismatch: expected {expected_performance_id!r}, got {actual_id!r}"
        )

    zone_labels: dict[str, str] = {}
    for block in re.findall(r"(?s)<script[^>]*>(.*?)</script>", html):
        id_matches = list(_ZONE_ID_RE.finditer(block))
        label_matches = list(_ZONE_LABEL_RE.finditer(block))
        if len(id_matches) != len(label_matches):
            raise BfiContractError(
                f"zone-script parsing error: {len(id_matches)} zone ID(s) but "
                f"{len(label_matches)} label assignment(s)"
            )
        for id_m, lbl_m in zip(id_matches, label_matches, strict=True):
            zone_labels[id_m.group(1).upper()] = lbl_m.group(1)

    tree = lxml.html.fromstring(html)
    label_els = cast(list[Any], tree.xpath("//*[contains(@class,'zone-label')]"))
    price_els = cast(list[Any], tree.xpath("//*[contains(@class,'price-zone-price-text')]"))
    label_to_price: dict[str, Decimal | None] = {}
    for lbl_el, prc_el in zip(label_els, price_els, strict=False):
        lbl_text = (lbl_el.text_content() or "").strip()
        prc_text = (prc_el.text_content() or "").strip()
        prc_m = _PRICE_RE.search(prc_text)
        label_to_price[lbl_text] = Decimal(prc_m.group(1).replace(",", "")) if prc_m else None

    price_zones: dict[str, PriceZone] = {
        guid: PriceZone(zone_id=guid, label=lbl, price=label_to_price.get(lbl))
        for guid, lbl in zone_labels.items()
    }

    seen: dict[str, Seat] = {}
    circles = cast(list[Any], tree.xpath("//*[local-name()='circle' and @data-status]"))
    for circle in circles:
        raw_status: str = circle.get("data-status", "")
        seat_id: str = circle.get("id", "")
        row: str = circle.get("data-seat-row", "")
        seat_num_str: str = circle.get("data-seat-seat", "")
        note: str = circle.get("data-note") or circle.get("title") or ""

        try:
            column = int(seat_num_str)
        except (ValueError, TypeError):
            raise BfiContractError(
                f"seat {seat_id!r}: invalid seat number {seat_num_str!r}"
            ) from None

        cx_str: str = circle.get("cx", "")
        cy_str: str = circle.get("cy", "")
        try:
            x = float(cx_str)
            y = float(cy_str)
        except (ValueError, TypeError):
            raise BfiContractError(
                f"seat {seat_id!r}: invalid coordinates cx={cx_str!r} cy={cy_str!r}"
            ) from None
        if not (math.isfinite(x) and math.isfinite(y)):
            raise BfiContractError(f"seat {seat_id!r}: non-finite coordinates")

        zone: PriceZone | None = None
        parent = circle.getparent()
        while parent is not None:
            pid = parent.get("id", "").upper()
            if _GUID_RE.match(pid):
                if pid not in price_zones:
                    price_zones[pid] = PriceZone(zone_id=pid, label="(unlisted zone)", price=None)
                zone = price_zones[pid]
                break
            parent = parent.getparent()

        base_status = _STATUS_MAP.get(raw_status, SeatStatus.UNKNOWN)
        zone_label_str = zone.label if zone else None
        status = SeatStatus.RESTRICTED if is_restricted_access(zone_label_str, note) else base_status

        seat = Seat(
            seat_id=seat_id,
            raw_status_code=raw_status,
            status=status,
            zone=zone,
            note=note,
            row=row,
            column=column,
            x=x,
            y=y,
        )

        if seat_id in seen:
            existing = seen[seat_id]
            if existing.row != seat.row or existing.raw_status_code != seat.raw_status_code:
                raise BfiContractError(
                    f"duplicate seat {seat_id!r} disagrees on row/status"
                )
            continue

        seen[seat_id] = seat

    if not seen:
        raise BfiContractError("seat map contains no seat circles")

    return SeatMap(performance_id=actual_id, seats=tuple(seen.values()))
