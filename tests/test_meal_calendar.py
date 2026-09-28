"""Real HA entity, service, actor permissions and provider boundary tests."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from homeassistant.auth.permissions import PolicyPermissions
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anylist.calendar import (
    AnyListMealCalendar,
    register_meal_services,
)
from .conftest import FakeCoordinator
from .test_meal_plan import provider, user_data


def meal_entity(hass):
    transport, api = provider()
    coordinator = FakeCoordinator(api.get_plan())
    entry = MockConfigEntry(domain="anylist", entry_id="account-1")
    entity = AnyListMealCalendar(coordinator, api, entry)
    entity.hass = hass
    entity.entity_id = "calendar.anylist_meals"
    entity.async_write_ha_state = Mock()
    hass.data.setdefault("anylist_meal_entities", {})[entity.entity_id] = entity
    register_meal_services(hass)
    return entity, transport


def policy(user, *, read=True, control=False):
    user.permissions = PolicyPermissions(
        {
            "entities": {
                "entity_ids": {
                    "calendar.anylist_meals": {"read": read, "control": control}
                }
            }
        },
        None,
    )


async def call(hass, user, service, **data):
    return await hass.services.async_call(
        "anylist",
        service,
        {"entity_id": "calendar.anylist_meals", **data},
        context=Context(user_id=user.id),
        blocking=True,
        return_response=True,
    )


async def test_read_only_user_can_read_but_cannot_schedule(hass, hass_read_only_user):
    entity, transport = meal_entity(hass)
    policy(hass_read_only_user)
    result = await call(hass, hass_read_only_user, "get_meal_plan")
    assert result["calendar_id"] == "calendar-1"
    assert result["can_edit"] is False
    assert result["events"][0]["recipe_id"] == "recipe-1"
    assert "raw" not in result["events"][0]
    with pytest.raises(Unauthorized):
        await call(
            hass, hass_read_only_user, "create_meal", date="2026-09-29", title="Soup"
        )
    transport.post.assert_not_called()


async def test_denied_or_inactive_actor_never_reads_provider(hass, hass_read_only_user):
    entity, transport = meal_entity(hass)
    transport.reset_mock()
    policy(hass_read_only_user, read=False)
    with pytest.raises(Unauthorized):
        await call(hass, hass_read_only_user, "get_meal_plan")
    policy(hass_read_only_user, read=True, control=True)
    hass_read_only_user.is_active = False
    with pytest.raises(Unauthorized):
        await call(hass, hass_read_only_user, "get_meal_plan")
    transport.get_user_data.assert_not_called()


async def test_permission_rechecked_after_waiting_for_another_write(
    hass, hass_read_only_user
):
    entity, transport = meal_entity(hass)
    policy(hass_read_only_user, control=True)
    await entity.operation_lock.acquire()
    pending = asyncio.create_task(
        call(hass, hass_read_only_user, "create_meal", date="2026-09-29", title="Soup")
    )
    await asyncio.sleep(0)
    policy(hass_read_only_user, control=False)
    entity.operation_lock.release()
    with pytest.raises(Unauthorized):
        await pending
    transport.post.assert_not_called()


async def test_readback_does_not_leak_after_user_revocation(hass, hass_read_only_user):
    entity, transport = meal_entity(hass)
    policy(hass_read_only_user)

    def revoke():
        hass_read_only_user.is_active = False
        return user_data()

    transport.get_user_data.side_effect = revoke
    with pytest.raises(Unauthorized):
        await call(hass, hass_read_only_user, "get_meal_plan")


async def test_calendar_dates_remain_all_day_across_timezone(hass):
    entity, _ = meal_entity(hass)
    result = await entity.async_get_events(
        hass,
        datetime(2026, 9, 28, tzinfo=timezone.utc),
        datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert len(result) == 1
    assert result[0].start.isoformat() == "2026-09-28"
    assert result[0].end.isoformat() == "2026-09-29"
    assert result[0].uid == "event-1"
    assert entity.extra_state_attributes["family_hub_kind"] == "meal_plan"


async def test_unloaded_entity_cannot_write_after_queue_wait(hass, hass_read_only_user):
    entity, transport = meal_entity(hass)
    policy(hass_read_only_user, control=True)
    await entity.operation_lock.acquire()
    pending = asyncio.create_task(
        call(hass, hass_read_only_user, "create_meal", date="2026-09-29", title="Soup")
    )
    await asyncio.sleep(0)
    hass.data["anylist_meal_entities"].clear()
    entity.operation_lock.release()
    with pytest.raises(HomeAssistantError):
        await pending
    transport.post.assert_not_called()


async def test_opt_in_loads_real_calendar_platform_and_unloads(hass):
    from .conftest import FakeAnyListClient

    fake = FakeAnyListClient()
    fake.get_user_data = Mock(return_value=user_data())
    entry = MockConfigEntry(
        domain="anylist",
        data={"email": "test@example.com", "password": "test"},
        options={"meal_plan_enabled": True},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.anylist.AnyListClient.login", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entities = hass.data.get("anylist_meal_entities", {})
    assert len(entities) == 1
    entity_id = next(iter(entities))
    assert hass.states.get(entity_id).attributes["family_hub_kind"] == "meal_plan"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not entities


async def test_authorized_non_admin_can_create_and_read_confirmed_result(
    hass, hass_read_only_user
):
    from .test_meal_plan import operation
    from custom_components.anylist import client as wire

    entity, transport = meal_entity(hass)
    policy(hass_read_only_user, control=True)

    def save(*args):
        raw = wire._first_value(operation(transport), 3)
        transport.get_user_data.return_value = user_data([raw])

    transport.post.side_effect = save
    result = await call(
        hass, hass_read_only_user, "create_meal", date="2026-09-29", title="Soup"
    )
    assert result["status"] == "confirmed"
    assert result["event"]["title"] == "Soup"
    assert transport.post.call_count == 1


async def test_control_without_read_does_not_expose_meal_fields(
    hass, hass_read_only_user
):
    entity, transport = meal_entity(hass)
    policy(hass_read_only_user, read=False, control=True)
    with pytest.raises(Unauthorized):
        await call(
            hass, hass_read_only_user, "create_meal", date="2026-09-29", title="Soup"
        )
    transport.post.assert_not_called()


async def test_revocation_during_provider_preflight_prevents_write(
    hass, hass_read_only_user
):
    entity, transport = meal_entity(hass)
    policy(hass_read_only_user, control=True)

    def revoke():
        hass_read_only_user.is_active = False
        return user_data()

    transport.get_user_data.side_effect = revoke
    with pytest.raises(Unauthorized):
        await call(
            hass, hass_read_only_user, "create_meal", date="2026-09-29", title="Soup"
        )
    transport.post.assert_not_called()


async def test_cancelled_request_holds_lock_until_worker_finishes(
    hass, hass_read_only_user
):
    import threading

    entity, transport = meal_entity(hass)
    policy(hass_read_only_user)
    started = asyncio.Event()
    finish = threading.Event()

    def fetch():
        hass.loop.call_soon_threadsafe(started.set)
        finish.wait(timeout=3)
        return user_data()

    transport.get_user_data.side_effect = fetch
    task = asyncio.create_task(call(hass, hass_read_only_user, "get_meal_plan"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert entity.operation_lock.locked()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not entity.operation_lock.locked()


async def test_private_calendar_subscription_url_is_not_logged(hass, caplog):
    from .conftest import FakeAnyListClient

    fake = FakeAnyListClient()
    entry = MockConfigEntry(
        domain="anylist",
        data={"email": "test@example.com", "password": "test"},
        options={"meal_plan_calendar": True},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.anylist.AnyListClient.login", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert "https://icalendar.anylist.com/redacted.ics" not in caplog.text


async def test_real_websocket_read_response_and_denied_write(
    hass, hass_read_only_user, hass_read_only_access_token, hass_ws_client
):
    entity, transport = meal_entity(hass)
    policy(hass_read_only_user)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    await client.send_json(
        {
            "id": 1,
            "type": "call_service",
            "domain": "anylist",
            "service": "get_meal_plan",
            "service_data": {"entity_id": entity.entity_id},
            "return_response": True,
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["response"]["can_edit"] is False
    assert response["result"]["response"]["events"][0]["id"] == "event-1"
    await client.send_json(
        {
            "id": 2,
            "type": "call_service",
            "domain": "anylist",
            "service": "create_meal",
            "service_data": {
                "entity_id": entity.entity_id,
                "date": "2026-09-29",
                "title": "Soup",
            },
            "return_response": True,
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    # HA's call_service wrapper serializes service Unauthorized as this code.
    assert response["error"]["code"] == "home_assistant_error"
    assert response["error"]["message"] == "Unauthorized"
    transport.post.assert_not_called()
