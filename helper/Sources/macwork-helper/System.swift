import AppKit
import ApplicationServices
import Carbon.HIToolbox
import IOKit
import NaturalLanguage
import PDFKit
import UniformTypeIdentifiers

// MARK: - apps

private func appInfo(_ a: NSRunningApplication) -> [String: Any] {
    var d: [String: Any] = ["pid": Int(a.processIdentifier), "name": a.localizedName ?? "", "active": a.isActive, "hidden": a.isHidden]
    if let b = a.bundleIdentifier { d["bundle_id"] = b }
    if let u = a.bundleURL { d["path"] = u.path }
    d["regular"] = a.activationPolicy == .regular
    if let t = a.launchDate { d["launched"] = t.timeIntervalSince1970 }
    return d
}

/// Quit an app the polite way (like ⌘Q): it may still ask to save, in which case it keeps running and says so.
func appsQuit(_ p: Params) throws -> Any {
    guard let pidNum = p["pid"] as? Int, let app = NSRunningApplication(processIdentifier: pid_t(pidNum)) else {
        return ["terminated": true]
    }
    // force: for an app the eval harness launched itself and that will not quit politely — never for one
    // the person had open. The engine's tidy never passes it.
    let asked = (p["force"] as? Bool ?? false) ? app.forceTerminate() : app.terminate()
    let deadline = Date().addingTimeInterval(Double(p["timeout_ms"] as? Int ?? 3000) / 1000)
    while !app.isTerminated && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
    return ["terminated": app.isTerminated, "asked": asked]
}

func appsRunning(_ p: Params) throws -> Any {
    let all = p["all"] as? Bool ?? false
    return NSWorkspace.shared.runningApplications.filter { all || $0.activationPolicy == .regular }.map(appInfo)
}

func appsFrontmost(_ p: Params) throws -> Any {
    guard let a = NSWorkspace.shared.frontmostApplication else { return ["app": NSNull()] }
    var d = appInfo(a)
    let el = AXUIElementCreateApplication(a.processIdentifier)
    if let w = axAttr(el, kAXFocusedWindowAttribute), let t = axAttr(w as! AXUIElement, kAXTitleAttribute) as? String { d["window"] = t }
    return ["app": d]
}

/// Bring an app forward. AX "frontmost" works from a background helper where NSRunningApplication.activate
/// is refused by cooperative activation; fall back to it anyway.
func appsActivate(_ p: Params) throws -> Any {
    guard let pidNum = p["pid"] as? Int else { throw RPCError("bad_params", "pid required") }
    let pid = pid_t(pidNum)
    let el = AXUIElementCreateApplication(pid)
    let err = AXUIElementSetAttributeValue(el, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
    if err != .success { NSRunningApplication(processIdentifier: pid)?.activate() }
    return ["ok": true]
}

// MARK: - keyboard & mouse

/// ANSI *positions*, used only for keys that are the same key everywhere (Return, Tab, the arrows, F1…).
/// A character is never looked up here: see `keyCode(for:)`.
private let keyCodes: [String: Int] = {
    var m: [String: Int] = [
        "a": kVK_ANSI_A, "b": kVK_ANSI_B, "c": kVK_ANSI_C, "d": kVK_ANSI_D, "e": kVK_ANSI_E, "f": kVK_ANSI_F, "g": kVK_ANSI_G,
        "h": kVK_ANSI_H, "i": kVK_ANSI_I, "j": kVK_ANSI_J, "k": kVK_ANSI_K, "l": kVK_ANSI_L, "m": kVK_ANSI_M, "n": kVK_ANSI_N,
        "o": kVK_ANSI_O, "p": kVK_ANSI_P, "q": kVK_ANSI_Q, "r": kVK_ANSI_R, "s": kVK_ANSI_S, "t": kVK_ANSI_T, "u": kVK_ANSI_U,
        "v": kVK_ANSI_V, "w": kVK_ANSI_W, "x": kVK_ANSI_X, "y": kVK_ANSI_Y, "z": kVK_ANSI_Z,
        "0": kVK_ANSI_0, "1": kVK_ANSI_1, "2": kVK_ANSI_2, "3": kVK_ANSI_3, "4": kVK_ANSI_4, "5": kVK_ANSI_5, "6": kVK_ANSI_6,
        "7": kVK_ANSI_7, "8": kVK_ANSI_8, "9": kVK_ANSI_9,
        "return": kVK_Return, "enter": kVK_Return, "tab": kVK_Tab, "space": kVK_Space, "delete": kVK_Delete,
        "backspace": kVK_Delete, "forwarddelete": kVK_ForwardDelete, "escape": kVK_Escape, "esc": kVK_Escape,
        "left": kVK_LeftArrow, "right": kVK_RightArrow, "up": kVK_UpArrow, "down": kVK_DownArrow,
        "home": kVK_Home, "end": kVK_End, "pageup": kVK_PageUp, "pagedown": kVK_PageDown,
        "-": kVK_ANSI_Minus, "=": kVK_ANSI_Equal, "[": kVK_ANSI_LeftBracket, "]": kVK_ANSI_RightBracket,
        ";": kVK_ANSI_Semicolon, "'": kVK_ANSI_Quote, ",": kVK_ANSI_Comma, ".": kVK_ANSI_Period, "/": kVK_ANSI_Slash,
        "\\": kVK_ANSI_Backslash, "`": kVK_ANSI_Grave,
    ]
    let fkeys = [kVK_F1, kVK_F2, kVK_F3, kVK_F4, kVK_F5, kVK_F6, kVK_F7, kVK_F8, kVK_F9, kVK_F10, kVK_F11, kVK_F12]
    for (i, k) in fkeys.enumerated() { m["f\(i + 1)"] = k }
    return m
}()

private let modifierFlags: [String: CGEventFlags] = [
    "cmd": .maskCommand, "command": .maskCommand, "shift": .maskShift, "alt": .maskAlternate, "option": .maskAlternate,
    "opt": .maskAlternate, "ctrl": .maskControl, "control": .maskControl, "fn": .maskSecondaryFn,
]

/// "cmd+shift+n", "return", "escape".
// MARK: - who last touched the Mac

/// Telling the user's input apart from our own.
///
/// Events we post with `CGEvent.post(tap: .cghidEventTap)` are injected into the same HID stream the system
/// measures idle time from, so `secondsSinceLastEventType` is reset by our own typing. Read naively it says
/// "the user is busy" every time the engine presses a key — which both costs a wait on every step and, worse,
/// makes it impossible to notice that the user really did come back. So each injection is bracketed, and the
/// last genuine user event is tracked separately.
final class InputTracker {
    private let lock = NSLock()
    private var lastSynthetic: TimeInterval = -Double.greatestFiniteMagnitude
    private var lastUser: TimeInterval = 0
    private let slack: TimeInterval = 0.02   // an event this close to our own injection is taken to be ours
    private let now: () -> TimeInterval
    private let hidIdle: () -> Double

    init(now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         hidIdle: @escaping () -> Double = {
             CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: CGEventType(rawValue: ~0)!)
         }) {
        self.now = now
        self.hidIdle = hidIdle
    }

    /// Fold whatever the HID system knows into `lastUser`, unless the most recent event was one of ours.
    private func refreshLocked() {
        let at = now() - hidIdle()
        if at > lastSynthetic + slack {
            lastUser = max(lastUser, at)
        }
    }

    /// Call around anything that posts events: `before` banks the user activity up to this moment, `after`
    /// marks everything up to now as ours (set after posting, so our own events fall inside the mark).
    func before() { lock.lock(); refreshLocked(); lock.unlock() }
    func after() { lock.lock(); lastSynthetic = now(); lock.unlock() }

    func snapshot() -> (user: Double, hid: Double, ours: Bool) {
        lock.lock()
        defer { lock.unlock() }
        refreshLocked()
        let hid = hidIdle()
        return (now() - lastUser, hid, now() - hid <= lastSynthetic + slack)
    }
}

