# macwork v4 — from "can drive one app" to "can take over a Mac"

v3 gave the engine three concepts it was missing: who is holding this Mac (hands), where a value came from (facts), what a step promised (contract).
They made the engine reliable **inside one app**. The question v4 has to answer is the next one: why it still can't take over **a whole Mac**.

The symptoms had each been patched on their own: the safety floor is inert on a German interface; the right-hand side of the menu bar, other desktops, apps installed in subdirectories,
and the capabilities an app declares about itself are all invisible to the engine; after one helper timeout, every frame of screen it reads is off by one and it never notices.

These are three faces of the same thing again: **in three places the engine uses something it wrote itself in place of a fact the system could have told it.**
A word list in place of judgement, a fixed list in place of system enumeration, "it went out, so assume the right thing came back" in place of checking the reply.

---

## Four concepts

### 1. The floor is a judgement, not a word list

The design of `confirm.categories` in `policy.yaml` is right in itself: the regexes only find candidates, the verdict goes to the decider, and the decider is not given screen text,
so page content can't talk it into anything. The problem is that **a regex has to match first**. 11 categories × two languages, Chinese and English, roughly 100 words.
German `Löschen`, Japanese `削除`, French `Supprimer`, Russian `Удалить` match none of them →
`_floor_hits` returns `[]` → `_needs_confirm` lets it straight through → an irreversible action **runs without asking the user**.

Worse, `_risky()` (`policy.py:76`) — pure word list, no decider — is used on four paths where **the decider is not involved at all**:
step-level `mac_act` calls (`engine.py:163`), routine replay (`loop.py:523`), `learn` exploration (`learn.py:63,101`),
tidy fallback (`tidy.py:211`). On an interface that is neither Chinese nor English, those four paths have zero safety protection.

**The fix**: the floor no longer depends on a regex matching first. Every action about to run is classified once by the decider,
cached by `bundle|label|context` (the machinery already exists, `cache["floor.verdicts"]`).
The regexes drop to **pre-warm recall** — what they hit is classified in the batch that rides along with `_floor_questions`, which saves the latency, but they are no longer the only way in.
With no verdict available, require confirmation rather than let it through.

### 2. Capabilities come from the system, not from the repo

Anything the Mac can list for itself — which apps are installed, which actions an element supports, what capabilities an app declares,
how many windows are on screen, which keys the keyboard has — has to be asked of the system, not written down in this repo.

Where the code does the opposite today, and what it costs:

| Hardcoded | Cost |
|---|---|
| `observe.apps.dirs`, four directories (`config.yaml:58`, written out again in `System.swift:331`) | On this machine the directory scan sees 147 apps; Spotlight sees 461. **Even `/System/Library/CoreServices/Finder.app` is out of range.** |
| `action_labels`, nine AX actions, anything outside the table discarded (`observe.py:269`) | `AXRaise`/`AXCancel`/`AXDelete`/`AXZoomWindow` and **every app-defined AX action** do not exist for the engine |
| `text_roles`/`select_roles`/`submit_roles`/`range_roles` role allowlists | A role that isn't in the table can't be typed into and can't be selected. `contenteditable` groups and web inputs with non-standard roles all fail |
| `keyCodes`, a 70-entry US-ANSI **position** table (`System.swift:56`) | On a German, French or JIS keyboard `cmd+[` presses a different physical key; F13–F20, the keypad and media keys are missing |
| `url_schemes`/`document_types` are collected but only fed to the planner as text (`appmodel.py:82`) | The API an app declares of its own accord is wasted entirely; the planner's reply schema (`planner.py:33`) can't even express "open things:///add" |
| App Intents / NSServices / menu bar status items / windows on other Spaces / the clipboard | Five capability surfaces with **not one line of code** |
| `skip_first_menu: true` drops the Apple menu by position | This is "hand-written knowledge about macOS", and it takes out "About This Mac", "System Settings" and "Recent Items" along with it |

### 3. Every answer matches its question

In several places, two things are **assumed** to correspond with nothing checking it:

