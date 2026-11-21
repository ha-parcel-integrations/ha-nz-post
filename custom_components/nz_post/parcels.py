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

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from html import unescape
import logging
import re
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

# Keyed by NZ Post's own display text, which every event carries. Ambiguous
# wording is deliberately left out so it falls through to ``unknown`` and the
# one-shot warning: every delivery-attempt/collection code whose text has not
# been seen on a real parcel.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "Tracking number allocated to parcel": ParcelStatus.REGISTERED,
    "Shipment data submitted": ParcelStatus.REGISTERED,
    "Label created": ParcelStatus.REGISTERED,
    "Parcel pickup request received": ParcelStatus.REGISTERED,
    "Parcel pickup request allocated to driver": ParcelStatus.REGISTERED,
    "Collected from sender": ParcelStatus.IN_TRANSIT,
    "Picked up/Collected": ParcelStatus.IN_TRANSIT,
    "Handover to airline": ParcelStatus.IN_TRANSIT,
    "International departure": ParcelStatus.IN_TRANSIT,
    "Flight arrival": ParcelStatus.IN_TRANSIT,
    "International arrival": ParcelStatus.IN_TRANSIT,
    "Handover for arrival processing": ParcelStatus.IN_TRANSIT,
    "With border agency": ParcelStatus.IN_TRANSIT,
    "Held for clearance": ParcelStatus.IN_TRANSIT,
    "Released for delivery": ParcelStatus.IN_TRANSIT,
    "Cleared for import": ParcelStatus.IN_TRANSIT,
    "Arrival at sort depot": ParcelStatus.IN_TRANSIT,
    "Arrival at sorting centre": ParcelStatus.IN_TRANSIT,
    # "handed over and will be ready for collection soon" — not collectable
    # yet, so in_transit rather than at_pickup_point. Whether the hand-over is
    # to a collection point or a courier depot is unconfirmed.
    "Handed over": ParcelStatus.IN_TRANSIT,
    "Processing at international depot": ParcelStatus.IN_TRANSIT,
    "Processed at depot": ParcelStatus.IN_TRANSIT,
    "Processed at outbound depot": ParcelStatus.IN_TRANSIT,
    "Processed at facility": ParcelStatus.IN_TRANSIT,
    "At local/regional depot": ParcelStatus.IN_TRANSIT,
    "In transit with airline": ParcelStatus.IN_TRANSIT,
    "In transit to local depot": ParcelStatus.IN_TRANSIT,
    "Ready for courier": ParcelStatus.IN_TRANSIT,
    "With courier for delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "Delayed": ParcelStatus.PROBLEM,
    "Delivery attempted - refer to card": ParcelStatus.PROBLEM,
    "Delivered": ParcelStatus.DELIVERED,
}

# Fallback for an event that arrives without display text. Only codes whose
# NZ Post wording is known are listed.
_EDIFACT_STATUS_MAP: dict[str, ParcelStatus] = {
    "997": ParcelStatus.REGISTERED,
    "205": ParcelStatus.REGISTERED,
    "206": ParcelStatus.REGISTERED,
    "5303": ParcelStatus.REGISTERED,
    "6908": ParcelStatus.REGISTERED,
    "13": ParcelStatus.IN_TRANSIT,
    "144": ParcelStatus.IN_TRANSIT,  # pairs with "Handed over"
    "5000": ParcelStatus.IN_TRANSIT,
    "8": ParcelStatus.IN_TRANSIT,
    "58": ParcelStatus.IN_TRANSIT,
    "591": ParcelStatus.IN_TRANSIT,
    "595": ParcelStatus.IN_TRANSIT,
    "6028": ParcelStatus.IN_TRANSIT,
    "5007": ParcelStatus.IN_TRANSIT,
    "6025": ParcelStatus.IN_TRANSIT,
    "5015": ParcelStatus.IN_TRANSIT,
    "5016": ParcelStatus.IN_TRANSIT,
    "5019": ParcelStatus.IN_TRANSIT,
    "5023": ParcelStatus.IN_TRANSIT,
    "5017": ParcelStatus.IN_TRANSIT,
    "5360": ParcelStatus.IN_TRANSIT,
    "5020": ParcelStatus.IN_TRANSIT,
    "5041": ParcelStatus.IN_TRANSIT,
    "5042": ParcelStatus.IN_TRANSIT,
    "5064": ParcelStatus.IN_TRANSIT,
    "5280": ParcelStatus.IN_TRANSIT,
    "5282": ParcelStatus.IN_TRANSIT,
    "5283": ParcelStatus.IN_TRANSIT,
    "5284": ParcelStatus.IN_TRANSIT,
    "5285": ParcelStatus.IN_TRANSIT,
    "5292": ParcelStatus.IN_TRANSIT,
    "6069": ParcelStatus.IN_TRANSIT,
    "6068": ParcelStatus.IN_TRANSIT,
    "6063": ParcelStatus.IN_TRANSIT,
    "6049": ParcelStatus.IN_TRANSIT,
    "6054": ParcelStatus.IN_TRANSIT,
    "6071": ParcelStatus.IN_TRANSIT,
    "5199": ParcelStatus.IN_TRANSIT,
    "5213": ParcelStatus.IN_TRANSIT,
    "32": ParcelStatus.OUT_FOR_DELIVERY,
    "5045": ParcelStatus.OUT_FOR_DELIVERY,
    "40": ParcelStatus.PROBLEM,
    "52": ParcelStatus.PROBLEM,
    "22": ParcelStatus.DELIVERED,
    "5022": ParcelStatus.DELIVERED,
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
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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
            return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
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
        "text": f"{round(length)} x {round(width)} x {round(height)} cm",
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
            "status": map_event_status(
                event.get("status") or event.get("edifact_code")
            ),
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


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The public payload only confirms a code and a timeline. Location, signer
    and the original full timeline are deliberately excluded from ``raw``.
    """
    tracking_code = raw.get("tracking_reference")
    events = [
        event for event in raw.get("tracking_events", []) if isinstance(event, dict)
    ]
    events.sort(
        key=lambda event: (
            parse_iso(to_iso_timestamp(event.get("date_time")))
            or datetime.max.replace(tzinfo=UTC)
        )
    )
    newest = events[-1] if events else {}
    status_code = newest.get("status") or newest.get("edifact_code")
    status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED
    planned_from, planned_to = planned_for_status(status, None, None)
    pickup = is_pickup(
        None,
        status,
        (
            _STATUS_MAP.get(code) or _EDIFACT_STATUS_MAP.get(code)
            for code in (
                event.get("status") or event.get("edifact_code") for event in events
            )
            if code
        ),
    )
    delivered_event = next(
        (
            event
            for event in reversed(events)
            if map_parcel_status(event.get("status") or event.get("edifact_code"))
            is ParcelStatus.DELIVERED
        ),
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
        "delivered_at": to_iso_timestamp(delivered_event.get("date_time"))
        if delivered and delivered_event
        else None,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