let input = InputTracker()

/// While any app has secure input on (a password field, some terminals), the window server drops synthetic key
/// events without a word. Typing into that silence and calling the step unverified is worse than failing.
func requireKeyboard() throws {
    if IsSecureEventInputEnabled() {
        throw RPCError("secure_input", "secure input is on (a password field has focus somewhere): keystrokes would be dropped")
    }
}

func inputKey(_ p: Params) throws -> Any {
    guard let combo = (p["combo"] as? String)?.lowercased(), !combo.isEmpty else { throw RPCError("bad_params", "combo required") }
    let parts = combo.split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }
    var flags = CGEventFlags()
    for m in parts.dropLast() {
        guard let f = modifierFlags[m] else { throw RPCError("bad_params", "unknown modifier \(m)") }
        flags.insert(f)
    }
    guard let key = parts.last, let resolved = keyCode(for: key) else {
        throw RPCError("bad_params", "unknown key \(parts.last ?? "")")
    }
    let code = Int(resolved.code)
    if resolved.shift { flags.insert(.maskShift) }
    try requireKeyboard()
    input.before()
    defer { input.after() }
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: src, virtualKey: CGKeyCode(code), keyDown: down)
        e?.flags = flags
        e?.post(tap: .cghidEventTap)
    }
    return ["ok": true]
}

/// Which key to press to get this character or named key, on the keyboard the user actually has.
///
/// The table above maps names to ANSI positions. On a German, French or JIS keyboard those positions hold
/// different characters — `cmd+[` pressed the key where an American keyboard has `[`, which on a German one
/// is `ü`. For anything that is a character, the current layout is asked instead; only keys that exist in
/// the same place on every keyboard (Return, Tab, the arrows, the function row) come from the table.
func keyCode(for name: String) -> (code: CGKeyCode, shift: Bool)? {
    if name.count == 1, let ch = name.first, let hit = asciiKeyMap()[ch] {
        return hit
    }
    if let ansi = keyCodes[name] {
        return (CGKeyCode(ansi), false)
    }
    return nil
}

/// Character -> (key code, needs shift) on the current ASCII-capable keyboard layout.
func asciiKeyMap() -> [Character: (CGKeyCode, Bool)] {
    guard let src = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue(),
          let ptr = TISGetInputSourceProperty(src, kTISPropertyUnicodeKeyLayoutData) else { return [:] }
    let data = Unmanaged<CFData>.fromOpaque(ptr).takeUnretainedValue() as Data
    var map: [Character: (CGKeyCode, Bool)] = [:]
    data.withUnsafeBytes { raw in
        guard let layout = raw.bindMemory(to: UCKeyboardLayout.self).baseAddress else { return }
        for code in 0..<128 {
            for shift in [false, true] {
                var dead: UInt32 = 0
                var length = 0
                var chars = [UniChar](repeating: 0, count: 4)
                let mods: UInt32 = shift ? UInt32((shiftKey >> 8) & 0xFF) : 0
                let err = UCKeyTranslate(layout, UInt16(code), UInt16(kUCKeyActionDown), mods, UInt32(LMGetKbdType()),
                                         OptionBits(kUCKeyTranslateNoDeadKeysBit), &dead, 4, &length, &chars)
                if err == noErr, length == 1, let ch = String(utf16CodeUnits: chars, count: 1).first, map[ch] == nil,
                   !ch.isNewline, ch != "\t" {
                    map[ch] = (CGKeyCode(code), shift)
                }
            }
        }
    }
    map["\n"] = (CGKeyCode(kVK_Return), false)
    map["\t"] = (CGKeyCode(kVK_Tab), false)
    return map
}

private func postKey(_ code: CGKeyCode, shift: Bool = false, cmd: Bool = false) {
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)
        var flags = CGEventFlags()
        if shift { flags.insert(.maskShift) }
        if cmd { flags.insert(.maskCommand) }
        e?.flags = flags
        e?.post(tap: .cghidEventTap)
    }
    usleep(6000)
}

private func postUnicode(_ text: String) {
    let src = CGEventSource(stateID: .hidSystemState)
    let units = Array(text.utf16)
    var i = 0
    while i < units.count {
        let chunk = Array(units[i..<min(i + 20, units.count)])
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: down)
            e?.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: chunk)
            e?.post(tap: .cghidEventTap)
        }
        i += 20
        usleep(4000)
    }
}

/// Paste text through the clipboard and put the user's clipboard back afterwards.
private func pasteRestoring(_ text: String, vKey: CGKeyCode) {
    let pb = NSPasteboard.general
    let saved: [[(NSPasteboard.PasteboardType, Data)]] = (pb.pasteboardItems ?? []).map { item in
        item.types.compactMap { t in item.data(forType: t).map { (t, $0) } }
    }
    pb.clearContents()
    pb.setString(text, forType: .string)
    postKey(vKey, cmd: true)
    usleep(250_000)   // let the app read it before the user's clipboard comes back
    pb.clearContents()
    if !saved.isEmpty {
        pb.writeObjects(saved.map { pairs in
            let item = NSPasteboardItem()
            for (t, d) in pairs { item.setData(d, forType: t) }
            return item
        })
    }
}

// MARK: - typing, and handing the keyboard back

/// What the focused element shows where typed keys go: the insertion point and the few characters right before
/// it, in UTF-16 units as Accessibility counts them — or, for an element with no insertion point, all it shows.
struct Caret: Equatable {
    var location: Int?
    var before: String = ""
    var value: String? = nil
}

/// One look at the focused element while the keys land.
enum CaretLook: Equatable {
    case text(Caret)
    case none        // no focused element, or one that shows no text (attribute unsupported, no value)
    case busy        // it did not answer in time: the app is still busy, likely reading the keys
}

