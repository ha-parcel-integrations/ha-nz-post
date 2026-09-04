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
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracking_url,
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


def test_missing_status_codes_map_to_nothing_without_warning(caplog):
    assert map_parcel_status(None) is ParcelStatus.UNKNOWN
    assert map_parcel_status("") is ParcelStatus.UNKNOWN
    assert map_event_status(None) is None
    assert map_event_status("") is None
    assert "issues/new" not in caplog.text


def test_parse_iso_handles_empty_unparseable_and_naive_values():
    assert parse_iso(None) is None
    assert parse_iso("") is None
    assert parse_iso("not-a-date") is None
    assert parse_iso("2026-09-01T10:00:00").tzinfo is not None


def test_to_iso_timestamp_treats_numbers_as_epoch_milliseconds():
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(1_756_000_000_000).startswith("2025-")
    assert to_iso_timestamp(1e30) is None
    assert to_iso_timestamp("2026-09-01T10:00:00Z") == "2026-09-01T10:00:00Z"


def test_format_dimensions_needs_all_three_sides():
    assert format_dimensions(None, 20, 10) is None
    assert format_dimensions(20, None, 10) is None
    assert format_dimensions(20, 20, None) is None
    assert format_dimensions(20, 20, 10)["text"] == "20 x 20 x 10 cm"


def test_clean_description_passes_none_through():
    assert clean_description(None) is None


def test_history_skips_malformed_events_and_keeps_unparseable_dates_last():
    history = build_history(
        [
            "not-a-dict",
            {"status": "Delivered"},
            {"date_time": "not-a-date", "status": "Delivered"},
            {"date_time": 1_756_000_000_000, "status": "Delivered"},
        ]
    )
    assert len(history) == 2
    assert history[-1]["timestamp"] == "not-a-date"


def test_tracking_url_needs_a_code():
    assert tracking_url(None) is None
    assert tracking_url("") is None
    assert "trackid=CODE" in tracking_url("CODE")
