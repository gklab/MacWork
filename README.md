# MacWork

**A worker that lives in your Mac.** [macwork.org](https://macwork.org)

MacWork operates a Mac through its **native interfaces** — menus, the Accessibility tree, apps, files, Shortcuts,
Services, App Intents — with [TypeSafe Jev](https://docs.typesafe.ai) making the fast "which one / is it done"
decisions. Every one of those is the Mac describing itself; nothing here knows the name of a single app or
website, because you cannot know what someone has installed.
It works the way a colleague would: it looks at the screen, decides one step at a time, asks before anything
irreversible, hands the keyboard back the moment you touch it, and tidies up after itself.

Most computer-use agents look at screenshots and click pixels. MacWork reads what macOS already knows in
structured form and acts through the same interfaces assistive technology uses, which is faster, steadier and
keeps screenshots off the network entirely.

```
caller (Claude Code, a voice assistant, your script)
   │  MCP: mac_do / mac_resume / mac_observe / mac_act
   ▼
macwork  (Python engine: policy, privacy, decisions)
   │  observe → one Jev request (which action + which kind of move) → safety floor → act → wait for the UI to settle
   │  local JSON-RPC
   ▼
macwork-helper   (small signed Swift app that holds the macOS permissions)
   AX trees & actions · AXObserver events · keyboard/mouse · AppleScript · on-device name tagging
```

## Why it is built this way

* **Jev is a System-1 model**: it answers typed questions (pick one of ≤255 options, yes/no, score) with
  calibrated probabilities in about a second, and writes no text. So the engine turns every screen into a list
  of *affordances* and asks one request per step, with the questions fanned out in parallel:
  *next action · what kind of move is right now (act, done, rethink, ask the user, blocked, impossible) · did
  the last step work · is it really done · is this screen asking to confirm something destructive*.
* **Callers stay in charge of language and planning.** Text to type, search queries and file names come from
  the caller. A task that needs them stops with `need_input` instead of guessing.
* **The engine has no strategy of its own.** Whether to act, look for another route, ask the user or stop is
  the decider's choice on the live screen (the `move` question); a different route comes from the planner. The
  engine keeps only facts (what changed nothing from which screen), budgets, and the safety floor: destructive
  or outward-facing steps (`policy.yaml`) return `need_confirm`.
* **Native first, pixels never (yet).** Candidates come from the app's menus (every command, with its
  shortcut), the focused window's Accessibility tree (buttons, fields, lists), running/installed apps (with
  localized names), Spotlight, Shortcuts, Services and App Intents. Electron/Chromium apps get their tree switched on
  (`AXManualAccessibility`). The engine verifies each step by waiting for Accessibility events, not by sleeping.
* **No hand-written operating knowledge.** No app names, no table of "useful" shortcuts, no word lists that
  decide what to click or what is safe to explore, no relevance ranking. App commands come from the app itself
  (its menus with their own shortcuts, its Accessibility tree, its scripting dictionary); what to do with them is
  judged by the decider, and routes it cannot see are proposed by the planner. The only fixed list left that
  decides anything is the physical keys (Return, Escape, Tab, paging); the safety floor's word list only orders
  the work, it does not judge. Question wording, thresholds, redaction rules,
  providers and channels live in YAML; new providers/channels/deciders plug in through entry points.
* **Too many options? Fold, don't guess.** Jev takes at most 255 options. When a screen offers more, the largest
  groups (a big menu, the list of installed apps) become one "look into …" entry each, which the decider can open
  like a person opens a menu — nothing is dropped by a relevance guess.

## How it copes with an app it has never seen

Nothing in the engine names an app. Everything comes from what the Mac can tell about it:

1. **See everything** – the menu bar (every command, with shortcuts), the focused window's Accessibility
   tree with *every* action an element offers (press, context menu, increase/decrease, scroll), open pop-up and
   context menus, and the Help menu's search field to find a command by name. Electron/Chromium apps get their
   tree switched on. Where the tree is thin (canvas and custom-drawn UIs) or a control has no label, the window
   is read **on-device** (ScreenCaptureKit + Vision OCR): unlabeled controls are named by the text in or near
   them, and on-screen text becomes clickable. An optional local image describer (`observe.vision.describer_cmd`)
   can name icons. Screenshots never leave the Mac.
2. **Understand it** – an app model per `bundle_id@version`: its scripting dictionary (commands it can do
   without UI, run as typed AppleScript literals, never raw code), URL schemes, document types, and a screen
   map learned while operating — which action on which screen led where, and which did nothing (flagged to the
   decider). `macwork learn <app>` builds the map ahead of time, exploring only what the decider judges to just show
   or navigate, and undoing each step with whatever the decider picks as the way back.
