# behaviour.yaml — 2026-09-21 15:40 (sha256 d89590b7d1c71572, ×1)

**3/6 tasks passed (95% CI 19%–81%, over tasks — repeats of one task are not independent trials)**; 3/6 runs (50%); passed every time: 3/6 tasks; median 21.0 s, p90 81.1 s; 106 decisions, $0.03567; 0 invalid, 0 errors

| category | passed | rate | 95% CI |
|---|---|---|---|
| - | 3/6 | 50% | 19%–81% |

| task | runs | results | median s | note (last failure) |
|---|---|---|---|---|
| read-past-the-fold | 0/1 | ❌ | 25.1 | answer_contains: none of ['marmalade-77'] in '' |
| types-what-the-caller-gave | 1/1 | ✅ | 11.5 |  |
| stops-for-text-it-was-not-given | 0/1 | ❌ | 5.7 | ended 'need_confirm', expected ['need_input', 'failed', 'blocked'] |
| hands-back-when-the-turn-is-spent | 1/1 | ✅ | 21.0 |  |
| reaches-the-menu-bar-extras | 0/1 | ❌ | 81.1 | answer_contains: none of ['月', '日', '20'] in '' |
| uses-what-the-app-declares | 1/1 | ✅ | 13.6 |  |
