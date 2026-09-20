# When you add something new

This engine can drive an app it has never seen. That does not come from us knowing the app
in advance. It comes from two things:

* **what the app declares about itself** — App Intents, scripting dictionaries, NSServices,
  URL schemes, Shortcuts
* **what the UI's own design says** — the Accessibility tree, menus, roles, the actions each
  element declares it supports

So there is only one rule:

> **Do not write knowledge about the outside world into this repository.**
> Least of all what a user might have installed or might use — you can never know.

Here is how to hold to it.

---

## Three questions, in order

### ① Is this one action, or a loop?

This is the easiest place to go wrong, and going wrong here costs the most.

If something new needs to "take a step, look at the result, then decide the next step,"
**it is not a provider, it is a sequence of actions**. Take it apart and let the main loop
drive it; **never write another loop.**

Searching the web went wrong here once. It was written as a subsystem, so it had: its own
browser, its own identity, its own step budget, its own decider calls, and — because it went
around the main loop — no facts, no safety floor, no contract, no privacy redaction. Taken
apart, it is four actions that already exist:

```
open a browser     apps provider
search             the browser's own scripting command, or the address bar (in AX, just a text field)
open a result      the window provider's press
read this page     readall / vision
is that enough     the main loop's verified / done — it is already asking this
```

Not one line needs to know the name of a search engine. Switch the user to any browser and it
keeps working.

### ② Has the Mac already described it?

If it has, write a provider that reads that, and **put no knowledge in the code**. The ones
that already do this:

| what it reads | where it reads it from |
|---|---|
| what an app can do | `Metadata.appintents/extract.actionsdata`, scripting dictionaries, `NSServices`, `CFBundleURLTypes` |
| what is in the interface | the AX tree, menus, `AXActionDescription` (the system's own, localized action names) |
| what this element can do | `AXUIElementIsAttributeSettable`, not "is its role in my table" |
| which apps are installed | Spotlight plus a LaunchServices round trip, not four hardcoded directories |
| does this file have text in it | the UTType hierarchy, not a list of extensions |
| is there a phone number in this text | `NSDataDetector`, not a hand-written regex |
| which language to read the screen in | `Locale.preferredLanguages` ∩ the languages the recognizer supports |
| which key to press | a reverse lookup in the current keyboard layout, not a US-ANSI position table |

Every one of these is under 60 lines. What they have in common: **change the Mac, change the
language, change the app, and the code does not change.**

### ③ Has the Mac not described it at all?

Make sure that is really true first — we got it wrong once. We first asserted that "Chrome's
pages can be read over AX"; in fact the great majority of those 767 affordances were menu
items. Measured properly, Chrome exposes only 41 nodes, all of them toolbar, with both
accessibility flags set and after waiting 5 seconds too.

When it really is not there, the only way out is to look at what was rendered (on-device OCR),
and **to state the cost plainly**, rather than pulling in a foreign runtime to route around it.

---

## Telling whether something "brings in knowledge"

Ask: **if the user installs something tomorrow that we have never heard of, will this line of
code be wrong?**

```
❌  "duckduckgo" / "bing"                 the names of two search engines
❌  'class="b_algo"'                      the structure of their HTML — they redesign, it fails silently
❌  '#mw-content-text'                    Wikipedia's body div, inside a "general" page reader
❌  ["AXTextField", "AXTextArea", …]      a control whose role is not in the table cannot be typed into
❌  [/Applications, /System/Applications]  an app installed anywhere else does not exist
❌  '删除|移除|抹掉' (as the only test)      German Löschen does not match, and the safety floor is gone

✅  kAXPressAction / NSServices / CFBundleURLTypes    Apple's vocabulary, not somebody's app
✅  UTType.conforms(to: .text)                        ask the type hierarchy, do not list formats
✅  AXUIElementIsAttributeSettable(AXValue)           ask the element, do not consult a role table
✅  Locale.characterDirection(forLanguage:)           ask the system, do not decide the language yourself
```

The difference is not "whether there is a string constant," it is **whether that constant
describes the system's vocabulary or the outside world**.

One important exception: **the safety floor's word list may stay**, but it may only decide
"what to classify first," never "whether this action is safe." The cost of the list missing a
word has to be "slower," never "did it without asking."

---

## The shape it lands in

```toml
[project.entry-points."macwork.providers"]   # find what can be done   (ctx, observation) -> None
[project.entry-points."macwork.channels"]    # do it                   (ctx, affordance, params) -> Outcome
[project.entry-points."macwork.deciders"]    # decide which one         decide(state, questions) -> answers
```

Something new should land as one of these three, **and add zero new concepts**. If it will not
fit, go back to question ①: it is most likely a loop, not an action.

Once it is written, check that it inherits these automatically — if it does not, you went
around the main loop:

* will the safety floor classify it (`policy.py`, every action about to run goes through it)
* do the values it produces go into facts (a value that has not been seen may not be written out)
* does the text it sends out get redacted (`Gate` is the only door to the decider)
* does it count against the task's step and cost budget
* once it is done, does anyone check that it really did it (`contract.py`)

---

## Measure after you add it

Every "this ought to be better" judgement in this project has been changed by data at least
once:

* capability probing was meant to **replace** the role allowlist — measuring showed it can only
  be unioned with it: AX saying "not settable" does not mean "cannot be used," and the engine
  still has click and keystroke as two fallbacks.
* "offer every AX action that is not in the table" — measuring showed `AXScrollToVisible`
  appears on 428 of 441 elements, and all of them have `AXPress` as well.
* "keep list rows that have scrolled out of view" — measuring showed that is 3 seconds per
  observation.
* the type table for scripting commands was assumed to be hardcoded — measuring showed 31 of
  the 32 entries stop at `specifier`, and a specifier is code, which is a safety boundary, not
  a word list.

So: **measure first, then change; then measure again.** Use `macwork surfaces` (read-only, it
performs no actions) to see what each capability surface can see right now, and how long it
takes.
