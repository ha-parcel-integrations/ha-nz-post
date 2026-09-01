"""Tests for the NZ Post public tracking API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.nz_post.api import NZPostApiClient, NZPostApiError

CODE = "EXAMPLE123456"


def _session_returning(status: int, body: object) -> MagicMock:
    response = AsyncMock(status=status, headers={})
    response.json = AsyncMock(
        side_effect=json.JSONDecodeError("x", body, 0) if isinstance(body, str) else None,
        return_value=None if isinstance(body, str) else body,
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get.return_value = context
    return session


async def test_get_parcel_returns_first_populated_result():
    session = _session_returning(200, {"success": True, "status_code": 0, "results": [{"tracking_reference": CODE}]})
    assert await NZPostApiClient(session).async_get_parcel(CODE) == {"tracking_reference": CODE}
    assert session.get.call_args.kwargs["params"] == {"tracking_reference": CODE}


async def test_get_parcel_returns_none_for_body_not_found_and_nested_errors():
    for payload in (
        {"success": False, "status_code": 2, "results": []},
        {"success": True, "status_code": 0, "results": [{"errors": [{"code": "400002"}]}]},
        {"success": True, "status_code": 0, "results": []},
    ):
        assert await NZPostApiClient(_session_returning(200, payload)).async_get_parcel(CODE) is None


async def test_get_parcel_raises_for_http_and_malformed_json():
    with pytest.raises(NZPostApiError):
        await NZPostApiClient(_session_returning(500, {})).async_get_parcel(CODE)
    with pytest.raises(NZPostApiError):
        await NZPostApiClient(_session_returning(200, "not json")).async_get_parcel(CODE)


async def test_get_parcel_raises_for_non_dict_payload():
    with pytest.raises(NZPostApiError):
        await NZPostApiClient(_session_returning(200, ["not", "a", "dict"])).async_get_parcel(CODE)


async def test_get_parcel_skips_non_dict_entries_in_results():
    payload = {"success": True, "status_code": 0, "results": ["not a dict", {"tracking_reference": CODE}]}
    assert await NZPostApiClient(_session_returning(200, payload)).async_get_parcel(CODE) == {
        "tracking_reference": CODE
    }


async def test_get_parcel_429_with_unparseable_retry_after():
    response = AsyncMock(status=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get.return_value = context

    with pytest.raises(NZPostApiError) as excinfo:
        await NZPostApiClient(session).async_get_parcel(CODE)
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after is None


async def test_get_parcel_propagates_network_error():
    session = MagicMock()
    session.get.side_effect = aiohttp.ClientError("boom")
    with pytest.raises(aiohttp.ClientError):
        await NZPostApiClient(session).async_get_parcel(CODE)
