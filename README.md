<div align="center">

# MacWork

**An open-source computer-use agent for macOS, built on the Accessibility API instead of screenshots.**

[![CI](https://github.com/gklab/MacWork/actions/workflows/ci.yml/badge.svg)](https://github.com/gklab/MacWork/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![macOS](https://img.shields.io/badge/macOS-13%2B-black.svg)](#installation)
[![MCP](https://img.shields.io/badge/MCP-server-8A2BE2.svg)](#mcp-server)

[Website](https://macwork.org) · [Design](docs/design-v4.md) · [Benchmarks](docs/benchmarks.md) · [Extending](docs/extending.md) · [Triggers](docs/triggers.md)

</div>

---

MacWork is an AI agent that operates a Mac the way a person does — any app, including ones it has never
seen — and exposes that ability as a command-line tool and a
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for Claude Code, Claude Desktop and any
other MCP client.

Where most computer-use agents screenshot the display and click pixels, MacWork reads what macOS already
knows in structured form — the Accessibility (AX) tree, menus, Services, URL schemes, Shortcuts,
LaunchServices, Spotlight — and acts through the same interfaces assistive technology uses. Each step is one
typed question to a fast decision model ([TypeSafe Jev](https://docs.typesafe.ai)): *which of these actions,
and is it done?*

| | screenshot-based agents | MacWork |
|---|---|---|
| sees | pixels, through a vision model | the Accessibility tree, menus and system registries; on-device OCR only where an app exposes no tree |
| acts by | clicking coordinates | AX actions, menu commands, key equivalents, Services, URL schemes, Shortcuts |
| decides with | free-form generation | a choice among ≤255 typed options, with calibrated probabilities, in about a second |
| leaves the machine | screenshots | redacted text only — never a screenshot |
| app support | whatever the model recognises | whatever the app declares about itself; no per-app code |

## Contents

[Features](#features) · [Architecture](#architecture) · [Installation](#installation) · [Usage](#usage) ·
[MCP server](#mcp-server) · [Capabilities](#capabilities) · [Design principles](#design-principles) ·
[Safety](#safety) · [Privacy](#privacy) · [Evaluation](#evaluation) · [Configuration](#configuration) ·
[Extending](#extending) · [FAQ](#faq) · [Status](#status-and-limitations)

## Features

- **Works with apps it has never seen.** Nothing in the repository knows the name of a single app. Every
  capability is the Mac describing itself, in whatever language the Mac is set to.
- **Native macOS automation.** Accessibility tree and actions, menu bar and key equivalents, menu-bar
  extras, Services, URL schemes, scripting dictionaries, Shortcuts, LaunchServices, Spotlight, FSEvents.
- **MCP server and CLI.** Goal-level (`mac_do`) and step-level (`mac_observe` / `mac_act`) control, a task
  queue, resumable tasks, and event triggers (`macwork watch`).
- **A safety floor.** Deleting, sending, paying, overwriting, signing in, installing and running code stop
  for confirmation. Actions are classified from what they are, never from text on the screen, so a page
  cannot talk its way past it.
- **Privacy by construction.** Names, emails, phone and card numbers are replaced with stable pseudonyms on
  this Mac before any request is made. Every request is written to a local audit log.
- **Undo.** `macwork revert` puts back what a task changed, using each app's own undo command.
- **Measured, not asserted.** An evaluation suite with paired significance tests, a step profiler, and a
  privacy leak check ship with the project.
- **Pluggable.** Observation providers, action channels and decision models register through Python entry
  points.

## Architecture

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

The caller owns language and planning; the engine owns facts, budgets and the safety floor; the decider
makes every judgement call on the live screen. The full design is in [docs/design-v4.md](docs/design-v4.md).

## Installation

Requirements: macOS 13+, Python 3.10+, a Swift toolchain (Xcode or the Command Line Tools), and a
[TypeSafe](https://docs.typesafe.ai) API key — or a local model, see [FAQ](#faq).

```sh
git clone https://github.com/gklab/MacWork.git && cd MacWork
pip install -e ".[dev]"
macwork helper install      # builds the Swift helper into ~/Applications/MacWork Helper.app
macwork key set             # your TypeSafe API key, into the login Keychain
macwork doctor --ask        # opens the Accessibility and Screen Recording prompts
macwork doctor --planners   # asks each backend for a real answer — this is the check that fails loudly
```

Run `macwork doctor --planners` before anything else: without a decider that can answer, the engine does not
take a single step, and it says so rather than showing a green light.

macOS grants Accessibility to a *code signature*, not a path. `helper install` uses a Developer ID from your
Keychain if it finds one (or `--sign <identity>`), so the grant survives rebuilds. In development the helper
can run as a child of your terminal instead (`helper.mode: stdio`) and inherit its permissions.

## Usage

```sh
macwork surfaces                                  # read-only: what every capability surface can see, and its cost
macwork do "open Calculator and compute 23×47"
macwork do "fill in the recipient" -i text=me@example.com
macwork revert <task-id>                          # put back what a task changed, newest change first
macwork watch                                     # run tasks when the Mac announces something
macwork audit                                     # every request, exactly as it was sent
```

### MCP server

```sh
claude mcp add macwork -- macwork serve           # Claude Code
```

Any other MCP client takes the same command over stdio:

```json
{ "mcpServers": { "macwork": { "command": "macwork", "args": ["serve"] } } }
```

For long-running clients, `macwork serve --transport streamable-http` listens on
`http://127.0.0.1:8977/mcp`, and `macwork daemon install` keeps it resident as a LaunchAgent.

### Two levels of control

| level | tools | who decides |
|---|---|---|
| **goal** | `mac_do(goal, inputs, app)` → `done` · `failed` · `need_input` · `need_confirm` · `ambiguous` · `need_continue`; continue with `mac_resume`. `mac_submit` queues it and returns at once | the decider, asking the caller when it must |
| **step** | `mac_observe(app, goal)` → affordances; `mac_act(id, params, confirm)` | the caller — the decider is asked nothing but "what does this action do" |

One Mac, one keyboard: tasks run one at a time, in a queue the caller can see and cancel (`mac_status`,
`mac_cancel`).

## Capabilities

| surface | asked of |
|---|---|
| every command an app has | its menu bar, with each item's own key equivalent |
| what a control can do | `AXActionNames` / `AXActionDescription` on the element itself |
| whether a field takes text | `AXUIElementIsAttributeSettable`, not a table of roles |
| windows the app has | all of them, on every Space — not just the focused one |
| prompts and notifications | any process's readable window with no Dock presence, overlapping or not |
| menu-bar status items | `kAXExtrasMenuBarAttribute`, across every process that has one |
| what an app can do without its UI | scripting dictionary · `NSServices` · `CFBundleURLTypes` |
| installed apps | Spotlight + LaunchServices (405 here, against 146 by scanning directories) |
| a window the tree cannot describe | ScreenCaptureKit + Vision, on-device; text becomes clickable, the wheel scrolls |
| whole documents | the window or the file read in full, not just the part on screen |
| the person's own automations | `shortcuts`, with what the shortcut returns |
| files | UTType hierarchy, `NSDataDetector`, PDFKit; which file a window is showing (`AXDocument`) |
| events to wake on | NSWorkspace · `com.apple.screenIsLocked` · FSEvents · pasteboard change count |

An app the engine has never seen is handled by the same surfaces, plus a model learned while operating it
(`bundle_id@version`: which action on which screen led where, and which did nothing). `macwork learn <app>`
builds that map ahead of time, exploring only what the decider judges to merely show or navigate.

Browsing the web is a goal like any other: it drives the browser already on this Mac, signed into the
sessions you are already signed into. There is no bundled browser and no built-in list of search engines — a
browser is an app. The cost is stated honestly: Chrome puts no page content in the Accessibility tree
(measured: 41 nodes, all toolbar), so a page is read from the screen on-device and a long one takes a scroll.

## Design principles

1. **No knowledge of the outside world in the repository.** You cannot know what someone has installed, so
   nothing is keyed on an app, a label or a language. The rule and how to keep it:
   [docs/extending.md](docs/extending.md).
2. **The engine has no strategy of its own.** Whether to act, find another route, ask or stop is the
   decider's call on the live screen. The engine keeps facts — what changed nothing, which choice was taken
   back from which screen, what a task already holds — and never offers a completed action again.
3. **Typed questions, never prose.** Each step is one request with its questions fanned out in parallel:
   next action · what kind of move · did the last step work · is it really done · is this screen risky.
4. **Arithmetic stays in code.** Option labels state measured facts ("returns all 17 lines at once; the
   window shows only part"); the decider interprets facts, it does not compute them.
5. **Fold, don't rank.** Above 255 options the largest groups become one "look into …" entry each, opened
   the way a person opens a menu. Nothing is dropped by a relevance guess.
6. **Callers own language.** Text to type, queries and file names come from the caller; a task that needs
   them stops with `need_input` rather than inventing them.

## Safety

The safety floor in `policy.yaml` is a floor, not a way of deciding what to do.

* **Every action about to run is classified** from what it is and where it sits, never from screen text —
  the defence against prompt injection. The word lists only say what to classify first; a German `Löschen`
  matching none of them must never read as safe.
* **A verdict cannot release what it does not cover.** "It only edits this window" is a judgement; "this
  channel moves files" is a fact about the action, and the classifier does not overrule a fact.
* **Approval is per action and spent on use.** Confirming one action does not confirm the next.
* **Standing grants instead of tuned thresholds.** A confirmation can be remembered — for one action in one
  app, like a macOS privacy grant — so the same scene does not ask twice and nobody edits a number.
  Only a person gives one (`macwork grants allow <task-id>`, or "always" at the prompt); an MCP caller cannot
  grant itself one, deleting, sending and paying can never have one, and `macwork grants revoke` takes it back.
* On a screen judged risky, every other action needs confirmation too, unless it only backs out.
* While the screen is locked, only non-UI channels run. `engine.mode: yield` (the default) gives the
  keyboard back the moment you touch it.
* `deny.bundle_ids` / `deny.patterns` are never touched; raw AppleScript is off by default; the app the
  caller runs in is never driven. System permission prompts, passwords and captchas are never operated.

## Privacy

Everything a decider or planner sees passes through `privacy.py` on this Mac first.

* Names found by on-device NaturalLanguage, and configured patterns (email, phone, ID and card numbers, IPs,
  addresses), become stable pseudonyms — `⟦PERSON_1⟧` is the same person for the whole task.
* `privacy.redact.names` hides names you list; `never_send` withholds whole fields; home paths become `~`;
  **if tagging fails, text is withheld rather than sent.**
* Every request is written as sent to a local audit log. `audit.dry_run: true` sends nothing at all.
* No screenshots leave the machine and there is no telemetry. The API key lives in the Keychain.

`macwork privacy-check` measures the leak rate on a synthetic corpus and reports it as it is — including
that on-device tagging finds no personal names in Russian, Korean, Greek, Arabic or Vietnamese.

## Evaluation

```sh
macwork eval --suite evals/v2.yaml                   # → evals/reports/{stamp}.{json,md}
macwork compare <before>.json <after>.json           # paired, exact McNemar; drives nothing
macwork profile                                      # where each step's time goes, and how many steps were wasted
```

`evals/v2.yaml` holds 29 tasks over navigation, multi-step, cross-app, question answering, must-not and
prompt injection, using only apps that ship with macOS. The current result is **14/29 on a single run** —
which is not a success rate: the move from 12/29 was 2 fixed and 0 broken, p = 0.50. `compare` refuses to
pair reports from a different suite or decider. History and method: [docs/benchmarks.md](docs/benchmarks.md).

## Configuration

Defaults are in `macwork/defaults/{config,questions,policy,privacy}.yaml`. Override any key in
`~/.config/macwork/<same name>.yaml` or with `MACWORK__SECTION__KEY=value`. A test fails if the defaults ship
a key nothing reads.

## Extending

```toml
[project.entry-points."macwork.providers"]
my_provider = "my_pkg:provide"      # (ctx, observation) -> None, appends Affordances
[project.entry-points."macwork.channels"]
my_channel  = "my_pkg:execute"      # (ctx, affordance, params) -> Outcome
[project.entry-points."macwork.deciders"]
my_decider  = "my_pkg:MyDecider"    # decide(state, questions) -> answers
```

List the provider in `observe.providers`, or pick the decider with `decider.kind`. Read
[docs/extending.md](docs/extending.md) first — it is one rule and three questions for applying it.

## FAQ

**How is this different from Anthropic Computer Use, OpenAI Operator or other screenshot agents?**
Those look at pixels. MacWork reads the structure macOS already exposes, so it needs no vision model for
most apps, sends no screenshots anywhere, and acts on elements rather than coordinates.

**How is it different from AppleScript, Shortcuts, Hammerspoon or Keyboard Maestro?**
Those run scripts someone wrote for a known app. MacWork is given a goal in plain language and works out
the steps on apps nobody scripted — and it can call your Shortcuts when one fits.

**Does it work on a Mac that is not set to English?**
Yes. The project is written in English and keyed on nothing the interface says; it is developed on a
Chinese-language Mac and tested against German and Japanese labels.

**Does it work with Electron or Chromium apps?**
Their trees appear when asked for, which MacWork does automatically. Where a window exposes nothing at all,
it falls back to on-device OCR.

**Can it run without a cloud model?**
`decider.kind: local` uses any OpenAI-compatible local server or Apple's on-device model. It is uncalibrated
and therefore trusted less on purpose: it cannot release an action the safety floor has flagged.

**Which MCP clients work?**
Any that speak MCP over stdio or streamable HTTP — Claude Code, Claude Desktop, Cursor and others.

## Status and limitations

**Early.** Accuracy on your own tasks is something to measure, not assume.

Open: repeats on the eval suite and a committed baseline · the must-not category at 2/4 · drag-and-drop
beyond named endpoints, and precise gestures · held keys and relative mouse input (games) · name tagging for
five languages the on-device tagger does not support · App Intents, for which macOS offers no public API to
third-party processes (a Shortcut the person built is the route that works).

## License

Apache-2.0. See [LICENSE](LICENSE).

---

<sub>MacWork is an independent open-source project. It is not affiliated with, endorsed by, or sponsored by
Apple Inc., Anthropic or OpenAI. "Mac" and "macOS" are trademarks of Apple Inc.</sub>
