"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nz_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.nz_post.contents import CONTENTS_STORE_MAX_CODES
from custom_components.nz_post.coordinator import NZPostCoordinator

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

CLIENT = "custom_components.nz_post.api.NZPostApiClient"
COORDINATOR = f"{NZPostCoordinator.__module__}.NZPostCoordinator"
UNTRACKED = "EXAMPLE555555"
STORE_KEY = f"{DOMAIN}.contents.%s"


async def _setup(hass, codes: list[str] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes or []],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code: _sample(code)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _sample(code: str) -> dict:
    return delivered_sample(code) if code == DELIVERED_CODE else active_sample(code)


async def _call(hass, service: str, data: dict) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()
    # A refresh requested inside the debouncer's cooldown runs after it.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    coordinator = entry.runtime_data.coordinator
    parcels = {p["barcode"]: p for p in [*coordinator.data, *coordinator.delivered]}
    return parcels[code]["contents"]


def _quiet(entry):
    """Patch the carrier fetch and the refresh; both must stay unused."""
    return (
        patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()),
        patch(f"{COORDINATOR}.async_request_refresh", new=AsyncMock()),
        patch.object(entry.runtime_data.coordinator, "async_update_listeners"),
    )


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_sets_replaces_and_clears_without_a_fetch(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    fetch, refresh, listeners = _quiet(entry)
    with fetch as fetch_mock, refresh as refresh_mock, listeners as listeners_mock:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": " Running  shoes "},
        )
        assert listeners_mock.call_count == 1
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Winter socks"},
        )
        assert listeners_mock.call_count == 2
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "  "},
        )
        assert listeners_mock.call_count == 3
        fetch_mock.assert_not_called()
        refresh_mock.assert_not_called()


async def test_update_contents_shows_on_the_parcel(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    with patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": " Running  shoes "},
        )
        assert _contents(entry, ACTIVE_CODE) == "Running shoes"
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Winter socks"},
        )
        assert _contents(entry, ACTIVE_CODE) == "Winter socks"
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE})
        assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_reaches_a_delivered_parcel(hass):
    entry = await _setup(hass, [DELIVERED_CODE])
    with patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: DELIVERED_CODE, "contents": "Running shoes"},
        )
    assert _contents(entry, DELIVERED_CODE) == "Running shoes"


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: " example999999 ", "contents": "Running shoes"},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert entry.runtime_data.coordinator.contents_store.get(UNTRACKED) == (
        "Running shoes"
    )
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_unchanged_contents_writes_nothing_and_notifies_nobody(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    fetch, refresh, listeners = _quiet(entry)
    store = entry.runtime_data.coordinator.contents_store
    with fetch as fetch_mock, refresh as refresh_mock, listeners as listeners_mock:
        await _call(hass, "update_contents", data)
        assert listeners_mock.call_count == 1
        with patch.object(store._store, "async_delay_save") as save:
            await _call(hass, "update_contents", data)
            await _call(hass, "track_parcel", data)
            await _call(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
            save.assert_not_called()
        assert listeners_mock.call_count == 1
        fetch_mock.assert_not_called()
        refresh_mock.assert_not_called()


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: ""})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code_and_still_refreshes(hass):
    entry = await _setup(hass)
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code: _sample(code)),
    ) as fetch:
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
        )
        fetch.assert_awaited_with(ACTIVE_CODE)

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    with patch(
        f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
    ):
        await _call(hass, "track_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert _contents(entry, ACTIVE_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_without_a_fetch(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    track = {CONF_TRACKING_CODE: ACTIVE_CODE}
    fetch, refresh, listeners = _quiet(entry)
    with fetch as fetch_mock, refresh as refresh_mock, listeners as listeners_mock:
        await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
        await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
        await _call(hass, "track_parcel", {**track, "contents": ""})
        assert listeners_mock.call_count == 3
        fetch_mock.assert_not_called()
        refresh_mock.assert_not_called()
    assert _contents(entry, ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_track_parcel_contents_need_a_loaded_entry(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
        )


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_on_an_unloaded_entry_still_untracks(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})
    assert entry.options[CONF_PARCELS] == []


async def test_the_store_drops_the_oldest_code_at_the_cap(hass):
    entry = await _setup(hass)
    store = entry.runtime_data.coordinator.contents_store
    for number in range(CONTENTS_STORE_MAX_CODES):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: f"CODE{number}", "contents": f"Item {number}"},
            blocking=True,
        )
    assert store.get("CODE0") == "Item 0"

    await hass.services.async_call(
        DOMAIN,
        "update_contents",
        {CONF_TRACKING_CODE: "CODE200", "contents": "Item 200"},
        blocking=True,
    )

    assert store.get("CODE0") is None
    assert store.get("CODE1") == "Item 1"
    assert store.get("CODE200") == "Item 200"


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch(
        f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_an_entry_without_a_store_file_works(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    assert STORE_KEY % entry.entry_id not in hass_storage
    assert _contents(entry, ACTIVE_CODE) is None


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass, [ACTIVE_CODE])
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert events == []
