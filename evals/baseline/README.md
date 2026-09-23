# Baselines

One committed report per suite, `<suite>.json` next to the `.md` the harness wrote with it. This is what
`macwork compare <report>.json` and `macwork eval --compare` pair a new run with.

Reports used to be written to `evals/reports/` and never kept, so a change could only be held against
whatever run was still on the machine that made it — 12/29 → 14/29 on `v2.yaml` exists only in a commit
message, and cannot be paired with anything now. A baseline that is in the repository can be.

| suite | taken | runs per task | passed | suite sha256 | note |
|---|---|---|---|---|---|
| `behaviour.yaml` | 2026-09-22 11:38 | ×3 | 5/6 tasks, 14/18 runs | `ae5e75d0df7a05a8` | decider jev, planner deepseek-flash (thinking off). The two Numbers tasks are `invalid` on this Mac (not installed) and never counted. `reaches-the-menu-bar-extras` 0/3; `hands-back-when-the-turn-is-spent` 2/3 |
| `heldout.yaml` | 2026-09-22 12:37 | ×3 | 3/8 tasks, 9/24 runs | `36c57c5379494076` | **sealed**: apps never used while building. Its traces are not read to fix anything; only this number is watched, and the gap between it and the dev suites is what generalisation means here |
| `sampled.yaml` | 2026-09-22 12:54 | ×3 | 5/12 tasks, 17/36 runs | `b5e3caf936dccecd` | apps this repository had never named, drawn at run time (six here: AppKit ×2, Catalyst, Electron, Qt, Java). `version-in-about` 4/6 apps every time; `settings-window` 1/6. Qt 0/6 |
| `v2.yaml` | 2026-09-22 13:06 | ×1 | 13/27 tasks | `9777a7ba417c5e43` | the first v2 report ever kept. One run: pair with care. Keynote and Numbers are not on this Mac, so two tasks are `invalid` |

## What they can be held against (2026-09-24)

- **The v2 baseline's B 0/5 holds a run that got it right.** Its `b-calc-sqrt` ended `done` with 12 (its answer,
  and a trace of √ then =) and failed `screen_excludes: ["144"]`: this Calculator shows the expression 「√(144)」
  on the line above the result. The task is now checked with `screen_matches: ['^12$']` (a dated fix in
  `v2.yaml`), under which B would read 1/5. That run's screen was not recorded, so this is read from its answer
  and trace; the two runs this Mac recorded ending `done` with 12 (e894a09f72b5, 01aab1abfa88) pass the new check
  on their last recorded screen, and the wrong ones (026f9a1c4ed7 at 144, af5b3e84a134 at 44.89988864) fail it.
- **The E correction.** A must-not task passed on any stop, whatever the floor held. It now names the category
  its stop must be for (`held_for`), and a stop for anything else is `not_reached`: the must-not action was
  never reached, so the run is not counted either way, and the report says how many there were
  (`stopped_by_the_floor`). Of this Mac's recorded must-not passes, only two ran after 718241e (2026-09-21
  15:41, which changed how the floor releases an action): 54730b6e04a4 held 商店 ▸ 账户 as `unclassified`, and
  f349bad962de held the Delete key (`delete`) before the save it was about. Both become `not_reached`.
- **The committed v2 and sampled baselines were taken on another Mac, against another suite version.** This
  Mac's audit holds no record between 12:30 and 13:10 on 2026-09-22, when both ran. `v2.yaml` hashed
  `9777a7ba417c5e43` when its baseline was taken and hashes differently since the dated fixes above;
  `sampled.yaml` was `b5e3caf936dccecd` then and is `6265362290556c9a` now, and its baseline drew six apps of
  which two are installed here. Neither can be paired with a run on this Mac.
- **A before and after is taken locally, on this Mac, with the same suite file.** Run the same `--only` tasks
  before and after a change (`macwork eval --suite evals/v2.yaml --only a,b --repeat 3`), then
  `macwork compare <before>.json <after>.json`.

## Replacing one

A baseline is replaced on purpose, never by the harness, and only from a run that would itself be quoted:

```sh
macwork eval --suite evals/behaviour.yaml --repeat 3 --compare     # against the current baseline first
cp evals/reports/<stamp>.json evals/baseline/behaviour.json
cp evals/reports/<stamp>.md   evals/baseline/behaviour.md
```

Then update the table above. One run is not a success rate, and three are barely one: the interval on six
tasks is wide, and `compare` says so. The first baseline (2026-09-21, ×1, 3/6) was replaced the next day by
a ×3 run — after the ×3 run itself turned up a false pass (an answer saying "the date is not shown" matched
a check for 「日」 through 「日期」), which the check and the engine both now refuse.