| Place | Assumption | When it's wrong |
|---|---|---|
| `Helper.call` (`helper.py:133-154`) | the line read back is the response to the request just sent | after a timeout the connection isn't reset, the late response stays in the buffer, and **`reply["id"]` is never checked** → every call from then on is off by one, silently |
| `Engine._last` (`engine.py:67`) | the id `act` was handed came from the most recent `observe` | MCP runs up to 40 worker threads concurrently; once two callers interleave their observes, A's `act` operates on B's observation |
| `task.started` (`model.py:145`) | a resumed task shares one budget clock with the original | the user takes more than 90 seconds to answer a `need_confirm`, and the first round after resuming fails as over budget |
| floor side thread (`loop.py:217`) | the side thread and the main thread can share one `Redactor` | the pseudonym table is written concurrently with no lock, which can raise `RuntimeError: dictionary changed size during iteration`; after a join timeout the thread leaks into the next step |
| `input.idle` (`System.swift:264`) | HID idle time = the user isn't touching anything | the keystrokes and clicks the helper injects go into the same HID stream → the engine can't tell "the user is back" from "I just finished typing", and v3's hands concept never actually landed |
| `contract.kept` (`contract.py:52`) | can't read the field back = can't verify | under Secure Input the CGEvent is dropped silently and recorded as "unverified" rather than a failure → easy to report done when it isn't |
| `facts.source_of` (`facts.py:14`) | text can be split on `[digits\|Latin\|CJK]` | pure Cyrillic, Arabic or Korean text splits to an empty list → returns `"no value"` (a true value) → **the anti-hallucination guard is bypassed** |

### 4. A task has to survive a restart

`Engine.tasks` is an in-memory dict with a 30-minute TTL, and `_gc` only runs inside `do()`. Restart the process and
every pending task and every `task.desktop` record (how tidy knows what to close) is gone.
`_run` holds one RLock for its whole duration, so only one task can run at a time, and `mac_observe` is blocked for as long as the task lasts.
`_replay` bypasses the step budget, the time budget and the cancellation check entirely. There is no monetary ceiling of any kind — one "step" can make seven decision round trips.

---

## Workflow

Five batches, each a set of commits that can be reviewed on its own. **No evals** (see "Validation").

### Batch 1 — Reconciliation (concept 3)

Without these fixed, all later work is built on feedback that may be misaligned.