/// How the wait for the keys ended.
enum Landing: String {
    case read        // the keys are all there: the insertion point is where they end, the last of them before it
    case settled     // what the element shows changed, then stayed the same for the quiet time
    case unreadable  // the element showed no text for the quiet time
    case timeout     // the ceiling
}

private func isBreak(_ ch: Character) -> Bool { ch.unicodeScalars.contains { $0 == "\n" || $0 == "\r" || $0 == "\t" } }

/// The last keys of a text: what follows its last Return or Tab, at most 8 characters of it.
func keyTail(_ text: String) -> String {
    let after = text.lastIndex(where: isBreak).map { text[text.index(after: $0)...] } ?? text[...]
    return String(after.suffix(8))
}

/// What must sit right before the insertion point once every key of `text` has landed, or "" when nothing can
/// say so: a Return or a Tab does what the app makes of it — a new line, the next field, sending the text —
/// so no position and no text follow from them (0 of 222 texts typed on 09-20..23 held one).
func landingTail(_ text: String) -> String { text.contains(where: isBreak) ? "" : keyTail(text) }

private func folded(_ s: String) -> String { s.folding(options: [.caseInsensitive], locale: nil) }

/// Wait until the focused element shows the keys have arrived, and say how it ended.
///
/// - `.read`: the insertion point is at `expectAt` with `tail` (case-folded) right before it; for an element
///   that shows only a value, the value changed from `before` and holds the tail more often than it did. An
///   empty tail never reads. The position, not the text alone: the text is already there when it is typed
///   again after select-all, or appended to a document that ends with it, and "abab" typed after "ab" reads
///   "abab" two keys early. A value that already held the tail holds it after the first key too.
/// - `.settled`: what the element shows changed, then stayed unchanged for `quiet` — an app that rewrites what
///   was typed (curly quotes, autocorrect) never shows the tail.
/// - `.unreadable`: no text for `quiet`.
/// - `.timeout`: `ceiling`.
/// A look the app is too busy to answer advances neither quiet time: an app that cannot answer may still be
/// reading the keys, and only the ceiling ends that wait.
func awaitLanding(tail: String, expectAt: Int?, before: Caret?, ceiling: TimeInterval, quiet: TimeInterval,
                  now: () -> TimeInterval, pause: TimeInterval = 0.015, sleep: (TimeInterval) -> Void,
                  look: () -> CaretLook) -> Landing {
    let start = now()
    let want = folded(tail)
    func count(_ s: String) -> Int { folded(s).components(separatedBy: want).count - 1 }
    func landed(_ c: Caret) -> Bool {
        guard !want.isEmpty else { return false }
        if let at = c.location { return at == expectAt && folded(c.before) == want }
        guard let value = c.value, let was = before?.value, value != was else { return false }
        return count(value) > count(was)
    }
    var last = before              // what the element showed at the last look that read text
    var changed = false            // it has shown something other than what it showed before the keys
    var sameSince: TimeInterval?   // since when it has shown `last`
    var noneSince: TimeInterval?   // since when it has shown no text
    while true {
        let seen = look()
        let t = now()
        switch seen {
        case .busy:
            sameSince = nil
            noneSince = nil
        case .none:
            sameSince = nil
            let since = noneSince ?? t
            noneSince = since
            if t - since >= quiet { return .unreadable }
        case .text(let c):
            noneSince = nil
            if landed(c) { return .read }
            if let l = last, c != l {
                changed = true
                last = c
                sameSince = t
            } else if last == nil {
                last = c               // nothing was read before the keys: this is what later looks compare with
                sameSince = t
            } else if sameSince == nil {
                sameSince = t
            }
            if changed, let s = sameSince, t - s >= quiet { return .settled }
        }
        if t - start >= ceiling { return .timeout }
        sleep(pause)
    }
}

/// How selecting the ASCII keyboard layout went.
enum LayoutSwitch: Equatable {
    case notNeeded   // it was already the current input source
    case inEffect    // selected, and in effect
    case didNotTake  // selected, and still not in effect after 0.5 s
}

/// Everything typing does to the Mac, behind one seam, so the order can be checked without a key being posted.
struct TypeEffects {
    var selectASCII: () -> LayoutSwitch
    var restore: () -> Void                  // the person's own input source back
    var key: (CGKeyCode, Bool) -> Void       // one key press, shifted or not
    var paste: (String) -> Void              // through the clipboard, which is restored afterwards
    var unicode: (String) -> Void            // synthetic unicode key events
    var after: () -> Void                    // everything up to now was ours (`input.after()`)
    var look: (Int) -> CaretLook             // the focused element, with that many UTF-16 units before the caret
    var now: () -> TimeInterval
    var sleep: (TimeInterval) -> Void

    /// The real Mac.
    static func live(pasteKey: CGKeyCode) -> TypeEffects {
        var previous: TISInputSource?
        return TypeEffects(
            selectASCII: {
                guard let current = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue(),
                      let ascii = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue(),
                      !CFEqual(ascii, current) else { return .notNeeded }
                previous = current
                TISSelectInputSource(ascii)
                // the switch is asynchronous: key presses sent before it lands go through the input method
                // (pinyin turns "jev" into "je'v"), so wait until it is really active
                for _ in 0..<50 {
                    if let now = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue(), CFEqual(now, ascii) { return .inEffect }
                    usleep(10_000)
                }
                return .didNotTake
            },
            restore: { if let previous { TISSelectInputSource(previous) } },
            key: { code, shift in postKey(code, shift: shift) },
            paste: { pasteRestoring($0, vKey: pasteKey) },
            unicode: postUnicode,
            after: { input.after() },
            look: caretNow,
            now: { ProcessInfo.processInfo.systemUptime },
            sleep: { usleep(useconds_t(max(0, $0) * 1_000_000)) })
    }
}

