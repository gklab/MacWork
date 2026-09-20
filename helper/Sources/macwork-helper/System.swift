import AppKit
import ApplicationServices
import Carbon.HIToolbox
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
    let asked = app.terminate()
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
    guard let key = parts.last, let code = keyCodes[key] else { throw RPCError("bad_params", "unknown key \(parts.last ?? "")") }
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

/// Character -> (key code, needs shift) on the current ASCII-capable keyboard layout.
private func asciiKeyMap() -> [Character: (CGKeyCode, Bool)] {
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

/// Type text into whatever has keyboard focus. ASCII goes as real key presses on an ASCII keyboard layout
/// (selected for the duration, so an active input method cannot turn "print" into pinyin candidates — and
/// custom editors that ignore synthetic unicode events still get it); anything else goes by ``non_ascii``:
/// "paste" (clipboard, restored afterwards; works everywhere) or "unicode" (synthetic unicode key events).
func inputType(_ p: Params) throws -> Any {
    guard let text = p["text"] as? String else { throw RPCError("bad_params", "text required") }
    try requireKeyboard()
    input.before()
    defer { input.after() }
    let nonAscii = p["non_ascii"] as? String ?? "paste"
    let map = asciiKeyMap()
    let current = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue()
    let ascii = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue()
    var switched = false
    var asciiActive = true
    if let ascii, let current, !CFEqual(ascii, current) {
        TISSelectInputSource(ascii)
        switched = true
        // the switch is asynchronous: key presses sent before it lands go through the input method
        // (pinyin turns "jev" into "je'v"), so wait until it is really active
        asciiActive = false
        for _ in 0..<50 {
            if let now = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue(), CFEqual(now, ascii) { asciiActive = true; break }
            usleep(10_000)
        }
        usleep(30_000)
    }
    defer { if switched, let current { TISSelectInputSource(current) } }
    if !asciiActive {   // could not get a plain keyboard layout: paste everything instead of typing through an IME
        pasteRestoring(text, vKey: map["v"]?.0 ?? CGKeyCode(kVK_ANSI_V))
        return ["ok": true, "chars": text.count, "method": "paste"]
    }
    var pending = ""
    func flushPending() {
        guard !pending.isEmpty else { return }
        if nonAscii == "unicode" { postUnicode(pending) } else { pasteRestoring(pending, vKey: map["v"]?.0 ?? CGKeyCode(kVK_ANSI_V)) }
        pending = ""
    }
    for ch in text {
        if let (code, shift) = map[ch] {
            flushPending()
            postKey(code, shift: shift)
        } else {
            pending.append(ch)
        }
    }
    flushPending()
    return ["ok": true, "chars": text.count]
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
func fileReadText(_ p: Params) throws -> Any {
    guard let path = p["path"] as? String else { throw RPCError("bad_params", "path required") }
    let url = URL(fileURLWithPath: path)
    let limit = p["max_chars"] as? Int ?? 20_000
    // whether a file has text in it is the system's judgement, from its type, not ours from its name. Without
    // this the readers below happily return an icon or a binary as mojibake, and the task would record that as
    // something it had read.
    let type = (try? url.resourceValues(forKeys: [.contentTypeKey]))?.contentType
    let readable: [UTType] = [.text, .pdf, .rtf, .html, .xml, .compositeContent, .sourceCode, .json]
    let declared = type.map { t in readable.contains(where: { t.conforms(to: $0) }) } ?? false
    // an extension nobody registered gets a made-up type that conforms to nothing (a .toml, say), so an
    // unknown type is not a "no" — it is a "find out", and finding out means the bytes really decoding
    let unknown = type?.isDynamic ?? true
    guard declared || unknown else {
        throw RPCError("not_text", "\(url.lastPathComponent) is a \(type?.localizedDescription ?? "file"), not something with text in it")
    }
    let kind = type?.identifier ?? "public.data"
    if declared, type?.conforms(to: .pdf) == true, let doc = PDFDocument(url: url) {
        return ["text": String((doc.string ?? "").prefix(limit)), "kind": kind, "pages": doc.pageCount]
    }
    // only for a type that says it holds text: this reader is lenient enough to turn an icon into mojibake
    if declared, let s = try? NSAttributedString(url: url, options: [:], documentAttributes: nil) {
        return ["text": String(s.string.prefix(limit)), "kind": kind]
    }
    if let data = try? Data(contentsOf: url), let s = String(data: data.prefix(limit * 4), encoding: .utf8) {
        return ["text": String(s.prefix(limit)), "kind": kind]
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

// MARK: - windows on screen

/// Every window on screen, front to back, with its owner and layer (no titles: those need Screen Recording).
/// Lets the engine notice what covers an app — a permission prompt or an alert from another process.
/// Every window, or only the ones on screen. "On screen" means this Space: a window the task opened and then
/// switched away from is not gone, and tracking it as gone loses it for good.
func screenWindows(_ p: Params) throws -> Any {
    let all = p["all"] as? Bool ?? false
    let options: CGWindowListOption = all ? [.optionAll, .excludeDesktopElements] : [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        return [Any]()
    }
    let onScreen: Set<Int> = all
        ? Set(((CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]) ?? [])
            .compactMap { $0[kCGWindowNumber as String] as? Int })
        : []
    return list.compactMap { w -> [String: Any]? in
        guard let pid = w[kCGWindowOwnerPID as String] as? Int, let b = w[kCGWindowBounds as String] as? [String: Any] else { return nil }
        let frame = ["X", "Y", "Width", "Height"].map { safeInt((b[$0] as? Double) ?? Double((b[$0] as? Int) ?? 0)) }
        let app = NSRunningApplication(processIdentifier: pid_t(pid))
        let number = w[kCGWindowNumber as String] as? Int ?? 0
        return ["pid": pid, "id": number,
                "owner": w[kCGWindowOwnerName as String] as? String ?? "", "layer": w[kCGWindowLayer as String] as? Int ?? 0,
                "frame": frame, "regular": app?.activationPolicy == .regular,
                "alpha": w[kCGWindowAlpha as String] as? Double ?? 1,
                "on_screen": all ? onScreen.contains(number) : true]
    }
}
