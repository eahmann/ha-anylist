# Family Hub fork: meal planning contract

This fork extends `moryoav/ha-anylist` at
`d09679e4d21e87e70642956128f49f60e4c29847`. It keeps AnyList authoritative;
the calendar is a polled projection, not a second meal database.

## Enable and use

Enable **Meal planning** in the integration's options. This creates a native
`calendar` entity for the account's meal plan. The existing private iCalendar
URL option remains separate and is not needed. Existing entries stay opted out
until deliberately enabled. Account subscription capabilities still apply.

The native calendar supports viewing only. Rich meal actions deliberately use
their own contract, rather than pretending meals are generic appointments.
State attributes `family_hub_kind: meal_plan` and `meal_plan_protocol: 1` let
Family Hub keep this calendar separate from appointment feeds.

All actions take the exact `entity_id` of the loaded AnyList meal calendar:

| Action | Additional fields | Response |
| --- | --- | --- |
| `anylist.get_meal_plan` | None | `calendar_id`, `can_edit`, `labels`, `events` |
| `anylist.create_meal` | Required `date`; `title` or `recipe_id`; optional `details`, `label_id`, `scale_factor` | Confirmed event with stable ID and version |
| `anylist.update_meal` | `event_id`, `expected_version`, and only fields being changed | Confirmed event with new version |
| `anylist.delete_meal` | `event_id`, `expected_version` | Confirmed absence |

Use `return_response: true` on the HA WebSocket `call_service` request.
Writes return `{status: "confirmed", event: {...}}`; deletion returns a null
event. Reads return public records containing ID, local date, title, resolved
summary, details, recipe ID, label ID, serving scale, and version. Raw provider
messages and credentials are never exposed. Labels include stable IDs, names,
colors and sort order. Undated queue records retain a null date and do not
appear as invented appointments in the native calendar.

Dates are `YYYY-MM-DD` calendar dates, not UTC timestamps. To move a meal, update
only its date. To remove a recipe or label association pass null; an empty
string clears details. A recipe or nonblank title must remain. Serving scale
must be finite, greater than zero, and at most 100. Recipe and label references
are checked against the same account before mutation.

## Permissions and failure behavior

Read actions require the actual HA user's read permission on the meal entity;
write actions require control. Inactive and removed users are denied. Authority
and entity identity are rechecked after waiting, after the provider preflight,
and before returning data. Calls without a user ID are trusted server-side HA
automations, as with native HA services. Client-selected household members and
the Family Hub PIN do not grant provider authority.

The preflight compares an expected record version before editing or deleting.
Edits preserve unknown fields, recipe links, serving scale and original IDs.
This detects earlier changes but **does not establish a server-side atomic
compare-and-swap guarantee**; another AnyList client can still race the request.

Success requires a complete provider snapshot and immediate readback by the exact event ID. A timeout,
missing record, conflicting result, or failed readback returns an uncertain
outcome. Refresh before deciding what to do; never automatically repeat a
mutation. The underlying client still refreshes its token after a definitive
401. Cancellation holds the operation lock until the socket-limited worker
finishes. External changes arrive at the configured polling interval, and a
confirmed local change requests a calendar refresh.

Existing recipe actions are retained (search, detail, creation, editing,
deletion, ingredient-to-list). They retain upstream's action authorization;
the new meal-entity permission checks do not retrofit those older actions.
Ingredient batches are not transactional: interruption can leave some items
added. No automatic replay or rollback is promised. Recipe-to-list quantities
now include the current AnyList quantity field and support scaling decimal,
fraction and mixed-number prefixes; descriptive quantities remain unchanged.

Todo rename uses `set-list-item-name`, preserving checked state, quantities,
categories, stores and other fields rather than reconstructing the item.
Native HA todo actions retain their entity authorization checks.

## Protocol evidence and verification boundary

- `phildenhoff/anylist_rs` at `0698dc9de81dd7a50856f2d890a5397277644251`:
  `src/meal_planning.rs` and `src/protobuf/anylist.proto` informed the wire layout.
- `bobby060/anylist-js` at `dc666c38393014b0bec4c5ce49e5c8ef443566a8`:
  `lib/meal-planning-calendar-event.js`, `lib/item.js`, and definitions informed
  single-event deletion, targeted rename, and modern item quantities.
- AnyList's public web application was inspected on 2026-09-27 at
  `https://www.anylist.com/static/webapp/js/app.min.js`, SHA-256
  `df401c1986a04e52ade67e0aad6b6952142559c61d9a386a710eb6a5cd3be3ed`.
  Its general save selects `new-event` or `update-event`; deletion passes the
  existing record as `updatedEvent`. Its `set-event-details` command is
  note-specific, so the JS reference is not copied blindly for general edits.
  Private feeds, credentials, household records and the downloaded bundle are
  excluded from this repository.

Tests run on actual HA Core 2026.9.3 / Python 3.14.7 with synthetic provider
records and intercepted network boundaries. They cover wire fields, retained
metadata, stale edits, missing/uncertain readbacks, real HA permissions,
revocation before dispatch, cancellation, options and platform load/unload.

Local result on 2026-09-27: **238 tests passed**, including all 194 upstream
tests and a real authenticated HA WebSocket read/denied-write check. Ruff's
undefined/unused checks pass on the new modules/tests. Run `pip install -r
requirements-test.txt` then `python -m pytest -q` with Python 3.14.

No live household AnyList login or provider mutation has been performed for
this fork. Add/move/edit/delete, both AnyList clients' refresh, account feature
availability, and ingredient scaling still require a dedicated live test meal
and shopping list. Passing local tests does not establish that live acceptance.