/// The focused element as the keys land, read with AXUIElementCopy* directly and never through axAttr: a slow
/// answer here is an app busy reading the keys, and must not mark it as not answering.
func caretNow(_ n: Int) -> CaretLook {
    var raw: CFTypeRef?
    switch AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute as CFString, &raw) {
    case .success: break
    case .cannotComplete: return .busy
    default: return .none
    }
    guard let raw, CFGetTypeID(raw) == AXUIElementGetTypeID() else { return .none }
    let el = raw as! AXUIElement
    func value() -> (String?, busy: Bool) {
        var v: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(el, kAXValueAttribute as CFString, &v)
        return (err == .success ? v as? String : nil, err == .cannotComplete)
    }
    var r: CFTypeRef?
    let got = AXUIElementCopyAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, &r)
    if got == .cannotComplete { return .busy }
    var range = CFRange()
    guard got == .success, let r, CFGetTypeID(r) == AXValueGetTypeID(), AXValueGetValue(r as! AXValue, .cfRange, &range) else {
        let (v, busy) = value()          // no insertion point: what the element shows
        if busy { return .busy }
        return v.map { .text(Caret(location: nil, value: $0)) } ?? .none
    }
    let at = range.location
    guard n > 0, at > 0 else { return .text(Caret(location: at)) }
    var want = CFRange(location: max(0, at - n), length: min(n, at))
    if let param = AXValueCreate(.cfRange, &want) {
        var s: CFTypeRef?
        let err = AXUIElementCopyParameterizedAttributeValue(el, kAXStringForRangeParameterizedAttribute as CFString, param, &s)
        if err == .cannotComplete { return .busy }
        if err == .success, let s = s as? String { return .text(Caret(location: at, before: s)) }
    }
    let (v, busy) = value()              // no string for a range: cut it from the value
    if busy { return .busy }
    return .text(Caret(location: at, before: v.flatMap { markedSubstring($0, location: want.location, length: want.length) } ?? ""))
}

/// Type `text` through `fx`: select the ASCII layout, post the keys, mark them ours, wait until they have
/// landed, and only then give the person's input source back.
///
/// The input source used to go back as soon as the last key was posted. An app still 1-4 keys behind a 6 ms
/// stream then read those keys through the input method: 21 of 265 typing steps on 09-20..23 left the end of
/// the text composing. Whatever lands while the ASCII layout is still selected was typed by it, so waiting
/// for the keys before giving the input source back makes them committed text. No wait when no switch was
/// needed, for an empty text, or when the text was pasted because the layout did not take.
func typeText(_ text: String, keys map: [Character: (CGKeyCode, Bool)], nonAscii: String,
              ceiling: TimeInterval, quiet: TimeInterval, fx: TypeEffects) -> [String: Any] {
    var markedOurs = false
    func ours() {             // exactly once: right after the last key, on the paste fallback, or on the way out
        if !markedOurs { markedOurs = true; fx.after() }
    }
    defer { ours() }
    guard !text.isEmpty else { return ["ok": true, "chars": 0, "switched": false, "landed": "no_switch", "handback_ms": 0] }
    let tail = keyTail(text)
    let n = tail.utf16.count
    let layout = fx.selectASCII()
    var before: Caret?
    if layout != .notNeeded {
        // let the switch settle; the caret is read inside those 30 ms, so a slow app does not delay the keys
        let t0 = fx.now()
        if layout == .inEffect, case .text(let c) = fx.look(n) { before = c }
        let left = 0.03 - (fx.now() - t0)
        if left > 0 { fx.sleep(left) }
    }
    if layout == .didNotTake {   // could not get a plain keyboard layout: paste everything instead of typing through an IME
        fx.paste(text)
        ours()
        fx.restore()
        return ["ok": true, "chars": text.count, "method": "paste", "switched": true, "landed": "no_switch", "handback_ms": 0]
    }
    var pending = ""
    func flushPending() {
        guard !pending.isEmpty else { return }
        if nonAscii == "unicode" { fx.unicode(pending) } else { fx.paste(pending) }
        pending = ""
    }
    for ch in text {
        if let (code, shift) = map[ch] {
            flushPending()
            fx.key(code, shift)
        } else {
            pending.append(ch)
        }
    }
    flushPending()
    ours()                    // a person touching the Mac during the wait is the person, not us
    guard layout == .inEffect else {
        return ["ok": true, "chars": text.count, "switched": false, "landed": "no_switch", "handback_ms": 0]
    }
    let t0 = fx.now()
    let landing = awaitLanding(tail: landingTail(text), expectAt: before?.location.map { $0 + text.utf16.count },
                               before: before, ceiling: ceiling, quiet: quiet, now: fx.now, sleep: fx.sleep,
                               look: { fx.look(n) })
    let held = fx.now() - t0
    fx.restore()
    return ["ok": true, "chars": text.count, "switched": true, "landed": landing.rawValue, "handback_ms": safeInt(held * 1000)]
}

/// Type text into whatever has keyboard focus. ASCII goes as real key presses on an ASCII keyboard layout
/// (selected for the duration, so an active input method cannot turn "print" into pinyin candidates — and
/// custom editors that ignore synthetic unicode events still get it); anything else goes by ``non_ascii``:
/// "paste" (clipboard, restored afterwards; works everywhere) or "unicode" (synthetic unicode key events).
///
/// When the layout had to be selected, the person's input source goes back only once the keys have landed:
/// at most ``handback_ms`` (default 1000) after the last key, and ``quiet_ms`` (default 250) after the focused
/// element stopped changing or while it shows no text. The reply says how: ``landed`` (read, settled,
/// unreadable, timeout, or no_switch when no layout was held), ``handback_ms`` and ``switched``.
func inputType(_ p: Params) throws -> Any {
    guard let text = p["text"] as? String else { throw RPCError("bad_params", "text required") }
    try requireKeyboard()
    input.before()
    let map = asciiKeyMap()
    let ceiling = max(0, (p["handback_ms"] as? NSNumber)?.doubleValue ?? 1000) / 1000
    let quiet = max(0, (p["quiet_ms"] as? NSNumber)?.doubleValue ?? 250) / 1000
    return typeText(text, keys: map, nonAscii: p["non_ascii"] as? String ?? "paste", ceiling: ceiling, quiet: quiet,
                    fx: .live(pasteKey: map["v"]?.0 ?? CGKeyCode(kVK_ANSI_V)))
}

func inputClick(_ p: Params) throws -> Any {
    guard let x = p["x"] as? Double ?? (p["x"] as? Int).map(Double.init),
          let y = p["y"] as? Double ?? (p["y"] as? Int).map(Double.init) else { throw RPCError("bad_params", "x, y required") }
    let right = (p["button"] as? String) == "right"
    let count = p["count"] as? Int ?? 1
    input.before()
    defer { input.after() }
    let pt = CGPoint(x: x, y: y)
    let (down, up, btn): (CGEventType, CGEventType, CGMouseButton) = right ? (.rightMouseDown, .rightMouseUp, .right) : (.leftMouseDown, .leftMouseUp, .left)
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: pt, mouseButton: btn)?.post(tap: .cghidEventTap)
    for n in 1...max(1, count) {
        for t in [down, up] {
            let e = CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: pt, mouseButton: btn)
            e?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
            e?.post(tap: .cghidEventTap)
        }
    }
    return ["ok": true]
}

