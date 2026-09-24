# macwork v5 — measured, and then rebuilt where the measurements pointed

v4 made the engine ask the system instead of writing things down, and made the safety floor a judgement
instead of a word list. What it did not have was a way to say whether any of that worked: no baseline was
ever kept, one run was quoted as a rate, and two of the passes in the first ×3 run turned out to be false.
v5 starts from the numbers and ends in four structural changes the numbers pointed at.

## What was measured (2026-09-22, decider jev, planner deepseek-flash)

| suite | what it measures | ×N | tasks | runs |
|---|---|---|---|---|
| `behaviour.yaml` | dev suite, behaviours v2 predates | ×3 | 5/6 | 14/18 |
| `v2.yaml` | dev suite, 29 tasks in six categories | ×1 | 13/27 | A 5/5 · B 0/5 · C 1/5 · D 2/5 · E 3/4 · F 2/3 |
| `heldout.yaml` | apps never used while building — **sealed** | ×3 | 3/8 | 9/24 |
| `sampled.yaml` | apps the repository has never named, drawn at run time | ×3 | 5/12 | AppKit 7/12 · Java 4/6 · Catalyst 3/6 · Electron 3/6 · Qt 0/6 |

Per step (profiler, 149 real steps): 4.1 s, of which the decider's round trip is 734 ms (660 of them the
route), 4.2 decider requests, and 45% of steps wasted — circling, or acting on an unchanged screen.

**The generalisation gap** is the number this project watches: dev suites pass 48–67% of runs, held-out
apps 38%, never-named apps 47%. A change that lifts the dev suites and not the other two is tuning. The
sampled draw self-heals: once an app is named in a commit, it leaves the pool.

## What the runs found, all general

Read out of traces, none of it out of the code: reading a file was gated (the floor had no verdict for a
read), making a folder was gated (none for creating), a version number was pseudonymised as an IP address,
an About panel a Qt app draws itself was "a picture that changed where no text did" and nothing read it,
the planner wrote `file://~/…` back from a path it had been shown as `~`, a menu item's reference had
expired by the next look, Finder refused `AXOpen` on an icon, a switch to another app was recorded as taken
while the old one was still in front, an app's own recovery dialog was called a thing only the user could
dismiss, a "done" with the Open dialog up, and an answer saying "the date is not shown" that passed a check
for 「日」 through 「日期」. Each has a commit, a test, and where it applies a config switch.

## Four structural changes

### 1. Facts first, the sentence second (`model.Change`)

What an action did to the screen was a sentence for the decider, and the engine read its own sentence
back (`endswith("no text on screen did")`). `Change` holds the facts — what appeared, what went, window
and app before and after, how much of the picture moved — and `describe()` renders the sentence. The facts
ride on the step (`Step.change`) through a restart. Nothing parses prose any more; `Spent` and `cause` did
the same for budgets and endings earlier in the day.

### 2. One ledger (`budget.py`)

Every allowance lived where it was spent, with its own default and its own reading of "at the limit":
seven counters, fourteen sites, three modules. `ALLOWANCES` is the one table; a counter is spent through
`spend_allowance`, asked through `allowance_left`, and `ledger()` reports everything a run used of
everything it was allowed, in `outputs.budget` at every ending. The hidden +1 on replans is in the table
where it can be seen. The step, time, decision and cost ceilings stay in `_overspent` and are reported by
the same ledger.

### 3. Identity from where a control sits (`_structural_identity`, `Affordance.handle`)

Most window controls carry no Accessibility identifier, and there the label was the only identity — the
interface language, and what the control happens to show. Memory keyed on the full label meant a field
reading 「(now: hello)」 was a new action after every keystroke. A control the app gave no identifier is now
known by the chain of roles from the window down and its place among same-role siblings; content (rows,
cells, links, text) keeps its name, because a row's place changes with every sort. The loop's memory is
keyed on a *handle* — identity where there is one, the steady name where there is not — for both the
action offered and the step taken.

### 4. A step carries its evidence (`plan_evidence`, `step_evidence`)

A sub-goal advanced on one probability, and consecutive confident looks walked a whole plan without a
step being taken. The planner had been writing the expected result of each step all along, as prose the
engine stringified and ignored. Each step is now `{goal, evidence}`; the decider is asked, in the same
request as everything else, whether the screen shows that evidence, and the plan moves only when it does.
The task's live working state (`TaskScope`) went the same way: one object per task instead of per-task
keys in the engine's cache.

## Correct by construction

The four changes above were pointed at by measurements. The next four were pointed at by a question: if
the engine's own bugs are found only by running it on a Mac, the design is letting them in. Each closes a
class of bug rather than one instance.

* **One way into memory, one way out** (`Memory.note_*`, `withheld_reason`). Five call sites wrote memory
  by hand, each spelling the key its own way; a step recorded under the wrong spelling was simply never
  remembered. The loop asks one question — why is this option withheld — and gets one answer.
* **Every action promises something, and every promise is checked** (`contract.promise`, `kept`,
  `kept_by_change`). A typed, switched or returned promise is checked on the spot; a changed, opened or
  selected one at the next look, from the `Change` facts. A step that broke its promise is a failed step,
  not an ok one with a note.
