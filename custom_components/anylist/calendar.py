"""Native meal calendar and permission-checked meal editing actions."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from functools import partial
import logging

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from . import get_poll_interval
from .const import DOMAIN
from .meal_plan import MealPlanClient, MealPlanError

_LOGGER = logging.getLogger(__name__)
_ENTITIES = "anylist_meal_entities"
PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    api = MealPlanClient(entry.runtime_data.client)

    async def update():
        try:
            return await hass.async_add_executor_job(api.get_plan)
        except MealPlanError as error:
            raise UpdateFailed(str(error)) from error

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="AnyList meals",
        config_entry=entry,
        update_method=update,
        update_interval=timedelta(seconds=get_poll_interval(entry)),
    )
    await coordinator.async_config_entry_first_refresh()
    register_meal_services(hass)
    async_add_entities([AnyListMealCalendar(coordinator, api, entry)])


class AnyListMealCalendar(CoordinatorEntity, CalendarEntity):
    """Date-only provider records, distinct from ordinary appointments."""

    _attr_has_entity_name = True
    _attr_name = "Meal plan"
    _attr_supported_features = 0  # Rich/versioned writes use the actions below.
    _unrecorded_attributes = frozenset({"provider_calendar_id"})

    def __init__(self, coordinator, api, entry):
        super().__init__(coordinator)
        self.api = api
        self.calendar_id = coordinator.data.calendar_id
        self.operation_lock = asyncio.Lock()
        self._attr_unique_id = f"{entry.entry_id}_meal_plan"
        self._attr_device_info = DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Purple Cover, Inc.",
            name="AnyList",
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        entities = self.hass.data.setdefault(_ENTITIES, {})
        entities[self.entity_id] = self

        def remove():
            if entities.get(self.entity_id) is self:
                entities.pop(self.entity_id)

        self.async_on_remove(remove)

    @property
    def available(self):
        return (
            super().available and self.coordinator.data.calendar_id == self.calendar_id
        )

    @property
    def extra_state_attributes(self):
        return {
            "family_hub_kind": "meal_plan",
            "meal_plan_protocol": 1,
            "provider_calendar_id": self.calendar_id,
        }

    @staticmethod
    def _event(meal):
        start = date.fromisoformat(meal.date)
        return CalendarEvent(
            start=start,
            end=start + timedelta(days=1),
            summary=meal.summary,
            description=meal.details,
            uid=meal.id,
        )

    @property
    def event(self):
        today = dt_util.now().date().isoformat()
        candidates = sorted(
            (
                meal
                for meal in self.coordinator.data.events
                if meal.date and meal.date >= today
            ),
            key=lambda meal: (meal.date, meal.id),
        )
        return self._event(candidates[0]) if candidates else None

    async def async_get_events(self, hass, start_date, end_date):
        # HA ranges are instants. AnyList dates stay in HA's local calendar zone.
        start, end = dt_util.as_local(start_date), dt_util.as_local(end_date)
        return [
            self._event(meal)
            for meal in self.coordinator.data.events
            if meal.date
            and datetime.combine(
                date.fromisoformat(meal.date) + timedelta(days=1), time(), start.tzinfo
            )
            > start
            and datetime.combine(date.fromisoformat(meal.date), time(), start.tzinfo)
            < end
        ]


async def _authorize(hass, context, entity_id, *, control):
    # A missing user_id is a trusted server-side automation, matching HA services.
    # WebSocket/REST callers always carry their actual authenticated HA actor.
    if context.user_id is not None:
        user = await hass.auth.async_get_user(context.user_id)
        if (
            user is None
            or not user.is_active
            or not user.permissions.check_entity(entity_id, POLICY_READ)
            or not user.permissions.check_entity(
                entity_id, POLICY_CONTROL if control else POLICY_READ
            )
        ):
            raise Unauthorized(context=context, entity_id=entity_id)


async def _executor(hass, function):
    """Keep the operation lock until the socket-limited worker really finishes."""
    future = hass.async_add_executor_job(function)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(future)
        finally:
            raise


def register_meal_services(hass):
    if hass.services.has_service(DOMAIN, "get_meal_plan"):
        return

    async def handle(call):
        entity_id = call.data["entity_id"]
        write = call.service != "get_meal_plan"

        async def check(entity=None):
            await _authorize(hass, call.context, entity_id, control=write)
            current = hass.data.get(_ENTITIES, {}).get(entity_id)
            if (
                current is None
                or entity is not None
                and current is not entity
                or not current.available
            ):
                raise HomeAssistantError(
                    "This AnyList meal calendar is unavailable. Refresh before editing."
                )
            return current

        entity = await check()
        async with entity.operation_lock:
            await check(entity)
            try:
                if not write:
                    plan = await _executor(hass, entity.api.get_plan)
                    await check(entity)
                    if plan.calendar_id != entity.calendar_id:
                        raise HomeAssistantError(
                            "The meal calendar changed. Reload the integration."
                        )
                    can_edit = True
                    try:
                        await _authorize(hass, call.context, entity_id, control=True)
                    except Unauthorized:
                        can_edit = False
                    await check(entity)
                    return {
                        "calendar_id": plan.calendar_id,
                        "can_edit": can_edit,
                        "labels": plan.labels,
                        "events": [meal.public() for meal in plan.events],
                    }

                def before_write():
                    # Fresh identity/authority after the provider preflight read,
                    # immediately before sending the mutation from the worker.
                    asyncio.run_coroutine_threadsafe(check(entity), hass.loop).result()

                values = {
                    key: value
                    for key, value in call.data.items()
                    if key not in ("entity_id", "event_id", "expected_version")
                }
                if call.service == "create_meal":
                    function = partial(
                        entity.api.create,
                        entity.calendar_id,
                        values,
                        before_write=before_write,
                    )
                elif call.service == "update_meal":
                    function = partial(
                        entity.api.update,
                        entity.calendar_id,
                        call.data["event_id"],
                        call.data["expected_version"],
                        values,
                        before_write=before_write,
                    )
                else:
                    function = partial(
                        entity.api.delete,
                        entity.calendar_id,
                        call.data["event_id"],
                        call.data["expected_version"],
                        before_write=before_write,
                    )
                result = await _executor(hass, function)
                await check(entity)
                # Readback already confirmed the write. A background refresh
                # updates the native calendar without changing the outcome.
                hass.async_create_task(entity.coordinator.async_request_refresh())
                return {
                    "status": "confirmed",
                    "event": result.public() if result else None,
                }
            except MealPlanError as error:
                raise HomeAssistantError(str(error)) from None

    base = {vol.Required("entity_id"): cv.entity_id}
    fields = {
        vol.Optional("date"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("details"): cv.string,
        vol.Optional("recipe_id"): vol.Any(None, cv.string),
        vol.Optional("label_id"): vol.Any(None, cv.string),
        vol.Optional("scale_factor"): vol.Any(int, float),
    }
    identity = {
        vol.Required("event_id"): cv.string,
        vol.Required("expected_version"): cv.string,
    }
    for name, schema in (
        ("get_meal_plan", base),
        ("create_meal", {**base, **fields, vol.Required("date"): cv.string}),
        ("update_meal", {**base, **fields, **identity}),
        ("delete_meal", {**base, **identity}),
    ):
        hass.services.async_register(
            DOMAIN,
            name,
            handle,
            schema=vol.Schema(schema),
            supports_response=SupportsResponse.OPTIONAL,
        )
