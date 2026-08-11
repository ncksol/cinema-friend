"""Tests for cinema_friend.bfi.seat_map."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cinema_friend.bfi.seat_map import is_restricted_access, parse_seat_map
from cinema_friend.domain.bfi import SeatStatus
from cinema_friend.domain.errors import BfiContractError
from tests.factories.bfi_html import PERFORMANCE_ID, ZONE_ID, seat_map_html

# ---------------------------------------------------------------------------
# Step 2 / Step 5 tests
# ---------------------------------------------------------------------------


def test_parses_and_deduplicates_seats():
    seat_map = parse_seat_map(seat_map_html(), PERFORMANCE_ID)
    assert seat_map.performance_id == PERFORMANCE_ID
    assert len(seat_map.seats) == 2
    assert seat_map.seats[0].zone is not None
    assert seat_map.seats[0].zone.label == "1 Standard"
    assert seat_map.seats[0].zone.price == Decimal("22.00")


def test_access_note_or_zone_marks_seat_restricted():
    assert is_restricted_access("BFI IMAX wheelchair space", "") is True
    assert is_restricted_access(None, "companion seat to be sold with a wheelchair space") is True


# ---------------------------------------------------------------------------
# Performance ID mismatch
# ---------------------------------------------------------------------------


def test_performance_id_mismatch_raises():
    with pytest.raises(BfiContractError, match="mismatch"):
        parse_seat_map(seat_map_html(), "00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# No seat circles
# ---------------------------------------------------------------------------


def test_no_seat_circles_raises():
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
</script></html>"""
    with pytest.raises(BfiContractError, match="no seat circles"):
        parse_seat_map(html, PERFORMANCE_ID)


# ---------------------------------------------------------------------------
# Duplicate seat ID disagreement
# ---------------------------------------------------------------------------


def test_duplicate_disagrees_on_row_raises():
    extra = (
        '<circle id="seat-1" data-status="A" data-seat-section="BFI IMAX" '
        'data-seat-row="M" data-seat-seat="17" cx="340" cy="180"/>'
    )
    with pytest.raises(BfiContractError, match="disagrees"):
        parse_seat_map(seat_map_html(extra_circles=extra), PERFORMANCE_ID)


def test_duplicate_disagrees_on_status_raises():
    extra = (
        '<circle id="seat-1" data-status="S" data-seat-section="BFI IMAX" '
        'data-seat-row="L" data-seat-seat="17" cx="340" cy="180"/>'
    )
    with pytest.raises(BfiContractError, match="disagrees"):
        parse_seat_map(seat_map_html(extra_circles=extra), PERFORMANCE_ID)


# ---------------------------------------------------------------------------
# O status → CONTENDED
# ---------------------------------------------------------------------------


def test_o_status_maps_to_contended_and_is_not_available():
    extra = (
        '<circle id="seat-3" data-status="O" data-seat-section="BFI IMAX" '
        'data-seat-row="L" data-seat-seat="19" cx="368" cy="180"/>'
    )
    seat_map = parse_seat_map(seat_map_html(extra_circles=extra), PERFORMANCE_ID)
    contended = next(s for s in seat_map.seats if s.seat_id == "seat-3")
    assert contended.status is SeatStatus.CONTENDED
    assert contended.status is not SeatStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Restricted access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "zone_label",
    [
        "BFI IMAX wheelchair space",
        "Wheelchair Space",
        "assistant seat",
        "companion seat",
        "Companion Space",
    ],
)
def test_restricted_zone_labels_are_restricted(zone_label: str):
    assert is_restricted_access(zone_label, "") is True


@pytest.mark.parametrize(
    "note",
    [
        "wheelchair position only",
        "companion seat to be sold with a wheelchair space",
        "for assistant only",
    ],
)
def test_restricted_notes_are_restricted(note: str):
    assert is_restricted_access(None, note) is True


@pytest.mark.parametrize(
    "zone_label",
    [
        "1 Standard",
        "Premium",
        "VIP",
        "Circle",
        "Front Stalls",
    ],
)
def test_ordinary_zones_are_not_restricted(zone_label: str):
    assert is_restricted_access(zone_label, "") is False


def test_restricted_zone_seat_status_becomes_restricted():
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
let priceZoneId = "{ZONE_ID}";
priceZoneInfo[priceZoneId].label = "Wheelchair Space";
</script>
<div class="zone-label">Wheelchair Space</div>
<div class="price-zone-price-text">- £0.00</div>
<svg><g id="{ZONE_ID}">
  <circle id="wc-1" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="A" data-seat-seat="1" cx="10" cy="10"/>
</g></svg></html>"""
    seat_map = parse_seat_map(html, PERFORMANCE_ID)
    assert seat_map.seats[0].status is SeatStatus.RESTRICTED


# ---------------------------------------------------------------------------
# Unlisted zone GUID
# ---------------------------------------------------------------------------


def test_unknown_zone_guid_produces_unlisted_zone():
    unknown_guid = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
</script>
<svg><g id="{unknown_guid}">
  <circle id="x-1" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="X" data-seat-seat="1" cx="10" cy="10"/>
</g></svg></html>"""
    seat_map = parse_seat_map(html, PERFORMANCE_ID)
    assert seat_map.seats[0].zone is not None
    assert seat_map.seats[0].zone.label == "(unlisted zone)"
    assert seat_map.seats[0].zone.price is None


