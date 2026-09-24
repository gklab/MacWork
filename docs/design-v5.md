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
  consulted about. Ending a task on it alone is not made yet: a task still ends `no_route` on rethinks that
  brought no new route (see "A rethink asks; it waits only on evidence" below).
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

## An app that is starting or busy is waited for, not read blind (2026-09-24)

Since the helper's 20 s cooldown (22ce357), a look taken while the app did not answer Accessibility was still
treated as a look at it. The windows provider wrote `open_windows: []`, so the decider read "none: the app has no
window open" beside "nothing of its window can be read", and was offered "bring back the main window". All 10
in-loop opens since then were followed by such a look. Of 158 such looks the decider voted rethink on 144 and
wait on none, and chose reopen on 45 (49 of 2,338 answering looks); 53 plans were made right after one, and 16
tasks ended on one, 12 of the 67 `no_route` endings among them. With helper 0.2.0, which believes a timeout only
until it asks again and says whether an app is still launching:

* **A look waits for the app first** (`EffectsMixin._await_app`): it asks at once every `engine.ready_poll_ms`
  until the app answers, for at most `engine.open_front_s`, each wait one of the ledger's `ready_waits`. An app
  that stays silent that long is not waited for again until it answers, and a helper older than 0.2.0, which
  cannot be asked at once, is not polled. The helper says `not_answering` only on a call it skips: the call whose
  own question timed out replies with nothing, as for an app with no window, so an empty reply is asked for again
  at once (`onscreen.app_readiness`).
* **What such an app has on screen is asked of the window server** (`observe.windows`), which needs no answer
  from it. No empty window list is written for it, and no reopen is offered to an app that has not answered or is
  still starting. A launch counts as starting only within `engine.open_front_s` of its start: some processes
  never report finished launching.
* **The decider and the planner are told the same thing**: it is still starting, or busy; it does not answer
  Accessibility yet; its window is, or is not, on screen.
