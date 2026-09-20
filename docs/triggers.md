# Letting it wake up on its own

The engine has always been able to be sent to do something, and to be resumed, but **nothing
has ever woken it up**. The daemon sits there running, and every task happens because someone
started it — which makes the whole thing a tool, rather than something that lives on this Mac.

What is missing is not a scheduler (macOS has one; `launchd` is what runs this). What is
missing is the other half: **"when this happens, go do that."**

```
macwork watch          # wait, indefinitely
macwork watch --check  # only say what it would listen for, then exit
```

The rules live in `~/.config/macwork/triggers.yaml`, and **do not ship with the project**.
Which app and which folder matter to you is exactly what this project should not know in
advance.

## Shape

```yaml
triggers:
  - name: file the invoices I download
    when: {event: file.changed, path: ~/Downloads/*.pdf}
    do: rename this PDF by its invoice date and put it in the Invoices folder
    debounce_s: 30

  - name: look at the calendar after unlocking
    when: {event: screen.unlocked}
    do: which meetings are left today, and when is the next one?

  - name: mute when a certain app starts
    when: {event: app.launched, bundle_id: com.example.thing}
    do: set the system volume to mute
    debounce_s: 300

  # `do` is a goal in your own words, in any language — the engine reads the screen in whatever
  # language the Mac is set to, so this one works exactly as well as the three above:
  - name: 下载的发票归档
    when: {event: file.changed, path: ~/Downloads/*.pdf}
    do: 把这个 PDF 按发票日期重命名，放进"发票"文件夹
```

Every entry under `when` has to hold. Everything other than `event` is matched as a wildcard
(regexes are accepted too), so `app.*`, or a whole directory tree, does not require this to
invent a syntax of its own.

| key | what it matches |
|---|---|
| `event` | the kind of event, see the table below |
| `app` / `bundle_id` | which app (app events only) |
| `path` | which file (file and volume events only) |
| `text` | any field in the event — a fallback |

The remaining keys: `do` (required, the goal handed to the engine), `inputs`, `debounce_s`
(default 30), `enabled`, `pass_event` (default true: pass the event itself into the task as
`inputs.event`).

## What it can listen for

This is not a list I wrote; the helper reports it itself (`kinds`, in the reply to
`events.watch`):

```
app.launched   app.quit     app.activated   app.hidden
screen.woke    screen.slept screen.locked   screen.unlocked
volume.mounted volume.unmounted
file.changed   clipboard.changed
```

Every one of these is **the system announcing something about itself**: NSWorkspace
notifications, `com.apple.screenIsLocked`, FSEvents, the pasteboard's change count. Not one of
them needs to know what any app is.

Subscribe only to what the rules use: a rule that watches a folder should not also start
polling the clipboard along the way.

## Why it is not a provider

A trigger is a **caller**, the same kind of thing as a person or an MCP client. It starts the
main loop from outside, and therefore inherits the safety floor, facts, redaction and budgets —
not one of which needs reimplementing. There is not a single line in `macwork/watch.py` that
drives this Mac; it only calls `engine.submit()`.

This is the answer to question ① in [extending.md](extending.md): "Is this one action, or a
loop?" — neither. It is **where the loop starts**.

## Keeping it on

```bash
macwork daemon install     # the MCP service in launchd
```

The daemon currently runs `macwork serve`. To keep triggers resident, change
`ProgramArguments` in the plist to `watch`, or install both (each with its own Label).

## Notes

* **Debouncing is the default, not an option.** Writing into a folder fires once per file; the
  rule that responds to it should not run once per file. The default is 30 seconds.
* **Tasks queue, they do not run concurrently.** One Mac, one keyboard; rules that fire at the
  same time queue in order (`macwork doctor` shows the queue).
* **Clipboard events report shape only, not content** (the change count and the list of types).
  Password managers put the real thing there, and this will be running all the time.
* **Missed events are reported.** The helper's ring buffer has a limit; when you come back after
  being away too long, the log says how many were dropped rather than pretending nothing
  happened.
