"""Provider-wire tests use invented records; no network or household account."""

from unittest.mock import Mock

import pytest

from custom_components.anylist import client as wire
from custom_components.anylist.meal_plan import (
    MealPlanClient,
    MealPlanError,
    MealPlanUncertainError,
)


def event(*, day="2026-09-28", title="Pasta", identifier="event-1"):
    return (
        wire._field_string(1, identifier)
        + wire._field_int32(2, 27)
        + wire._field_string(3, "calendar-1")
        + wire._field_string(4, day)
        + wire._field_string(5, title)
        + wire._field_string(6, "Keep this note")
        + wire._field_string(7, "recipe-1")
        + wire._field_string(8, "dinner")
        + wire._field_int32(9, 9)
        + wire._field_double(10, 2.5)
        + wire._field_string(99, "future provider field")
    )


def user_data(events=None, *, calendar="calendar-1"):
    label = (
        wire._field_string(1, "dinner")
        + wire._field_string(3, calendar)
        + wire._field_string(4, "aabbcc")
        + wire._field_string(5, "Dinner")
    )
    plan = wire._field_string(1, calendar) + wire._field_bool(2, True)
    plan += b"".join(
        wire._field_message(4, e) for e in ([event()] if events is None else events)
    )
    plan += wire._field_message(6, label)
    recipe = wire._field_string(1, "recipe-1") + wire._field_string(3, "Pasta")
    return wire._field_message(4, plan) + wire._field_message(
        3, wire._field_message(3, recipe)
    )


def provider(data=None):
    transport = Mock()
    transport.user_id.return_value = "user-1"
    transport.get_user_data.return_value = user_data() if data is None else data
    return transport, MealPlanClient(transport)


def operation(transport):
    endpoint, payload = transport.post.call_args.args
    assert endpoint == "data/meal-planning-calendar/update"
    return wire._parse_fields(wire._first_value(wire._parse_fields(payload), 1))


def test_read_keeps_provider_identity_labels_scale_and_recipe_title():
    _, api = provider(user_data([event(title="")]))
    plan = api.get_plan()
    assert plan.calendar_id == "calendar-1"
    assert plan.labels[0]["name"] == "Dinner"
    item = plan.events[0]
    assert (item.id, item.date, item.recipe_id, item.label_id, item.scale_factor) == (
        "event-1",
        "2026-09-28",
        "recipe-1",
        "dinner",
        2.5,
    )
    assert item.summary == "Pasta"
    assert len(item.version) == 64


@pytest.mark.parametrize("payload", [b"", wire._field_message(4, b""), b"\x22\xff"])
def test_missing_or_malformed_plan_is_not_a_successful_empty_list(payload):
    _, api = provider(payload)
    with pytest.raises(MealPlanError):
        api.get_plan()


def test_move_preserves_every_other_field_and_sends_original():
    transport, api = provider()
    version = api.get_plan().events[0].version

    def save(endpoint, payload):
        updated = wire._first_value(operation(transport), 3)
        transport.get_user_data.return_value = user_data([updated])

    transport.post.side_effect = save
    result = api.update("calendar-1", "event-1", version, {"date": "2026-09-30"})
    op = operation(transport)
    updated = wire._parse_fields(wire._first_value(op, 3))
    original = wire._parse_fields(event())
    assert result.date == "2026-09-30"
    assert wire._first_value(op, 4) == event()
    metadata = wire._parse_fields(wire._first_value(op, 1))
    # General edits in AnyList's public web client use update-event. The JS
    # reference's set-event-details is only the note-specific operation there.
    assert wire._first_string(metadata, 2) == "update-event"
    for number in original.keys() - {4}:
        assert updated[number] == original[number]


@pytest.mark.parametrize(
    "changes",
    [
        {"date": "2026-02-30"},
        {"date": "20260930"},
        {"scale_factor": float("nan")},
        {"scale_factor": 0},
        {"unexpected": "x"},
        {"recipe_id": "foreign"},
        {"label_id": "foreign"},
    ],
)
def test_invalid_edits_never_dispatch(changes):
    transport, api = provider()
    with pytest.raises(MealPlanError):
        api.update("calendar-1", "event-1", api.get_plan().events[0].version, changes)
    transport.post.assert_not_called()


