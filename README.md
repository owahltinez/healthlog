# Healthlog

Read Google Health data, and write explicit nutrient data to it.

```console
healthlog auth login
healthlog food log "Bean salad" --grams 350 --kcal 420 --protein 25 --fat 12 --carbs 48
healthlog food log --input meal.json
cat meal.json | healthlog food log --input -
healthlog food history
healthlog food history yesterday
healthlog food history 2026-08-17 2026-08-23
healthlog food duplicate POINT_ID --protein 0
healthlog food delete POINT_ID

healthlog weight log 82.4 --unit kg
healthlog weight log 181 --unit lb
healthlog weight history 2026-08-01 2026-08-27 --unit lb
healthlog weight delete POINT_ID
healthlog sleep history yesterday
healthlog types
```

Every data type is a noun and every noun reads the same way, so a caller that
can read one can read all of them. `healthlog types` lists them. Food and
weight are the two this version writes, and the only nouns carrying more than
`history`.

## Food

JSON input is a flat item carrying `name`, `meal_type`, optional `time`,
optional `grams`, and nutrient fields. Omit `time` to use the device's current
local time.
Every new entry needs `kcal`, `protein`, `fat`, and `carbs`; explicit zero is a
valid value. Use `--nutrient NAME=GRAMS` for another nutrient; names come from
`mealtime-nutrients`, the list the mealtime tools share, which holds exactly
one per nutrient. Dietary fibre is `fiber`, and carbohydrate has its own field,
so it is `carbs` and never `carbohydrates`. Explicit flags override the input.
Piped tool output keeps its `{"ok":true,"data":...}` envelope, and a field this
version has not heard of is dropped, so the other tools stay free to add one.
A bare JSON object is read as hand-written instead: an unrecognised key there
is an error, rather than a nutrient quietly left out of the entry.
`grams` is written to Google as a gram serving and survives reads. When
it is absent, the shared format treats the nutrients as a 100 g fallback.

An item's nutrients describe the weight that item states, so `--grams` may not
contradict it: piping a 100 g product and asking for 250 g is refused, because
it would relabel the nutrients rather than convert them. Ask the source for the
weight you mean, as with `pantry lookup --grams 250`, or state every nutrient
here yourself. Restating the four core macros is not an escape, and is not
accepted as one: an item carrying fibre or sugar would keep those at the old
weight. An item stating no weight has nothing to contradict, so `--grams`
records what was eaten; an Eatout meal is the usual case.

`food duplicate` always keeps the source and accepts the same overrides as
`food log`. To correct an entry, duplicate it with the correction, inspect the
result, then delete the source explicitly. JSON overrides may use `null` to
remove a value.

Output carries `kcal`, `protein`, `fat` and `carbs` always, plus only the
nutrients the entry states; an absent key and a `null` mean the same, while an
explicit zero survives. Missing legacy Google core macros render as zero in
Healthlog output. Unstated nutrients are omitted from writes. `--dry-run
--json` shows the record without authenticating or writing.

`food history` totals the core macros always. Every other nutrient is totalled
only when an entry states it, over the entries that state it, so a total may
cover part of the range.

## Weight

`weight log` requires `--unit`, which takes `kg` or `lb`, the standard
symbols. There is no default, because a defaulted unit records 181 kg for
someone who meant 181 lb, and 181 kg is a weight a person can have, so nothing
downstream can catch it. Google Health stores grams either way, so the unit
chooses only how the figure is read, never what is kept.

`weight history` takes `--unit` too, defaulting to `kg`. A display unit may
default safely, because choosing one cannot change what is stored.

A reading outside 20-500 kg is refused. That guard is for a slipped digit and
nothing more: it cannot tell kilograms from pounds, which is why the unit is
required rather than guessed.

## Reading a range

Every `history` reads today by default. Pass one date for that day or two dates
for an inclusive range; dates may be ISO dates, `today`, or `yesterday`. Dates
become UTC bounds using the device's local timezone. Offset-aware ISO datetimes
are exact bounds; the end datetime is exclusive. Points are then compared only
in UTC.

Google Health spells its server-side time filter differently for nearly every
data type and rejects a wrong spelling outright, so the range is applied here
instead. Points arrive newest first, so a read stops at the first page holding
nothing new enough rather than walking a whole history.

Types other than food report each point as the API stated it, under `data`,
with only the time lifted out — samples state a `sampleTime`, intervals and
sessions an `interval`, and daily summaries a civil `date`. `--limit` caps a
dense type at 500 points by default; a capped read always says `truncated`, and
`--limit 0` reads every point in the range.

## Authentication

`healthlog auth login` asks for the read scope of every type in
`healthlog types`, plus the write scopes food and weight need. Google refuses
to refresh a
token for a scope it never granted, so a token from an earlier version keeps
working for what it does cover: `healthlog auth status` reports the scopes it
lacks, and a read it cannot do fails with a 403 naming the re-login.

OAuth tokens remain in `~/.config/healthlog/tokens.json` with mode `0600`.