3. **Plan when it matters** – a planner (System 2) is consulted when the decider calls for another route
   (`rethink`), as a second opinion before giving up (`blocked` / `impossible`), or when a step needs text: it
   splits the goal into sub-goals, writes what must be typed, and suggests concrete moves (labels on screen, key
   combos, text) — offered to the decider as options, never run unasked.
   Backends: DeepSeek's official API, any local OpenAI-compatible server (LM Studio, Ollama, mlx_lm.server),
   Claude, or Apple's on-device Foundation Models. `auto` prefers on-device ones; cloud planners only ever see
   redacted text, and pseudonyms are restored before anything is typed.
4. **Get faster with use** – tasks that worked become routines (stored by meaning, not coordinates) that Jev
   can pick in one decision and that replay step by step with every element looked up again on the live screen.

## Three levels of control

| level | tools | who drives |
|---|---|---|
| goal | `mac_do(goal, inputs, app)` → `done` · `failed` · `need_input` · `need_confirm` · `ambiguous` · `need_continue`; continue with `mac_resume` | Jev, asking the caller when it must |
| step | `mac_observe(app, goal)` → affordances; `mac_act(id, params, confirm)` | the caller — the decider is asked nothing but the safety floor's "what does this action do" |

Looking something up on the web is a goal like any other: `mac_do("find out when Python 3.14 came out")`
drives the browser that is already on this Mac, with the sessions you are already signed into. There is no
bundled browser and no built-in list of search engines — a browser is an app, and it is operated like any
other app. What that costs: a page is read from the screen (on-device OCR), so a long one takes a scroll or
two, because Chrome does not put page content in the Accessibility tree at all (measured: 41 nodes, all
toolbar, with every accessibility flag set).

## Install

Requires macOS 13+, Python 3.10+, and the Swift toolchain (Xcode or Command Line Tools).

```sh
pip install -e ".[web]"            # or: uv pip install -e ".[web]"
macwork helper install                 # builds the Swift helper into ~/Applications/MacWork Helper.app
macwork key set                        # stores your TypeSafe API key in the login Keychain
macwork doctor
macwork surfaces                       # read-only: what each capability surface can see right now
```

Grant **MacWork Helper** Accessibility access (System Settings ▸ Privacy & Security ▸ Accessibility). Apps you
drive through AppleScript ask for Automation permission the first time. During development the helper can run
as a child of your terminal instead (`helper.mode: stdio`), inheriting the terminal's permissions.

macOS grants Accessibility to a *code signature*, not to a path. `helper install` uses a Developer ID from
your Keychain if it finds one (or `--sign <identity>`), so the grant survives rebuilds; without one it signs
ad hoc and you will be asked again every time you reinstall.

To keep the engine running instead of starting it per call:

```sh
macwork daemon install     # a LaunchAgent: starts at login, comes back from a crash
macwork daemon status
```

## Use

```sh
macwork observe --app Finder                          # what can be done right now
macwork do "open Calculator and compute 23×47"        # the decider drives; asks you when it must
macwork do "fill in the recipient" -i text=me@example.com
macwork web "what changed in Python 3.14" --query "Python 3.14 what's new"
macwork serve                                         # MCP over stdio
macwork learn "Preview"                               # explore an app safely, build its model
macwork skills                                        # learned routines
macwork eval --suite evals/heldout.yaml              # apps never used while tuning -> evals/reports/
macwork key set deepseek                              # planner key (Keychain); or DEEPSEEK_API_KEY
```

As an MCP server (e.g. Claude Code):

```sh
claude mcp add macwork -- macwork serve
```

For long-running clients (voice assistants), run `macwork serve --transport streamable-http` and connect to
`http://127.0.0.1:8977/mcp`.

## Privacy

Everything the decider sees passes through `privacy.py` on this Mac first:

* names found by on-device NaturalLanguage (clause by clause, in one batch) and configured patterns (email,
  phone, ID and card numbers, IPs) become stable pseudonyms such as `⟦PERSON_1⟧` — the same person gets the
  same token for the whole task, so decisions still work;
* names from the Mac's own vocabulary (installed app names) are never mistaken for people or companies;
* `privacy.redact.names` hides names you list, whatever the tagger thinks; `never_send` withholds whole fields;
  home paths become `~`; if tagging fails, text is withheld rather than sent;
* every request is written, as sent, to a local audit log (`macwork audit`); with `audit.dry_run: true` nothing
  is sent at all;
* no screenshots, no telemetry. The API key lives in the Keychain.

The tagger is good but not perfect; review `macwork audit` for your own workloads and extend the rules.

## Safety