* **Such a look advances nothing, and a vote about the route, the user or the goal waits first.** No sub-goal
  advances on it. A rethink, ask-user, blocked or impossible vote first spends one of the decider's waits; after
  that a rethink takes the chosen option without a planner call, and the others end as before, with cause
  `app_not_answering` and a reason naming the app. `_consult` does not ask the planner while a wait is still
  possible. Two votes are still taken as on any look: done, behind the verify and second-opinion gates (the
  second opinion asks the planner's judge about that look), and a low progress vote, which counts the step before
  it toward the no-progress rule and as having gone wrong when the planner is next asked.

## What the tree does not describe is read off the screen (2026-09-24)

Offline, with fakes (scratchpad `b_head_probe` scenarios 2, 3 and 7b, `skeptic11/probe.py`): an answering app whose
tree lists no window, with one on screen, was told it had none and offered "bring back the main window"; the
window on screen of an app that does not answer was never read; a panel an app draws in a window of its own was
never read; and a panel drawn in a window the tree describes was read and dropped (3 to 5 lines, where six texts
outside every node make a canvas) or not read at all (a tree that covers the window).

* **The app's windows are asked of the window server on every look** (`observe.windows`). One its tree holds
  nowhere — matched by its windows' titles and frames and by the focused window's nodes, a popover's frame
  inside the window server's that takes in its arrow — is listed as on screen and not described, and no reopen
  is offered while one is up. Up to `observe.windows.read_by_sight` of them are read by sight, by their number
  (helper 0.2.0's `window_id`). That is 0 until a read-only survey of this Mac's apps has counted how many such
  windows are ones nobody sees: they are listed, not read.
* **A window the tree does not give stands in for it** (`window_stand_in`): the one on screen of an app that
  does not answer, or of one whose tree lists no window. It is read by its number, with no fingerprint asked (of
  a silent app that is three 0.5 s timeouts), keyed on the look's glance as well, since no action is taken while
  the engine waits. From an app that does not answer it is text only: a busy app applies the events it is sent
  to whatever it shows once it catches up.
* **What a step drew is read where the picture changed.** `sight.compare` says where (`region`), and after a step
  that changed the picture and no word of the tree, the text there that the tree does not hold is read — ten lines
  at most — and put first in the screen text, where the cap cannot cut it. What the tree holds is every word it
  gives, a field's contents and a control's name included: of the 85 looks after such a step in this Mac's audit,
  50 followed a checkbox, a menu or pop-up button, or typing into or selecting in a field or a document — steps
  that change how a control or a field looks, where the words on screen are that control's name and that field's
  contents. What was read stays there while that part of the window looks the same, so the next step does not
  report it gone, and where it no longer does it is read again: a panel that closed is gone, one with a line
  changed is not. One read, taken after the step and good while the window looks as it did, serves this and the
  loop's own reading of the window, which names its unlabeled controls. A step recorded before the region was
  measured is read as it was.
* **Text read by sight is counted** (`read_by_sight_lines`): a check that reads the screen text reads it too, and
  pass rates across this change are compared with that split.

## A rethink asks; it waits only on evidence (2026-09-24)

A rethink vote stopped the task for a planner call, and when a route came the action chosen with it was dropped.
On 09-22..23 (62 tasks, held-out goals left out) the loop sat waiting on the planner for most of the two key tasks:
28.8 of 44.3 s and 27.3 of about 35 s. Of 201 rethink looks, 23 chose the planner's own suggestion (7 waited for a
call, 57 s: both key tasks among them, and both replans dropped the 「type 17」 and 「type serendipity」 chosen), 96
chose an action on offer with nothing gone wrong (60 calls, 575 s; after 24 of them the next look chose the same
action again), 47 came after a step that got nowhere (26 calls, 291 s), 8 had nothing to act on and 27 looked into
a group of options. And every rethink that brought nothing counted toward the ending alike: of the 20 `no_route`
endings, 16 came on a question refused as asked already with its allowance left, 2 of them on the planner's own
suggestion.

* **A question to the planner is one call at a time, and can be stopped** (`planner.PlannerCall`,
  `ConsultMixin._planner_call`). It runs on a thread of its own; a newer question stops the one before it, and a
  local server is sent the next only once the stopped one has let go, at its next line (`engine._planner_lock`):
  asked twice at once it works on both answers. The on-device planner answers through the helper's main
  connection and is never asked beside the loop.
* **The planner's own suggestion is taken** with no question asked and nothing counted.
* **With nothing gone wrong, an action the floor lets through and a screen not judged risky, the route is asked
  for beside the loop** (`planner.beside`, the switch) while the action goes through every gate as before, and it
  is taken in at the first look after it lands (`_merge_beside`). That route is tried before the planner is asked
  again: the stretch the no-progress rule found is not asked about at that look.
* **Otherwise the route is waited for, and never past the run's time** (`engine.budget_s`): a step that got
  nowhere (`_nowhere`, what the no-progress rule counts), nothing to act on, an action the floor stops for (20 of
  the 96 above, by the floor's last verdict before the look), a planner that cannot be asked beside the loop, a
  call still on its way, or no fruitless rethink left. An answer too late for its run is `late`, and the loop's own
  budget check says what happens next.
* **A question says what it came to** (`consult.Route`): a new route, blocked, the same route, an empty answer
  (the last one stands), asked already (on this exact screen, structure and text, for this reason), the allowance
  spent, no planner, an error, late, pending, or not asked because the app could not be read. The fruitless count
  takes the same route, an empty answer, an error, a spent allowance and no planner; never an "asked already", a
  look nobody could read, or an answer late or still on its way. A new route resets it.
* **The ending is the one it was**: `engine.max_fruitless_rethinks` of those, with `min_steps_before_giving_up`
  taken or nothing untried, end the task `no_route`, and `_finish` names it `planner_unreachable` when every
  question failed and the planner could not be reached or refused (`planner_down`). Ending on the no-progress rule
  alone is not made: 12 of the 20 `no_route` tasks had their last step judged 0.2 or more, and would have run on
  to the step budget. Under the kept ending, the 14 decided by an "asked already" refusal on an action of the
  decider's own would go on (a replay): at most 75 more steps within their runs, about 244 more decider requests
  at their own rate.
* **The planner's seconds are counted apart**: `outputs.budget.planner_seconds` {waited, beside} for the run, and
  every step record's `planner_waited_ms` and `planner_beside_ms`.

Stopping a local call is not free. mlx_lm 0.31.3 notices a closed connection at its next write, a keep-alive once
per 2048-token chunk of prompt it reads, and the client closes it at the next line it reads: measured on this Mac,
a recorded replan sent right after another was stopped 3 s into its prefill had its first token 11.5-19.3 s after
the stop (31.4 s once, the stopped prompt two recorded ones long), against 4.9-7.2 s on an idle server.
