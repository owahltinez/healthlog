---
name: healthlog
description: Read Google Health data, log nutrient data, and inspect food history.
---

# Healthlog

Every data type is a noun, and every noun takes `history`. Food and weight are
the two that also write. Use JSON output when consuming commands:

```console
healthlog food log --input - --json
healthlog food history [START] [END] --json
healthlog food duplicate POINT_ID --input - --json
healthlog food delete POINT_ID --yes --json
healthlog weight log VALUE --unit kg|lb --json
healthlog weight history [START] [END] [--unit kg|lb] --json
healthlog weight delete POINT_ID --yes --json
healthlog types --json
```

`weight log` writes, so run it only with user authorization, and never guess
`--unit`: it is required because a default records 181 kg for someone who meant
181 lb, and no later check can catch that. If the user states a number without
a unit, ask which they mean rather than assuming.

`food log` writes to Google Health, so run it only with user authorization. New
entries require `kcal`, `protein`, `fat`, and `carbs`. `--input -` accepts one
flat JSON item from Pantry, Eatout, Recipes, or Healthlog: pipe their `--json`
output whole, envelope included, and unknown fields are dropped. An object you
compose yourself must carry known keys only. Known keys are the shared
`mealtime-nutrients` names, one per nutrient: dietary fibre is `fiber`, and
`carbohydrates` is not a key at all, because carbohydrate is `carbs`. Omit
`time` unless the user specified one; Healthlog uses the device time, so never
look up "now".

Never scale a nutrient by hand, and never pass `--grams` to change a piped
item's weight: an item's nutrients describe the weight it states, so healthlog
refuses a `--grams` that contradicts it. Ask the source for the weight instead,
as with `pantry lookup --grams 250`. An item stating no weight, such as an
Eatout meal, has nothing to contradict, so `--grams` there records what was
eaten.

`history` defaults to today. Bounds accept `today`, `yesterday`, ISO dates, or
offset-aware ISO datetimes. Dates use the device timezone.

`food duplicate` creates a copy and never deletes its source. To correct an
entry, duplicate it, inspect the copy, then delete the source only with
explicit user authorization. Food output states the four core macros and only
the nutrients an entry carries; an absent key means nothing is known, and
totals ignore it.

A duplicate keeps the source time. To re-log the same food now, take the time
from the device clock rather than a guess:

```console
healthlog food duplicate POINT_ID --time "$(date +%Y-%m-%dT%H:%M:%S%z)" --json
```

Every other noun reads only. `healthlog types --json` lists them; do not guess
a noun, because a type this version cannot read is not a command. Each point
carries `id`, `time`, and `data` holding the record verbatim as Google Health
stated it, so read the figures out of `data` rather than assuming field names.
Reads cap at 500 points and report `truncated` when the cap was hit; pass
`--limit 0` for a complete range.

A 403 means the stored token predates the data type. Tell the user to run
`healthlog auth login`; never run it unprompted, since it opens a browser.
