"""Tests for NZ Post's pure parcel mapping helpers."""
import pytest

from custom_components.nz_post.const import (
    CAPABILITIES,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.nz_post.parcels import (
    build_history,
    clean_description,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
)

from .payloads import active_sample, delivered_sample, event


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("Tracking number allocated to parcel", ParcelStatus.REGISTERED),
        ("Shipment data submitted", ParcelStatus.REGISTERED),
        ("997", ParcelStatus.REGISTERED),
        ("Handover to airline", ParcelStatus.IN_TRANSIT),
        ("Processed at depot", ParcelStatus.IN_TRANSIT),
        ("8", ParcelStatus.IN_TRANSIT),
        ("With courier for delivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("32", ParcelStatus.OUT_FOR_DELIVERY),
        ("Delivered", ParcelStatus.DELIVERED),
        ("22", ParcelStatus.DELIVERED),
    ],
)
def test_maps_all_observed_statuses(signal, expected):
    assert map_parcel_status(signal) is expected


def test_unknown_status_warns_once(caplog):
    assert map_parcel_status("New status") is ParcelStatus.UNKNOWN
    assert map_parcel_status("New status") is ParcelStatus.UNKNOWN
    assert caplog.text.count("New status") == 1
    assert "issues/new" in caplog.text


def test_history_sorts_cleans_and_caps():
    events = [
        event("Delivered", "2026-04-29T13:12:42+12:00", "Delivered <b>&amp; signed</b>", "22"),
        event("Shipment data submitted", "2026-04-27T23:03:58+12:00", "\x00Submitted"),
    ]
    history = build_history(events)
    assert [item["raw_status"] for item in history] == ["Shipment data submitted", "Delivered"]
    assert history[-1]["status"] is ParcelStatus.DELIVERED
    assert parse_iso(history[0]["timestamp"]).tzinfo is not None
    many = [event("Processed at depot", f"2026-05-{day:02d}T10:00:00+12:00", "Moved") for day in range(1, 26)]
    assert len(build_history(many)) == 20


def test_clean_description_removes_html_entities_and_controls():
    assert clean_description("One <strong>&amp;</strong>\x00 two") == "One & two"


def test_normalize_delivered_is_safe_and_canonical():
    raw = delivered_sample()
    raw["tracking_events"][0]["signed_by"] = "Jane Doe"
    raw["tracking_events"][0]["depot_name"] = "Private depot"
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["barcode"] == "EXAMPLE123456"
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-04-29T13:12:42+12:00"
    assert parcel["sender"] is parcel["receiver"] is None
    assert parcel["url"] == "https://www.nzpost.co.nz/tools/tracking?trackid=" + parcel["barcode"]
    assert parcel["weight"] is parcel["dimensions"] is parcel["pickup_point"] is None
    assert parcel["pickup"] is False
    assert "signed_by" not in str(parcel)
    assert "Private depot" not in str(parcel)
    assert parcel["raw"]["tracking_events"][0]["description"] == "Delivered to recipient"


def test_normalize_has_the_exact_canonical_key_set():
    assert list(normalize_parcel(delivered_sample())) == [
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


def test_sort_parcels_by_timestamp_keeps_missing_values_last():
    parcels = [
        {"barcode": "later", "planned_from": "2026-05-02T10:00:00+00:00"},
        {"barcode": "missing", "planned_from": None},
        {"barcode": "earlier", "planned_from": "2026-05-01T10:00:00+00:00"},
    ]
    assert [parcel["barcode"] for parcel in sort_parcels_by_ts(parcels, "planned_from")] == [
        "earlier",
        "later",
        "missing",
    ]


def test_normalize_uses_newest_event_even_when_payload_is_unsorted():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["history"] is None


def test_placeholder_and_unknown_history_status_are_safe():
    assert normalize_parcel({"tracking_reference": "CODE"})["status"] is ParcelStatus.UNKNOWN
    assert map_event_status("Unseen") is None


def test_capabilities_match_confirmed_payload():
    assert CAPABILITIES == frozenset({"url", "history"})
    assert CAPABILITIES <= KNOWN_CAPABILITIES
