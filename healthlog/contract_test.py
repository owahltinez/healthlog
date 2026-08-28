"""Small contracts for the Google Health read and write boundaries."""

import json
from datetime import UTC, datetime, timedelta, timezone

import httpx
from click.testing import CliRunner
from google.oauth2.credentials import Credentials
from mealtime_nutrients import CORE_NUTRIENTS, NUTRIENTS

from healthlog.auth import SCOPES, TOKEN_URI, get_auth_status, get_credentials
from healthlog.cli import _total, app, meal_json
from healthlog.client import GoogleHealthClient, GoogleHealthError
from healthlog.datatypes import (
    DATA_TYPES,
    FOOD_ID,
    SCOPE_PREFIX,
    by_id,
    noun,
)
from healthlog.models import (
    MealLog,
    MealType,
    TimeInterval,
    WeightLog,
    point_time,
)


def meal(**changes) -> MealLog:
    defaults = {
        "name": "Water",
        "meal_type": MealType.SNACK,
        "interval": TimeInterval.from_start(
            datetime(2026, 8, 22, 12, tzinfo=UTC)
        ),
        "kcal": 0,
    }
    return MealLog(**(defaults | changes))


def test_api_payload_omits_unknowns_and_keeps_explicit_zero() -> None:
    log = meal(grams=90)

    payload = log.to_api_payload()["nutritionLog"]
    assert payload["energy"] == {"kcal": 0}
    assert "totalFat" not in payload
    assert "totalCarbohydrate" not in payload
    assert "nutrients" not in payload
    assert payload["serving"] == {
        "amount": 90,
        "foodMeasurementUnitDisplayName": "gram",
    }


def test_vocabulary_is_the_shared_one() -> None:
    """One shared list of names, accepted whole and extended by none."""
    item = {"name": "Everything"} | dict.fromkeys(reversed(NUTRIENTS), 1)
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(item),
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert all(data[name] == 1 for name in NUTRIENTS)
    # However an item spelled them, the output states the shared order.
    assert [name for name in data if name in NUTRIENTS] == list(NUTRIENTS)


def test_payload_routing_follows_the_shared_mapping() -> None:
    """Dedicated objects for kcal, carbs and fat; protein is an array entry."""
    log = meal(kcal=100, protein=10, fat=5, carbs=20, nutrients={"fiber": 3})

    payload = log.to_api_payload()["nutritionLog"]

    assert payload["energy"] == {"kcal": 100}
    assert payload["totalCarbohydrate"] == {"grams": 20}
    assert payload["totalFat"] == {"grams": 5}
    assert payload["nutrients"] == [
        {"nutrient": "DIETARY_FIBER", "quantity": {"grams": 3}},
        {"nutrient": "PROTEIN", "quantity": {"grams": 10}},
    ]

    restored = MealLog.from_api_payload(log.to_api_payload())

    assert restored.protein == 10
    assert restored.nutrients == {"fiber": 3}