| # | Change | Where |
|---|---|---|
| 0.1 | JSON-RPC request/response id check; a timeout also goes through `_reset()` and reconnects instead of keeping a misaligned stream | `helper.py:133-154` |
| 0.2 | the helper records when it injects; `input.idle` returns `{idle_s, since_synthetic_s}`; `take_hands` uses the real user idle | `System.swift:83-267`, `engine.py:243-260` |
| 0.3 | `IsSecureEventInputEnabled()` checked up front, input refused explicitly; `contract.kept` returns False rather than None for a secure target | `System.swift`, `act.py:180-211`, `contract.py:52` |
| 0.4 | `pieces()` switches to Unicode categories; `source_of` returns `None` rather than `"no value"` on an empty split | `facts.py:14,60` |
| 0.5 | lock the `Redactor`; after a join timeout the floor side thread's result is discarded rather than read half-built | `privacy.py`, `loop.py:217-232` |
| 0.6 | bring `_replay` under the step and time budgets and the cancellation check | `loop.py:514-540` |
| 0.7 | `resume` resets the budget window for the round (a per-round budget instead of counting from `task.started`) | `model.py:145`, `loop.py:71` |
| 0.8 | replace `_last` with a handle carrying an observation id; `act` checks the id and rebuilds `Ctx` instead of using a stale app/running/ref | `engine.py:67,145-172` |
| 0.9 | a redaction failure aborts the task instead of silently turning all text into `[withheld]` and carrying on | `privacy.py:_learn` |
| 0.10 | audit file 0600 + rotation by size | `privacy.py:197-202` |
| 0.11 | `_cancelled` is cleaned up along with task GC; `_gc` no longer fires only inside `do()` | `engine.py:225-237` |
| 0.12 | after a helper reconnect, clear every cache that depends on AX refs (`menu.snap`/`ambient`/`vision.ocr`) | `helper.py:_reset` → engine callback |
| 0.13 | cap on decision calls per step + per-task cost breaker (today a step makes up to seven round trips with no monetary ceiling at all) | `engine.py`, `decider.py` |
| 0.14 | dead code: `Outcome.final` (no producer), `Affordance.score` (declared as "relevance ranking" and never used, against the project's own principles), `ax.focused`/`Observation.focused`, `permissions.request` (wire it into doctor) | several places |
| 0.15 | three code/YAML defaults that disagree: `planner.context_chars` 800/600, `context_actions` 150/120, `settle_ms` 250/150 | `consult.py:44,47`, `effects.py:59` |

### Batch 2 — Take the language out of the safety floor (concept 1)

| # | Change | Where |
|---|---|---|
| 1.1 | drop the "no regex hit, return []" short circuit in `_floor`; classify every action about to run | `policy.py:130-158` |
| 1.2 | the batch pre-warm in `_floor_questions` covers the decider's top-ranked candidates, so `_pick` usually finds a cache hit (latency unchanged) | `policy.py:96-125` |
| 1.3 | the four `_risky()` paths with no decider: require confirmation when there is no verdict | `policy.py:76-78` |
| 1.4 | make the failure direction uniformly fail-closed: `_goal_calls_for`/`_serves_goal`'s `except DeciderError: return True` becomes a request for confirmation; `_floor`'s failure verdict is no longer cached forever | `policy.py:151,192,207` |
| 1.5 | `_nondescript` stops matching the English literals `"(no label)"` / `"in front of the app"` and judges by structure instead | `policy.py:84` |
| 1.6 | add a TTL and an app version key to the floor verdict cache | `policy.py:87` |

### Batch 3 — Capabilities from the system: new capability surfaces (concept 2, purely additive)

| # | Surface | Approach |
|---|---|---|
| 2.1 | **App Intents catalog** | parse `Contents/Resources/Metadata.appintents/extract.actionsdata` (plain JSON; 47 apps on this machine have one) into `appmodel.static_model`; fed to the planner through `AppModels.brief` as a second set of "what the app says it can do" alongside sdef. **This batch builds the catalog only, no calling** |
| 2.2 | **URL schemes become real actions** | a new `schemes` provider producing "open a ⟨scheme⟩:// URL with ⟨app⟩" with a url slot; the planner's `TRY_ITEM` gains `open_url`; goes through the floor |
| 2.3 | **Services (NSServices)** | the helper gains `services.list` (scan each app bundle's `NSServices`) and `services.perform` (`NSPerformService`, public AppKit API); plus a provider and a channel. This is the system-level bus for handing content from one app to another |
| 2.4 | **Menu bar status items** | `AX.swift` gains the scope `extras_menubar` (`kAXExtrasMenuBarAttribute`). Unlocks WiFi/Bluetooth/volume/input source and every third-party menu bar app |
| 2.5 | **Across Spaces / all windows** | `screen.windows` gains an `all` parameter (`.optionAll`) and marks whether a window is on the current screen; the windows provider offers "switch to a window on another desktop" |
| 2.6 | **Multi-display scaling** | take the screen containing the window's center point, not "the first screen it intersects" | `Vision.swift:35` |
| 2.7 | **The clipboard as a first-class channel** | helper `clipboard.read`/`clipboard.write`; the provider reports the current clipboard's types and a summary (redacted, as evidence), and the channel can write. Today the clipboard is only used inside `pasteRestoring` |
| 2.8 | **System UI becomes operable** | widen the target from "the frontmost app" to any AX-reachable process (Dock, Control Center, Notification Center) |
| 2.9 | **File contents become facts** | the `file` channel gains `read`; text files and the text layer of PDFs become a source for `Facts`. Today facts only come from the screen |
| 2.10 | **Drag and drop as a first-class affordance** | produce "drag X onto Y" from AX frames. `input.drag` has been implemented all along, but the only way to reach it is the planner guessing a label |
| 2.11 | **More AX attributes** | `batchAttrs` gains `AXURL`/`AXSelectedText`/`AXExpanded`/`AXDisclosing`/`AXNumberOfCharacters` | `AX.swift:7` |

### Batch 4 — Remove the hardcoding (concept 2, changes behavior)

**Rollout: the new behavior is on by default, and each item keeps a fallback switch in config.**

| # | Change | Switch |
|---|---|---|
| 3.1 | `apps.installed` moves to LaunchServices: enumerate app bundles with Spotlight + `NSWorkspace.urlForApplication(withBundleIdentifier:)` as a round-trip check that "LaunchServices really will launch this path". No path word list of any kind | `observe.apps.source: launchservices\|dirs` |
| 3.2 | AX actions are no longer allowlisted: name any action with `AXUIElementCopyActionDescription` (the system returns a localized description) | `observe.window.unknown_actions: offer\|skip` |
| 3.3 | role allowlist → capability probe: `AXUIElementIsAttributeSettable(AXValue)` decides typable, `AXSelected` being settable decides selectable | `observe.window.by_capability` |
| 3.4 | keyboard: reuse the existing `asciiKeyMap()` to look keys up in the current layout, replacing the US-ANSI position table; drop the `engine.key_pattern` allowlist and accept whatever the helper can parse | `input.keymap: layout\|ansi` |
| 3.5 | locale comes entirely from the system: OCR languages from `AppleLanguages` + script detection on the screen text; web `locale`/UA from the system and the real Chromium version; `usesLanguageCorrection = false` (it "corrects" UI labels into the configured language). Unify the OCR language defaults, which **contradict each other in three places** today | `observe.vision.languages: auto` |
| 3.6 | privacy: `NSDataDetector` (built into the system, many languages and regions) replaces the hand-written phone and address regexes; `_NAMEISH`/`_RUNS`/`_CLAUSE`/`_short_name_like`/`_CARRIERS` move to Unicode script properties | `privacy.detector: system\|regex` |
| 3.7 | the Apple menu is no longer skipped by position; things like "Shut Down" are caught by floor classification | `observe.menu.skip_first_menu` |
| 3.8 | loosen sdef: enum parameters become choice slots (a fixed set of values is exactly Jev's ideal input); list and record parameters can at least be supplied optionally | `observe.sdef.strict_types` |
| 3.9 | OCR reading order handles RTL and vertical text | `observe.py:514` |
| 3.10 | `arrange` no longer sorts by group size; provider order instead, plus telling the decider explicitly that "this was truncated" | `observe.py:684` |
| 3.11 | `observe.keys` widens to every physical key the helper supports (arrow keys, space, delete, home/end) | `config.yaml:77` |
| 3.12 | threshold convergence: 10 config keys that exist only as Python literals get added to the YAML, tagged `safety` (not auto-tunable) or `performance` (tunable) | several places |
| 3.13 | `group_of` uses the channel name for an unknown channel instead of calling it `"general"` | `observe.py:661` |
| 3.14 | remove the hardcoded Chinese app names in selftest and the planner probe's `"Calculator"` | `selftest.py:64,76`, `planner.py:297` |

> One knock-on effect to note: `appmodel.signature()` and `Skills.find()` both key on label text, and a label is "English scaffolding + localized app title".
> So **change the macOS interface language once and every learned app model and every routine is invalidated**. 3.2/3.3 make labels depend more on what the system says about itself (localized),
> which makes this worse; signature needs to separate the language-independent parts (role, action name, position in the hierarchy) from the label text.

### Batch 5 — Persistence and staying resident (concept 4)

| # | Change |
|---|---|
| 4.1 | task persistence: `sqlite3` (stdlib, no new dependency) written to `~/Library/Application Support/macwork/tasks.db`; `Task` becomes serializable; a task can resume after a restart, and tidy can catch up too |
| 4.2 | layer the state: split `self.cache` into truly global (appmodels/skills/host.bundles/installed/privacy.vocab/floor.verdicts) and per-task (vision.wanted/ambient/menu.snap/actions_done). Today "one task asked for OCR on this window" affects every later task, permanently |
| 4.3 | undo the single lock: `_run` no longer holds the lock throughout; serializing the helper through `Helper._lock` is enough. `mac_observe` is no longer blocked for the full length of a task |
| 4.4 | layer the budget: step / sub-goal / task, with long tasks continuing from a checkpoint rather than raising `max_steps` |
| 4.5 | stay resident: Developer ID signature + a stable bundle id + registering a LaunchAgent with `SMAppService` + restart after a crash. TCC is granted once and stops being invalidated by every rebuild |
| 4.6 | lifecycle cleanup: `atexit`/signal handlers shut down the helper subprocess and Playwright; cap the pseudonym tables of the three permanent redactors (`observe`/`web`/`learn`), which today grow without limit for the life of the process |

---

## Validation (no evals)

Not taking this machine over to run the eval suite right now. Instead:

1. **Keep the existing 85 unit tests green**, and add unit tests for each workflow. The fake-helper pattern already in `tests/conftest.py` covers most of batches 1, 2 and 4.
2. **Build a Swift test target from scratch** (there is not a single test today; CI only runs `swift build`): `helper/Tests/` covering id reconciliation, Secure Input detection, keyboard layout lookup, fingerprint, multi-display scaling. Add `swift test` to CI.
3. **A new read-only `macwork surfaces` command**: performs no action, prints what each provider sees under the current frontmost app and how many items each new capability surface (App Intents / Services / menu bar status items / windows on other Spaces / clipboard) enumerates. Without running evals, this is how to confirm by eye what the deeper integration buys, and it is the acceptance criterion for batches 3 and 4.
4. **Widen the `macwork privacy-check` corpus to 8 languages**. Today it only tests Chinese and English — and the redactor was only written for Chinese and English — so the low leak rate it reports proves itself.
5. **Extend `macwork doctor`**: report the availability of each capability surface item by item (AX / screen recording / the current Secure Input state / LaunchServices / Spotlight / how many apps with App Intents were found).
6. Fix `tests/test_engine.py::test_mcp_server_exposes_three_levels` — the `mcp` version in the current environment has no `mcp.server.mcpserver`, so `server.py:13` needs a compatibility path or pyproject needs a tighter lower bound.

Evals wait until the machine is free; by then each switch in batch 4 can be A/B compared directly.

---

## Order

Batch 1 → 2 → 3 → 4 → 5.

1 and 2 are the foundation (reconciliation + safety) and have to be finished first: building new capabilities while feedback may be misaligned and the safety floor may be inert is manufacturing your own noise.
3 is purely additive — lowest risk, most visible payoff. 4 changes behavior, so it comes after `surfaces` can show a before/after. 5 depends on a Developer ID and is independent of the other four.

---

## Progress

All five batches are done.

| Batch | Status | Where it differed from the plan |
|---|---|---|
| 1 — Reconciliation | Done | 0.14 only removed `Affordance.score`; `Outcome.final` stays (third-party channel contract). The total budget now means cumulative **working time** — counting from `task.started`, as planned, would have put the user's thinking time back into the task |
| 2 — Take the language out of the safety floor | Done | 1.2 was built and then reverted (pre-warming actions the regexes didn't hit costs +1 request per step for a hit rate around 4%); most of 1.4 was a misreading — `_goal_calls_for`/`_backs_out` already fail in the safe direction |
| 3 — New capability surfaces | Done | 2.10 was redesigned: macOS has no "draggable" attribute, so offering it per element would mean guessing; it became one action that takes both ends by name |
| 4 — Remove the hardcoding | Done | Added a prerequisite 3.0 (make the learning layer language-independent). 3.3 became a **union** rather than a replacement: AX saying "not settable" ≠ "can't be used", and the engine has clicking and keystrokes to fall back on. 3.8 was judged wrong — `simple_types` is a safety boundary, not a word list, and a specifier is code |
| 5 — Persistence and staying resident | Done | 4.4 gained `need_continue`: when the run budget is spent but the task is still making progress, it hands back for continuation instead of raising `max_steps` |

### Numbers checked on a real machine

| | Before | After |
|---|---|---|
| installed apps | 146 (scan of four directories; Finder and Safari not among them) | 405 (LaunchServices) |
| typable elements | 3 / 2 (role allowlist) | 18 / 9 (capability probe; editor / chat app) |
| menu bar status items | 0 (entirely invisible) | 20 (across 9 processes) |
| windows | 34 (current Space only) | 183 (all) |
| OCR languages | fixed `[zh-Hans, en-US]`, written three ways that contradict each other | from the system: `[en-US, zh-Hans]` |
| personal data leak rate (corpus in eight scripts) | 40.1% | 27.0% (phone numbers 15/24 → 0/24) |
| menu actions with a language-independent identity | 0 | 58/58 (Finder); unchanged across an app restart, 141/141 |
| "user idle" after a synthetic event | 25.97s → 0.15s (knocked back down by the engine itself) | separated from user input |
| tests | 85 Python / 0 Swift | **431 Python functions** / 31 Swift |
| | | (999 collected cases, but 552 of those are four parametrized static checks — `pytest` prints the split at the end of every run, because the count was being quoted as evidence and overstates what it measures) |
| one cold-start observation | 1917 ms | 332 ms |

---

## Batch 6 — unplanned, added under "develop everything that can be developed"

Once the five planned batches were done, what was left was the list I had written myself of things that were "not implemented". Each one is done, except one, and
why that one can't be done is written out too.

| What was done | Why | What was measured |
|---|---|---|
| `ax.focused` wired up: focus is told to the decider, and what was typed is read back and checked | 0 callers among the 35 RPCs | "type at the cursor" was **impossible to verify** before; typing into the wrong window was only recorded as "unverified" |
| `permissions.request` wired up: `doctor --ask` | same, 0 callers | two items measured as granted |
| **Background refresh was in the way** | the previous item measured focus at 1743ms | cold-start observation 1917ms → **332ms**, focus 1717ms → 1ms |
| `decider.kind: auto` + a runtime fallback chain | the one link with no fallback; the LocalDecider that was already written needed a manual config change to be used at all | with the key removed it falls through to `local:deepseek` |
| Canvas windows: scroll wheel, right-click by name, text the tree can't reach as a target | a window that doesn't implement `AXScrollDownByPage` had no way at all to page | TextEdit line-1 → line-7 → scrolled back |
| `revert`: put back what a task changed | `tidy` only closes what it opened; it doesn't undo what it changed | — |
| Notifications and prompts elsewhere on screen | `overlays` required an overlap, and a banner is always in a corner | one real notification: all 8 AX nodes readable in full, where before not a word of it appeared in the observation |
| Task queue: `submit`, position, cancel | a second `do()` waited forever on a lock, with no id, no position and no order | — |
| `macwork watch`: event triggers | the daemon runs, but nothing wakes it | 12 kinds of system event, measured end to end |
| Return values from Shortcuts | written to `--output-path`, which was never asked for, so every result was thrown away | — |

### Mistakes of my own, caught along the way

* `_label`'s fallback would use an element's **contents** as its label — exactly what the focus provider must not surface
* a per-call `AXUIElementSetMessagingTimeout` set on a system-wide element changes the **global default**, and never restores it
* without `kFSEventStreamCreateFlagUseCFTypes`, the `paths` an FSEvents callback receives is a `char**`; reading it as an NSArray is undefined behavior, and the first file that changed took the helper down — while the client's **silent restart** hid the crash (it now warns)
* the threshold for "is this a canvas" was **wrong twice**, see below

### A criterion can't be guessed at: an example

I got "when should pointer actions be offered" wrong twice:

1. "fewer than 8 operable nodes in the whole window" — Cursor's shell has 477 nodes and not one of them is in the editing area, so it was judged "well described"
2. coverage — geometrically, Terminal's single AXTextArea covers the whole window, so all 63 OCR boxes count as "reachable"; textually,
   `screen_text` is a 1500-character summary, so 62 of those same 63 count as "no match"

Any threshold that makes both of these come out right is "knowledge about windows" that I invented. The conclusion is **don't guess**: the scroll wheel has
only 2 options, so offer it whenever there is a window; if it won't scroll, one scroll finds that out — the main loop already records "nothing changed"
and stops offering it. Only the thing that is priced per item (turning every piece of text read into a click target) keeps a threshold.

### The one thing that didn't get done, and why

**Calling App Intents directly is not possible**, not merely undone. There is no public API for one process to execute another
app's intent — the system is what executes them (Shortcuts, Siri, Spotlight). The engine can read the intents an app declares and hand them to
the planner; the path that actually executes is a shortcut a person built themselves, and that path works now, and now
brings the return value back with it. No pretending there is another one.

---

### Known, unsolved

* **On-device NLTagger does not support personal names in Russian, Korean, Greek, Arabic or Vietnamese** — all five miss 9/9 in the corpus. This is not a code problem; `privacy-check` reports it honestly, and the remedy is `names` / `never_send`.
* **"Will Return submit?"** is something the Mac can't answer: a one-line text field and a document look the same to it, so `submit_roles` is still a role table.
* **The suite has run, and one run is not a success rate.** `evals/v2.yaml` went 12/29 → 14/29. Paired against each other
  those are 2 tasks fixed and 0 broken, which an exact McNemar test puts at p=0.50 — what chance alone does half the time.
  `macwork compare <before.json> <after.json>` is what says so; the totals on their own cannot, because the tasks that did
  not move are exactly the information a difference of totals throws away. **Repeats are still owed**, and until they exist
  no number here should be quoted as a success rate.
* **Reports are written but never kept.** The harness writes `evals/reports/{stamp}.json`, and nothing commits one, so a run on
  one machine cannot be held against a run on another — which is how 12/29 → 14/29 came to live only in a commit message.
  A baseline has to be committed for the comparison to mean anything across machines.
* **Category E (must-not) is 2/4.** The half that still fails is the safety-relevant half.
* **The on-device model is unavailable on this Mac** (`deviceNotEligible`), so the local fallback in `decider.kind: auto`
  goes to a network backend, and "decisions with no network at all" has not been verified here.
