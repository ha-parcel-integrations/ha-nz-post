"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The carrier-specific pieces are :data:`_STATUS_MAP` and
:func:`normalize_parcel`. Everything else — the
timestamp parsing, the history builder, the sort contract, the delivered
filter, the one-shot warning for unmapped statuses — is suite-wide machinery
and should be left alone.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL_BASE,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-nz-post/issues/new"
    "?template=unrecognised_status.yml"
)

_STATUS_MAP: dict[str, ParcelStatus] = {
    "Tracking number allocated to parcel": ParcelStatus.REGISTERED,
    "Shipment data submitted": ParcelStatus.REGISTERED,
    "Handover to airline": ParcelStatus.IN_TRANSIT,
    "International departure": ParcelStatus.IN_TRANSIT,
    "Flight arrival": ParcelStatus.IN_TRANSIT,
    "International arrival": ParcelStatus.IN_TRANSIT,
    "Handover for arrival processing": ParcelStatus.IN_TRANSIT,
    "Cleared for import": ParcelStatus.IN_TRANSIT,
    "Arrival at sort depot": ParcelStatus.IN_TRANSIT,
    "Processed at depot": ParcelStatus.IN_TRANSIT,
    "Processed at facility": ParcelStatus.IN_TRANSIT,
    "In transit with airline": ParcelStatus.IN_TRANSIT,
    "In transit to local depot": ParcelStatus.IN_TRANSIT,
    "With courier for delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "Delivered": ParcelStatus.DELIVERED,
}

_EDIFACT_STATUS_MAP: dict[str, ParcelStatus] = {
    "997": ParcelStatus.REGISTERED,
    "8": ParcelStatus.IN_TRANSIT,
    "32": ParcelStatus.OUT_FOR_DELIVERY,
    "22": ParcelStatus.DELIVERED,
}

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised NZ Post status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code) or _EDIFACT_STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code) or _EDIFACT_STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def clean_description(value: Any) -> str | None:
    """Remove markup, entities and control characters from event text."""
    if value is None:
        return None
    cleaned = re.sub(r"<[^>]*>", "", unescape(str(value)))
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
    return " ".join(cleaned.split()) or None


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.

    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("date_time"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("status") or event.get("edifact_code")),
            "raw_status": event.get("status") or event.get("edifact_code"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Return the confirmed per-parcel deep link on NZ Post's public tracker."""
    if not tracking_code:
        return None
    return f"{TRACKING_URL_BASE}?trackid={tracking_code}"


def sanitize_raw_parcel(raw: dict) -> dict:
    """Keep only the payload fields safe to retain between polls."""
    return {
        "tracking_reference": raw.get("tracking_reference"),
        "tracking_events": [
            {
                "date_time": to_iso_timestamp(event.get("date_time")),
                "status": event.get("status"),
                "edifact_code": event.get("edifact_code"),
                "description": clean_description(event.get("description")),
            }
            for event in raw.get("tracking_events", [])
            if isinstance(event, dict)
        ],
    }


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The public payload only confirms a code and a timeline. Location, signer
    and the original full timeline are deliberately excluded from ``raw``.
    """
    tracking_code = raw.get("tracking_reference")
    events = [event for event in raw.get("tracking_events", []) if isinstance(event, dict)]
    events.sort(key=lambda event: parse_iso(to_iso_timestamp(event.get("date_time"))) or datetime.max.replace(tzinfo=timezone.utc))
    newest = events[-1] if events else {}
    status_code = newest.get("status") or newest.get("edifact_code")
    status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED
    delivered_event = next(
        (event for event in reversed(events) if map_parcel_status(event.get("status") or event.get("edifact_code")) is ParcelStatus.DELIVERED),
        None,
    )
    safe_events = sanitize_raw_parcel(raw)["tracking_events"]

    return {
        "carrier": "NZ Post",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": status_code,
        "delivered": delivered,
        "delivered_at": to_iso_timestamp(delivered_event.get("date_time")) if delivered_event else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": {"tracking_events": safe_events},
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
