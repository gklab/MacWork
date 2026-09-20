import AppKit
import ApplicationServices
import Carbon.HIToolbox
import NaturalLanguage

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
func inputKey(_ p: Params) throws -> Any {
    guard let combo = (p["combo"] as? String)?.lowercased(), !combo.isEmpty else { throw RPCError("bad_params", "combo required") }
    let parts = combo.split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }
    var flags = CGEventFlags()
    for m in parts.dropLast() {
        guard let f = modifierFlags[m] else { throw RPCError("bad_params", "unknown modifier \(m)") }
        flags.insert(f)
    }
    guard let key = parts.last, let code = keyCodes[key] else { throw RPCError("bad_params", "unknown key \(parts.last ?? "")") }
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

/// Seconds since the last keyboard/mouse input from the user (to yield instead of fighting over the Mac).
func inputIdle(_ p: Params) throws -> Any {
    let s = CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: CGEventType(rawValue: ~0)!)
    return ["idle_s": s]
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

func appsInstalled(_ p: Params) throws -> Any {
    let dirs = p["dirs"] as? [String] ?? ["/Applications", "/System/Applications", "/System/Applications/Utilities", "~/Applications"]
    let fm = FileManager.default
    var out: [[String: Any]] = []
    for d in dirs {
        let dir = (d as NSString).expandingTildeInPath
        guard let items = try? fm.contentsOfDirectory(atPath: dir) else { continue }
        for item in items where item.hasSuffix(".app") {
            let path = dir + "/" + item
            var name = localizedAppName(path) ?? fm.displayName(atPath: path)
            if name.hasSuffix(".app") { name = String(name.dropLast(4)) }
            var d: [String: Any] = ["name": name, "file": String(item.dropLast(4)), "path": path]
            if let b = Bundle(path: path)?.bundleIdentifier { d["bundle_id"] = b }
            out.append(d)
        }
    }
    return out
}

// MARK: - windows on screen

/// Every window on screen, front to back, with its owner and layer (no titles: those need Screen Recording).
/// Lets the engine notice what covers an app — a permission prompt or an alert from another process.
func screenWindows(_ p: Params) throws -> Any {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return [Any]()
    }
    return list.compactMap { w -> [String: Any]? in
        guard let pid = w[kCGWindowOwnerPID as String] as? Int, let b = w[kCGWindowBounds as String] as? [String: Any] else { return nil }
        let frame = ["X", "Y", "Width", "Height"].map { safeInt((b[$0] as? Double) ?? Double((b[$0] as? Int) ?? 0)) }
        let app = NSRunningApplication(processIdentifier: pid_t(pid))
        return ["pid": pid, "id": w[kCGWindowNumber as String] as? Int ?? 0,
                "owner": w[kCGWindowOwnerName as String] as? String ?? "", "layer": w[kCGWindowLayer as String] as? Int ?? 0,
                "frame": frame, "regular": app?.activationPolicy == .regular,
                "alpha": w[kCGWindowAlpha as String] as? Double ?? 1]
    }
}
