"""Rename regression: preserve identity, completion, and other item fields."""

from unittest.mock import Mock

import pytest
from homeassistant.components.todo import TodoItem
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anylist import client as wire
from custom_components.anylist.todo import AnyListTodoEntity
from .conftest import FakeCoordinator, fake_item, fake_list


def test_rename_is_a_single_field_operation():
    client = wire.AnyListClient(
        access_token="x",
        refresh_token="x",
        user_id="user",
        is_premium_user=True,
        client_identifier="test",
    )
    client.post = Mock()
    client.rename_item("list-1", "item-1", "Oat milk")
    endpoint, payload = client.post.call_args.args
    assert endpoint == "data/shopping-lists/update"
    op = wire._parse_fields(wire._first_value(wire._parse_fields(payload), 1))
    metadata = wire._parse_fields(wire._first_value(op, 1))
    assert wire._first_string(metadata, 2) == "set-list-item-name"
    assert wire._first_string(op, 3) == "item-1"
    assert wire._first_string(op, 4) == "Oat milk"
    assert (
        6 not in op
    )  # Never replace the item and erase quantity/category/checked state.


async def test_todo_rename_only_does_not_uncheck(hass):
    shopping = fake_list(items=[fake_item(is_checked=True)])
    client = Mock()
    coordinator = FakeCoordinator({"lists": [shopping]})
    entity = AnyListTodoEntity(
        coordinator, client, shopping, MockConfigEntry(domain="anylist")
    )
    entity.hass = hass
    await entity.async_update_todo_item(TodoItem(uid="item-1", summary="Oat milk"))
    client.rename_item.assert_called_once_with("list-1", "item-1", "Oat milk")
    client.uncheck_item.assert_not_called()
    client.cross_off_item.assert_not_called()


async def test_todo_rename_requires_current_uid(hass):
    shopping = fake_list()
    client = Mock()
    entity = AnyListTodoEntity(
        FakeCoordinator({"lists": [shopping]}),
        client,
        shopping,
        MockConfigEntry(domain="anylist"),
    )
    entity.hass = hass
    with pytest.raises(HomeAssistantError):
        await entity.async_update_todo_item(TodoItem(uid="deleted", summary="Oat milk"))
    client.rename_item.assert_not_called()