* **One rule for a task going nowhere** (`engine.max_no_progress`, `_no_progress_run`). Repeats, circles
  and an unchanged screen were three counters with three thresholds that agreed by accident. A step went
  nowhere if it failed, broke its promise, led back or moved nothing; a run of those is the one thing
  consulted about and the one thing that ends the task.
* **Invariants checked where they must hold** (`invariants.py`). After every look: options can be told
  apart and remembered, and every action the look found is an option or inside an entry that names it;
  after every step: it says where it was taken from and what it promised, and a broken promise is never
  an ok step; at every ending: the status is one the callers know and a stop short says why. A violation
  is the engine's own bug, recorded as a fact in `outputs.invariants_broken` and counted per run in every
  eval report — never an exception, never something to dig out of a trace.

## What a spreadsheet in a Qt app taught (2026-09-22, WPS, two runs, both failed)

* **Coverage is measured over the window, not read off its chrome** (`observe.undescribed_share`). The
  grid was in the tree as nothing at all — not a scroll area, not a group — behind a toolbar of 35
  actionable nodes, so every signal that looked at what the tree *offered* saw a well-described window.
  The share of the window's area no described node covers is computed for every window; past the same
  `canvas_area_share`, the screen is read on the first look, its text becomes targets, and the empty
  crossings of a grid become places. When even the screen shows nothing there, the decider is told, and a
  task that ends on it ends with cause `window_unreadable`.
* **Progress is nearer to the step's evidence, not "the screen changed"** (`progress_toward`). New,
  Create New, Create New, Create New each opened something and each counted as an intended effect, while
  the plan sat at its first sub-goal for nine steps. Where the planner said what a step should leave on
  screen, the progress question is asked against that, and a planner consulted about a stuck task is told
  which sub-goal it is stuck on. Found on the way: the no-progress rule always counted the newest step as
  "somewhere" because it is judged on the look that follows it, so the rule had never fired.
* **Every ending short of done carries a cause**, checked as an invariant; the diagnosis passes `budget`
  or `no_route` through and names `needs_user`, `impossible`, `unclear_goal`, `unfinished` otherwise.

### …and then done on the Mac (same day, WPS Office, one CSV)

The three changes above were written without the Mac; the first run with it failed exactly as before, and
the trace named five more things, each general and each measured:

* **Two helper processes and no capture works.** In stdio mode the second connection was a second child,
  and with two alive every ScreenCaptureKit capture in both hung to its 8 s timeout — every OCR since the
  glance overlap was introduced, in every run that day. One process, a side socket for the second connection.
* **A window-sized action is not coverage.** "Raise the window" carried the window's frame and so
  "covered" all 60 texts read off the sheet, which was then declared unreadable.
* **The recogniser detects the language per line.** With the Mac's first language English, every Chinese
  cell came back as Latin noise; reversed, every cell read. Neither order is right for a screen with both.
* **The screen in front is never folded.** 123 on-screen options were one "look into the window" behind
  Format ▸ Rows ▸ Hide shown in full, because the on-screen group was over `fold_groups_over` like the
  list of every installed app. Nor is it cut: when it alone is more than one choice holds, its first part
  is shown and the rest is one "look into the rest of the window" away, open while the task stays in that
  window. A group the decider opens gets the room the screen leaves, at least `fold_groups_over` of it at
  a time (all of it when smaller), until the next step or another window; kept open for the whole task,
  one look into the 535 installed apps had held all of Calculator's controls out of the options for nine
  looks.
* **A formula is made of what is on screen.** The planner wrote `=SUM(B2:B4)` and the judge that keeps
  invented values out refused it as a computed figure; the second opinion on done doubted 2388's provenance.

Result: the same task, failed twice before, done in 33 s — the empty crossing of the 「合计」 row and the
「金额」 column clicked, the formula typed, the total on screen.

## Also in v5

* An app's own sheet or dialog is an interruption like a prompt from another process: the task, something
  to set aside, or the user's, judged the same way.
* Two independent judgements must agree before a task ends `blocked` (planner and decider) or `done`
  after acting (decider and planner). A second opinion that could not be had is no opinion.
* Windows new since the last look are read first, and read from the screen once when they first appear;
  a picture that changed where no text did is read by sight on that very look.
* The `read` and `create` verdicts; the floor asks about what the floor is for.
* The step level (`mac_observe`, `mac_act`) goes through the same redaction as the decider, and is audited.
* The harness: tasks whose app is not installed are `invalid`; a pass by answering without finishing is
  counted apart; reports never carry the home directory; the sweep sees every Space and force-quits its
  own stubborn apps; `{day}` for checks that need today's date.

## What v5 does not do

* **Verify itself on a Mac.** Everything after the ×3 baselines is unmeasured; the next Mac session runs
  the four suites ×3 with `--compare` and the profiler before anything else is claimed.
* **Measure the task state model.** Each sub-goal now carries the evidence the planner expects on screen,
  and the plan advances only when the decider judges the screen to show it (`step_evidence`); whether that
  moves the B (multi-step) and C (cross-app) categories is for the next run to say.
* **Short sequences without a decision.** Every step is one decider round trip; the only structural way
  below ~3 s a step is to carry out a planner's short sequence while each step's precondition holds.
* **Another interface language, measured.** The floor's words are now derived once per interface language
  the Mac uses (`words.py`), so the higher bar applies to a German or Japanese delete as it does to an
  English one; what the planner answers for a language nobody here reads is unverified.
