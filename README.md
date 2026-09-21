<div align="center">

# MacWork

**A worker that lives in your Mac.**

[![CI](https://github.com/gklab/MacWork/actions/workflows/ci.yml/badge.svg)](https://github.com/gklab/MacWork/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![macOS](https://img.shields.io/badge/macOS-13%2B-black.svg)](#requirements)

[macwork.org](https://macwork.org) · [Design](docs/design-v4.md) · [Benchmarks](docs/benchmarks.md) · [Extending](docs/extending.md)

</div>

---

MacWork operates a Mac through its **native interfaces** — the Accessibility tree, menus, Services, URL
schemes, Shortcuts, App Intents, LaunchServices, Spotlight — with [TypeSafe Jev](https://docs.typesafe.ai)
making the fast *which one / is it done* decisions.

Most computer-use agents screenshot the display and click pixels. MacWork reads what macOS already knows in
structured form and acts through the same interfaces assistive technology uses: faster, steadier, and no
screenshot ever leaves the machine.

It works the way a colleague would — looks at the screen, decides one step at a time, asks before anything
irreversible, hands the keyboard back the moment you touch it, and tidies up after itself.

```
caller  (Claude Code · a voice assistant · your script)
   │  MCP:  mac_do · mac_submit · mac_resume · mac_observe · mac_act · mac_revert
   ▼
macwork               Python engine — policy, privacy, decisions, budgets
   │  observe → one decider request (which action · what kind of move) → safety floor → act → wait for the UI
   │  local JSON-RPC over a 0600 socket
   ▼
macwork-helper        small signed Swift app holding the macOS permissions
   AX trees & actions · AXObserver events · keyboard & mouse · ScreenCaptureKit + Vision
   NSServices · LaunchServices · Spotlight · FSEvents · on-device name tagging
```

## Design

**Nothing here knows the name of a single app.** You cannot know what someone has installed, so every
capability is the Mac describing itself — an app's own menus and their shortcuts, the actions an element
says it supports, the UTType hierarchy rather than a list of extensions, `AXUIElementIsAttributeSettable`
rather than a table of roles, LaunchServices rather than four hardcoded directories. The rule and how to
keep it are written down in [docs/extending.md](docs/extending.md).

**The engine has no strategy of its own.** Whether to act, look for another route, ask the user or stop is
the decider's call on the live screen. The engine keeps only facts (what changed nothing, from which
screen), budgets, and the safety floor.

**The decider answers typed questions, never prose.** Jev picks one of ≤255 options, or yes/no, with
calibrated probabilities in about a second. So every screen becomes a list of *affordances* and each step
is one request with its questions fanned out in parallel: next action · what kind of move · did the last
step work · is it really done · is this screen asking to confirm something destructive.

**Callers own language and planning.** Text to type, queries and file names come from the caller. A task
that needs them stops with `need_input` rather than inventing them.

**Too many options? Fold, don't rank.** Above 255 options the largest groups become one "look into …" entry
each, which the decider opens like a person opens a menu. Nothing is dropped by a relevance guess.

## Quick start

### Requirements

macOS 13+ · Python 3.10+ · Swift toolchain (Xcode or Command Line Tools)

```sh
pip install -e ".[dev]"
macwork helper install      # builds the Swift helper into ~/Applications/MacWork Helper.app
macwork key set             # your TypeSafe API key, into the login Keychain
macwork doctor --ask        # opens the Accessibility and Screen Recording prompts
macwork doctor --planners   # asks each backend for a real answer — this is the one that fails loudly
```

`macwork doctor --planners` is worth running before anything else: without a decider that can answer, the
engine does not take a single step, and it says so rather than showing a green light.

macOS grants Accessibility to a *code signature*, not a path. `helper install` uses a Developer ID from
your Keychain if it finds one (or `--sign <identity>`), so the grant survives rebuilds. In development the
helper can run as a child of your terminal instead (`helper.mode: stdio`) and inherit its permissions.

### Use

```sh
macwork surfaces                                  # read-only: what every capability surface can see, and its cost
macwork do "open Calculator and compute 23×47"
macwork do "fill in the recipient" -i text=me@example.com
macwork revert <task-id>                          # put back what a task changed, newest change first
macwork watch                                     # run tasks when the Mac announces something
macwork serve                                     # MCP over stdio
```

As an MCP server:

```sh
claude mcp add macwork -- macwork serve
```

For long-running clients, `macwork serve --transport streamable-http` and connect to
`http://127.0.0.1:8977/mcp`. To keep it resident, `macwork daemon install` writes a LaunchAgent.

## What it can reach

| surface | asked of |
|---|---|
| every command an app has | its menu bar, with each item's own key equivalent |
| what a control can do | `AXActionNames` / `AXActionDescription` on the element itself |
| whether a field takes text | `AXUIElementIsAttributeSettable`, not a table of roles |
| windows the app has | all of them, on every Space — not just the focused one |
| prompts and notifications | any process's readable window with no Dock presence, overlapping or not |
| menu-bar status items | `kAXExtrasMenuBarAttribute`, across every process that has one |
| what an app can do without its UI | scripting dictionary · `NSServices` · `CFBundleURLTypes` · App Intents |
| installed apps | Spotlight + LaunchServices (405 here, against 146 by scanning directories) |
| a window the tree cannot describe | ScreenCaptureKit + Vision, on-device; text becomes clickable, the wheel scrolls |
| whole documents | the window read in full, not just the part on screen |
| the person's own automations | `shortcuts`, with what the shortcut returns |
| files | UTType hierarchy, `NSDataDetector`, PDFKit |
| events to wake on | NSWorkspace · `com.apple.screenIsLocked` · FSEvents · pasteboard change count |

An app the engine has never seen is handled by the same surfaces, plus a model learned while operating it
(`bundle_id@version`: which action on which screen led where, and which did nothing). `macwork learn <app>`
builds that map ahead of time, exploring only what the decider judges to merely show or navigate.

Looking something up on the web is a goal like any other: it drives the browser already on this Mac, signed
into the sessions you are already signed into. There is no bundled browser and no built-in list of search
engines — a browser is an app. The cost is honest: Chrome puts no page content in the Accessibility tree at
all (measured: 41 nodes, all toolbar, with every accessibility flag set), so a page is read from the screen
on-device and a long one takes a scroll or two.

## Two levels of control

| level | tools | who decides |
|---|---|---|
| **goal** | `mac_do(goal, inputs, app)` → `done` · `failed` · `need_input` · `need_confirm` · `ambiguous` · `need_continue`; continue with `mac_resume`. `mac_submit` queues it and returns at once | the decider, asking the caller when it must |
| **step** | `mac_observe(app, goal)` → affordances; `mac_act(id, params, confirm)` | the caller — the decider is asked nothing but "what does this action do" |

One Mac, one keyboard: tasks run one at a time, in a queue the caller can see and cancel (`mac_status`).

## Safety

The safety floor in `policy.yaml` is a floor, not a way of deciding what to do. Deleting, sending, paying,
saving over something, pasting the clipboard, signing in, agreeing to terms, installing — all return
`need_confirm`.

* **Every action about to run is classified**, from what it is and where it sits, never from screen text —
  so text on a page cannot argue an action out of the floor. The word lists in `policy.yaml` only say what
  to classify first; they are two languages wide, and a German `Löschen` matching nothing must never read as
  safe.
* **A verdict cannot release what it does not cover.** "It only changes what this window holds" is a
  judgement about prose; "this is the channel that moves files" is a fact about the action, and where a fact
  is available the classifier does not overrule it.
* On a screen the decider judges risky, every other action needs confirmation too — unless the decider,
  asked about that action, judges that it only backs out.
* While the screen is locked, only non-UI channels run. `engine.mode: yield` (the default) gives the
  keyboard back the moment you touch it.
* `deny.bundle_ids` / `deny.patterns` are never touched; raw AppleScript is off by default; the app the
  caller itself runs in is never driven.
* What a task changed can be put back (`mac_revert`) using each app's own undo command — this repo does not
  know the word. It refuses while you have been using the Mac, and stops at the first change it cannot undo.

## Privacy

Everything the decider sees passes through `privacy.py` on this Mac first.

* Names found by on-device NaturalLanguage and configured patterns (email, phone, ID and card numbers, IPs,
  addresses) become stable pseudonyms — `⟦PERSON_1⟧` is the same person for the whole task, so decisions
  still work.
* Names from the Mac's own vocabulary (installed apps) are never mistaken for people.
* `privacy.redact.names` hides names you list whatever the tagger thinks; `never_send` withholds whole
  fields; home paths become `~`; **if tagging fails, text is withheld rather than sent**.
* Every request is written as sent to a local audit log (`macwork audit`). `audit.dry_run: true` sends
  nothing at all.
* No screenshots leave the machine, no telemetry. The API key lives in the Keychain.

`macwork privacy-check` measures the leak rate on a synthetic corpus and reports it honestly — including
that on-device tagging finds no personal names at all in Russian, Korean, Greek, Arabic or Vietnamese.

## Measuring

```sh
macwork eval --suite evals/v2.yaml                   # → evals/reports/{stamp}.{json,md}
macwork eval --suite evals/v2.yaml --compare evals/reports/<earlier>.json
macwork compare <before>.json <after>.json           # reads reports; drives nothing
```

`evals/v2.yaml` is the comprehensive suite: 29 tasks over navigation, multi-step, cross-app, answers,
must-not and prompt injection. It currently passes **14/29**.

That number is one run, and a single run is not a success rate. `compare` pairs the tasks by id, names what
moved in each direction and runs an exact McNemar test on the ones that did — which is how we know that the
improvement from 12/29 to 14/29 is 2 fixed and 0 broken, or p = 0.50: what chance alone does half the time.
It refuses to compare across a different suite or a different decider. Repeats are still owed.

Suites use apps that ship with macOS, so anyone can run them. Tasks for apps only you have installed belong
in `evals/local.yaml`, which is not in the repo. Full history in [docs/benchmarks.md](docs/benchmarks.md).

## Configuration

Defaults are in `macwork/defaults/{config,questions,policy,privacy}.yaml`. Override any key in
`~/.config/macwork/<same name>.yaml` or with `MACWORK__SECTION__KEY=value`. A test fails if the defaults
ship a key nothing reads — a setting that does nothing reads as a promise.

## Extending

```toml
[project.entry-points."macwork.providers"]
my_provider = "my_pkg:provide"      # (ctx, observation) -> None, appends Affordances
[project.entry-points."macwork.channels"]
my_channel  = "my_pkg:execute"      # (ctx, affordance, params) -> Outcome
[project.entry-points."macwork.deciders"]
my_decider  = "my_pkg:MyDecider"    # decide(state, questions) -> answers
```

List the provider in `observe.providers`, or pick the decider with `decider.kind`. Before adding anything,
read [docs/extending.md](docs/extending.md) — it is one rule ("do not write knowledge about the outside
world into this repo") and three questions for applying it.

## Status and limits

**Early.** The decider's accuracy on your own tasks is something to measure, not assume.

Known and open: repeats on the eval suite, and a committed baseline to compare against · category E
(must-not) at 2/4 · drag-and-drop beyond named endpoints, and precise gestures · on-device name tagging for
five languages it does not support · calling an app's App Intents directly, for which macOS offers no
public API to any process but the system (a Shortcut the person built is the route that works).

Deliberately never operated: system permission prompts, passwords and captchas.

## License

Apache-2.0. See [LICENSE](LICENSE).

---

<sub>MacWork is an independent open-source project. It is not affiliated with, endorsed by, or sponsored by
Apple Inc. "Mac" and "macOS" are trademarks of Apple Inc.</sub>
