# Refactor for v3 — give the three missing concepts a home

The v3 design (docs/design-v3.md) names three things the engine was missing: who holds the Mac, where a value
came from, and what a step promised. Each was patched into whatever file was nearest. This refactor gives each
one module, so the next change has an obvious place to go.

| module | holds | comes from |
|---|---|---|
| `policy.py` | what the engine may do at all: denied apps, the host app, the safety floor (words → classification → verdict), what needs the caller's confirmation, and the goal-only questions (does the goal call for this, does leaving serve it) | judge.py |
| `facts.py` | the working set: what each screen showed, with where and when; whether a piece of text is traceable to something seen (or to the caller's inputs / the goal) | task.carried, scattered result_screen code |
| `contract.py` | one place for what a step requires before it runs (the hands, the app really in front, the values it needs) and what it must show afterwards (typed text in the field, the app frontmost, something changed) | scattered checks in loop.py / effects.py / act.py |
| `judge.py` | only judgements the decider makes for the loop: is it really done, why did the run not finish, which alternatives to offer, the moves | judge.py (slimmed) |
| `model.py` | `Task` stays the task; its bookkeeping moves into two small records: `Pace` (looks, waits, redos, settles, replans) and `Memory` (what did nothing, what was declined, screens seen, facts) | model.py (30+ flat fields) |

Unchanged: `loop.py` (the four phases), `effects.py` (acting and waiting), `consult.py` (the planner),
`tidy.py`, `learn.py`, `observe.py`, `act.py`, `engine.py` (the facade).

Rules for the refactor: no behaviour changes in the same step as a move; tests stay green after every step;
public and private method names the tests use keep working (the mixins keep their methods, they just live
elsewhere).