# ---------------------------------------------------------------------------
# Fix round 1: U → UNAVAILABLE semantics
# ---------------------------------------------------------------------------


def test_u_status_maps_to_unavailable():
    extra = (
        '<circle id="seat-u" data-status="U" data-seat-section="BFI IMAX" '
        'data-seat-row="L" data-seat-seat="20" cx="382" cy="180"/>'
    )
    seat_map = parse_seat_map(seat_map_html(extra_circles=extra), PERFORMANCE_ID)
    u_seat = next(s for s in seat_map.seats if s.seat_id == "seat-u")
    assert u_seat.status is SeatStatus.UNAVAILABLE


def test_u_status_is_not_reserved_or_available():
    extra = (
        '<circle id="seat-u2" data-status="U" data-seat-section="BFI IMAX" '
        'data-seat-row="L" data-seat-seat="21" cx="396" cy="180"/>'
    )
    seat_map = parse_seat_map(seat_map_html(extra_circles=extra), PERFORMANCE_ID)
    u_seat = next(s for s in seat_map.seats if s.seat_id == "seat-u2")
    assert u_seat.status is not SeatStatus.RESERVED
    assert u_seat.status is not SeatStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Fix round 1: zone-script mismatched pairs raise BfiContractError
# ---------------------------------------------------------------------------


def test_zone_script_extra_id_raises():
    """A script block with two zone IDs but one label assignment is malformed."""
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
let priceZoneId = "{ZONE_ID}";
let priceZoneId = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE";
priceZoneInfo[priceZoneId].label = "1 Standard";
</script>
<div class="zone-label">1 Standard</div>
<div class="price-zone-price-text">- £22.00</div>
<svg><g id="{ZONE_ID}">
  <circle id="s-1" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="A" data-seat-seat="1" cx="10" cy="10"/>
</g></svg></html>"""
    with pytest.raises(BfiContractError, match="zone-script"):
        parse_seat_map(html, PERFORMANCE_ID)


def test_zone_script_extra_label_raises():
    """A script block with one zone ID but two label assignments is malformed."""
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
let priceZoneId = "{ZONE_ID}";
priceZoneInfo[priceZoneId].label = "1 Standard";
priceZoneInfo[priceZoneId].label = "Duplicate Label";
</script>
<div class="zone-label">1 Standard</div>
<div class="price-zone-price-text">- £22.00</div>
<svg><g id="{ZONE_ID}">
  <circle id="s-2" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="A" data-seat-seat="1" cx="10" cy="10"/>
</g></svg></html>"""
    with pytest.raises(BfiContractError, match="zone-script"):
        parse_seat_map(html, PERFORMANCE_ID)


# ---------------------------------------------------------------------------
# Fix round 1: invalid / non-finite coordinate handling
# ---------------------------------------------------------------------------


def test_missing_cx_raises():
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
</script>
<svg><g id="{ZONE_ID}">
  <circle id="bad-1" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="A" data-seat-seat="1" cy="10"/>
</g></svg></html>"""
    with pytest.raises(BfiContractError, match="invalid coordinates"):
        parse_seat_map(html, PERFORMANCE_ID)


def test_non_numeric_cy_raises():
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
</script>
<svg><g id="{ZONE_ID}">
  <circle id="bad-2" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="A" data-seat-seat="1" cx="10" cy="notanumber"/>
</g></svg></html>"""
    with pytest.raises(BfiContractError, match="invalid coordinates"):
        parse_seat_map(html, PERFORMANCE_ID)


def test_inf_cx_raises():
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
</script>
<svg><g id="{ZONE_ID}">
  <circle id="bad-3" data-status="A" data-seat-section="BFI IMAX"
    data-seat-row="A" data-seat-seat="1" cx="inf" cy="10"/>
</g></svg></html>"""
    with pytest.raises(BfiContractError, match="non-finite"):
        parse_seat_map(html, PERFORMANCE_ID)


# ---------------------------------------------------------------------------
# Fix round 1: data-seat-section is required seat identity, parsed fail-closed
# ---------------------------------------------------------------------------


def test_parses_seat_section_into_domain_seat():
    seat_map = parse_seat_map(seat_map_html(), PERFORMANCE_ID)
    assert all(s.section == "BFI IMAX" for s in seat_map.seats)


def test_missing_seat_section_raises():
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
</script>
<svg><g id="{ZONE_ID}">
  <circle id="bad-4" data-status="A"
    data-seat-row="A" data-seat-seat="1" cx="10" cy="10"/>
</g></svg></html>"""
    with pytest.raises(BfiContractError, match="section"):
        parse_seat_map(html, PERFORMANCE_ID)


def test_empty_seat_section_raises():
    html = f"""<html><script>
getPerformanceEcommerceObject({{"item_id":"{PERFORMANCE_ID}"}})
</script>
<svg><g id="{ZONE_ID}">
  <circle id="bad-5" data-status="A" data-seat-section=""
    data-seat-row="A" data-seat-seat="1" cx="10" cy="10"/>
</g></svg></html>"""
    with pytest.raises(BfiContractError, match="section"):
        parse_seat_map(html, PERFORMANCE_ID)
