---
name: healthlog
description: Read Google Health data across nutrition, activity, sleep, and body metrics, and log food, weight, and height.
---

# Healthlog

## All types

Every data type is a noun. Every noun takes `history` and `latest`; the ones
`healthlog types --json` marks `"writes": true` also take `log` and `delete`.
Read that rather than guessing from a noun's name, and never tell a user a type
cannot be written without checking it. Do not guess a noun either, because a
type this version cannot read is not a command. Pass `--json` whenever you are
consuming the output rather than showing it.

```console
healthlog types --json
healthlog TYPE latest --json
healthlog TYPE history [START] [END] --json
```

`history` defaults to today. Bounds accept `today`, `yesterday`, ISO dates, or
offset-aware ISO datetimes. Dates use the device timezone. Reads cap at 500
points and report `truncated` when the cap was hit; pass `--limit 0` for a
complete range.

Each point carries `id`, `time`, and `data` holding the record verbatim as
Google Health stated it, so read the figures out of `data` rather than assuming
field names.

`latest` exists because `history` answers a range. Height changes once a
decade, so `height history` over today finds nothing and reads as "no height
recorded" when a reading has been sitting there since 2017. For any value that
holds until it changes — height, weight, VO2 max — ask `latest` first, and use
`history` only when the question really is about a period.

A request for "my day", "today", or a log with no noun in it is not a food
question. Food is one of 35 types, and answering it alone silently drops the
steps, exercise, sleep, and energy that were sitting there. Read `types --json`
and cover the scopes that hold data: nutrition, activity, sleep, and metrics.
Say a scope was empty rather than omitting it, so the user can tell "no run
today" from "never asked".

A 403 means the stored token predates the data type. Tell the user to run
`healthlog auth login`; never run it unprompted, since it opens a browser.

## Food

### Reading

```console
healthlog food history [START] [END] --json
```

`food history` already aggregates, returning `totals` and `meals`. Output
states the four core macros and only the nutrients an entry carries; an absent
key means nothing is known, and totals ignore it.

### Writing

```console
healthlog food log --input - --json
healthlog food duplicate POINT_ID --input - --json
healthlog food delete POINT_ID --yes --json
```

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

#### Correcting an entry

`food duplicate` creates a copy and never deletes its source, so it is how you
log the same food a second time. It is only half of a correction. A
`duplicate` reports its `source`, which stays `"deleted": false` until you
delete it.

To correct an entry: duplicate it with the fix, inspect the copy, then delete
the source. **Leaving the source is not the safe outcome.** Two entries for one
meal are counted twice in every total covering them, so stopping after the copy
silently inflates the day rather than erring on the side of caution. Deleting
is the destructive-looking step, but skipping it is the one that corrupts data.

Ask for authorization once, for the correction as a whole, and name both parts:
"replace entry X with Y, deleting X". Do not treat the delete as a separate
question to raise later, and never report a correction as done while the
superseded entry is still there. If authorization for the delete is refused,
delete the copy instead and leave the original, so the log still holds one
entry per meal.

A duplicate keeps the source time. To re-log the same food now, take the time
from the device clock rather than a guess:

```console
healthlog food duplicate POINT_ID --time "$(date +%Y-%m-%dT%H:%M:%S%z)" --json
```

## Sleep

Reads only. A night starts the evening before, so `sleep history today` finds
nothing while a full night sits under yesterday's date. Query `sleep history
yesterday today` and attribute the night to the day it ended.

A record whose `updateTime` equals its `createTime` (top level and every stage)
is a truncated fragment, not a short night: a mid-night wake split it and the
correction never synced. Report it as incomplete, leave it out of averages, and
send the user to the tracker's own app for that night's figure.

## Activity

Reads only. Interval types are recorded per bucket, not per day: `steps history
today` returns dozens of one-minute points, so a daily figure is the sum of
their values, not the point count or the last point. The value key differs per
type and is not guessable — steps hold `count`, distance `millimeters`, energy
`kcal` — so read one point's `data` before summing.

## Weight and height

### Reading

```console
healthlog weight latest --json
healthlog weight history [START] [END] [--unit kg|lb] --json
healthlog height latest --json
```

### Writing

```console
healthlog weight log VALUE --unit kg|lb --json
healthlog weight delete POINT_ID --yes --json
healthlog height log VALUE --unit cm|m|in --json
```

`weight log` and `height log` write, so run them only with user authorization,
and never guess `--unit`. It is required because a default records 181 kg for
someone who meant 181 lb, and no later check can catch that. If the user states
a number without a unit, ask which they mean rather than assuming.