def test_written_spellings_reach_their_wire_name() -> None:
    result = CliRunner().invoke(
        app,
        "food log Spelled --calories 100 --protein 1 --fat 1 --carbs 1"
        " --nutrient saturated-fat=5 --nutrient FIBRE=3"
        " --dry-run --json".split(),
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["kcal"] == 100
    assert data["saturated_fat"] == 5
    assert data["fiber"] == 3


def test_api_payload_restores_recorded_utc_offset() -> None:
    offset = timezone(timedelta(hours=10))
    original = meal(
        interval=TimeInterval.from_start(
            datetime(2026, 8, 22, 21, 30, tzinfo=offset)
        )
    )
    payload = original.to_api_payload()
    interval = payload["nutritionLog"]["interval"]
    interval["startTime"] = "2026-08-22T11:30:00+00:00"
    interval["endTime"] = "2026-08-22T11:31:00+00:00"

    restored = MealLog.from_api_payload(payload)

    assert restored.interval.start.isoformat() == "2026-08-22T21:30:00+10:00"
    assert restored.interval.end.isoformat() == "2026-08-22T21:31:00+10:00"


def test_json_input_and_output_are_flat() -> None:
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(
            {
                "name": "Water",
                "kcal": 0,
                "protein": 0,
                "fat": 0,
                "carbs": 0,
                "sodium": None,
            }
        ),
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["kcal"] == 0
    assert data["protein"] == 0
    assert "sodium" not in data
    assert "nutrients" not in data


def test_item_carries_the_core_macros_and_nothing_unstated() -> None:
    """Absence and null mean the same, so an unstated nutrient is omitted."""
    item = {"name": "Water", "kcal": 0, "protein": 0, "fat": 0, "carbs": 0}
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(item),
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert all(data[name] == 0 for name in CORE_NUTRIENTS)
    assert [n for n in NUTRIENTS if n in data] == list(CORE_NUTRIENTS)


def test_item_carries_a_stated_nutrient_only() -> None:
    item = BARE_ITEM | {"fiber": 0, "saturated_fat": 2.1}
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(item),
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["fiber"] == 0
    assert data["saturated_fat"] == 2.1
    assert [n for n in NUTRIENTS if n in data] == [
        *CORE_NUTRIENTS,
        "fiber",
        "saturated_fat",
    ]


def test_legacy_core_macro_renders_as_zero() -> None:
    """Google Health can return an explicit zero as absent; only the four."""
    values = meal_json(meal(kcal=0, protein=None, fat=None, carbs=None))

    assert [values[name] for name in CORE_NUTRIENTS] == [0, 0, 0, 0]


def test_new_entry_requires_every_core_nutrient() -> None:
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps({"name": "Incomplete", "kcal": 1}),
    )

    assert result.exit_code == 1
    assert "need kcal, protein, fat, and carbs" in result.output


def test_piped_whole_item_keeps_total_nutrients() -> None:
    item = {
        "ok": True,
        "data": {
            "name": "Protein bar",
            "grams": 50,
            "kcal": 200,
            "protein": 20,
            "fat": 5,
            "carbs": 15,
        },
    }
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(item),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["protein"] == 20
    assert json.loads(result.output)["data"]["grams"] == 50


BARE_ITEM = {"name": "Typo", "kcal": 100, "protein": 1, "fat": 1, "carbs": 1}
TYPO_ITEM = BARE_ITEM | {"saturatd_fat": 5}


def test_bare_object_rejects_an_unknown_key_by_name() -> None:
    """A bare object is hand-authored, so a misspelling is a mistake."""
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(TYPO_ITEM),
    )

    assert result.exit_code == 1, result.output
    assert "saturatd_fat" in result.output


def test_bare_object_of_known_keys_logs() -> None:
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(BARE_ITEM | {"saturated_fat": 5}),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["saturated_fat"] == 5


def test_envelope_drops_an_unknown_key_in_silence() -> None:
    """The same typo from a tool is that tool's field, not a mistake here."""
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps({"ok": True, "data": TYPO_ITEM}),
    )

    assert result.exit_code == 0, result.output
    assert "saturatd_fat" not in json.loads(result.output)["data"]


