"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.nz_post import parcels
from custom_components.nz_post.const import ParcelStatus
from custom_components.nz_post.parcels import (
    is_pickup,
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)

from .payloads import active_sample, delivered_sample, event

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

NZ = ZoneInfo("Pacific/Auckland")


def _problem_after_delivery() -> dict:
    sample = delivered_sample()
    sample["tracking_events"].insert(
        0, event("Delayed", "2026-04-30T09:00:00+12:00", "Delayed", "40")
    )
    return sample


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "problem_after_delivery": _problem_after_delivery,
    "bare": lambda: {"tracking_reference": "EXAMPLE000001"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_an_earlier_delivery_event_does_not_deliver_an_active_parcel():
    parcel = normalize_parcel(_problem_after_delivery())
    assert parcel["status"] is ParcelStatus.PROBLEM
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_delivered_at_is_the_delivery_event():
    parcel = normalize_parcel(delivered_sample())
    assert parse_iso(parcel["delivered_at"]) == datetime(
        2026, 4, 29, 1, 12, 42, tzinfo=UTC
    )


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_pickup_is_false_until_a_collection_ready_status_exists(parcel):
    assert parcel["pickup"] is False


def test_pickup_sticks_after_collection(monkeypatch):
    """Once a collection-ready status is mapped, a collected parcel stays pickup."""
    monkeypatch.setitem(
        parcels._STATUS_MAP, "Ready for collection", ParcelStatus.AT_PICKUP_POINT
    )
    sample = delivered_sample()
    sample["tracking_events"].append(
        event("Ready for collection", "2026-04-28T20:00:00+12:00", "Ready")
    )
    collected = normalize_parcel(sample)
    assert collected["delivered"] is True
    assert collected["pickup"] is True

    waiting = normalize_parcel(
        {
            "tracking_reference": "EXAMPLE000002",
            "tracking_events": [
                event("Ready for collection", "2026-04-28T20:00:00+12:00", "Ready")
            ],
        }
    )
    assert waiting["status"] is ParcelStatus.AT_PICKUP_POINT
    assert waiting["pickup"] is True
    assert waiting["planned_from"] is None


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=NZ)
    end = datetime.fromisoformat(end)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == parse_iso(start).astimezone(NZ).date()


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=NZ)
    start = datetime.fromisoformat(start)
    end = datetime.fromisoformat(end)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()


def test_the_window_is_emptied_for_finished_statuses():
    window = planned_window("2099-04-29", None, zone=NZ)
    for status in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert planned_for_status(status, *window) == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, *window) == window


def test_is_pickup_prefers_the_route_flag():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.IN_TRANSIT) is False
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.AT_PICKUP_POINT])