/// Scroll where the pointer is put.
///
/// A canvas app draws its own content and exposes no Accessibility tree to scroll: `AXScrollDownByPage` is
/// not there to be performed, and whether Page Down does anything depends on what has keyboard focus. The
/// wheel is the one way in that does not assume the app implements something — it goes to whatever view is
/// under the pointer, which is how a mouse works.
///
/// Units are lines, the same as a real wheel reports, so "3" means what it means to the app rather than
/// some pixel count that scrolls a different distance in every app.
func inputScroll(_ p: Params) throws -> Any {
    func num(_ k: String) -> Double? { p[k] as? Double ?? (p[k] as? Int).map(Double.init) }
    guard let x = num("x"), let y = num("y") else { throw RPCError("bad_params", "x, y required") }
    let dy = Int32(num("dy") ?? 0), dx = Int32(num("dx") ?? 0)
    guard dy != 0 || dx != 0 else { throw RPCError("bad_params", "dy or dx required") }
    input.before()
    defer { input.after() }
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved,
            mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left)?.post(tap: .cghidEventTap)
    // in steps: a single large wheel event is treated as a flick by some views and overshoots
    let steps = max(1, min(abs(Int(dy)) + abs(Int(dx)), p["steps"] as? Int ?? 3))
    for i in 1...steps {
        let part: (Int32) -> Int32 = { Int32(round(Double($0) * Double(i) / Double(steps))) - Int32(round(Double($0) * Double(i - 1) / Double(steps))) }
        CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2,
                wheel1: part(dy), wheel2: part(dx), wheel3: 0)?.post(tap: .cghidEventTap)
        usleep(30_000)
    }
    return ["ok": true]
}

/// Drag with the left button from one point to another, in small steps (apps track the movement, not just the ends).
func inputDrag(_ p: Params) throws -> Any {
    func num(_ k: String) -> Double? { p[k] as? Double ?? (p[k] as? Int).map(Double.init) }
    guard let x1 = num("x1"), let y1 = num("y1"), let x2 = num("x2"), let y2 = num("y2") else { throw RPCError("bad_params", "x1, y1, x2, y2 required") }
    let steps = max(2, p["steps"] as? Int ?? 12)
    input.before()
    defer { input.after() }
    let a = CGPoint(x: x1, y: y1), b = CGPoint(x: x2, y: y2)
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: a, mouseButton: .left)?.post(tap: .cghidEventTap)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: a, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(80_000)
    for i in 1...steps {
        let f = Double(i) / Double(steps)
        let pt = CGPoint(x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f)
        CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(15_000)
    }
    usleep(80_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: b, mouseButton: .left)?.post(tap: .cghidEventTap)
    return ["ok": true]
}

/// What is on the clipboard — by default only its shape, never its contents: a password manager puts real
/// secrets there, and this is read on every look. `preview_chars` opts into a prefix of the text.
func clipboardRead(_ p: Params) throws -> Any {
    let pb = NSPasteboard.general
    let text = pb.string(forType: .string)
    var out: [String: Any] = ["change_count": pb.changeCount,
                              "types": (pb.types ?? []).map { $0.rawValue },
                              "chars": text?.count ?? 0]
    if let n = p["preview_chars"] as? Int, n > 0, let t = text {
        out["text"] = String(t.prefix(n))
    }
    return out
}

/// Put text on the clipboard. The clipboard is how apps that share nothing else exchange data; pasting it
/// somewhere is a separate action, and one the safety floor gates.
func clipboardWrite(_ p: Params) throws -> Any {
    guard let text = p["text"] as? String else { throw RPCError("bad_params", "text required") }
    let pb = NSPasteboard.general
    pb.clearContents()
    pb.setString(text, forType: .string)
    return ["ok": true, "change_count": pb.changeCount]
}

/// Phone numbers, addresses, dates and links, found the way the system finds them.
///
/// The hand-written patterns this replaces knew mainland-Chinese mobile numbers and North American ones, and
/// street names in English and Chinese — so a German, Japanese or Brazilian address went to the decider in
/// the clear. NSDataDetector is the same machinery the OS uses to underline a phone number in an email, and
/// it knows the formats of everywhere.
func nlDetect(_ p: Params) throws -> Any {
    guard let texts = p["texts"] as? [String] else { throw RPCError("bad_params", "texts required") }
    let wanted = Set(p["kinds"] as? [String] ?? ["PHONE", "ADDRESS"])
    var types: NSTextCheckingResult.CheckingType = []
    if wanted.contains("PHONE") { types.insert(.phoneNumber) }
    if wanted.contains("ADDRESS") { types.insert(.address) }
    if wanted.contains("LINK") { types.insert(.link) }
    if wanted.contains("DATE") { types.insert(.date) }
    guard !types.isEmpty, let detector = try? NSDataDetector(types: types.rawValue) else {
        return texts.map { _ in [Any]() }
    }
    return texts.map { text -> [[String: Any]] in
        let range = NSRange(text.startIndex..., in: text)
        return detector.matches(in: text, range: range).compactMap { m in
            guard let r = Range(m.range, in: text) else { return nil }
            let kind: String
            switch m.resultType {
            case .phoneNumber: kind = "PHONE"
            case .address: kind = "ADDRESS"
            case .link: kind = "LINK"
            case .date: kind = "DATE"
            default: return nil
            }
            return ["type": kind, "text": String(text[r])]
        }
    }
}

/// The text of a file.
///
/// A task may only write what it saw, and until now "saw" meant a screen: a file the goal is about had to be
/// opened in an app and read off its window. The frameworks already know how to read these formats, so
/// nothing here parses one by hand, and what kind of file it is comes from the system, not from its name.
/// Move a file or folder to the Trash — the system's own trashItem, so it keeps where it came from and the
/// user can put it back. Deleting outright is deliberately not offered: the engine never destroys anything
/// that the person cannot get back, and the safety floor still asks before this runs at all.
func fileTrash(_ p: Params) throws -> Any {
    guard let path = p["path"] as? String else { throw RPCError("bad_params", "path required") }
    let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
    var moved: NSURL?
    do {
        try FileManager.default.trashItem(at: url, resultingItemURL: &moved)
    } catch {
        throw RPCError("trash_failed", "\(url.lastPathComponent): \(error.localizedDescription)")
    }
    var out: [String: Any] = ["path": url.path]
    if let now = moved as URL? { out["now_at"] = now.path }      // where it can be put back from
    return out
}

