"""AnyList's current quantity field must survive recipe-to-list operations."""

import pytest

from custom_components.anylist import client as wire


def test_current_quantity_takes_precedence_over_deprecated_text():
    raw = (
        wire._field_string(1, "item")
        + wire._field_string(3, "list")
        + wire._field_string(4, "Flour")
        + wire._field_string(18, "old")
        + wire._field_message(21, wire._field_string(3, "1 1/2 cups"))
    )
    assert wire._parse_list_item(raw).quantity == "1 1/2 cups"


def test_new_item_encodes_quantity_for_current_anylist_apps():
    raw = wire._pb_list_item(
        item_id="i",
        list_id="l",
        name="Flour",
        user_id="u",
        checked=False,
        quantity="2 cups",
    )
    fields = wire._parse_fields(raw)
    quantity = wire._parse_fields(wire._first_value(fields, 21))
    assert wire._first_string(quantity, 3) == "2 cups"


@pytest.mark.parametrize(
    "quantity,scale,expected",
    [
        ("1/2 cup", 2, "1 cup"),
        ("1 1/2 cups", 2, "3 cups"),
        ("0.5 tsp", 3, "1.5 tsp"),
        ("to taste", 2, "to taste"),
    ],
)
def test_scale_preserves_units_and_understands_fractions(quantity, scale, expected):
    assert wire._scale_quantity(quantity, scale) == expected
