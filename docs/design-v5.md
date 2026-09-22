# macwork v5 — measured, and then rebuilt where the measurements pointed

v4 made the engine ask the system instead of writing things down, and made the safety floor a judgement
instead of a word list. What it did not have was a way to say whether any of that worked: no baseline was
ever kept, one run was quoted as a rate, and two of the passes in the first ×3 run turned out to be false.
v5 starts from the numbers and ends in three structural changes the numbers pointed at.

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

## Three structural changes

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
* **A task state model.** The planner writes an expected result per sub-goal; the engine still advances a
  sub-goal on one probability. Aligning expected evidence with observed `Change` is the next structural
  step, and the one the B (multi-step) and C (cross-app) categories are waiting on.
* **A per-task context.** `engine.cache` still holds per-task keys (`pictures`, `vision.wanted`,
  `windows.seen`) beside global ones, collected by name.
* **Short sequences without a decision.** Every step is one decider round trip; the only structural way
  below ~3 s a step is to carry out a planner's short sequence while each step's precondition holds.
* **Another interface language.** The floor's two bars still depend on which words the lists know.
