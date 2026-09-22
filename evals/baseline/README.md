# Baselines

One committed report per suite, `<suite>.json` next to the `.md` the harness wrote with it. This is what
`macwork compare <report>.json` and `macwork eval --compare` pair a new run with.

Reports used to be written to `evals/reports/` and never kept, so a change could only be held against
whatever run was still on the machine that made it — 12/29 → 14/29 on `v2.yaml` exists only in a commit
message, and cannot be paired with anything now. A baseline that is in the repository can be.

| suite | taken | runs per task | passed | suite sha256 | note |
|---|---|---|---|---|---|
| `behaviour.yaml` | 2026-09-21 15:40 | ×1 | 3/6 tasks | `d89590b7d1c71572` | the suite has since gained two tasks (`table-total`, `table-largest-row`); `compare` pairs by task id and says the suite changed |
| `v2.yaml` | — | — | — | — | **no report was kept.** The 14/29 in `docs/benchmarks.md` has no file behind it |

## Replacing one

A baseline is replaced on purpose, never by the harness, and only from a run that would itself be quoted:

```sh
macwork eval --suite evals/behaviour.yaml --repeat 3 --compare     # against the current baseline first
cp evals/reports/<stamp>.json evals/baseline/behaviour.json
cp evals/reports/<stamp>.md   evals/baseline/behaviour.md
```

Then update the table above. One run is not a success rate: a baseline from a single run (the one here)
is the honest starting point, not the standard — a ×3 run should replace it as soon as the machine is free.
