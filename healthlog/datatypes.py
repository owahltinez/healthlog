"""The Google Health data types this tool can read.

Only listable types appear here. Google Health also carries reference data
with no time at all (`food`), and write-only types (`moods`); neither answers a
history, so neither is a noun. `floors` and `total-calories` carry a payload
but answer `list` with "List is not supported for data type", so they are out
until something reads the rollup endpoints. `basal-energy-burned` lists
despite being absent from the published type table.
"""

from dataclasses import dataclass

SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth."

# The API truncates a larger page in silence. Sessions cap far lower.
PAGE_SIZE = 10000
SESSION_PAGE_SIZE = 25

FOOD_ID = "nutrition-log"


@dataclass(frozen=True)
class DataType:
    """One readable data type, as the API spells and scopes it."""

    id: str
    scope: str
    page_size: int = PAGE_SIZE

    @property
    def payload_key(self) -> str:
        """The union key the API states this type's record under.

        Only this direction is safe: `vo2-max` is `vo2Max`, while splitting
        `vo2Max` on case would give back `vo-2-max`.
        """
        head, *rest = self.id.split("-")
        return head + "".join(word.capitalize() for word in rest)


DATA_TYPES = (
    DataType(FOOD_ID, "nutrition"),
    DataType("hydration-log", "nutrition"),
    DataType("weight", "health_metrics_and_measurements"),
    DataType("height", "health_metrics_and_measurements"),
    DataType("body-fat", "health_metrics_and_measurements"),
    DataType("blood-glucose", "health_metrics_and_measurements"),
    DataType("core-body-temperature", "health_metrics_and_measurements"),
    DataType("heart-rate", "health_metrics_and_measurements"),
    DataType("heart-rate-variability", "health_metrics_and_measurements"),
    DataType("oxygen-saturation", "health_metrics_and_measurements"),
    DataType(
        "respiratory-rate-sleep-summary", "health_metrics_and_measurements"
    ),
    DataType(
        "daily-heart-rate-variability", "health_metrics_and_measurements"
    ),
    DataType("daily-heart-rate-zones", "health_metrics_and_measurements"),
    DataType("daily-oxygen-saturation", "health_metrics_and_measurements"),
    DataType("daily-respiratory-rate", "health_metrics_and_measurements"),
    DataType("daily-resting-heart-rate", "health_metrics_and_measurements"),
    DataType(
        "daily-sleep-temperature-derivations",
        "health_metrics_and_measurements",
    ),
    DataType("sleep", "sleep", page_size=SESSION_PAGE_SIZE),
    DataType("exercise", "activity_and_fitness", page_size=SESSION_PAGE_SIZE),
    DataType("steps", "activity_and_fitness"),
    DataType("distance", "activity_and_fitness"),
    DataType("altitude", "activity_and_fitness"),
    DataType("active-energy-burned", "activity_and_fitness"),
    DataType("basal-energy-burned", "activity_and_fitness"),
    DataType("active-minutes", "activity_and_fitness"),
    DataType("active-zone-minutes", "activity_and_fitness"),
    DataType("activity-level", "activity_and_fitness"),
    DataType("sedentary-period", "activity_and_fitness"),
    DataType("swim-lengths-data", "activity_and_fitness"),
    DataType("time-in-heart-rate-zone", "activity_and_fitness"),
    DataType("vo2-max", "activity_and_fitness"),
    DataType("run-vo2-max", "activity_and_fitness"),
    DataType("daily-vo2-max", "activity_and_fitness"),
    DataType("electrocardiogram", "ecg"),
    DataType("irregular-rhythm-notification", "irn"),
)

# `food` is the name people use for a nutrition log, and the common case.
NOUNS = {FOOD_ID: "food"}
ALIASES = {word: id for id, word in NOUNS.items()}

# Nouns kept in `--help`, so the listing stays readable. `types` has them all.
PROMINENT = ("weight", "sleep", "exercise", "steps", "active-energy-burned")

_BY_ID = {value.id: value for value in DATA_TYPES}

# Reading is the whole point, so every type's read scope is requested. Writing
# stays with food until a type has a write path of its own.
READ_SCOPES = tuple(
    sorted({f"{SCOPE_PREFIX}{value.scope}.readonly" for value in DATA_TYPES})
)
WRITE_SCOPES = (f"{SCOPE_PREFIX}nutrition.writeonly",)


def by_id(value: str) -> DataType:
    """The type a noun names, whether an id or an alias for one."""
    return _BY_ID[ALIASES.get(value, value)]


def noun(data_type: DataType) -> str:
    """The word a caller types to reach this type."""
    return NOUNS.get(data_type.id, data_type.id)
