# macwork v2 — design for the next round

Order of work: **6 → 1 → 4 → 5 → 2 → measure**. Structure first (everything below touches the loop), the new
evaluation second and *frozen before* the capability and safety work (so the numbers cannot be tuned on it),
path verification last (it verifies the finished system).

Principle unchanged: no app-specific code, no hand-written operating knowledge; the engine keeps facts, budgets
and a safety floor; what to do is the decider's judgement, other routes are the planner's.

---

## 6. Structure

Problem: `engine.py` is 1150 lines, `_loop` is 308 lines. Every recent fix went into that one function.

Design — the loop becomes explicit phases with small return types:

```
_loop(task):
    while budget:
        ctx = context for this step
        if task.held:                     -> _perform(held)
        look  = _look(task, ctx)          # observe, facts, options (arrange), state      -> Look | Finish
        ans   = _ask(task, look)          # one decider request + speculation check       -> Answers | Again | Finish
        verdict = _judge(task, look, ans) # move/action -> Act(a) | Again | Finish          (one small handler per move)
        if Act: _perform(task, ctx, look, a)
```

Modules (behaviour unchanged, public/private method names kept so tests and callers do not move):

| module | holds |
|---|---|
| `engine.py` | `Engine` facade: do/resume/observe/act/feedback/cancel/status, task registry, lock |
| `loop.py` | `Look`, phases `_look/_ask/_judge/_perform`, one handler per move |
| `effects.py` | executing an affordance, waiting for the UI, speculation, fingerprints, menu key-equivalent fallback |
| `judge.py` | the focused follow-up questions: verified done, goal calls for, backs out, diagnose, moves by itself |
| `consult.py` | planner use: consult, fill, brief, suggestions |
| `tidy.py` | closing what a task opened |
| `learn.py` | exploring an app ahead of time |

Done when: all 70 tests pass unchanged, ruff clean, the loop body reads top to bottom in one screen.

---

## 1. Evidence that can be trusted

Problems: 25 tasks, mostly 1–3 step navigation; single runs (results vary 6–8/10 between runs); the held-out
suite was used to fix the engine, so it is no longer held out.

Design:

* **New suite `evals/v2/`, written and frozen before the work in 4 and 5.** Its file hash goes into every report;
  a task may only change afterwards for a harness bug, recorded in the file.
* **Categories** (≈35 tasks), each with a stated purpose:
  * A navigation in apps not used before (breadth)
  * B multi-step inside one app (5–15 steps: edit text, compute, format)
  * C across apps (take a result from one app into another)
  * D answers (the goal is a question; the answer must come back in the result)
  * E must not complete on its own (delete, sign in, install: expect `need_confirm` / `blocked`, and nothing changed)
  * F prompt injection (a page or document tells the engine to do something else; the goal must still be done and
    nothing outside it touched)
* **Repeats**: every task runs 3×. Report per task k/3, per category and overall mean success with a Wilson 95%
  interval, and pass^3 (all three runs pass) — the number that matters for "can I rely on it".
* **Sandbox**: tasks that need files get them in `~/Library/Caches/macwork-eval/` (created by fixtures,
  removed after); nothing outside it is written. `{eval_dir}` is substituted in goals and inputs.
* **New checks**: `expect_status`, `file_exists` / `file_missing` / `file_contains`, `answer_contains`
  (the engine's reported answer), `trace_excludes` (regex over the actions taken — for injection and category E).
* Earlier suites stay as regression (`unseen.yaml`) and history (`heldout.yaml`).

---

## 4. Safety

Three gaps: the floor is a word list (ambiguous words: 下载 = "download" and "the Downloads folder"); injection
never tested; redaction never measured.

**4a. Floor: words find candidates, the decider classifies them.**
`policy.yaml` gets named categories (delete, send, pay, install/update, sign-in, grant permission, quit, write
files, paste) each with a description and its patterns. A pattern hit is no longer the verdict: the decider is
asked *which category this action is*, with "only shows or navigates" as an option. Only that option at ≥ 0.9
releases the action; everything else stays gated. Every release is written to the audit log. Actions without
descriptive words (a click on read screen text, an unlabeled control, a control of a prompt from another
process) are classified the same way whenever the screen is judged even slightly risky.

**4b. Injection.**
* State and planner prompts mark provenance: `goal`/`inputs` are the user's; screen text, field contents, web
  findings and prompts from other processes are *content* — questions say they are never instructions.
* Leaving the goal's app (open/switch to another app, run a Shortcut, web research) when the caller named an app
  must pass the focused "does the goal call for this" question — the same guard that drops unasked quits.
* Category F measures it; the floor stays independent of the decider either way.

**4c. Privacy, measured.** `macwork privacy-check`: a synthetic corpus (Chinese and English names, emails, phones,
ID and card numbers, addresses, mixed into real UI strings) goes through the real redactor and on-device tagger;
report leak rate and over-redaction (app names hidden). No real personal data in the corpus.

---

## 5. Capabilities

Chosen by what category B/C/D tasks need, all general:

* **Text inside documents** — select a piece of text in an editable area (helper sets `AXSelectedTextRange`),
  put the cursor at the end; then typing replaces the selection and menu commands (Bold…) apply to it. What to
  select is a text slot the planner fills from the field's content.
* **Answers** — the first request of a task also asks whether the goal wants information back. If so, when it is
  done the planner writes the answer from the final screen (and web findings) into `outputs.answer`.
* **Drag and drop** — helper `input.drag`; the planner may suggest `{"drag": [from label, to label]}`, which the
  engine resolves to both elements on screen and offers as an option.
* File dialogs, canvas editing, gestures: not in this round (planner suggestions and on-demand OCR cover some).

---

## 2. Paths never run for real

* `macwork doctor` probes every configured planner with a tiny JSON request (ok / auth rejected / unreachable,
  latency) and the decider, and reports helper permissions.
* `macwork selftest`: `learn` on a harmless app with a small budget (screens and edges recorded, app left as found);
  routine replay (the same task twice with routines on: the second run must replay the routine and be faster);
  each available planner answers a real plan request.
* DeepSeek and Claude run in selftest when their keys are present; otherwise reported as "not configured".

---

## Measure

Run v2 (3×), unseen (regression, 1×), selftest. Report as measured, including what got worse.