func fileReadText(_ p: Params) throws -> Any {
    guard let path = p["path"] as? String else { throw RPCError("bad_params", "path required") }
    let url = URL(fileURLWithPath: path)
    let limit = p["max_chars"] as? Int ?? 20_000
    // whether a file has text in it is the system's judgement, from its type, not ours from its name. Without
    // this the readers below happily return an icon or a binary as mojibake, and the task would record that as
    // something it had read.
    let type = (try? url.resourceValues(forKeys: [.contentTypeKey]))?.contentType
    // Three mechanisms, not a list of formats anyone thought of: .rtf, .html, .xml, .source-code and
    // .json all conform to .text already, so naming them was redundant — and naming formats is how a list
    // ends up excluding whatever the user installs next.
    //   .text             the system says it is text
    //   .pdf              PDFKit reads it
    //   .compositeContent documents AppKit reads (.docx, .rtfd) that are not "text"
    let readable: [UTType] = [.text, .pdf, .compositeContent]
    let declared = type.map { t in readable.contains(where: { t.conforms(to: $0) }) } ?? false
    // an extension nobody registered gets a made-up type that conforms to nothing (a .toml, say), so an
    // unknown type is not a "no" — it is a "find out", and finding out means the bytes really decoding.
    // `public.data` is the same situation said differently: it is what a file with no extension at all
    // gets, which is README, Makefile and every dotfile, and those were being refused outright.
    let unknown = type.map { $0.isDynamic || $0 == .data } ?? true
    guard declared || unknown else {
        throw RPCError("not_text", "\(url.lastPathComponent) is a \(type?.localizedDescription ?? "file"), not something with text in it")
    }
    let kind = type?.identifier ?? "public.data"
    if declared, type?.conforms(to: .pdf) == true, let doc = PDFDocument(url: url) {
        return ["text": String((doc.string ?? "").prefix(limit)), "kind": kind, "pages": doc.pageCount]
    }
    // only for a type that says it holds text: this reader is lenient enough to turn an icon into mojibake
    // `truncated` because a prefix that does not say so reads as the whole file, and what a task read is
    // what it is then allowed to write back
    if declared, let s = try? NSAttributedString(url: url, options: [:], documentAttributes: nil) {
        return ["text": String(s.string.prefix(limit)), "kind": kind, "truncated": s.string.count > limit]
    }
    if let data = try? Data(contentsOf: url), let s = String(data: data.prefix(limit * 4), encoding: .utf8) {
        return ["text": String(s.prefix(limit)), "kind": kind, "truncated": s.count > limit]
    }
    throw RPCError("not_text", "\(url.lastPathComponent) could not be read as text")
}

/// Run a Service — the system-wide "hand this content to that app" mechanism every app can publish into.
///
/// The content goes on a pasteboard of our own, never the general one: the user's clipboard is theirs. The
/// name is the item as it appears in the Services menu, "Submenu/Item" for the ones that sit in a submenu,
/// which is exactly the form the providing bundle declares in its own Info.plist.
func servicesPerform(_ p: Params) throws -> Any {
    guard let name = p["name"] as? String, !name.isEmpty else { throw RPCError("bad_params", "name required") }
    let pb = NSPasteboard(name: NSPasteboard.Name("dev.macwork.service"))
    pb.clearContents()
    var wrote = false
    if let files = p["files"] as? [String], !files.isEmpty {
        wrote = pb.writeObjects(files.map { URL(fileURLWithPath: $0) as NSURL })
    }
    if let text = p["text"] as? String, !text.isEmpty {
        wrote = pb.setString(text, forType: .string) || wrote
    }
    guard wrote else { throw RPCError("bad_params", "a service needs text or files to act on") }
    return ["ok": NSPerformService(name, pb)]
}

/// Seconds since the last keyboard/mouse input *from the user* — our own injections excluded, which is what
/// the engine means when it asks whether it may take the keyboard. `hid_idle_s` is the raw system figure.
func inputIdle(_ p: Params) throws -> Any {
    let s = input.snapshot()
    return ["idle_s": s.user, "hid_idle_s": s.hid, "last_input_was_ours": s.ours,
            "secure_input": IsSecureEventInputEnabled()]
}

// MARK: - AppleScript (Automation permission is asked per target app, attributed to this helper)

func scriptRun(_ p: Params) throws -> Any {
    guard let source = p["source"] as? String else { throw RPCError("bad_params", "source required") }
    guard let script = NSAppleScript(source: source) else { throw RPCError("script", "cannot compile") }
    var err: NSDictionary?
    let out = script.executeAndReturnError(&err)
    if let err {
        throw RPCError("script", "\(err[NSAppleScript.errorNumber] ?? ""): \(err[NSAppleScript.errorMessage] ?? err)")
    }
    return ["result": (out.stringValue as Any?) ?? NSNull()]
}

// MARK: - on-device named entities (for redaction before anything leaves the Mac)

/// ``text``: one string -> [entity]; ``texts``: many strings tagged one by one (each keeps its own context,
/// which tags names far better than one long blob) -> [[entity]] in one round trip, one reused tagger.
private let nameTagger = NLTagger(tagSchemes: [.nameType])
private let wantedNames: [NLTag: String] = [.personalName: "PERSON", .placeName: "PLACE", .organizationName: "ORG"]

private func tagNames(_ text: String) -> [[String: Any]] {
    nameTagger.string = text
    var out: [[String: Any]] = []
    nameTagger.enumerateTags(in: text.startIndex..<text.endIndex, unit: .word, scheme: .nameType,
                             options: [.omitWhitespace, .omitPunctuation, .joinNames]) { tag, range in
        if let tag, let kind = wantedNames[tag] {
            out.append(["type": kind, "text": String(text[range]),
                        "start": text.unicodeScalars.distance(from: text.unicodeScalars.startIndex, to: range.lowerBound),
                        "end": text.unicodeScalars.distance(from: text.unicodeScalars.startIndex, to: range.upperBound)])
        }
        return true
    }
    return out
}

/// Names the on-device tagger can find.
///
/// Its coverage is uneven and it does not report its own limits usefully: `availableTagSchemes` says it
/// cannot do names in Chinese, and yet it finds them. So there is no flag here to build a policy on — what
/// there is instead is `macwork privacy-check`, which measures the coverage against a corpus of eight
/// scripts and says plainly which languages come back untouched.
func nlEntities(_ p: Params) throws -> Any {
    if let texts = p["texts"] as? [String] { return texts.map(tagNames) }
    guard let text = p["text"] as? String else { throw RPCError("bad_params", "text or texts required") }
    return tagNames(text)
}

// MARK: - this Mac

