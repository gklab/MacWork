# Controls: input that lasts

Most of what MacWork does is *operate* an app: press this, type that, choose a menu item. Some things are
*steered* instead — a game, a map, a 3D viewport, a canvas that pans while a key is down. They read how long a
key stays down and how far the mouse moved, and they tell Accessibility nothing about what their keys do.

Nothing in this repository may know what `W` does in any program. Somebody does, and they say so.

## Declaring controls

For one task, by the caller:

```json
{"goal": "walk to the door and open it",
 "inputs": {"controls": {
   "move forward": {"keys": ["w"], "ms": [300, 1200]},
   "sprint":       {"keys": ["shift", "w"], "ms": 1000},
   "look left":    {"move": {"dx": -200}, "ms": 150},
   "look right":   {"move": {"dx": 200}, "ms": 150},
   "use":          {"buttons": ["right"], "ms": 100}}}}
```

For an app, by the person, in `~/.config/macwork/config.yaml`:

```yaml
observe:
  controls:
    apps:
      com.example.game:
        jump: {keys: [space], ms: 80}
```

The caller's word for a task wins over the person's for the app. Each entry needs `ms` — a length, or a list
of lengths — and at least one of `keys`, `buttons` (`left`, `right`, `middle`) or `move` (`dx`, `dy`, in
points, relative). Keys and a move together are one action: walking while turning. An entry that cannot be
understood is left out and reported in the observation (`controls_rejected`); nothing is guessed.

Each length is its own option, labelled with what will physically happen:

    move forward — hold w for 0.3 s
    move forward — hold w for 1.2 s
    look left — move the pointer by (-200, 0) over 0.15 s

Choosing between stated lengths is a choice a decider can make. Producing a number is not.

## What is guaranteed

**Nothing is ever left held.** There is no "key down" call and no "key up" call to forget. The only primitive
is `input.hold`: a hold of stated length, carried out and released inside one request to the helper. It is
released on every way out — completion, an error, `input.release_all` from another connection, the helper
being told to quit — and it has a ceiling of 10 s whatever is asked (`observe.controls.max_ms`, 5 s by
default, is the engine's own). Outside an exclusive run, the person touching the Mac ends a hold at once and
the step is recorded as failed, saying so.

Every hold still meets the safety floor like any other action. Where the floor stops one that is harmless in
your program, a [standing grant](../README.md#safety) answers it once.

## What the engine may conclude

Everything the loop has learned to conclude — *that did nothing*, *that has had its turns from this screen*,
*that choice was taken back* — rests on being able to see what an action did. A view that draws itself tells
Accessibility nothing, so the engine looks at it the way a person does: a coarse glance at the window before
and after (`observe.sight`: a 32×32 grid of brightness, about 30 ms, never leaves the Mac). It does not know
what the picture is *of*; it knows how much of it changed and roughly where, and says so as a fact:

    move forward — hold w for 1.2 s -> the picture in the window changed (38% of it, across the window); no text on screen did

A hold that changed nothing — walking into a wall — is withdrawn like any other action that did nothing. One
that keeps changing the picture can be taken again and again: that is not being stuck.

Where no glance can be had (no Screen Recording permission, an older helper), a hold reports that its effect
may not be visible (`Outcome.unseen`) and nothing is concluded from it at all. The history then says so
rather than claiming nothing happened.

## What is still missing

Seeing *that* the picture changed is not seeing *what is in it*. On-device OCR reads the text in such a window
and nothing else, so in a view with no text the decider chooses among the declared controls knowing only the
goal, what it has done, and how the picture responded. A general, on-device description of what a window
shows — for any window, with no knowledge of any program — is the capability that is missing, and it is not
built yet.
