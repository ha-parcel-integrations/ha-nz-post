"""NZ Post public tracking API client.

Keep the contract the coordinator relies on:

* ``async_get_parcel`` returns the raw per-parcel dict on success,
* returns ``None`` when the carrier says the tracking code is unknown or not
  yet scanned (a normal, expected state — never an error),
* raises :class:`NZPostApiError` for anything else, with
  ``status_code`` set on a non-2xx response and ``retry_after`` set when the
  carrier's own ``Retry-After`` header on a 429 could be parsed as seconds —
  the coordinator's backoff (Section 3 of the dynamic-polling plan) reads
  both,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""
from __future__ import annotations

from typing import Any

import aiohttp

from .const import TRACKING_API_URL


class NZPostApiError(Exception):
    """Raised when a NZ Post API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"NZ Post API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class NZPostApiClient:
    """Client for the public NZ Post tracking endpoint.

    No authentication: the endpoint is keyed on the tracking code alone.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the parcel dict for a known parcel, or ``None`` when the
        endpoint reports the code as unknown — which is also what a
        not-yet-scanned parcel gets. Any other failure envelope or non-2xx
        status raises :class:`NZPostApiError`; malformed JSON and unexpected
        response shapes do likewise; network errors propagate
        as ``aiohttp.ClientError``.
        """
        async with self._session.get(
            TRACKING_API_URL, params={"tracking_reference": tracking_code}
        ) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                raise NZPostApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise NZPostApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                # content_type=None: consumer endpoints routinely serve JSON as
                # text/plain, and aiohttp would otherwise refuse to parse it.
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise NZPostApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise NZPostApiError("unexpected body (not a JSON object)")

        results = payload.get("results")
        if payload.get("status_code") == 2 or not isinstance(results, list):
            return None
        for result in results:
            if not isinstance(result, dict):
                continue
            if result.get("errors"):
                return None
            return result
        return None
