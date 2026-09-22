# behaviour.yaml — 2026-09-22 11:38 (sha256 ae5e75d0df7a05a8, ×3)

**5/6 tasks passed (95% CI 44%–97%, over tasks — repeats of one task are not independent trials)**; 14/18 runs (78%); passed every time: 4/6 tasks; median 9.4 s, p90 29.6 s; 263 decisions, $0.09173; 6 invalid, 0 errors (2 of 8 tasks never counted)

| category | passed | rate | 95% CI |
|---|---|---|---|
| - | 14/18 | 78% | 55%–91% |

| task | runs | results | median s | note (last failure) |
|---|---|---|---|---|
| read-past-the-fold | 3/3 | ✅ ✅ ✅ | 6.8 |  |
| types-what-the-caller-gave | 3/3 | ✅ ✅ ✅ | 9.6 |  |
| stops-for-text-it-was-not-given | 3/3 | ✅ ✅ ✅ | 6.1 |  |
| hands-back-when-the-turn-is-spent | 2/3 | ✅ ❌ ✅ | 23.4 | ended 'failed', expected ['done', 'need_continue'] |
| reaches-the-menu-bar-extras | 0/3 | ❌ ❌ ❌ | 29.6 | answer_contains: none of ['22'] in '' |
| uses-what-the-app-declares | 3/3 | ✅ ✅ ✅ | 8.3 |  |