/// This Mac's own identity — its serial number and hardware UUID — as the platform expert holds them, so the
/// engine can keep them out of what leaves the Mac. The serial number of a leftover About This Mac window went
/// out in 1,926 decider requests: no name tagger calls it a name, and no pattern knows its shape, so the Mac is
/// asked for it instead. {serial, uuid}; empty when the registry does not say.
func systemIdentity(_ p: Params) throws -> Any {
    let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
    guard service != 0 else { return [String: Any]() }
    defer { IOObjectRelease(service) }
    var out: [String: Any] = [:]
    for (key, name) in [(kIOPlatformSerialNumberKey, "serial"), (kIOPlatformUUIDKey, "uuid")] {
        if let v = IORegistryEntryCreateCFProperty(service, key as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() as? String,
           !v.isEmpty {
            out[name] = v
        }
    }
    return out
}

// MARK: - installed apps (localized display names: "计算器" for Calculator.app)

/// System apps keep their localized names in InfoPlist.loctable ({locale: {key: value}}), which
/// displayName(atPath:) ignores for a process without its own localizations.
private func localizedAppName(_ path: String) -> String? {
    guard let b = Bundle(path: path) else { return nil }
    let prefs = Bundle.preferredLocalizations(from: b.localizations, forPreferences: Locale.preferredLanguages)
    let table = path + "/Contents/Resources/InfoPlist.loctable"
    if let dict = NSDictionary(contentsOfFile: table) as? [String: [String: Any]] {
        for loc in prefs + [Locale.preferredLanguages.first ?? ""] {
            if let names = dict[loc], let n = (names["CFBundleDisplayName"] ?? names["CFBundleName"]) as? String { return n }
        }
    }
    for loc in prefs {  // classic .lproj/InfoPlist.strings
        if let p = b.path(forResource: "InfoPlist", ofType: "strings", inDirectory: nil, forLocalization: loc),
           let d = NSDictionary(contentsOfFile: p), let n = (d["CFBundleDisplayName"] ?? d["CFBundleName"]) as? String { return n }
    }
    return nil
}

/// Every bundle Spotlight knows about. Spotlight indexes the whole disk, so this finds apps wherever they
/// were put — including the ones macOS keeps outside the usual folders, such as Finder.
private func spotlightAppPaths(timeout: Double) -> [String] {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/mdfind")
    task.arguments = ["kMDItemContentType == 'com.apple.application-bundle'"]
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = FileHandle.nullDevice
    guard (try? task.run()) != nil else { return [] }
    let deadline = Date().addingTimeInterval(timeout)
    let data = (try? pipe.fileHandleForReading.readToEnd()) ?? Data()
    while task.isRunning && Date() < deadline { usleep(10_000) }
    if task.isRunning { task.terminate() }
    return String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init)
}

/// The apps this Mac would actually launch.
///
/// Scanning a few fixed folders is both too narrow and too wide. Too narrow: it misses everything installed
/// anywhere else — on this Mac 147 found that way against 461 bundles on disk, with
/// /System/Library/CoreServices/Finder.app among the missing. Too wide, if one simply took every bundle:
/// build products, simulator copies and helper apps buried inside other apps are not things a user can open.
///
/// Neither judgement needs a list of paths to believe in. Spotlight says where the bundles are; LaunchServices
/// says which one it would open for a given bundle identifier. An app counts as installed when the two agree
/// — that is the system's own answer to "would this launch", and it costs no knowledge of our own.
func appsInstalled(_ p: Params) throws -> Any {
    let fm = FileManager.default
    let dirs = p["dirs"] as? [String] ?? ["/Applications", "/System/Applications", "/System/Applications/Utilities", "~/Applications"]
    let useSpotlight = (p["source"] as? String ?? "launchservices") != "dirs"

    var candidates: [String] = []
    for d in dirs {                            // always included: Spotlight can be off, or still indexing
        let dir = (d as NSString).expandingTildeInPath
        for item in (try? fm.contentsOfDirectory(atPath: dir)) ?? [] where item.hasSuffix(".app") {
            candidates.append(dir + "/" + item)
        }
    }
    if useSpotlight {
        candidates += spotlightAppPaths(timeout: (p["timeout_s"] as? Double) ?? 8)
    }

    var out: [[String: Any]] = []
    var seen = Set<String>()
    for candidate in candidates {
        guard candidate.hasSuffix(".app") else { continue }
        // a bundle inside another bundle is that app's business, not something a person opens
        if candidate.dropLast(4).contains(".app/") { continue }
        guard let bundleId = Bundle(path: candidate)?.bundleIdentifier else { continue }
        guard !seen.contains(bundleId) else { continue }
        seen.insert(bundleId)

        // LaunchServices is asked which copy it would actually open for this identifier, and that is the one
        // reported — not the copy that happened to be found. It collapses duplicates (a build product, an old
        // version in Downloads) and it finds apps the search could not see at all: Safari really lives in a
        // cryptex, and requiring the found path to match would have dropped it.
        let path = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId)?.standardizedFileURL.path ?? candidate
        let bundle = Bundle(path: path) ?? Bundle(path: candidate)
        let item = (path as NSString).lastPathComponent
        var name = localizedAppName(path) ?? fm.displayName(atPath: path)
        if name.hasSuffix(".app") { name = String(name.dropLast(4)) }
        var d: [String: Any] = ["name": name, "file": String(item.dropLast(4)), "path": path, "bundle_id": bundleId]
        // the app's own statement that it runs without a window (a menu-bar agent, a system service). Not a
        // reason to hide it — the engine reaches such apps through their status items — but the decider
        // should know that "open it" will not put anything on screen.
        let info = bundle?.infoDictionary ?? [:]
        if (info["LSUIElement"] as? Bool ?? false) || (info["LSBackgroundOnly"] as? Bool ?? false)
            || (info["LSUIElement"] as? String) == "1" || (info["LSBackgroundOnly"] as? String) == "1" {
            d["background"] = true
        }
        out.append(d)
    }
    return out
}

/// Which apps this Mac would open a file with, as LaunchServices answers it — the system's own list, in its
/// own order (the default handler first). A goal that says "open it in Safari" needs that to be an option;
/// without it the engine could only ask the system to open the file and take whatever came up.
func appsOpeners(_ p: Params) throws -> Any {
    guard let raw = p["path"] as? String else { throw RPCError("bad_params", "path required") }
    let url = URL(fileURLWithPath: (raw as NSString).expandingTildeInPath)
    guard FileManager.default.fileExists(atPath: url.path) else { return [Any]() }
    let limit = p["limit"] as? Int ?? 6
    var out: [[String: Any]] = []
    var seen = Set<String>()
    let preferred = NSWorkspace.shared.urlForApplication(toOpen: url)
    var urls = NSWorkspace.shared.urlsForApplications(toOpen: url)
    if let preferred, let i = urls.firstIndex(of: preferred) { urls.remove(at: i) }      // the default one leads
    for appURL in ([preferred].compactMap { $0 } + urls) {
        let path = appURL.standardizedFileURL.path
        guard let bundleId = Bundle(path: path)?.bundleIdentifier, !seen.contains(bundleId) else { continue }
        seen.insert(bundleId)
        var name = localizedAppName(path) ?? FileManager.default.displayName(atPath: path)
        if name.hasSuffix(".app") { name = String(name.dropLast(4)) }
        out.append(["name": name, "path": path, "bundle_id": bundleId, "default": appURL == preferred])
        if out.count >= limit { break }
    }
    return out
}

// MARK: - windows on screen

