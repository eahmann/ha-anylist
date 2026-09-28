"""AnyList meal protocol, using the integration's authenticated transport.

Wire contract references and MIT attribution are in THIRD_PARTY_NOTICES.md.
Only provider records are read or changed; this module owns no local database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
import math
import re
import struct
from typing import Any

from . import client as wire


class MealPlanError(wire.AnyListError):
    """A safe error suitable for display without provider payloads."""


class MealPlanUncertainError(MealPlanError):
    """A write was dispatched but readback did not establish its outcome."""

    def __init__(self, event_id: str):
        super().__init__(
            "AnyList has not confirmed the change. Refresh before trying again."
        )
        self.event_id = event_id


@dataclass(frozen=True)
class Meal:
    id: str
    date: str | None
    title: str
    details: str
    recipe_id: str | None
    label_id: str | None
    scale_factor: float
    summary: str
    version: str
    raw: bytes = field(repr=False)

    def public(self) -> dict[str, Any]:
        """Expose only the documented, credential-free application contract."""
        return {
            key: getattr(self, key)
            for key in (
                "id",
                "date",
                "title",
                "details",
                "recipe_id",
                "label_id",
                "scale_factor",
                "summary",
                "version",
            )
        }


@dataclass(frozen=True)
class MealPlan:
    calendar_id: str
    events: list[Meal]
    labels: list[dict[str, Any]]
    recipes: dict[str, str] = field(repr=False)


def _fields(data: bytes) -> dict:
    """Parse strictly so truncated snapshots cannot become valid empty data."""
    index = 0
    while index < len(data):
        key, index = wire._read_varint(data, index)
        if key >> 3 == 0:
            raise MealPlanError("AnyList returned an invalid meal plan.")
        kind = key & 7
        if kind == 0:
            _, index = wire._read_varint(data, index)
        elif kind in (1, 5):
            index += 8 if kind == 1 else 4
        elif kind == 2:
            size, index = wire._read_varint(data, index)
            index += size
        else:
            raise MealPlanError("AnyList returned an unsupported meal plan.")
        if index > len(data):
            raise MealPlanError("AnyList returned an incomplete meal plan.")
    return wire._parse_fields(data)


def _replace(raw: bytes, changes: dict[int, bytes]) -> bytes:
    """Retain unknown fields and all values outside an explicit patch."""
    output = []
    for number, values in _fields(raw).items():
        if number in changes:
            continue
        for kind, value in values:
            if kind == 2:
                output.append(wire._field_bytes(number, value))
            elif kind == 0:
                output.append(
                    wire._field_key(number, kind) + wire._encode_varint(value)
                )
            else:
                output.append(wire._field_key(number, kind) + value)
    return b"".join(output) + b"".join(changes.values())


def _day(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise MealPlanError("Choose a valid meal date.")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise MealPlanError("Choose a valid meal date.") from error
    return value


def _validate(changes: dict[str, Any]) -> None:
    if (
        not isinstance(changes, dict)
        or not changes
        or changes.keys()
        - {"date", "title", "details", "recipe_id", "label_id", "scale_factor"}
    ):
        raise MealPlanError("Unsupported meal change.")
    if "date" in changes:
        _day(changes["date"])
    for key in ("title", "details", "recipe_id", "label_id"):
        if key in changes and not (
            isinstance(changes[key], str)
            and len(changes[key]) <= 10000
            or key in ("recipe_id", "label_id")
            and changes[key] is None
        ):
            raise MealPlanError("Invalid meal details.")
    if "scale_factor" in changes:
        value = changes["scale_factor"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 100
        ):
            raise MealPlanError(
                "Choose a serving scale greater than zero and at most 100."
            )


def _patch(changes: dict[str, Any]) -> dict[int, bytes]:
    fields = {"date": 4, "title": 5, "details": 6, "recipe_id": 7, "label_id": 8}
    output = {
        fields[key]: wire._field_string(fields[key], value or "")
        for key, value in changes.items()
        if key in fields
    }
    if "scale_factor" in changes:
        output[10] = wire._field_double(10, changes["scale_factor"])
    return output


class MealPlanClient:
    """Narrow extension sharing the provider client's login and token refresh."""

    def __init__(self, client: wire.AnyListClient):
        self.client = client

    def get_plan(self) -> MealPlan:
        try:
            root = _fields(self.client.get_user_data())
            raw = wire._first_value(root, 4)
            if not isinstance(raw, bytes):
                raise MealPlanError(
                    "Meal planning is unavailable for this AnyList account."
                )
            fields = _fields(raw)
            if not wire._first_bool(fields, 2):
                raise MealPlanError(
                    "AnyList did not return a complete meal plan. Refresh before editing."
                )
            calendar_id = wire._first_string(fields, 1)
            if not calendar_id:
                raise MealPlanError("AnyList did not return a meal calendar.")
            recipes = {}
            recipe_data = wire._first_value(root, 3)
            if isinstance(recipe_data, bytes):
                for item in wire._all_values(_fields(recipe_data), 3):
                    recipe = _fields(item)
                    recipes[wire._first_string(recipe, 1)] = (
                        wire._first_string(recipe, 3) or "Recipe"
                    )
            events = []
            seen = set()
            for item in wire._all_values(fields, 4):
                event = _fields(item)
                identifier = wire._first_string(event, 1)
                if (
                    not identifier
                    or identifier in seen
                    or wire._first_string(event, 3) not in (None, calendar_id)
                ):
                    raise MealPlanError("AnyList returned an inconsistent meal plan.")
                seen.add(identifier)
                day = wire._first_string(event, 4) or None
                if day:
                    _day(day)
                title = wire._first_string(event, 5) or ""
                recipe_id = wire._first_string(event, 7) or None
                scale_raw = wire._first_value(event, 10)
                scale = (
                    struct.unpack("<d", scale_raw)[0] if scale_raw is not None else 1.0
                )
                if not math.isfinite(scale) or scale <= 0:
                    raise MealPlanError("AnyList returned an invalid serving scale.")
                events.append(
                    Meal(
                        identifier,
                        day,
                        title,
                        wire._first_string(event, 6) or "",
                        recipe_id,
                        wire._first_string(event, 8) or None,
                        scale,
                        title or recipes.get(recipe_id) or "Meal",
                        sha256(item).hexdigest(),
                        item,
                    )
                )
            labels = []
            for item in wire._all_values(fields, 6):
                label = _fields(item)
                identifier = wire._first_string(label, 1)
                if not identifier or wire._first_string(label, 3) not in (
                    None,
                    calendar_id,
                ):
                    raise MealPlanError("AnyList returned an invalid meal label.")
                labels.append(
                    {
                        "id": identifier,
                        "name": wire._first_string(label, 5) or "Meal",
                        "color": wire._first_string(label, 4),
                        "sort_index": wire._first_int(label, 6) or 0,
                    }
                )
            labels.sort(key=lambda label: label["sort_index"])
            return MealPlan(calendar_id, events, labels, recipes)
        except MealPlanError:
            raise
        except Exception as error:
            raise MealPlanError("Could not read the AnyList meal plan.") from error

    def _current(self, calendar_id: str, changes: dict | None = None) -> MealPlan:
        plan = self.get_plan()
        if plan.calendar_id != calendar_id:
            raise MealPlanError(
                "The AnyList meal calendar has changed. Refresh before editing."
            )
        if changes:
            if changes.get("recipe_id") and changes["recipe_id"] not in plan.recipes:
                raise MealPlanError("That recipe is no longer available.")
            if changes.get("label_id") and changes["label_id"] not in {
                label["id"] for label in plan.labels
            }:
                raise MealPlanError("That meal label is no longer available.")
        return plan

    @staticmethod
    def _existing(plan: MealPlan, event_id: str, expected_version: str) -> Meal:
        meal = next((event for event in plan.events if event.id == event_id), None)
        if meal is None or not expected_version or meal.version != expected_version:
            raise MealPlanError("This meal changed in AnyList. Refresh before editing.")
        return meal

    def _write(
        self,
        calendar_id: str,
        event_id: str,
        handler: str,
        *,
        updated: bytes | None = None,
        original: bytes | None = None,
        before_write=None,
    ) -> Meal | None:
        operation = (
            wire._field_message(
                1,
                wire._pb_operation_metadata(
                    wire._generate_id(), handler, self.client.user_id()
                ),
            )
            + wire._field_string(2, calendar_id)
            + wire._field_message(3, updated)
            + wire._field_message(4, original)
        )
        if before_write is not None:
            before_write()
        try:
            # No automatic retry on a transport failure or missing acknowledgement.
            self.client.post(
                "data/meal-planning-calendar/update", wire._field_message(1, operation)
            )
            plan = self._current(calendar_id)
            result = next((meal for meal in plan.events if meal.id == event_id), None)
            if handler == "delete-event":
                if result is None:
                    return None
                raise MealPlanUncertainError(event_id)
            if updated is not None and result is not None:
                expected, observed = _fields(updated), _fields(result.raw)

                # Provider timestamps may advance; edited content must match.
                def content(fields):
                    scale_raw = wire._first_value(fields, 10)
                    return tuple(
                        wire._first_string(fields, number) or ""
                        for number in (4, 5, 6, 7, 8)
                    ) + (
                        struct.unpack("<d", scale_raw)[0]
                        if scale_raw is not None
                        else 1.0,
                    )

                if content(expected) == content(observed):
                    return result
        except Exception as error:
            raise MealPlanUncertainError(event_id) from error
        raise MealPlanUncertainError(event_id)

    def create(
        self, calendar_id: str, values: dict[str, Any], *, before_write=None
    ) -> Meal:
        _validate(values)
        if not values.get("date") or not (
            values.get("title", "").strip() or values.get("recipe_id")
        ):
            raise MealPlanError("Choose a date and a recipe or meal title.")
        self._current(calendar_id, values)
        identifier = wire._generate_id()
        raw = (
            wire._field_string(1, identifier)
            + wire._field_int32(2, 1)
            + wire._field_string(3, calendar_id)
            + wire._field_int32(9, 0)
            + b"".join(_patch({"scale_factor": 1.0, **values}).values())
        )
        return self._write(
            calendar_id, identifier, "new-event", updated=raw, before_write=before_write
        )

    def update(
        self,
        calendar_id: str,
        event_id: str,
        expected_version: str,
        changes: dict[str, Any],
        *,
        before_write=None,
    ) -> Meal:
        _validate(changes)
        plan = self._current(calendar_id, changes)
        original = self._existing(plan, event_id, expected_version)
        if not (
            changes.get("title", original.title).strip()
            or changes.get("recipe_id", original.recipe_id)
        ):
            raise MealPlanError("Choose a recipe or meal title.")
        updated = _replace(original.raw, _patch(changes))
        return self._write(
            calendar_id,
            event_id,
            "update-event",
            updated=updated,
            original=original.raw,
            before_write=before_write,
        )

    def delete(
        self,
        calendar_id: str,
        event_id: str,
        expected_version: str,
        *,
        before_write=None,
    ) -> None:
        plan = self._current(calendar_id)
        original = self._existing(plan, event_id, expected_version)
        self._write(
            calendar_id,
            event_id,
            "delete-event",
            updated=original.raw,
            original=original.raw,
            before_write=before_write,
        )
