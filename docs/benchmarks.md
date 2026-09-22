# Benchmarks

Numbers this project has actually measured, oldest last. They are kept out of the README because most of
them describe an engine that no longer exists — but deleting them would make it easy to quote the best one
forever.

**How scoring works.** A task passes only if the engine itself reports `done` *and* the on-screen check
holds; a task already satisfied before it starts is `invalid` and not counted, and one that failed because
the decider could not be reached is `error` and not counted either. `fresh: true` is the default: no learned
routines, empty app models, so a task is solved by reasoning on the live screen rather than by recall.

---

## Current

### `evals/v2.yaml` — 29 tasks, six categories (2026-09-20)

The comprehensive suite: navigation, multi-step, cross-app, answers, must-not, prompt injection.

| | passed | note |
|---|---|---|
| before the nine fixes | 12/29 | the first run crashed before any task; see below |
| after | **14/29** | category E (must-not) 0/4 → 2/4 |

**This is one run, and one run is not a success rate.** Paired against each other those two results are
2 tasks fixed and 0 broken, which an exact McNemar test puts at p = 0.50 — what chance alone does half the
time. `macwork compare <before.json> <after.json>` is what says so. Repeats are still owed.

**Neither report was kept.** The two runs above exist as numbers in a commit message and nothing else, so
nothing can be paired with them now. From 2026-09-22 a report is committed per suite in `evals/baseline/`
and `macwork compare <report>.json` pairs a new run with it; the first baseline is the behaviour suite's
single run of 2026-09-21 (3/6), and a ×3 run should replace it as soon as the machine is free
(`evals/baseline/README.md`). A pass that came from answering the question without finishing the task is
now counted apart in every report (`passed_by_answering_anyway`).

The first time the suite ran, it crashed. Nine things surfaced that no amount of reading the code had
found, among them: the harness called a function it never imported (436 unit tests were green over a
harness that could not start); the local planner named the first model the server *listed* rather than the
one it had loaded, and 404'd on every call; the answer guard demanded a word-for-word source, which no
counted answer can have; the safety floor treated a calculator's Clear as an irreversible delete.

### Per-step cost (2026-09-21, Apple silicon, macOS 26)

| stage | ms | |
|---|---|---|
| decide | ~700 | one round trip to the decider; 701 ms to the service against 286 ms to apple.com through the same tunnel |
| observe | ~556 | was 1039 before the menu cache was keyed on the window changing rather than thrown away per action |
| wait for the screen | ~380 | |
| act | ~103 | was 184 before every fixed pause in the action layer was replaced by waiting for the thing itself |

A cold first observation costs 332 ms (was 1917 ms, before background refreshes were given their own
connection to the helper).

---

## Historical

### Rounds 1–13 (to 2026-09-19)

These ran on the engine **before** the hand-written parts were removed — a table of common shortcuts, word
lists for "dismissable" buttons and "safe to explore" menus, relevance ranking, and an explore/ask/stuck
state machine tuned on the very suite it was scored against. They are not comparable with anything above.

| round | what changed | passed (valid) | median s/task | cost |
|---|---|---|---|---|
| 1 | first run | 10/15 (lenient scoring) | 13.6 | $0.020 |
| 2 | verified-front typing, key-code typing on an ASCII layout, anti-loop, learning fix | 11/13 | 7.9 | $0.014 |
| 4 | per-text targets in unlabeled containers, softer done veto, strict scoring | 8/11 (73%) | 14.2 | $0.014 |
| 6 | on-screen evidence for every question (visible controls, menu check marks) | 12/14 (86%) | 8.2 | $0.020 |
| 10 | performance work | 12/14 (86%) | 6.7 | $0.020 |
| 11 | planner proposes concrete moves, exploration, all windows — *with* learned routines | 13/14 (93%) | 8.0 | $0.013 |
| 12 | the same, fresh (no routines, empty app models) | 12/14 (86%) | 8.2 | $0.020 |
| 13 | fresh; planner fallback chain, cut-off replies salvaged, out-of-budget diagnosis | 13/13 ¹ | 6.1 | $0.014 |

¹ one task was pre-satisfied and excluded.

### After the hand-written parts were removed (2026-09-19)

| suite | passed | median s/task |
|---|---|---|
| `heldout.yaml` — apps never used while building | 8/10 | 10.3 |
| `unseen.yaml` — regression suite | 15/15 | 6.0–7.5 |

Held-out failures were instructive rather than embarrassing: *System Information* stopped at the hardware
overview, which already shows the memory size the check wanted elsewhere; *Apple Maps* on that machine
answered "未找到匹配的地点" to every search, including through a `maps://` link, so the task could not
succeed there — and the engine said so after one step instead of wandering.

What those runs taught the engine, all of it general and none of it app-specific: select rows of lists and
tables including ones scrolled out of view, and name them by their text; ignore text fields the system says
cannot be edited; see prompts from other processes over the app and answer them by least privilege; a
`wait` move for apps still loading; drop floor-gated actions the goal never asked for rather than bothering
the user; say when an app has no window open; diagnose early when no new route is to be had.

### Earlier performance passes

Per step, before the first pass: observe ~450 ms (325 of it OCR), decide ~410–700 ms, act ~100 ms, wait
400–1000 ms (2.5 s when nothing visibly changed).

| stage | before | after | how |
|---|---|---|---|
| waiting for the UI | 2.5 s on apps that post no Accessibility notifications | ~0.25 s | the helper polls a cheap window fingerprint and returns once the window is *quiet*; first-change-only returned mid-transition |
| "is it really done?" | a second request per task | 0 extra | asked in the same request as the step — questions are judged in parallel |
| name tagging for redaction | re-tagged every task | once | memoized across tasks; menus and buttons repeat |
| observe | ~450 ms | ~110 ms | OCR only where the tree is sparse or when asked for; one read per window content |
| waiting | 400–1000 ms | ~220–440 ms | decide on the first calm screen and check the fingerprint when the answer arrives |
| local planner | 27–36 s with its weights paged out | warm | woken in the background at task start |

A six-step calculator task went from 19.3 s to 5.8 s.

### One idea measured and dropped

Extra questions are nearly free — the decider answers four in the time it answers one (417 ms against 580
ms) — so it was asked what it would do *after* the action it chose, to take that step with no round trip.
Against what was actually chosen next, the prediction was right 3 times in 28, and no more often when it
claimed to be sure. A step taken on that is a step taken at random. The number is in a comment in `loop.py`
so the idea is not had twice.

### Other measurements

| | before | after |
|---|---|---|
| installed apps found | 146 (scanning four directories; Finder and Safari missing) | 405 (LaunchServices) |
| typing targets | 3 / 2 (role allowlist) | 18 / 9 (capability probing, in an editor / a chat app) |
| menu-bar status items | 0 (invisible) | 20 across 9 processes |
| windows | 34 (current Space only) | 183 (all) |
| personal-data leak rate, eight-script corpus | 40.1% | 27.0% (phone numbers 15/24 → 0/24) |
| "user idle" after a synthetic event | 25.97 s → 0.15 s, caused by the engine itself | separated from the user's own input |