# Verbatim `--json` payloads, so their wrapper keys stay pipeable.
SIBLING_ENVELOPES = {
    "pantry": {
        "found": True,
        "source": "afcd",
        "id": "F005580",
        "product": {
            "id": "F005580",
            "name": "Milk, cow, canned, evaporated, reduced fat (~2%)",
            "title": "Milk, cow, canned, evaporated, reduced fat (~2%)",
            "kcal": 90.8,
            "kj": 380.0,
            "protein": 7.8,
            "fat": 2.1,
            "carbs": 10.6,
            "fiber": 0,
            "sodium": None,
            "sugar": 10.6,
            "grams": 100,
            "source": "afcd",
        },
    },
    "eatout": {
        "generated_at": "2026-08-21T00:00:00.000Z",
        "count": 1,
        "candidates": [
            {
                "kind": "meal",
                "id": "cali-press-the-shredder-smoothie-regular",
                "name": "Cali Press - The Shredder Smoothie (Regular)",
                "kcal": 356,
                "protein": 31.8,
                "fat": 14.4,
                "carbs": 23.2,
                "fiber": None,
                "sodium": None,
                "sugar": None,
                "complete": True,
                "detail": {"restaurant": "Cali Press", "vegan": True},
            }
        ],
        "unverifiable": [],
    },
    "recipes": {
        "name": "Bean salad",
        "servings": 1,
        "tags": [],
        "notes": "",
        "grams": 350,
        "ingredients": [],
        "complete": True,
        "unresolved": [],
        "kcal": 420,
        "protein": 25,
        "fat": 12,
        "carbs": 48,
        "fiber": 9,
        "sodium": 0.4,
        "sugar": 6,
        "path": "/tmp/recipes/bean-salad.yaml",
    },
}


def test_sibling_envelopes_still_pipe_cleanly() -> None:
    for tool, data in SIBLING_ENVELOPES.items():
        result = CliRunner().invoke(
            app,
            ["food", "log", "--input", "-", "--dry-run", "--json"],
            input=json.dumps({"ok": True, "data": data}),
        )

        assert result.exit_code == 0, f"{tool}: {result.output}"
        assert json.loads(result.output)["data"]["kcal"] > 0, tool


def test_envelope_tolerates_a_field_this_version_never_saw() -> None:
    """The tools ship on their own schedules; a new field is not an error."""
    product = SIBLING_ENVELOPES["pantry"]["product"] | {"confidence": "high"}
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(
            {"ok": True, "data": {"found": True, "product": product}}
        ),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["kcal"] > 0


def test_payload_without_an_item_reports_the_missing_macros() -> None:
    """A Pantry miss keeps `product` null. The envelope makes it tool
    output, so the leftover wrapper keys are dropped, not reported."""
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(
            {
                "ok": True,
                "data": {
                    "found": False,
                    "source": "afcd",
                    "id": "NOSUCH",
                    "product": None,
                },
            }
        ),
    )

    assert result.exit_code == 1
    assert "need kcal, protein, fat, and carbs" in result.output


def test_carbohydrate_is_declared_once() -> None:
    """`carbs` owns `totalCarbohydrate`, so the enum spelling is not a key."""
    item = {"name": "Twice", "kcal": 100, "protein": 1, "fat": 1, "carbs": 25}

    bare = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--dry-run", "--json"],
        input=json.dumps(item | {"carbohydrates": 25}),
    )
    flag = CliRunner().invoke(
        app,
        "food log Twice --kcal 100 --protein 1 --fat 1 --carbs 25"
        " --nutrient carbohydrates=25 --dry-run --json".split(),
    )

    assert bare.exit_code == 1, bare.output
    assert "carbohydrates" in bare.output
    assert flag.exit_code == 1, flag.output
    assert "carbohydrates" in flag.output


def test_read_back_never_adds_a_second_carbohydrate() -> None:
    """CARBOHYDRATES is unreachable, so a foreign entry is not read as one."""
    payload = meal(carbs=25).to_api_payload()
    payload["nutritionLog"]["nutrients"] = [
        {"nutrient": "CARBOHYDRATES", "quantity": {"grams": 25}}
    ]

    restored = MealLog.from_api_payload(payload)

    assert restored.carbs == 25
    assert restored.nutrients == {}