* `policy.yaml`'s `confirm.categories` is a deliberate safety floor, not a way of deciding what to do:
  deleting, sending, paying, saving/overwriting, pasting the clipboard, signing in, agreeing to terms,
  installing or updating always need the caller's confirmation. **Every action about to run is classified**
  (from what it is and where it sits, never from screen text, so content on a page cannot argue it out of the
  floor) — the words in `policy.yaml` are only a hint about what to classify first and which categories to
  offer, because they are written in two languages and a German `Löschen` or a Japanese `削除` matches none of
  them. A miss must never read as "safe". `deny.bundle_ids` / `deny.patterns` are never touched; raw AppleScript
  is off by default.
* On a screen the decider judges risky, any other action needs confirmation too — unless the decider, asked about
  that very action, judges that it only backs out (cancel, close, later).
* While the screen is locked only non-UI channels run (`engine.locked_channels`).
* `engine.mode: foreground` yields to you while you type or move the mouse.
* Web and file contents are treated as data; goals come only from the caller.

## Configuration

Defaults live in `macwork/defaults/{config,questions,policy,privacy}.yaml`. Override any key in
`~/.config/macwork/<same name>.yaml`, or with environment variables such as
`MACWORK__ENGINE__MAX_STEPS=20`.

## Extending

```toml
[project.entry-points."macwork.providers"]
my_provider = "my_pkg:provide"        # (ctx, observation) -> None, appends Affordances
[project.entry-points."macwork.channels"]
my_channel = "my_pkg:execute"         # (ctx, affordance, params) -> Outcome
[project.entry-points."macwork.deciders"]
my_decider = "my_pkg:MyDecider"       # decide(state, questions) -> answers, TypeSafe wire shape
```

List the provider in `observe.providers`, or pick the decider with `decider.kind`.

## Measuring flexibility

`macwork eval` runs a suite of harmless tasks, checks each result on screen, cleans up, and writes a Markdown/JSON
report to `evals/reports/`. `evals/unseen.yaml` (Calculator, Finder, VLC, Numbers, Dictionary, …) was
run after every change while the engine was built, so it is **no longer unseen** — treat it as a regression
suite. `evals/heldout.yaml` holds apps that were never used while building or tuning (Clock, Audio MIDI Setup,
Disk Utility, System Information, Script Editor, Safari, Maps, ColorSync Utility); that is the
number to report. A locked screen makes a task `error`, not a failure.

### Latest results (2026-09-19, Apple silicon Mac, macOS 26; planner: a 30B model served locally by mlx_lm.server)

Strict scoring: the engine must itself report `done` and the on-screen check must hold; tasks already satisfied
before they start are reported `invalid` and not counted.

| round | what changed | passed (valid) | median s/task | cost |
|---|---|---|---|---|
| 1 | first run | 10/15 (lenient scoring) | 13.6 | $0.020 |
| 2 | verified-front typing, key-code typing on an ASCII layout, anti-loop, learning fix | 11/13 genuine | 7.9 | $0.014 |
| 4 | per-text targets in unlabeled containers, softer done veto, strict scoring | **8/11 (73%)** | 14.2 | $0.014 |

| 6 | on-screen evidence for every question (visible controls, menu check marks) | 12/14 (86%) | 8.2 | $0.020 |
| 10 | performance work (below) | 12/14 (86%) | 6.7 | $0.020 |
| 11 | planner proposes concrete moves, exploration, all windows, "blocked" diagnosis — with learned routines | 13/14 (93%) | 8.0 | $0.013 |
| 12 | same, **fresh** (no routines, empty app models: reasoning only) | 12/14 (86%) | 8.2 | $0.020 |
| 13 | fresh; planner fallback chain, cut-off planner replies salvaged, out-of-budget diagnosis | **13/13 (100%)** ¹ | **6.1** | $0.014 |

¹ one task was pre-satisfied (its result was still on screen) and excluded; run alone just before, it passed in
fresh mode by dismissing a notice and retrying the shortcut (6 steps, 24.8 s).

Fresh mode is the default for `macwork eval`: it measures reasoning on the live screen, not recall.

#### Current engine (2026-09-19, after removing the hand-written parts)

| suite | passed | median s/task | cost |
|---|---|---|---|
| `heldout.yaml` — 10 apps never used while building | **8/10** | 12.0 → 10.3 | $0.010 |
| `unseen.yaml` — regression suite | 15/15 | 8.1 → 6.0–7.5 | $0.014–0.019 |

