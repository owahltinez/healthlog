"""One grammar over every data type: a noun, then a verb."""

import json
import math
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, TextIO

import click
from agentcli import (
    JsonAwareGroup,
    RemoteError,
    UsageError,
    emit,
    json_option,
    limit_option,
    skill_group,
)
from mealtime_nutrients import (
    CORE_NUTRIENTS,
    NUTRIENTS,
    OPTIONAL_NUTRIENTS,
    UNREACHABLE_NUTRIENT_TYPES,
)

from healthlog import __version__, auth
from healthlog.client import GoogleHealthClient, GoogleHealthError
from healthlog.datatypes import (
    DATA_TYPES,
    FOOD_ID,
    PROMINENT,
    DataType,
    by_id,
    noun,
)
from healthlog.models import (
    CM_PER_INCH,
    CM_PER_M,
    GRAMS_PER_KG,
    HEIGHT_UNITS,
    KG_PER_LB,
    MM_PER_CM,
    WEIGHT_UNITS,
    HeightLog,
    MealLog,
    MealType,
    TimeInterval,
    WeightLog,
    point_time,
)

ITEM_FIELDS = ("name", "meal_type", "time", "grams")
# Every key an item may state, ordered so output follows the shared order.
INPUT_FIELDS = (*ITEM_FIELDS, *NUTRIENTS)
# Written spellings that are not the wire name.
NUTRIENT_ALIASES = {"calories": "kcal", "fibre": "fiber"}
NUTRIENT_ARGUMENT = re.compile(r"^([^=]+)=([^=]+)$")

EPILOG = """Every data type reads the same way:

\b
  healthlog food history yesterday
  healthlog weight history 2026-08-01 2026-08-27
  healthlog types                     # every type that can be read
"""


def _remote(exc: GoogleHealthError) -> RemoteError:
    """A remote failure, naming the one cause a person can act on.

    Reading a type the stored token never asked for is the expected failure
    after a release adds data types, and the API only says "missing scope".
    """
    message = str(exc)
    if "403" in message:
        message += " Run 'healthlog auth login' to grant the missing scope."
    return RemoteError(message)


def _nutrient_name(value: str) -> str:
    """A written nutrient spelling reduced to its wire name."""
    key = re.sub(r"[\s-]+", "_", value.strip().lower())
    return NUTRIENT_ALIASES.get(key, key)


