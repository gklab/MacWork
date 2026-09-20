# macwork v3 — three things the engine was missing

Symptoms that kept coming back, each patched on its own: the engine typed into the user's terminal; it grabbed
the keyboard while the user was typing; it wrote "391" into a document although the Calculator had not shown a
result yet (the planner had done the arithmetic itself); it left dialogs open; it reported `done` on a screen
that showed something else.

They are one thing each time: **the engine has no model of who holds the Mac, of where a value came from, or of
what a step promised to do.** Three concepts, each small, replace the patches.

---

## 1. The hands: who holds the Mac right now

The Mac has one keyboard and one front app. Today the engine takes them whenever it likes
(`engine.mode: background` is the default and means "never yield").

* The engine holds the **hands** only while the user is idle (`engine.yield_idle_s`). Any key or mouse move by
  the user revokes them at once: the current step finishes, the next one waits.
* Every action that needs the front app (keystrokes, typing, clicks) must take the hands first; actions that do
  not (Accessibility presses on a background window, scripting commands, web research) never do.
* `engine.mode: exclusive` is the opt-in for runs that own the machine (the eval suite says so on the first line
  of its output); `yield` is the default everywhere else.
* The host app — the terminal or client the engine runs under — is never touched, hands or not (already in
  `deny.host_app`).

## 2. Facts: nothing is written that was not seen

A task keeps a small **working set of facts**: what each screen showed, with where and when it was seen.

* A fact is recorded when the task leaves an app or finishes a step in it: `{app, window, text, step}`.
* Text the engine types must be traceable: it comes from the caller's `inputs`, from the goal itself, or it
  appears in a fact. The planner may phrase it, but it may not invent it — a value that is nowhere in the facts
  is refused, and the engine treats the step as "the value has to be fetched first".
* `outputs.answer` for a question follows the same rule: the answer must be present in the facts.
* This is what stops "the Calculator is still computing, the writer already types 391".

## 3. Step contract: preconditions, then a verified effect

Every action runs under a small contract instead of scattered checks.

| | what is required | if it fails |
|---|---|---|
| before | the hands (when the action needs the front app); the app is really frontmost; nothing modal from another process is in the way; every value the action needs is a fact | wait, take the hands, or ask — never act blind |
| after | the effect the action promised: typed text is in the field the app reads, a switch made that app frontmost, a window opened, the screen changed | the step is recorded as failed with the reason; it is not offered again from this screen; `done` can never be claimed on it |

The effect check is what a person does without thinking ("did that land?"), and it is the only honest basis for
`done`. Where an effect cannot be checked (a menu command with no visible result), the step stays "unverified"
and the stricter `done` question decides, as now.

---

## Order of work

1. Hands (ownership + yielding, default on; exclusive for evals).
2. Facts (record, and refuse text that is not traceable).
3. Step contract (preconditions and effect checks in one place, replacing the ad-hoc ones).
4. Re-run evals/v2 and report — these are the numbers that say whether the design holds.