/// Whether a window level is one an app's own windows live at: from the normal level up to, not including, the
/// main menu's — normal, floating, modal panel, utility — as the Mac numbers them. The menu bar, status items
/// and open menus sit at that level and above. Layer 0 alone left out an app's own floating and modal panels,
/// and any layer at all would count its status item's window as one of its windows.
func ordinaryLevel(_ layer: Int) -> Bool {
    Int(CGWindowLevelForKey(.normalWindow)) <= layer && layer < Int(CGWindowLevelForKey(.mainMenuWindow))
}

/// One window of the window server's list as the engine gets it, or nil when it is not wanted: without its
/// owner or its bounds, or another process's when `pidFilter` names one. `onScreen` holds the windows on this
/// Space when the list was of every window, nil when it was of the on-screen ones only.
func windowEntry(_ w: [String: Any], onScreen: Set<Int>?, regular: (Int) -> Bool, pidFilter: Int?,
                 imPids: Set<Int>) -> [String: Any]? {
    guard let pid = w[kCGWindowOwnerPID as String] as? Int, let b = w[kCGWindowBounds as String] as? [String: Any] else { return nil }
    if let only = pidFilter, pid != only { return nil }
    let frame = ["X", "Y", "Width", "Height"].map { safeInt((b[$0] as? Double) ?? Double((b[$0] as? Int) ?? 0)) }
    let number = w[kCGWindowNumber as String] as? Int ?? 0
    let layer = w[kCGWindowLayer as String] as? Int ?? 0
    var e: [String: Any] = ["pid": pid, "id": number, "owner": w[kCGWindowOwnerName as String] as? String ?? "",
                            "layer": layer, "frame": frame, "regular": regular(pid),
                            "alpha": w[kCGWindowAlpha as String] as? Double ?? 1,
                            "on_screen": onScreen.map { $0.contains(number) } ?? true,
                            "ordinary": ordinaryLevel(layer), "input_method": imPids.contains(pid)]
    // a title only for the one app asked about: it is that app's to show, and the list of everything on
    // screen stays without anyone's document names
    if pidFilter != nil, let title = w[kCGWindowName as String] as? String, !title.isEmpty { e["title"] = title }
    return e
}

/// A running process, as much of it as says whether it belongs to an input method.
struct RunningProcess {
    let pid: Int
    let bundleId: String?
    let bundlePath: String?
    let executablePath: String?
}

/// The processes of the input methods whose bundles are given: a process whose bundle identifier is one of
/// them, or whose bundle or executable lies inside one of those bundles — its services and helpers carry
/// identifiers of their own. Paths are compared with a trailing "/", so ".../Example.app2" is not inside
/// ".../Example.app".
func inputMethodPids(bundleIds: Set<String>, bundlePaths: [String], running: [RunningProcess]) -> Set<Int> {
    let roots = bundlePaths.map { $0.hasSuffix("/") ? $0 : $0 + "/" }
    func inside(_ path: String?) -> Bool {
        guard let path else { return false }
        return roots.contains { (path + "/").hasPrefix($0) }
    }
    var out = Set<Int>()
    for r in running {
        if let id = r.bundleId, bundleIds.contains(id) { out.insert(r.pid) }
        else if inside(r.bundlePath) || inside(r.executablePath) { out.insert(r.pid) }
    }
    return out
}

private func tisString(_ s: TISInputSource, _ key: CFString) -> String? {
    guard let p = TISGetInputSourceProperty(s, key) else { return nil }
    return Unmanaged<AnyObject>.fromOpaque(p).takeUnretainedValue() as? String
}

/// The processes of this Mac's enabled keyboard input methods, asked of Text Input Sources and LaunchServices:
/// no input method is known by name. Keyboard layouts have no process of their own, and palettes (the
/// Character Viewer, press-and-hold accents) are another category, opened on purpose. Measured at 1.3 ms a
/// call; on this Mac it finds the input method (by its bundle identifier) and its services process (inside
/// the bundle), and nothing else.
func inputMethodPids() -> Set<Int> {
    let filter = [kTISPropertyInputSourceCategory as String: kTISCategoryKeyboardInputSource as String] as CFDictionary
    let sources = (TISCreateInputSourceList(filter, false)?.takeRetainedValue() as? [TISInputSource]) ?? []   // enabled only
    var ids = Set<String>()
    for s in sources where tisString(s, kTISPropertyInputSourceType) != (kTISTypeKeyboardLayout as String) {
        if let id = tisString(s, kTISPropertyBundleID) { ids.insert(id) }
    }
    guard !ids.isEmpty else { return [] }
    let apps = NSWorkspace.shared.runningApplications
    var paths = ids.compactMap { NSWorkspace.shared.urlForApplication(withBundleIdentifier: $0)?.standardizedFileURL.path }
    for a in apps {
        if let id = a.bundleIdentifier, ids.contains(id), let u = a.bundleURL { paths.append(u.standardizedFileURL.path) }
    }
    let running = apps.map { RunningProcess(pid: Int($0.processIdentifier), bundleId: $0.bundleIdentifier,
                                            bundlePath: $0.bundleURL?.standardizedFileURL.path,
                                            executablePath: $0.executableURL?.standardizedFileURL.path) }
    return inputMethodPids(bundleIds: ids, bundlePaths: paths, running: running)
}

/// Every window on screen, front to back, with its owner and layer. Lets the engine notice what covers an app — a
/// permission prompt or an alert from another process — and find an app's windows without asking the app,
/// which a launching or busy app does not answer.
/// Every window, or only the ones on screen. "On screen" means this Space: a window the task opened and then
/// switched away from is not gone, and tracking it as gone loses it for good.
///
/// - `pid`: only that process's windows, each with its `title` where the window server has one. Titles are given
///   only for the one app asked about; reading them needs Screen Recording, which reading a window by sight
///   already needs.
/// - `ordinary`: a level an app's own windows live at (see `ordinaryLevel`).
/// - `input_method`: the window belongs to an enabled keyboard input method — its candidates, its status, its
///   settings — which is nobody's prompt.
func screenWindows(_ p: Params) throws -> Any {
    let all = p["all"] as? Bool ?? false
    let only = p["pid"] as? Int
    let options: CGWindowListOption = all ? [.optionAll, .excludeDesktopElements] : [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        return [Any]()
    }
    let onScreen: Set<Int>? = all
        ? Set(((CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]) ?? [])
            .compactMap { $0[kCGWindowNumber as String] as? Int })
        : nil
    let imPids = inputMethodPids()
    var regular: [Int: Bool] = [:]
    func isRegular(_ pid: Int) -> Bool {
        if let known = regular[pid] { return known }
        let r = NSRunningApplication(processIdentifier: pid_t(pid))?.activationPolicy == .regular
        regular[pid] = r
        return r
    }
    return list.compactMap { windowEntry($0, onScreen: onScreen, regular: isRegular, pidFilter: only, imPids: imPids) }
}
