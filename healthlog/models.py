"""The small nutrition-log shape sent to Google Health."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from typing import Any

from mealtime_nutrients import (
    API_FIELDS,
    API_NUTRIENTS,
    CORE_NUTRIENTS,
    ENERGY_NUTRIENT,
    ENERGY_UNIT,
)

# Read back: the wire name for each nutrient Google Health can return.
WIRE_NAMES = {member: name for name, member in API_NUTRIENTS.items()}


class MealType(str, Enum):
    MEAL_TYPE_UNSPECIFIED = "MEAL_TYPE_UNSPECIFIED"
    BREAKFAST = "BREAKFAST"
    LUNCH = "LUNCH"
    DINNER = "DINNER"
    SNACK = "SNACK"

    @classmethod
    def from_string(cls, value: str | None) -> "MealType":
        key = (value or "").strip().upper()
        aliases = {
            "B": cls.BREAKFAST,
            "L": cls.LUNCH,
            "D": cls.DINNER,
            "S": cls.SNACK,
        }
        return cls.__members__.get(
            key, aliases.get(key, cls.MEAL_TYPE_UNSPECIFIED)
        )


def _unit(nutrient: str) -> str:
    """Google Health's dedicated objects take grams; energy takes kcal."""
    return ENERGY_UNIT if nutrient == ENERGY_NUTRIENT else "grams"


def _offset(value: datetime) -> str:
    return f"{int((value.utcoffset() or timedelta()).total_seconds())}s"


@dataclass(frozen=True)
class TimeInterval:
    start: datetime
    end: datetime

    @classmethod
    def from_start(cls, start: datetime) -> "TimeInterval":
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return cls(start=start, end=start + timedelta(minutes=1))

    def as_api(self) -> dict[str, str]:
        return {
            "startTime": self.start.isoformat(),
            "endTime": self.end.isoformat(),
            "startUtcOffset": _offset(self.start),
            "endUtcOffset": _offset(self.end),
        }


@dataclass
class MealLog:
    name: str
    meal_type: MealType
    interval: TimeInterval
    kcal: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    grams: float | None = None
    nutrients: dict[str, float] = field(default_factory=dict)
    id: str | None = None

    def _figures(self) -> dict[str, float]:
        """Every recorded figure by wire name; the core macros are fields."""
        core = {name: getattr(self, name) for name in CORE_NUTRIENTS}
        return self.nutrients | {
            name: value for name, value in core.items() if value is not None
        }

    def to_api_payload(self) -> dict[str, Any]:
        """Omit unknown fields. Keep explicitly supplied zeros."""
        log: dict[str, Any] = {
            "foodDisplayName": self.name,
            "mealType": self.meal_type.value,
            "interval": self.interval.as_api(),
        }
        figures = self._figures()

        # API_FIELDS names own an object; the rest are array entries.
        for name, api_field in API_FIELDS.items():
            if name in figures:
                log[api_field] = {_unit(name): figures[name]}
        if self.grams is not None:
            log["serving"] = {
                "amount": self.grams,
                "foodMeasurementUnitDisplayName": "gram",
            }

        entries = {
            API_NUTRIENTS[name]: grams
            for name, grams in figures.items()
            if name in API_NUTRIENTS
        }
        if entries:
            log["nutrients"] = [
                {"nutrient": nutrient, "quantity": {"grams": grams}}
                for nutrient, grams in sorted(entries.items())
            ]
        return {"nutritionLog": log}

    @classmethod
    def from_api_payload(cls, data: dict[str, Any]) -> "MealLog":
        log = data.get("nutritionLog", data)
        raw_interval = log.get("interval") or {}
        start = _datetime(
            raw_interval.get("startTime"),
            utc_offset=raw_interval.get("startUtcOffset"),
        )
        end = _datetime(
            raw_interval.get("endTime"),
            start + timedelta(minutes=1),
            raw_interval.get("endUtcOffset"),
        )
        nutrients: dict[str, float] = {}
        for entry in log.get("nutrients") or []:
            member = str(entry.get("nutrient") or "").strip().upper()
            grams = (entry.get("quantity") or {}).get("grams")
            if member in WIRE_NAMES and grams is not None:
                nutrients[WIRE_NAMES[member]] = float(grams)
        protein = nutrients.pop("protein", None)
        serving = log.get("serving") or {}
        unit = str(serving.get("foodMeasurementUnitDisplayName") or "")
        grams = (
            _quantity(serving, "amount")
            if unit.casefold() in ("g", "gram", "grams")
            else None
        )
        return cls(
            id=(data.get("name") or data.get("id") or "").split("/")[-1]
            or None,
            name=str(log.get("foodDisplayName") or "Meal"),
            meal_type=MealType.from_string(log.get("mealType")),
            interval=TimeInterval(start=start, end=end),
            **{
                name: _quantity(log.get(api_field), _unit(name))
                for name, api_field in API_FIELDS.items()
            },
            protein=protein,
            grams=grams,
            nutrients=nutrients,
        )


def point_time(payload: dict[str, Any]) -> datetime | None:
    """The instant a data point states, in whichever shape states it.

    Samples carry a `sampleTime`, intervals and sessions an `interval`, daily
    summaries a civil `date`, and reference data no time at all.
    """
    interval = payload.get("interval")
    if isinstance(interval, dict) and interval.get("startTime"):
        return _datetime(
            interval["startTime"],
            utc_offset=interval.get("startUtcOffset"),
        )

    sample = payload.get("sampleTime")
    if isinstance(sample, dict) and sample.get("physicalTime"):
        return _datetime(
            sample["physicalTime"], utc_offset=sample.get("utcOffset")
        )

    # A daily summary names a calendar day, so it is read in local time.
    date = payload.get("date")
    if isinstance(date, dict) and date.get("year"):
        return datetime(
            int(date["year"]), int(date["month"]), int(date["day"])
        ).astimezone()

    return None


def _quantity(container: Any, key: str) -> float | None:
    value = container.get(key) if isinstance(container, dict) else None
    return float(value) if value is not None else None


def _datetime(
    value: Any,
    default: datetime | None = None,
    utc_offset: Any = None,
) -> datetime:
    parsed = (
        datetime.fromisoformat(str(value))
        if value is not None
        else default or datetime.now(UTC)
    )
    offset = _fixed_offset(utc_offset)
    return parsed.astimezone(offset) if offset else parsed


def _fixed_offset(value: Any) -> timezone | None:
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)s", str(value or ""))
    if match is None:
        return None
    try:
        return timezone(timedelta(seconds=float(match.group(1))))
    except ValueError:
        return None