def test_client_posts_one_nutrition_log() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "done": True,
                "response": seen["body"] | {"name": "users/me/dataPoints/123"},
            },
        )

    client = GoogleHealthClient(
        credentials=Credentials(token="token"),
        transport=httpx.MockTransport(handler),
    )
    saved = client.log_meal(meal())

    assert seen["path"].endswith("/nutrition-log/dataPoints")
    assert seen["body"]["nutritionLog"]["energy"] == {"kcal": 0}
    assert saved.id == "123"
    # Only what this version writes asks for a write scope.
    assert sorted(s for s in SCOPES if s.endswith("writeonly")) == sorted(
        [
            f"{SCOPE_PREFIX}nutrition.writeonly",
            f"{SCOPE_PREFIX}health_metrics_and_measurements.writeonly",
        ]
    )


def test_client_filters_food_log_with_utc_range() -> None:
    current = meal(
        interval=TimeInterval.from_start(datetime(2026, 8, 16, 12, tzinfo=UTC))
    )
    outside = meal(
        name="Coffee",
        interval=TimeInterval.from_start(datetime(2026, 8, 18, 0, tzinfo=UTC)),
    )
    start = datetime(2026, 8, 16, tzinfo=UTC)
    end = datetime(2026, 8, 18, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "filter" not in request.url.params
        return httpx.Response(
            200,
            json={
                "dataPoints": [
                    outside.to_api_payload(),
                    current.to_api_payload(),
                ]
            },
        )

    client = GoogleHealthClient(
        credentials=Credentials(token="token"),
        transport=httpx.MockTransport(handler),
    )

    assert [entry.name for entry in client.history(start, end)] == ["Water"]


def test_history_command_accepts_dates_and_datetimes(monkeypatch) -> None:
    class Client:
        interval: tuple[datetime, datetime] | None = None

        def history(self, start: datetime, end: datetime) -> list[MealLog]:
            Client.interval = (start, end)
            return []

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    runner = CliRunner()

    result = runner.invoke(
        app, ["food", "history", "2026-08-16", "2026-08-17", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert Client.interval == (
        datetime(2026, 8, 16).astimezone().astimezone(UTC),
        datetime(2026, 8, 18).astimezone().astimezone(UTC),
    )

    result = runner.invoke(
        app,
        [
            "food",
            "history",
            "2026-08-16T20:00:00+10:00",
            "2026-08-17T01:00:00+10:00",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert Client.interval == (
        datetime(2026, 8, 16, 10, tzinfo=UTC),
        datetime(2026, 8, 16, 15, tzinfo=UTC),
    )


def test_duplicate_never_deletes_and_delete_is_separate(monkeypatch) -> None:
    source = meal(
        id="old",
        name="Ginger beer",
        protein=None,
        fat=0,
        carbs=47,
    )

    class Client:
        saved: MealLog | None = None
        deleted: str | None = None

        def get_meal(self, point_id: str) -> MealLog:
            assert point_id == "old"
            return source

        def log_meal(self, value: MealLog) -> MealLog:
            Client.saved = value
            value.id = "new"
            return value

        def delete_meal(self, point_id: str) -> None:
            Client.deleted = point_id

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    result = CliRunner().invoke(
        app,
        [
            "food",
            "duplicate",
            "old",
            "--protein",
            "0",
            "--grams",
            "90",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert Client.saved is not None
    assert Client.saved.protein == 0
    assert Client.saved.carbs == 47
    assert Client.saved.grams == 90
    assert Client.saved.interval == source.interval
    assert Client.deleted is None
    assert json.loads(result.output)["data"]["id"] == "new"

    Client.deleted = None
    result = CliRunner().invoke(
        app,
        ["food", "duplicate", "old", "--time", "2026-08-22T22:30:00+10:00"],
    )

    assert result.exit_code == 0, result.output
    assert Client.saved is not None
    assert Client.saved.interval.start.isoformat() == (
        "2026-08-22T22:30:00+10:00"
    )
    assert Client.deleted is None

    result = CliRunner().invoke(
        app, ["food", "delete", "old", "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert Client.deleted == "old"


def test_history_totals_cover_the_core_and_stated_nutrients(
    monkeypatch,
) -> None:
    class Client:
        def history(self, start: datetime, end: datetime) -> list[MealLog]:
            return [
                meal(kcal=100, protein=10, fat=1, carbs=5),
                meal(
                    kcal=200,
                    protein=20,
                    fat=2,
                    carbs=6,
                    nutrients={"sugar": 3},
                ),
            ]

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    result = CliRunner().invoke(app, ["food", "history", "--json"])

    assert result.exit_code == 0, result.output
    totals = json.loads(result.output)["data"]["totals"]
    assert totals == {
        "kcal": 300,
        "protein": 30,
        "fat": 3,
        "carbs": 11,
        "sugar": 3,
    }


def test_history_totals_omit_a_nutrient_no_entry_states(monkeypatch) -> None:
    class Client:
        def history(self, start: datetime, end: datetime) -> list[MealLog]:
            return [meal(kcal=100, protein=10, fat=1, carbs=5)]

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    result = CliRunner().invoke(app, ["food", "history", "--json"])

    assert result.exit_code == 0, result.output
    totals = json.loads(result.output)["data"]["totals"]
    assert set(totals) == set(CORE_NUTRIENTS)


def test_totals_sum_reported_values_and_ignore_null() -> None:
    meals = [
        {"protein": 10.0},
        {"protein": None},
        {"protein": 5.0},
    ]

    assert _total(meals, "protein") == 15.0
    assert _total([{"protein": None}], "protein") is None
    assert _total([], "protein") is None


def test_payload_key_follows_the_kebab_id() -> None:
    """The API keys a payload by the camelCase form of the type's own id."""
    keys = {value.id: value.payload_key for value in DATA_TYPES}

    assert keys["nutrition-log"] == "nutritionLog"
    assert keys["weight"] == "weight"
    assert keys["active-energy-burned"] == "activeEnergyBurned"
    # An acronym keeps the API's own casing rather than a shouted one.
    assert keys["vo2-max"] == "vo2Max"
    assert keys["run-vo2-max"] == "runVo2Max"
    assert keys["daily-vo2-max"] == "dailyVo2Max"


def test_read_scopes_cover_every_readable_type() -> None:
    """A type this version lists is a type the login already asked for."""
    for value in DATA_TYPES:
        readonly = f"{SCOPE_PREFIX}{value.scope}.readonly"
        writeonly = f"{SCOPE_PREFIX}{value.scope}.writeonly"
        assert readonly in SCOPES or writeonly in SCOPES, value.id


def test_point_time_reads_every_record_shape() -> None:
    """Four shapes state a time; reference data states none."""
    interval = point_time(
        {
            "interval": {
                "startTime": "2026-08-22T11:30:00Z",
                "startUtcOffset": "36000s",
            }
        }
    )
    sample = point_time(
        {
            "sampleTime": {
                "physicalTime": "2026-08-22T11:30:00Z",
                "utcOffset": "36000s",
            }
        }
    )
    daily = point_time({"date": {"year": 2026, "month": 8, "day": 22}})

    assert interval is not None
    assert interval.isoformat() == "2026-08-22T21:30:00+10:00"
    assert sample is not None
    assert sample.isoformat() == "2026-08-22T21:30:00+10:00"
    assert daily is not None
    assert daily.date().isoformat() == "2026-08-22"
    assert point_time({"displayName": "Oats"}) is None


def sample_point(day: int, *, name: str = "p", key: str = "weight") -> dict:
    return {
        "name": f"users/me/dataTypes/{key}/dataPoints/{name}{day}",
        key: {
            "sampleTime": {
                "physicalTime": f"2026-08-{day:02d}T00:00:00Z",
                "utcOffset": "0s",
            },
            "weightGrams": 82000 + day,
        },
    }


def paged_transport(
    pages: list[list[dict]], seen: dict
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault("requests", []).append(dict(request.url.params))
        index = int(request.url.params.get("pageToken") or 0)
        body: dict = {"dataPoints": pages[index]}
        if index + 1 < len(pages):
            body["nextPageToken"] = str(index + 1)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def read_client(pages: list[list[dict]], seen: dict) -> GoogleHealthClient:
    return GoogleHealthClient(
        credentials=Credentials(token="token"),
        transport=paged_transport(pages, seen),
    )


def test_pager_reads_on_until_a_page_predates_the_range() -> None:
    """Points arrive newest first, so an all-older page ends the walk."""
    seen: dict = {}
    pages = [
        [sample_point(20), sample_point(19)],
        [sample_point(18), sample_point(17)],
        [sample_point(2), sample_point(1)],
        [sample_point(1, name="never")],
    ]
    client = read_client(pages, seen)

    points = client.points(
        by_id("weight"),
        datetime(2026, 8, 17, tzinfo=UTC),
        datetime(2026, 8, 21, tzinfo=UTC),
    )

    ids = [point["name"].split("/")[-1] for point in points]
    assert ids == ["p20", "p19", "p18", "p17"]
    # The fourth page is never fetched: the third already fell out of range.
    assert len(seen["requests"]) == 3


def test_pager_stops_at_the_limit_and_says_so() -> None:
    seen: dict = {}
    pages = [[sample_point(20), sample_point(19), sample_point(18)]]
    client = read_client(pages, seen)

    points = client.points(
        by_id("weight"),
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 21, tzinfo=UTC),
        limit=2,
    )

    assert len(points) == 2


def test_pager_asks_for_the_page_size_the_type_allows() -> None:
    """Sessions cap far lower than samples, and the API truncates silently."""
    seen: dict = {}
    read_client([[]], seen).points(
        by_id("sleep"),
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert seen["requests"][0]["pageSize"] == "25"
    assert by_id("weight").page_size == 10000


def test_generic_history_keeps_the_payload_verbatim(monkeypatch) -> None:
    class Client:
        asked: tuple[str, int] | None = None

        def points(self, data_type, start, end, limit=0) -> list[dict]:
            Client.asked = (data_type.id, limit)
            return [sample_point(20, key="bodyFat")]

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    result = CliRunner().invoke(app, ["body-fat", "history", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert Client.asked is not None
    assert Client.asked[0] == "body-fat"
    assert data["count"] == 1
    assert data["points"][0]["id"] == "p20"
    assert data["points"][0]["time"] == "2026-08-20T00:00:00+00:00"
    # Whatever the record held, it survives unread and unreshaped.
    assert data["points"][0]["data"]["weightGrams"] == 82020


def test_generic_history_reports_a_truncated_read(monkeypatch) -> None:
    """A capped read that looked complete would be read as a full day."""

    class Client:
        def points(self, data_type, start, end, limit=0) -> list[dict]:
            return [
                sample_point(day, key="bodyFat")
                for day in range(20, 20 - limit, -1)
            ]

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    result = CliRunner().invoke(
        app, ["body-fat", "history", "--limit", "2", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["truncated"] is True


def test_every_readable_type_answers_history(monkeypatch) -> None:
    """One grammar over every type, so no noun is a special case."""

    class Client:
        def points(self, data_type, start, end, limit=0) -> list[dict]:
            return []

        def history(self, start, end) -> list[MealLog]:
            return []

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    # The nutrition log answers to `food`, the name it is logged under.
    nouns = ["food"] + [
        value.id for value in DATA_TYPES if value.id != FOOD_ID
    ]

    for word in nouns:
        result = CliRunner().invoke(app, [word, "history", "--json"])

        assert result.exit_code == 0, f"{word}: {result.output}"
        assert json.loads(result.output)["data"]["count"] == 0


def test_food_is_the_nutrition_log_under_its_own_name() -> None:
    assert by_id("food") is by_id("nutrition-log")


def test_types_lists_what_can_be_read() -> None:
    result = CliRunner().invoke(app, ["types", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    listed = {value["noun"] for value in data["types"]}
    assert listed == {noun(value) for value in DATA_TYPES}
    assert "food" in listed
    assert "nutrition-log" not in listed
    assert "weight" in listed
    assert "sleep" in listed
    # Reference data and write-only types have no history to read.
    assert "food-measurement-unit" not in listed
    assert "moods" not in listed


def test_a_missing_scope_says_how_to_grant_it(monkeypatch) -> None:
    """Every token predating a new type reads it as a bare 403 otherwise."""

    class Client:
        def points(self, data_type, start, end, limit=0) -> list[dict]:
            raise GoogleHealthError(
                "Google Health returned 403: Required OAuth scope(s) are "
                "missing for this operation."
            )

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    result = CliRunner().invoke(app, ["weight", "history", "--json"])

    assert result.exit_code != 0
    assert "auth login" in json.loads(result.output)["error"]["message"]


def test_a_narrow_token_survives_a_new_scope(monkeypatch) -> None:
    """Adding a data type must not strand the food log behind a re-login."""
    granted = [f"{SCOPE_PREFIX}nutrition.writeonly"]
    monkeypatch.setattr(
        "healthlog.auth.load_tokens",
        lambda: {
            "token": "token",
            "refresh_token": "refresh",
            "token_uri": TOKEN_URI,
            "client_id": "id",
            "client_secret": "secret",
            "scopes": granted,
            # An absent expiry counts as expired, and would try a refresh.
            "expiry": "2099-01-01T00:00:00",
        },
    )

    creds = get_credentials()

    assert creds is not None
    # Asking for more than was granted makes Google refuse the refresh.
    assert creds.scopes == granted
    assert set(granted) < set(SCOPES)
    assert get_auth_status()["missing_scopes"] == sorted(
        set(SCOPES) - set(granted)
    )


def test_grams_may_not_contradict_the_stated_basis() -> None:
    """Nutrients describe the item's own grams, so another weight relabels."""
    item = {"ok": True, "data": BARE_ITEM | {"grams": 100, "sugar": 10.6}}
    result = CliRunner().invoke(
        app,
        ["food", "log", "--input", "-", "--grams", "250", "--dry-run"],
        input=json.dumps(item),
    )

    assert result.exit_code == 1, result.output
    assert "100" in result.output and "250" in result.output


def test_grams_agreeing_with_the_basis_is_no_contradiction() -> None:
    item = {"ok": True, "data": BARE_ITEM | {"grams": 100}}
    result = CliRunner().invoke(
        app,
        [
            "food",
            "log",
            "--input",
            "-",
            "--grams",
            "100",
            "--dry-run",
            "--json",
        ],
        input=json.dumps(item),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["grams"] == 100


def test_an_item_stating_no_basis_takes_the_given_weight() -> None:
    """An Eatout meal states absolute macros and no weight, so naming one
    records what was eaten rather than reinterpreting anything."""
    result = CliRunner().invoke(
        app,
        [
            "food",
            "log",
            "--input",
            "-",
            "--grams",
            "250",
            "--dry-run",
            "--json",
        ],
        input=json.dumps({"ok": True, "data": BARE_ITEM}),
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["grams"] == 250
    assert data["kcal"] == BARE_ITEM["kcal"]


def test_duplicate_refuses_to_relabel_a_weight(monkeypatch) -> None:
    source = meal(id="old", protein=1, fat=1, carbs=1, grams=90)

    class Client:
        saved: MealLog | None = None

        def get_meal(self, point_id: str) -> MealLog:
            return source

        def log_meal(self, value: MealLog) -> MealLog:
            Client.saved = value
            return value

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    result = CliRunner().invoke(
        app, ["food", "duplicate", "old", "--grams", "250"]
    )

    assert result.exit_code == 1, result.output
    assert "90" in result.output and "250" in result.output
    assert Client.saved is None


def test_weight_states_grams_at_the_sampled_time() -> None:
    log = WeightLog(kg=82.4, sampled=datetime(2026, 8, 28, 7, tzinfo=UTC))

    payload = log.to_api_payload()["weight"]

    assert payload["weightGrams"] == 82400
    assert payload["sampleTime"]["physicalTime"].startswith("2026-08-28T07:00")
    assert payload["sampleTime"]["utcOffset"] == "0s"

    restored = WeightLog.from_api_payload(log.to_api_payload())

    assert restored.kg == 82.4


def test_weight_log_needs_a_unit() -> None:
    """A defaulted unit logs 181 kg for someone who meant pounds."""
    result = CliRunner().invoke(app, ["weight", "log", "181", "--dry-run"])

    assert result.exit_code != 0
    assert "unit" in result.output.lower()


def test_weight_log_converts_each_unit() -> None:
    cases = {("82.4", "kg"): 82.4, ("181", "lb"): 82.1, ("181", "LB"): 82.1}
    for (value, unit), expected in cases.items():
        result = CliRunner().invoke(
            app,
            ["weight", "log", value, "--unit", unit, "--dry-run", "--json"],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert round(data["kg"], 1) == expected, (value, unit)
        # One stored truth, whatever unit stated it.
        assert data["grams"] == round(data["kg"] * 1000, 3)


def test_only_the_standard_unit_symbols_are_accepted() -> None:
    """A unit symbol takes no plural, so `lbs` is not one."""
    for unit in ("lbs", "kgs", "pounds", "kilos"):
        result = CliRunner().invoke(
            app, ["weight", "log", "82", "--unit", unit, "--dry-run"]
        )

        assert result.exit_code != 0, unit
        assert "'kg', 'lb'" in result.output, unit


def test_weight_outside_human_range_is_refused() -> None:
    """A backstop for a slipped digit, not for a wrong unit: 181 kg is a
    weight a person can have, so no range check can catch that one."""
    for value in ("8.2", "620"):
        result = CliRunner().invoke(
            app,
            ["weight", "log", value, "--unit", "kg", "--dry-run"],
        )

        assert result.exit_code == 1, result.output
        assert "kg" in result.output


def test_weight_log_posts_to_the_weight_type() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"response": seen["body"] | {"name": "users/me/w/9"}},
        )

    client = GoogleHealthClient(
        credentials=Credentials(token="token"),
        transport=httpx.MockTransport(handler),
    )
    saved = client.log_weight(
        WeightLog(kg=82.4, sampled=datetime(2026, 8, 28, 7, tzinfo=UTC))
    )

    assert seen["path"].endswith("/weight/dataPoints")
    assert seen["body"]["weight"]["weightGrams"] == 82400
    assert saved.id == "9"


def test_writing_weight_asks_for_the_scope_it_needs() -> None:
    assert f"{SCOPE_PREFIX}health_metrics_and_measurements.writeonly" in SCOPES


def test_weight_history_renders_the_unit_asked_for(monkeypatch) -> None:
    point = {
        "name": "users/me/dataTypes/weight/dataPoints/w1",
        "weight": {
            "sampleTime": {
                "physicalTime": "2026-08-28T00:00:00Z",
                "utcOffset": "0s",
            },
            "weightGrams": 82400,
        },
    }

    class Client:
        def points(self, data_type, start, end, limit=0) -> list[dict]:
            return [point]

    monkeypatch.setattr("healthlog.cli.GoogleHealthClient", Client)
    kg = CliRunner().invoke(app, ["weight", "history", "--json"])
    lb = CliRunner().invoke(
        app, ["weight", "history", "--unit", "lb", "--json"]
    )

    assert kg.exit_code == 0, kg.output
    assert json.loads(kg.output)["data"]["readings"][0]["kg"] == 82.4
    assert lb.exit_code == 0, lb.output
    assert (
        round(json.loads(lb.output)["data"]["readings"][0]["lb"], 1) == 181.7
    )