(Before → after the pipelining work below; task times include the harness's own 0.8 s pause after each task.)

Held-out failures: *System Information* — the engine stopped at the hardware overview, which already shows
the machine's memory size, while the check wanted the Memory section (the goal allows both readings);
*Maps* — Apple Maps on this Mac answers "未找到匹配的地点" for any search, also when opened directly with a
`maps://` link, so the task cannot succeed here; the engine now says so after one step instead of wandering.
Two held-out checks were corrected after the first run because they expected the wrong text for a correct
result (the stopwatch reads 分段/单段 on this macOS; the window is titled "MIDI工作室").

What the held-out runs taught the engine (all general, none app-specific): select rows of lists and tables
(including rows scrolled out of view) and name them by their text; ignore text fields the system says cannot be
edited; see prompts from other processes over the app (a permission request) and answer them by least privilege
— declining what the goal does not need, never granting without the user; a `wait` move for apps still loading;
drop floor-gated actions the goal never asked for instead of asking the user about them; tell the decider when
an app has no window open and offer to bring its main window back; diagnose early when no new route is to be
had; fold very large groups (every installed app) one level down; close apps a task opened when they are no
longer needed.

Rounds 1–13 ran on the engine *before* the hand-written parts were removed (a table of common shortcuts, word
lists for "dismissable" buttons and "safe to explore" menus, relevance ranking, a threshold-driven
explore/ask/stuck state machine tuned on this very suite). Those numbers are kept for the record; the current
engine has to earn its own, first on `evals/heldout.yaml`.

When the obvious route fails, the decider says so (`rethink`) and the planner proposes another route, whose
moves are offered as options; actions that changed nothing are not offered again from the same screen; menu
items an app ignores when pressed are retried with their own key equivalent; when only the user can continue
(sign in, a password, a permission) the result is `blocked` with the reason — after a second opinion from the
planner — and nothing is ever signed in for anyone. Running out of steps is diagnosed by the decider too.

### Pipelining (second performance pass)

Measured per step before: observe ~450 ms (325 of it OCR), decide ~410–700 ms, act ~100 ms, wait 400–1000 ms
(2.5 s when nothing visibly changed). Now:

| stage | before | after | how |
|---|---|---|---|
| observe | ~450 ms | **~110 ms** | OCR only where the tree is sparse, or when the decider asks to read the unlabeled controls; one read per window content (fingerprint cache) |
| wait for the UI | 400–1000 ms | **~220–440 ms** | *speculate*: decide on the first calm screen (80 ms) and check the window fingerprint when the answer arrives — moved → look again (at most once per step). "done" is judged only on a screen that has really settled |
| live windows (meters, statistics) | waited for a quiet that never came | ~220 ms | a window that changes with nobody acting is measured once and remembered; no quiet wait, no fingerprint check for it |
| closing apps after a task | ~0.4 s before returning | 0 | in the background after the result is returned |
| first decision after a pause | + TLS handshake | overlapped | the connection is warmed while the first screen is read |

The decider's own ~410 ms is a floor here: it did not change between 10 and 200 options or 1 and 6 questions;
most of it is the network round trip to the service (~220 ms here; a tunnelled connection makes it worse).

### Where the time goes, and what was done about it

Measured per step (`decision.timing` in every task trace):

| stage | before | after | how |
|---|---|---|---|
| waiting for the UI to react | 2.5 s per step on apps that post no Accessibility notifications (SwiftUI) | ~0.25 s | the helper also polls a cheap fingerprint of the window (0.2–60 ms) and returns once the window is **quiet** again — first-change-only returned mid-transition and cost accuracy |
| "is it really done?" | a second decider request per task | 0 extra | asked in the same request as every step (questions are judged in parallel) |
| name tagging for redaction | re-tagged every task | once | memoized across tasks (menus and buttons repeat) |
| menu tree | rescanned every step (400 ms on large menus) | reused | cached per app+window until any action; stale element refs invalidate it |
| local planner | 27–36 s when its weights had been paged out | warm | woken in the background at task start; 20 s timeout so a cold model never stalls a task |
| typing | IME could turn "jev" into "je'v" | exact | waits until the ASCII layout is really active, else pastes (clipboard restored) |

A 6-step calculator task went from 19.3 s to 5.8 s; what remains is mostly the decider's own round trip
(~0.4 s per step).

## Status and limits

Early. The decider's accuracy on your own tasks is something to measure (`macwork eval`), not assume; Jev accepts
text only, so vision is converted to text on the Mac first; drag-and-drop and precise gestures are not
covered; system permission prompts, passwords and captchas are deliberately never operated.

## License

Apache-2.0

---

MacWork is an independent open-source project. It is not affiliated with, endorsed by, or sponsored by Apple
Inc. "Mac" and "macOS" are trademarks of Apple Inc.

Both suites use apps that ship with macOS, so anyone can run them. Tasks for apps only you have installed belong
in `evals/local.yaml`, which is not part of the repo (`macwork eval --suite evals/local.yaml`).