def test_stale_version_and_wrong_calendar_never_dispatch():
    transport, api = provider()
    for calendar, version in [
        ("other", api.get_plan().events[0].version),
        ("calendar-1", "stale"),
    ]:
        with pytest.raises(MealPlanError):
            api.update(calendar, "event-1", version, {"date": "2026-09-30"})
    transport.post.assert_not_called()


def test_create_recipe_meal_confirms_exact_generated_id():
    transport, api = provider(user_data([]))

    def save(*args):
        transport.get_user_data.return_value = user_data(
            [wire._first_value(operation(transport), 3)]
        )

    transport.post.side_effect = save
    result = api.create(
        "calendar-1",
        {
            "date": "2026-09-30",
            "recipe_id": "recipe-1",
            "label_id": "dinner",
            "scale_factor": 1.5,
        },
    )
    assert result.id and result.id != "event-1"
    assert result.summary == "Pasta"
    assert result.scale_factor == 1.5
    metadata = wire._parse_fields(wire._first_value(operation(transport), 1))
    assert wire._first_string(metadata, 2) == "new-event"
    assert wire._first_string(metadata, 3) == "user-1"
    assert transport.post.call_count == 1


def test_delete_uses_id_and_requires_confirmed_absence():
    transport, api = provider()
    version = api.get_plan().events[0].version
    transport.post.side_effect = lambda *args: setattr(
        transport.get_user_data, "return_value", user_data([])
    )
    api.delete("calendar-1", "event-1", version)
    # anylist-js targets a single delete through updatedEvent, not a guessed handler.
    assert (
        wire._first_string(
            wire._parse_fields(wire._first_value(operation(transport), 3)), 1
        )
        == "event-1"
    )


@pytest.mark.parametrize(
    "failure", [None, wire.AnyListTimeoutError("private upstream payload")]
)
def test_unknown_write_outcome_is_not_retried_or_claimed_saved(failure):
    transport, api = provider()
    transport.post.side_effect = failure
    with pytest.raises(MealPlanUncertainError) as caught:
        api.create("calendar-1", {"date": "2026-09-30", "title": "Takeout"})
    assert "private" not in str(caught.value)
    assert caught.value.event_id
    assert transport.post.call_count == 1


def test_queue_items_are_not_given_an_invented_date():
    _, api = provider(user_data([event(day=None)]))
    assert api.get_plan().events[0].date is None


def test_delete_does_not_claim_success_when_record_still_exists():
    transport, api = provider()
    with pytest.raises(MealPlanUncertainError):
        api.delete("calendar-1", "event-1", api.get_plan().events[0].version)
    assert transport.post.call_count == 1


def test_clearing_optional_fields_accepts_provider_omission():
    transport, api = provider()
    version = api.get_plan().events[0].version

    def save(*args):
        updated = wire._first_value(operation(transport), 3)
        # Protobuf providers can normalize empty strings to absent fields.
        from custom_components.anylist.meal_plan import _replace

        transport.get_user_data.return_value = user_data(
            [_replace(updated, {6: b"", 7: b"", 8: b""})]
        )

    transport.post.side_effect = save
    result = api.update(
        "calendar-1",
        "event-1",
        version,
        {"details": "", "recipe_id": None, "label_id": None},
    )
    assert result.details == ""
    assert result.recipe_id is None


def test_pre_dispatch_authorization_runs_after_read_and_before_post():
    transport, api = provider()

    def denied():
        assert transport.get_user_data.called
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        api.create(
            "calendar-1", {"date": "2026-09-30", "title": "Soup"}, before_write=denied
        )
    transport.post.assert_not_called()


def test_incremental_snapshot_cannot_confirm_a_missing_record():
    from custom_components.anylist.meal_plan import _replace

    root = wire._parse_fields(user_data([]))
    partial_plan = _replace(wire._first_value(root, 4), {2: wire._field_bool(2, False)})
    transport, api = provider(wire._field_message(4, partial_plan))
    with pytest.raises(MealPlanError):
        api.get_plan()
