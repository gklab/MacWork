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
| `v2.yaml` | — | — | — | — | **no report was kept.** The 14/29 in `docs/benchmarks.md` has no file behind it; a ×3 run on 2026-09-22 was stopped by hand before it finished |

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
