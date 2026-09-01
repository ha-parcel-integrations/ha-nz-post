"""Redacted NZ Post-shaped tracking payloads shared by the tests."""
from __future__ import annotations

ACTIVE_CODE = "EXAMPLE999999"
DELIVERED_CODE = "EXAMPLE123456"


def event(status: str, timestamp, description: str, code: str | None = None) -> dict:
    """One entry of the carrier's own event timeline."""
    return {
        "status": status,
        "date_time": timestamp,
        "description": description,
        "edifact_code": code,
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative tracking response for a delivered parcel."""
    return {
        "tracking_reference": code,
        "tracking_events": [
            event("Delivered", "2026-04-29T13:12:42+12:00", "Delivered to <b>recipient</b>", "22"),
            event("With courier for delivery", "2026-04-29T08:46:00+12:00", "Out for delivery", "32"),
            event("Processed at depot", "2026-04-28T15:52:17+12:00", "At the sorting facility", "8"),
            event("Shipment data submitted", "2026-04-27T23:03:58+12:00", "Shipment announced"),
        ],
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel with an ETA window."""
    sample = delivered_sample(code)
    sample.update(
        {
            "tracking_events": sample["tracking_events"][1:],
        }
    )
    return sample

