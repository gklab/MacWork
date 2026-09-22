# sampled.yaml — 2026-09-22 12:54 (sha256 b5e3caf936dccecd, ×3)

**5/12 tasks passed (95% CI 19%–68%, over tasks — repeats of one task are not independent trials)**; 17/36 runs (47%); passed every time: 5/12 tasks; median 16.1 s, p90 28.0 s; 620 decisions, $0.2509; 0 invalid, 0 errors

| category | passed | rate | 95% CI |
|---|---|---|---|
| appkit | 7/12 | 58% | 32%–81% |
| catalyst | 3/6 | 50% | 19%–81% |
| electron | 3/6 | 50% | 19%–81% |
| java | 4/6 | 67% | 30%–90% |
| qt | 0/6 | 0% | 0%–39% |

| task | runs | results | median s | note (last failure) |
|---|---|---|---|---|
| settings-window--com.netease.uuremote | 3/3 | ✅ ✅ ✅ | 10.1 |  |
| settings-window--com.apple.stocks | 0/3 | ❌ ❌ ❌ | 28.0 | app_windows_grew: no new window of the app is on screen |
| settings-window--com.electron.oss-browser | 0/3 | ❌ ❌ ❌ | 16.5 | app_windows_grew: no new window of the app is on screen |
| settings-window--com.xk72.Charles | 1/3 | ❌ ❌ ✅ | 19.6 | ended 'failed', expected ['done'] (result on screen) |
| settings-window--com.lemon.lvpro | 0/3 | ❌ ❌ ❌ | 16.2 | app_windows_grew: no new window of the app is on screen |
| settings-window--de.RayLink | 0/3 | ❌ ❌ ❌ | 21.1 | app_windows_grew: no new window of the app is on screen |
| version-in-about--com.netease.uuremote | 3/3 | ✅ ✅ ✅ | 14.1 |  |
| version-in-about--com.apple.stocks | 3/3 | ✅ ✅ ✅ | 10.1 |  |
| version-in-about--com.electron.oss-browser | 3/3 | ✅ ✅ ✅ | 7.1 |  |
| version-in-about--com.xk72.Charles | 3/3 | ✅ ✅ ✅ | 6.8 |  |
| version-in-about--com.lemon.lvpro | 0/3 | ❌ ❌ ❌ | 20.4 | answer_contains: none of ['10.9.12865'] in '未能找到剪映专业版的版本号，当前屏幕上没有显示。' |
| version-in-about--de.RayLink | 1/3 | ✅ ❌ ❌ | 33.4 | answer_contains: none of ['8.1.3.8'] in '屏幕上没有显示 RayLink 的版本号，无法找到。' |