def _number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise UsageError(f"{field} must be a non-negative number or null")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UsageError(
            f"{field} must be a non-negative number or null"
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise UsageError(f"{field} must be a non-negative number or null")
    return number


def _time(value: Any) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise UsageError("time must be an ISO 8601 date-time") from exc
    return parsed if parsed.tzinfo else parsed.astimezone()


def _input(stream: TextIO | None) -> dict[str, Any]:
    if stream is None:
        return {}
    try:
        value = json.load(stream)
    except json.JSONDecodeError as exc:
        raise UsageError(f"input is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise UsageError("input must contain one JSON object")

    # Unwrap a JSON envelope or `product` wrapper, so output can be piped in.
    piped = "ok" in value
    if isinstance(value.get("data"), dict):
        value, piped = value["data"], True
    candidates = value.get("candidates")
    if isinstance(candidates, list) and len(candidates) == 1:
        value, piped = candidates[0], True
    if isinstance(value.get("product"), dict):
        value, piped = value["product"], True

    # Only a bare object is hand-authored, so only it reports an unknown key.
    unknown = sorted(set(value) - set(INPUT_FIELDS))
    if unknown and not piped:
        label = "key" if len(unknown) == 1 else "keys"
        raise UsageError(f"unknown input {label}: {', '.join(unknown)}")
    return {key: value[key] for key in INPUT_FIELDS if key in value}


def _nutrients(
    input_data: dict[str, Any], arguments: tuple[str, ...]
) -> tuple[dict[str, float | None], dict[str, float]]:
    combined = {
        key: value for key, value in input_data.items() if key in NUTRIENTS
    }
    for argument in arguments:
        match = NUTRIENT_ARGUMENT.fullmatch(argument.strip())
        if not match:
            raise UsageError("nutrients use NAME=GRAMS")
        combined[match.group(1).strip()] = match.group(2).strip()

    core: dict[str, float | None] = {}
    result: dict[str, float] = {}
    for name, value in combined.items():
        key = _nutrient_name(str(name))
        # Carbohydrate is `carbs`; a second spelling would declare it twice.
        if key.upper() in UNREACHABLE_NUTRIENT_TYPES:
            raise UsageError(f"use carbs, not {name}")
        if key in CORE_NUTRIENTS:
            core[key] = _number(value, key)
            continue
        if key not in NUTRIENTS:
            raise UsageError(f"unknown nutrient: {name}")
        number = _number(value, str(name))
        if number is not None:
            result[key] = number
    return core, result


def _value(flag: Any, input_data: dict[str, Any], *names: str) -> Any:
    if flag is not None:
        return flag
    for name in names:
        if name in input_data:
            return input_data[name]
    return None


def build_meal(
    *,
    name: str | None,
    input_data: dict[str, Any],
    kcal: float | None,
    protein: float | None,
    fat: float | None,
    carbs: float | None,
    grams: float | None,
    nutrient_args: tuple[str, ...],
    meal_type: str | None,
    consumed_at: str | None,
) -> MealLog:
    core, nutrients = _nutrients(input_data, nutrient_args)
    supplied = {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs}
    values = {
        key: _number(supplied[key], key)
        if supplied[key] is not None
        else core.get(key)
        for key in CORE_NUTRIENTS
    }
    missing = [key for key, value in values.items() if value is None]
    if missing:
        raise UsageError("new entries need kcal, protein, fat, and carbs")

    resolved_type = str(_value(meal_type, input_data, "meal_type") or "")
    resolved_grams = _number(_value(grams, input_data, "grams"), "grams")
    if resolved_grams == 0:
        raise UsageError("grams must be positive")

    # Nutrients describe the weight stated, so another relabels them.
    basis = _number(input_data.get("grams"), "grams")
    if basis is not None and resolved_grams != basis:
        raise UsageError(
            f"the item states {basis:g} g, so --grams "
            f"{resolved_grams:g} would relabel its nutrients rather than "
            f"convert them. Ask the source for {resolved_grams:g} g, as "
            f"with `pantry lookup --grams {resolved_grams:g}`, or state "
            "every nutrient here instead."
        )

    return MealLog(
        name=str(_value(name, input_data, "name") or "Meal"),
        meal_type=MealType.from_string(resolved_type),
        interval=TimeInterval.from_start(
            _time(_value(consumed_at, input_data, "time"))
        ),
        kcal=values["kcal"],
        protein=values["protein"],
        fat=values["fat"],
        carbs=values["carbs"],
        grams=resolved_grams,
        nutrients=nutrients,
    )


def meal_json(meal: MealLog) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": meal.id,
        "name": meal.name,
        "meal_type": meal.meal_type.value,
        "time": meal.interval.start.isoformat(),
    }
    # Legacy Google entries may omit a core macro; render those as zero.
    values.update({name: getattr(meal, name) or 0 for name in CORE_NUTRIENTS})
    # Every other nutrient appears only where the meal states a figure.
    values.update(meal.nutrients)
    if meal.grams is not None:
        values["grams"] = meal.grams
    return values


def _human(data: dict[str, Any]) -> list[str]:
    def value(key: str) -> str:
        value = data[key]
        return "?" if value is None else str(value)

    return [
        f"{data['name']} ({data['meal_type']})",
        "  ".join(f"{key} {value(key)}" for key in CORE_NUTRIENTS),
        data["time"],
    ]


def _total(meals: list[dict[str, Any]], key: str) -> float | None:
    values = [meal[key] for meal in meals if meal.get(key) is not None]
    return sum(values) if values else None


def _history_human(data: dict[str, Any]) -> list[str]:
    if not data["meals"]:
        return ["No food logged."]
    lines = []
    for meal in data["meals"]:
        consumed = datetime.fromisoformat(meal["time"]).strftime(
            "%Y-%m-%d %H:%M"
        )
        macros = "  ".join(f"{key} {meal[key]}" for key in CORE_NUTRIENTS)
        lines.append(f"{consumed}  {meal['name']}  {macros}")
    summary = data["totals"]
    lines.append(
        "Total  "
        + "  ".join(
            f"{key} {summary[key] if summary[key] is not None else '?'}"
            for key in CORE_NUTRIENTS
        )
    )
    return lines


def _history_value(value: str | None) -> date | datetime:
    today = datetime.now().astimezone().date()
    if value is None or value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UsageError(f"invalid date or datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise UsageError("history datetimes must include a UTC offset")
    return parsed


def _utc_bound(value: date | datetime, *, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if end:
        value += timedelta(days=1)
    return datetime.combine(value, time.min).astimezone().astimezone(UTC)


def _bounds(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    """The UTC interval a pair of written bounds selects.

    Every type reads the same way, so one reading of the bounds serves them
    all: no values means today, and one date means that local calendar day.
    """
    start_value = _history_value(start)
    if end is None and isinstance(start_value, datetime):
        raise UsageError("a history datetime needs an end datetime or date")
    end_value = _history_value(end) if end else start_value
    start_time = _utc_bound(start_value)
    end_time = _utc_bound(end_value, end=True)
    if end_time <= start_time:
        raise UsageError("history end must follow start")
    return start_time, end_time


HISTORY_HELP = """Show data points in a date or time range.

Values may be dates or offset-aware ISO datetimes. With no values, show
today; one value must be a date and selects that local calendar day. An
end date is inclusive; an end datetime is exclusive.
"""

# A cap bounds a dense type's history; truncation is always reported.
DEFAULT_LIMIT = 500

# Containers stating when a point happened, rather than what it recorded.
TIME_KEYS = ("interval", "sampleTime", "date")


def point_json(data_type: DataType, point: dict[str, Any]) -> dict[str, Any]:
    """One data point, keeping the record the API stated verbatim.

    Only the time is lifted out, because every type states one somewhere and
    a caller sorting or filtering should not have to know which shape.
    """
    payload = point.get(data_type.payload_key) or {}
    moment = point_time(payload)
    return {
        "id": (point.get("name") or "").split("/")[-1] or None,
        "time": moment.isoformat() if moment else None,
        "data": payload,
    }


def _figures(payload: dict[str, Any]) -> str:
    """A record's own figures on one line, whatever shape it has."""
    parts = []
    for key, value in payload.items():
        if key in TIME_KEYS:
            continue
        # A quantity is keyed by its unit, which a reader cannot guess.
        if isinstance(value, dict) and len(value) == 1:
            ((unit, only),) = value.items()
            parts.append(f"{key}={only} {unit}")
        elif isinstance(value, str | int | float | bool):
            parts.append(f"{key}={value}")
        else:
            parts.append(f"{key}=...")
    return "  ".join(parts) or "(no figures)"


def _points_human(data: dict[str, Any]) -> list[str]:
    if not data["points"]:
        return ["No data points."]
    lines = [
        f"{point['time'] or '?'}  {_figures(point['data'])}"
        for point in data["points"]
    ]
    if data["truncated"]:
        lines.append(
            f"Stopped at {data['count']} points. "
            "Pass --limit 0 to read every one."
        )
    return lines


# A person can weigh 181 kg: this catches a slipped digit, not a unit.
MIN_KG, MAX_KG = 20.0, 500.0

UNIT_HELP = "Required: a default would log pounds as kilos."
UNIT_CHOICE = click.Choice(list(WEIGHT_UNITS), case_sensitive=False)

LATEST_HELP = """Show the most recent reading, however old it is.

A history covers a range, so a value recorded years ago and unchanged since
reads as no value at all. This asks what it is rather than what changed.
"""


def _latest_point(data_type: DataType) -> dict[str, Any] | None:
    try:
        return GoogleHealthClient().latest(data_type)
    except GoogleHealthError as exc:
        raise _remote(exc) from exc


def emit_none(what: str, json_output: bool) -> None:
    """Nothing recorded, said once and the same way everywhere."""
    emit(
        {"count": 0, what: None},
        json_output=json_output,
        human=lambda data: [f"No {what} recorded."],
    )


def _kg(value: float, unit: str) -> float:
    """A stated weight in kilograms, whatever unit stated it."""
    kilos = value * WEIGHT_UNITS[unit]
    if MIN_KG <= kilos <= MAX_KG:
        return kilos
    # Skip the conversion for kg, which would read "8.2 kg is 8.2 kg".
    stated = "" if WEIGHT_UNITS[unit] == 1.0 else f"{value:g} {unit} is "
    raise UsageError(
        f"{stated}{kilos:.1f} kg, outside {MIN_KG:g}-{MAX_KG:g} kg. "
        "State the weight you mean."
    )


def weight_json(weight: WeightLog) -> dict[str, Any]:
    return {
        "id": weight.id,
        "kg": weight.kg,
        "lb": weight.kg / KG_PER_LB,
        # The figure Google Health actually stores, so a reader can check it.
        "grams": round(weight.kg * GRAMS_PER_KG, 3),
        "time": weight.sampled.isoformat(),
    }


@click.group("weight")
def weight_app() -> None:
    """Record and read body weight."""


@weight_app.command("log")
@click.argument("value", type=float)
@click.option(
    "--unit",
    type=UNIT_CHOICE,
    required=True,
    help=UNIT_HELP,
)
@click.option("--time", "measured_at", help="ISO 8601 date-time.")
@click.option("--dry-run", is_flag=True)
@json_option
def weight_log(
    value: float,
    unit: str,
    measured_at: str | None,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Record a body weight, stating the unit it is in."""
    weight = WeightLog(kg=_kg(value, unit), sampled=_time(measured_at))
    if not dry_run:
        try:
            weight = GoogleHealthClient().log_weight(weight)
        except GoogleHealthError as exc:
            raise _remote(exc) from exc
    emit(
        weight_json(weight),
        json_output=json_output,
        human=lambda data: [
            f"{data['kg']:.1f} kg ({data['lb']:.1f} lb)  {data['time']}"
        ],
    )


@weight_app.command("history", help=HISTORY_HELP)
@click.argument("start", required=False)
@click.argument("end", required=False)
@click.option("--unit", type=UNIT_CHOICE, default="kg", show_default=True)
@limit_option(default=DEFAULT_LIMIT)
@json_option
def weight_history(
    start: str | None,
    end: str | None,
    unit: str,
    limit: int,
    json_output: bool,
) -> None:
    start_time, end_time = _bounds(start, end)
    try:
        points = GoogleHealthClient().points(
            by_id("weight"), start_time, end_time, limit=limit
        )
    except GoogleHealthError as exc:
        raise _remote(exc) from exc
    readings = [
        weight_json(WeightLog.from_api_payload(point)) for point in points
    ]
    # A display unit cannot corrupt what is stored, so this one may default.
    shown = "lb" if WEIGHT_UNITS[unit.lower()] != 1.0 else "kg"
    emit(
        {"count": len(readings), "unit": shown, "readings": readings},
        json_output=json_output,
        human=lambda data: (
            [
                f"{r['time'][:16].replace('T', ' ')}  {r[data['unit']]:.1f}"
                f" {data['unit']}"
                for r in data["readings"]
            ]
            or ["No weight recorded."]
        ),
    )


@weight_app.command("delete")
@click.argument("point_id")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@json_option
def weight_delete(point_id: str, yes: bool, json_output: bool) -> None:
    """Delete one weight reading explicitly."""
    if json_output and not yes:
        raise UsageError("delete --json needs --yes")
    if not yes and not click.confirm(f"Delete weight {point_id}?"):
        click.echo("Deletion cancelled.")
        return
    try:
        GoogleHealthClient().delete_point(by_id("weight"), point_id)
    except GoogleHealthError as exc:
        raise _remote(exc) from exc
    emit(
        {"id": point_id, "deleted": True},
        json_output=json_output,
        human=lambda data: [f"Deleted {data['id']}."],
    )


@weight_app.command("latest", help=LATEST_HELP)
@json_option
def weight_latest(json_output: bool) -> None:
    point = _latest_point(by_id("weight"))
    if point is None:
        emit_none("weight", json_output)
        return
    emit(
        weight_json(WeightLog.from_api_payload(point)),
        json_output=json_output,
        human=lambda data: [
            f"{data['kg']:.1f} kg ({data['lb']:.1f} lb)  {data['time']}"
        ],
    )


MIN_CM, MAX_CM = 50.0, 250.0

HEIGHT_UNIT_CHOICE = click.Choice(list(HEIGHT_UNITS), case_sensitive=False)


def _cm(value: float, unit: str) -> float:
    """A stated height in centimetres, whatever unit stated it."""
    centimetres = value * HEIGHT_UNITS[unit]
    if MIN_CM <= centimetres <= MAX_CM:
        return centimetres
    stated = "" if HEIGHT_UNITS[unit] == 1.0 else f"{value:g} {unit} is "
    raise UsageError(
        f"{stated}{centimetres:.1f} cm, outside {MIN_CM:g}-{MAX_CM:g} cm. "
        "State the height you mean."
    )


def height_json(height: HeightLog) -> dict[str, Any]:
    return {
        "id": height.id,
        "cm": height.cm,
        "m": height.cm / CM_PER_M,
        "in": height.cm / CM_PER_INCH,
        # The figure Google Health actually stores, so a reader can check it.
        "mm": round(height.cm * MM_PER_CM),
        "time": height.sampled.isoformat(),
    }


def _height_human(data: dict[str, Any]) -> list[str]:
    return [f"{data['cm']:.1f} cm ({data['in']:.1f} in)  {data['time']}"]


@click.group("height")
def height_app() -> None:
    """Record and read height."""


@height_app.command("log")
@click.argument("value", type=float)
@click.option("--unit", type=HEIGHT_UNIT_CHOICE, required=True, help=UNIT_HELP)
@click.option("--time", "measured_at", help="ISO 8601 date-time.")
@click.option("--dry-run", is_flag=True)
@json_option
def height_log(
    value: float,
    unit: str,
    measured_at: str | None,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Record a height, stating the unit it is in."""
    height = HeightLog(cm=_cm(value, unit), sampled=_time(measured_at))
    if not dry_run:
        try:
            height = GoogleHealthClient().log_height(height)
        except GoogleHealthError as exc:
            raise _remote(exc) from exc
    emit(height_json(height), json_output=json_output, human=_height_human)


@height_app.command("latest", help=LATEST_HELP)
@json_option
def height_latest(json_output: bool) -> None:
    point = _latest_point(by_id("height"))
    if point is None:
        emit_none("height", json_output)
        return
    emit(
        height_json(HeightLog.from_api_payload(point)),
        json_output=json_output,
        human=_height_human,
    )


@height_app.command("history", help=HISTORY_HELP)
@click.argument("start", required=False)
@click.argument("end", required=False)
@limit_option(default=DEFAULT_LIMIT)
@json_option
def height_history(
    start: str | None, end: str | None, limit: int, json_output: bool
) -> None:
    start_time, end_time = _bounds(start, end)
    try:
        points = GoogleHealthClient().points(
            by_id("height"), start_time, end_time, limit=limit
        )
    except GoogleHealthError as exc:
        raise _remote(exc) from exc
    readings = [
        height_json(HeightLog.from_api_payload(point)) for point in points
    ]
    emit(
        {"count": len(readings), "readings": readings},
        json_output=json_output,
        human=lambda data: (
            [f"{r['time'][:10]}  {r['cm']:.1f} cm" for r in data["readings"]]
            or ["No height in that range. Try `healthlog height latest`."]
        ),
    )


@height_app.command("delete")
@click.argument("point_id")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@json_option
def height_delete(point_id: str, yes: bool, json_output: bool) -> None:
    """Delete one height reading explicitly."""
    if json_output and not yes:
        raise UsageError("delete --json needs --yes")
    if not yes and not click.confirm(f"Delete height {point_id}?"):
        click.echo("Deletion cancelled.")
        return
    try:
        GoogleHealthClient().delete_point(by_id("height"), point_id)
    except GoogleHealthError as exc:
        raise _remote(exc) from exc
    emit(
        {"id": point_id, "deleted": True},
        json_output=json_output,
        human=lambda data: [f"Deleted {data['id']}."],
    )


def read_group(data_type: DataType) -> click.Group:
    """The `history` verb for one data type.

    Every type reads through the same grammar, so a caller that can read one
    can read all of them without learning a second spelling.
    """

    @click.group(
        data_type.id,
        hidden=data_type.id not in PROMINENT,
        help=f"Read {data_type.id} data points from Google Health.",
    )
    def group() -> None:
        pass

    @group.command("history", help=HISTORY_HELP)
    @click.argument("start", required=False)
    @click.argument("end", required=False)
    @limit_option(default=DEFAULT_LIMIT)
    @json_option
    def history(
        start: str | None, end: str | None, limit: int, json_output: bool
    ) -> None:
        start_time, end_time = _bounds(start, end)
        try:
            points = GoogleHealthClient().points(
                data_type, start_time, end_time, limit=limit
            )
        except GoogleHealthError as exc:
            raise _remote(exc) from exc
        values = [point_json(data_type, point) for point in points]
        emit(
            {
                "count": len(values),
                "truncated": bool(limit) and len(values) >= limit,
                "points": values,
            },
            json_output=json_output,
            human=_points_human,
        )

    @group.command("latest", help=LATEST_HELP)
    @json_option
    def latest(json_output: bool) -> None:
        point = _latest_point(data_type)
        if point is None:
            emit_none(data_type.id, json_output)
            return
        emit(
            {
                "count": 1,
                "truncated": False,
                "points": [point_json(data_type, point)],
            },
            json_output=json_output,
            human=_points_human,
        )

    return group


@click.group(cls=JsonAwareGroup, epilog=EPILOG)
@click.version_option(__version__)
def app() -> None:
    """Read Google Health data, and log explicit nutrient data to it."""


@click.group("food")
def food_app() -> None:
    """Log and read nutrition entries."""


@food_app.command("log")
@click.argument("name", required=False)
@click.option(
    "--input",
    "input_file",
    type=click.File("r", encoding="utf-8"),
    help="JSON object from PATH, or '-' for stdin.",
)
@click.option("--kcal", "--calories", type=float)
@click.option("--protein", type=float)
@click.option("--fat", type=float)
@click.option("--carbs", type=float)
@click.option("--grams", type=click.FloatRange(min=0, min_open=True))
@click.option("--nutrient", multiple=True, help="NAME=GRAMS; repeatable.")
@click.option("--meal", "meal_type")
@click.option("--time", "consumed_at", help="ISO 8601 date-time.")
@click.option("--dry-run", is_flag=True)
@json_option
def log_command(
    name: str | None,
    input_file: TextIO | None,
    kcal: float | None,
    protein: float | None,
    fat: float | None,
    carbs: float | None,
    grams: float | None,
    nutrient: tuple[str, ...],
    meal_type: str | None,
    consumed_at: str | None,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Log one meal. Explicit flags override fields in INPUT."""
    meal = build_meal(
        name=name,
        input_data=_input(input_file),
        kcal=kcal,
        protein=protein,
        fat=fat,
        carbs=carbs,
        grams=grams,
        nutrient_args=nutrient,
        meal_type=meal_type,
        consumed_at=consumed_at,
    )
    if not dry_run:
        try:
            meal = GoogleHealthClient().log_meal(meal)
        except GoogleHealthError as exc:
            raise _remote(exc) from exc
    emit(meal_json(meal), json_output=json_output, human=_human)


@food_app.command("duplicate")
@click.argument("point_id")
@click.option("--name")
@click.option(
    "--input",
    "input_file",
    type=click.File("r", encoding="utf-8"),
    help="JSON overrides from PATH, or '-' for stdin.",
)
@click.option("--kcal", "--calories", type=float)
@click.option("--protein", type=float)
@click.option("--fat", type=float)
@click.option("--carbs", type=float)
@click.option("--grams", type=click.FloatRange(min=0, min_open=True))
@click.option("--nutrient", multiple=True, help="NAME=GRAMS; repeatable.")
@click.option("--meal", "meal_type")
@click.option("--time", "consumed_at", help="ISO 8601 date-time.")
@click.option("--dry-run", is_flag=True)
@json_option
def duplicate_command(
    point_id: str,
    name: str | None,
    input_file: TextIO | None,
    kcal: float | None,
    protein: float | None,
    fat: float | None,
    carbs: float | None,
    grams: float | None,
    nutrient: tuple[str, ...],
    meal_type: str | None,
    consumed_at: str | None,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Create another meal, optionally overriding fields."""
    client = GoogleHealthClient()
    try:
        source = client.get_meal(point_id)
        values = meal_json(source)
        input_values = _input(input_file)
        values.update(input_values)
        duplicate = build_meal(
            name=name,
            input_data=values,
            kcal=kcal,
            protein=protein,
            fat=fat,
            carbs=carbs,
            grams=grams,
            nutrient_args=nutrient,
            meal_type=meal_type,
            consumed_at=consumed_at,
        )
        if not dry_run:
            duplicate = client.log_meal(duplicate)
    except GoogleHealthError as exc:
        raise _remote(exc) from exc
    # Two entries for one meal double every total covering them.
    emit(
        meal_json(duplicate) | {"source": {"id": point_id, "deleted": False}},
        json_output=json_output,
        human=lambda data: [
            *_human(data),
            f"Source {data['source']['id']} still counts. Delete it if this "
            "replaced it, or both are totalled.",
        ],
    )


@food_app.command("delete")
@click.argument("point_id")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@json_option
def delete_command(point_id: str, yes: bool, json_output: bool) -> None:
    """Delete one meal explicitly."""
    if json_output and not yes:
        raise UsageError("delete --json needs --yes")
    if not yes and not click.confirm(f"Delete meal {point_id}?"):
        click.echo("Deletion cancelled.")
        return
    try:
        GoogleHealthClient().delete_meal(point_id)
    except GoogleHealthError as exc:
        raise _remote(exc) from exc
    emit(
        {"id": point_id, "deleted": True},
        json_output=json_output,
        human=lambda data: [f"Deleted {data['id']}."],
    )


@food_app.command("history")
@click.argument("start", required=False)
@click.argument("end", required=False)
@json_option
def history_command(
    start: str | None, end: str | None, json_output: bool
) -> None:
    """Show logged food in a date or time range.

    Values may be dates or offset-aware ISO datetimes. With no values, show
    today; one value must be a date and selects that local calendar day. An
    end date is inclusive; an end datetime is exclusive.
    """
    start_time, end_time = _bounds(start, end)
    try:
        meals = [
            meal_json(meal)
            for meal in GoogleHealthClient().history(start_time, end_time)
        ]
    except GoogleHealthError as exc:
        raise _remote(exc) from exc
    # A nutrient is totalled only where an entry states it; the core always.
    stated = {name for meal in meals for name in meal}
    optional = [name for name in OPTIONAL_NUTRIENTS if name in stated]
    summary = {key: _total(meals, key) for key in (*CORE_NUTRIENTS, *optional)}
    emit(
        {"count": len(meals), "totals": summary, "meals": meals},
        json_output=json_output,
        human=_history_human,
    )


@food_app.command("latest", help=LATEST_HELP)
@json_option
def food_latest(json_output: bool) -> None:
    point = _latest_point(by_id(FOOD_ID))
    if point is None:
        emit_none("food", json_output)
        return
    emit(
        meal_json(MealLog.from_api_payload(point)),
        json_output=json_output,
        human=_human,
    )


@click.group("auth")
def auth_app() -> None:
    """Manage Google OAuth credentials."""


@auth_app.command("login")
@click.option("--secrets", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--port", default=0, type=int)
@click.option("--remote", is_flag=True)
def auth_login(secrets: Path | None, port: int, remote: bool) -> None:
    """Authorize reading every data type, and writing food and weight."""
    if remote or auth.is_headless_or_ssh():
        auth.login_remote(client_config_path=secrets)
    else:
        auth.login(client_config_path=secrets, port=port)
    click.echo("Authenticated.")


def _auth_human(data: dict[str, Any]) -> list[str]:
    if not data["authenticated"]:
        return ["not authenticated"]
    lines = ["authenticated"]
    # A token granted before a data type existed reads it as a bare 403.
    if data["missing_scopes"]:
        lines.append(
            f"{len(data['missing_scopes'])} scope(s) not granted; "
            "run 'healthlog auth login' to read every data type."
        )
    return lines


@auth_app.command("status")
@json_option
def auth_status(json_output: bool) -> None:
    """Show whether usable credentials are stored."""
    emit(
        auth.get_auth_status(),
        json_output=json_output,
        human=_auth_human,
    )


@auth_app.command("logout")
def auth_logout() -> None:
    """Delete the stored OAuth token."""
    click.echo("Logged out." if auth.logout() else "No stored token.")


def _types_human(data: dict[str, Any]) -> list[str]:
    lines = []
    for value in data["types"]:
        # Only `food` differs, and seeing both keeps its id findable.
        spelling = value["noun"]
        if value["noun"] != value["id"]:
            spelling = f"{value['noun']} ({value['id']})"
        verbs = (
            "log, history, latest" if value["writes"] else "history, latest"
        )
        lines.append(f"{spelling}  [{value['scope']}]  {verbs}")
    return lines


@app.command("types")
@json_option
def types_command(json_output: bool) -> None:
    """List the data types this version can read."""
    values = [
        {
            "noun": noun(value),
            "id": value.id,
            "scope": value.scope,
            "writes": value.writes,
        }
        for value in DATA_TYPES
    ]
    emit(
        {"count": len(values), "types": values},
        json_output=json_output,
        human=_types_human,
    )


for command in (
    food_app,
    weight_app,
    height_app,
    auth_app,
    skill_group(name="healthlog", package="healthlog"),
):
    app.add_command(command)

# `food` owns a write path, so it is a group, not a generated read.
for data_type in DATA_TYPES:
    if data_type not in (by_id("food"), by_id("weight"), by_id("height")):
        app.add_command(read_group(data_type))
