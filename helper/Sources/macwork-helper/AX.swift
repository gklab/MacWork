import AppKit
import ApplicationServices
import Carbon.HIToolbox

/// Accessibility: snapshot trees, perform actions, set values, wait for UI events.
/// Mechanism only — which nodes matter, what to click and when to stop is the engine's call.

private let batchAttrs: [String] = [
    kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute,
    kAXEnabledAttribute, kAXFocusedAttribute, kAXPositionAttribute, kAXSizeAttribute, kAXChildrenAttribute,
    "AXPlaceholderValue", kAXHelpAttribute, kAXSelectedAttribute, kAXIdentifierAttribute,
    "AXMenuItemCmdChar", "AXMenuItemCmdModifiers", kAXRoleDescriptionAttribute, "AXMenuItemMarkChar",
    kAXURLAttribute, kAXSelectedTextAttribute, kAXSelectedTextRangeAttribute,
    kAXDocumentAttribute,
].map { $0 as String }

final class AXStore {
    private var gen = 0
    private var live: [Int] = []
    private var elements: [String: AXUIElement] = [:]
    let keepGenerations: Int

    // Cached menu trees outlive several snapshots, and a look is many snapshots: the window, every window,
    // each prompt from another process, each process with a status item. At 64 a cached menu tree from the
    // previous look was already expired on this one, and real tasks lost steps to it. An element reference
    // is a small thing; 256 is a few looks' worth.
    init(keepGenerations: Int = 256) { self.keepGenerations = keepGenerations }

    func newGeneration() -> Int {
        gen += 1
        live.append(gen)
        while live.count > keepGenerations {
            let old = "g\(live.removeFirst())."
            elements = elements.filter { !$0.key.hasPrefix(old) }
        }
        return gen
    }

    func add(_ el: AXUIElement, gen: Int, n: Int) -> String {
        let ref = "g\(gen).\(n)"
        elements[ref] = el
        return ref
    }

    private var transient = 0
    let keepTransient: Int = 32

    /// A reference for one element outside any snapshot — the focused element, asked for on every look and
    /// polled while a field takes focus. Each of those opened a whole generation: sixteen a typing action,
    /// against the 64 that are kept, so the menu and window trees the engine still held were pushed out by
    /// its own polling. These live in a ring of their own and evict nothing but each other.
    func addTransient(_ el: AXUIElement) -> String {
        transient += 1
        let ref = "f\(transient)"
        elements[ref] = el
        elements["f\(transient - keepTransient)"] = nil
        return ref
    }

    /// "g3.17" from a snapshot, or "app:<pid>" for an application root.
    func get(_ ref: String) throws -> AXUIElement {
        if ref.hasPrefix("app:"), let pid = pid_t(ref.dropFirst(4)) { return AXUIElementCreateApplication(pid) }
        guard let el = elements[ref] else { throw RPCError("stale_ref", "unknown or expired element ref \(ref); take a new snapshot") }
        return el
    }
}

let store = AXStore()

// MARK: - attribute helpers

private func axString(_ v: CFTypeRef?, limit: Int) -> String? {
    guard let v else { return nil }
    if CFGetTypeID(v) == AXValueGetTypeID() { return nil }
    var s: String?
    if let str = v as? String { s = str }
    else if let num = v as? NSNumber { s = num.stringValue }
    else if let url = v as? URL { s = url.absoluteString }
    else if let att = v as? NSAttributedString { s = att.string }
    guard var out = s, !out.isEmpty else { return nil }
    if out.count > limit { out = String(out.prefix(limit)) + "…" }
    return out
}

private func axBool(_ v: CFTypeRef?) -> Bool? {
    guard let v, CFGetTypeID(v) == CFBooleanGetTypeID() else { return nil }
    return CFBooleanGetValue((v as! CFBoolean))
}

private func axPoint(_ v: CFTypeRef?) -> CGPoint? {
    guard let v, CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
    var p = CGPoint.zero
    return AXValueGetValue(v as! AXValue, .cgPoint, &p) ? p : nil
}

private func axSize(_ v: CFTypeRef?) -> CGSize? {
    guard let v, CFGetTypeID(v) == AXValueGetTypeID() else { return nil }
    var s = CGSize.zero
    return AXValueGetValue(v as! AXValue, .cgSize, &s) ? s : nil
}

func axAttr(_ el: AXUIElement, _ name: String) -> CFTypeRef? {
    var v: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(el, name as CFString, &v)
    if err == .cannotComplete { Unresponsive.shared.note(el) }   // the app did not answer in time
    return err == .success ? v : nil
}

/// Apps that do not answer Accessibility.
///
/// Every call to such an app waits out the messaging timeout — measured on one: 500 ms per scope, 1500 ms
/// for "the focused window" (three attributes tried), and a full look cost nearly five seconds to learn
/// nothing. `.cannotComplete` is the system saying exactly this, so it is believed for a short while rather
/// than rediscovered on every call. Short, because an app that was busy may answer a moment later.
final class Unresponsive {
    static let shared = Unresponsive()
    private let lock = NSLock()
    private var seen: [pid_t: TimeInterval] = [:]
    var cooldown: TimeInterval = 20

    private func now() -> TimeInterval { ProcessInfo.processInfo.systemUptime }

    func note(_ el: AXUIElement) {
        var pid: pid_t = 0
        guard AXUIElementGetPid(el, &pid) == .success else { return }
        lock.lock(); seen[pid] = now(); lock.unlock()
    }

    /// True when this app timed out recently: ask it nothing, and say why.
    func skip(_ pid: pid_t) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let at = seen[pid] else { return false }
        if now() - at < cooldown { return true }
        seen.removeValue(forKey: pid)
        return false
    }

    func clear(_ pid: pid_t) { lock.lock(); seen.removeValue(forKey: pid); lock.unlock() }
}

/// Which running processes own a menu bar extra, in one call.
///
/// Asking each process over its own round trip costs seconds: status items belong mostly to background
/// agents, and there are hundreds of those. Here it is one round trip and one cheap attribute read each —
/// the full tree is only fetched for the processes that turn out to have one.
func axExtrasOwners(_ p: Params) throws -> Any {
    var out: [[String: Any]] = []
    for app in NSWorkspace.shared.runningApplications {
        // a process with no connection to the window server cannot own a status item, and asking it anyway is
        // what makes this slow: most of what is running is exactly that
        if app.activationPolicy == .prohibited { continue }
        let el = AXUIElementCreateApplication(app.processIdentifier)
        guard let bar = axAttr(el, kAXExtrasMenuBarAttribute) else { continue }
        let items = (axAttr(bar as! AXUIElement, kAXChildrenAttribute) as? [AXUIElement]) ?? []
        if items.isEmpty { continue }
        out.append(["pid": Int(app.processIdentifier), "name": app.localizedName ?? "", "items": items.count])
    }
    return out
}

func axActions(_ el: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(el, &names) == .success, let arr = names as? [String] else { return [] }
    return arr
}

/// What the app calls an action, in the user's language, straight from the element.
///
/// Only asked for actions the caller says it cannot name itself: every app may define its own, and a table
/// of the ones we happened to think of would make the rest invisible. One extra round trip per element that
/// has such an action, so the caller sends down what it already knows.
func axActionDescription(_ el: AXUIElement, _ action: String) -> String? {
    var desc: CFString?
    guard AXUIElementCopyActionDescription(el, action as CFString, &desc) == .success else { return nil }
    let s = (desc as String?)?.trimmingCharacters(in: .whitespaces)
    return (s?.isEmpty ?? true) ? nil : s
}

/// One node as a plain dictionary (children not included).
private func describe(_ el: AXUIElement, textLimit: Int, withActions: Bool, settableRoles: Set<String> = [],
                      byCapability: Bool = false, knownActions: Set<String> = []) -> (node: [String: Any], children: [AXUIElement], frame: CGRect?) {
    var raw: CFArray?
    AXUIElementCopyMultipleAttributeValues(el, batchAttrs as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &raw)
    let vals = (raw as? [CFTypeRef]) ?? []
    func at(_ i: Int) -> CFTypeRef? {
        guard i < vals.count else { return nil }
        let v = vals[i]
        if CFGetTypeID(v) == AXValueGetTypeID() && AXValueGetType(v as! AXValue) == .axError { return nil }
        return v
    }
    var node: [String: Any] = [:]
    let keys = ["role", "subrole", "title", "desc", "value"]
    for (i, k) in keys.enumerated() { if let s = axString(at(i), limit: textLimit) { node[k] = s } }
    if let b = axBool(at(5)), !b { node["enabled"] = false }
    if let b = axBool(at(6)), b { node["focused"] = true }
    var frame: CGRect?
    if let p = axPoint(at(7)), let s = axSize(at(8)), [p.x, p.y, s.width, s.height].allSatisfy({ $0.isFinite }) {
        frame = CGRect(origin: p, size: s)   // some apps report NaN/inf geometry: such a frame is left out, never converted
        node["frame"] = [safeInt(p.x), safeInt(p.y), safeInt(s.width), safeInt(s.height)]
    }
    let children = (at(9) as? [AXUIElement]) ?? []
    if let s = axString(at(10), limit: textLimit) { node["placeholder"] = s }
    if let s = axString(at(11), limit: textLimit) { node["help"] = s }
    if let b = axBool(at(12)), b { node["selected"] = true }
    if let s = axString(at(13), limit: 80) { node["ident"] = s }
    if let s = axString(at(14), limit: 8) {
        var cmd: [String: Any] = ["char": s]
        if let m = at(15) as? NSNumber { cmd["mods"] = m.intValue }
        node["cmd"] = cmd
    }
    if let s = axString(at(16), limit: 60) { node["rdesc"] = s }
    if let s = axString(at(17), limit: 4) { node["mark"] = s }   // ✓ on the current mode / a toggled setting
    if let u = at(18) as? NSURL, let s = u.absoluteString { node["url"] = String(s.prefix(300)) }
    if let s = axString(at(19), limit: textLimit) { node["selected_text"] = s }
    // Which file a window is showing, as the app itself reports it (a file URL). It answers "is that
    // document already open here?" without comparing a window title to a file name — two files can share
    // a name, and a title can be anything.
    if let s = at(21) as? String, let u = URL(string: s), u.isFileURL { node["document"] = u.path }
    // What can be done with this element, asked of the element itself rather than assumed from its role.
    // A role list makes anything with an unusual role — a web input, a contenteditable group, a custom
    // control — simply not exist for the engine; these attributes are the element's own answer.
    // Settable, not merely present. Inside a web view nearly every element answers AXSelected and
    // AXSelectedTextRange (with nothing in them), so "the attribute exists" would call 435 of 441 elements
    // selectable. Whether the app will actually accept a value is the question that matters.
    func settable(_ attr: String) -> Bool {
        var ok: DarwinBoolean = false
        return AXUIElementIsAttributeSettable(el, attr as CFString, &ok) == .success && ok.boolValue
    }
    if byCapability {
        if at(20) != nil, settable(kAXSelectedTextRangeAttribute as String) { node["text_range"] = true }
        if axBool(at(12)) != nil, settable(kAXSelectedAttribute as String) { node["selectable"] = true }
        // A settable AXValue is not the same as "text can be typed here": a scroll bar's position is settable
        // too, and reporting it as a typing target offered "type into the scroll bar (now: 0)". The raw value
        // has to actually be text — `axString` turns numbers into strings, so `node["value"]` cannot say.
        let raw = at(4)
        let valueIsText = (raw as? String) != nil || (raw as? NSAttributedString) != nil
        if valueIsText || node["placeholder"] != nil || at(20) != nil {
            node["editable"] = settable(kAXValueAttribute as String)
        }
    } else if let role = node["role"] as? String, settableRoles.contains(role) {
        node["editable"] = settable(kAXValueAttribute as String)
    }
    if withActions {
        let acts = axActions(el)
        if !acts.isEmpty { node["actions"] = acts }
        // the app's own name for anything the caller could not name; localized, and free of our vocabulary
        let unknown = acts.filter { !knownActions.contains($0) }
        if !unknown.isEmpty {
            var described: [String: String] = [:]
            for a in unknown { if let d = axActionDescription(el, a) { described[a] = d } }
            if !described.isEmpty { node["action_desc"] = described }
        }
    }
    return (node, children, frame)
}

// MARK: - snapshot

func axSnapshot(_ p: Params) throws -> Any {
    let started = Date()
    let maxNodes = p["max_nodes"] as? Int ?? 3000
    let maxDepth = p["max_depth"] as? Int ?? 40
    let maxChildren = p["max_children"] as? Int ?? 300
    let textLimit = p["max_text"] as? Int ?? 200
    let budget = Double(p["budget_ms"] as? Int ?? 3000) / 1000
    let visibleOnly = p["visible_only"] as? Bool ?? true
    let withActions = p["actions"] as? Bool ?? true
    let scope = p["scope"] as? String ?? "focused_window"
    let skipRoles = Set(p["skip_roles"] as? [String] ?? [])   // e.g. AXApplication/AXMenuBar inside a window scope
    let keepRoles = Set(p["keep_offscreen_roles"] as? [String] ?? [])   // e.g. rows: a list's content even when scrolled away
    // Reading a list's scrolled-away rows costs an Accessibility round trip each. Measured on a chat app:
    // 520 nodes in 211 ms with them clipped away, 1441 nodes in 3031 ms with them kept — three seconds on
    // every single look. Kept, but not without end: past this many, the rest of that list stays off-screen.
    let maxOffscreen = p["max_offscreen"] as? Int ?? 200
    var offscreen = 0
    let settableRoles = Set(p["settable_roles"] as? [String] ?? [])
    let byCapability = p["by_capability"] as? Bool ?? false
    let knownActions = Set(p["known_actions"] as? [String] ?? [])

    var roots: [AXUIElement] = []
    var pid: pid_t = 0
    if let ref = p["ref"] as? String {
        roots = [try store.get(ref)]
        AXUIElementGetPid(roots[0], &pid)
    } else {
        guard let pidNum = p["pid"] as? Int else { throw RPCError("bad_params", "pid or ref required") }
        pid = pid_t(pidNum)
        if Unresponsive.shared.skip(pid) {
            return ["nodes": [Any](), "ms": 0, "truncated": false, "not_answering": true]
        }
        let app = AXUIElementCreateApplication(pid)
        for (flag, attr) in [("manual_accessibility", "AXManualAccessibility"), ("enhanced_ui", "AXEnhancedUserInterface")] {
            if p[flag] as? Bool == true { AXUIElementSetAttributeValue(app, attr as CFString, kCFBooleanTrue) }
        }
        switch scope {
        case "app": roots = [app]
        case "menubar": if let m = axAttr(app, kAXMenuBarAttribute) { roots = [m as! AXUIElement] }
        // the right-hand end of the menu bar: Wi-Fi, Bluetooth, volume, the input method, and every third-party
        // status item. An ordinary Accessibility attribute, and a whole surface the engine could not see at all.
        case "extras_menubar": if let m = axAttr(app, kAXExtrasMenuBarAttribute) { roots = [m as! AXUIElement] }
        case "windows":
            roots = ((axAttr(app, kAXWindowsAttribute) as? [AXUIElement]) ?? []).filter {
                (axAttr($0, kAXRoleAttribute) as? String) == (kAXWindowRole as String)
            }
        case "open_menus":   // context / pop-up menus currently open (children of the app, not of any window)
            roots = ((axAttr(app, kAXChildrenAttribute) as? [AXUIElement]) ?? []).filter {
                (axAttr($0, kAXRoleAttribute) as? String) == (kAXMenuRole as String)
            }
        case "focused_window":
            // Some apps answer "focused window" with the desktop or the application itself: insist on a real window.
            func isWindow(_ el: AXUIElement) -> Bool { (axAttr(el, kAXRoleAttribute) as? String) == (kAXWindowRole as String) }
            for attr in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
                if let w = axAttr(app, attr), isWindow(w as! AXUIElement) { roots = [w as! AXUIElement]; break }
            }
            if roots.isEmpty, let ws = axAttr(app, kAXWindowsAttribute) as? [AXUIElement], let w = ws.first(where: isWindow) { roots = [w] }
        default: throw RPCError("bad_params", "unknown scope \(scope)")
        }
    }

    let gen = store.newGeneration()
    var nodes: [[String: Any]] = []
    var truncated = false
    var visited: [CFHashCode: [AXUIElement]] = [:]   // AX trees can contain cycles (an app listing itself as a child)
    func firstVisit(_ el: AXUIElement) -> Bool {
        let h = CFHash(el)
        if let seen = visited[h], seen.contains(where: { CFEqual($0, el) }) { return false }
        visited[h, default: []].append(el)
        return true
    }
    // DFS in document order; clip to the root's frame so scrolled-away content does not flood the list.
    var stack: [(el: AXUIElement, parent: String?, depth: Int, clip: CGRect?, free: Bool)] = roots.reversed().map { ($0, nil, 0, nil, false) }
    while let item = stack.popLast() {
        if nodes.count >= maxNodes || Date().timeIntervalSince(started) > budget { truncated = true; break }
        guard firstVisit(item.el) else { continue }
        let d = describe(item.el, textLimit: textLimit, withActions: withActions, settableRoles: settableRoles,
                         byCapability: byCapability, knownActions: knownActions)
        if item.depth > 0, let role = d.node["role"] as? String, skipRoles.contains(role) { continue }
        // Off-screen by geometry, whatever anyone's opinion of it — a kept row's children inherit "keep",
        // and they are most of the cost: 191 rows carried 1000 more nodes. Everything kept is counted.
        let offScreen = visibleOnly && item.clip != nil && d.frame != nil
            && (d.frame!.width > 0 || d.frame!.height > 0) && !item.clip!.intersects(d.frame!)
        var free = item.free || keepRoles.contains(d.node["role"] as? String ?? "")
        if offScreen, free {
            offscreen += 1
            if offscreen > maxOffscreen { truncated = true; free = false }
        }
        if offScreen, !free { continue }
        var node = d.node
        let ref = store.add(item.el, gen: gen, n: nodes.count)
        node["ref"] = ref
        node["depth"] = item.depth
        if let parent = item.parent { node["parent"] = parent }
        nodes.append(node)
        guard item.depth < maxDepth else { continue }
        var clip = item.clip
        if clip == nil, let f = d.frame, f.width > 0, f.height > 0, (node["role"] as? String) == (kAXWindowRole as String) { clip = f }
        let kids = d.children.prefix(maxChildren)
        if d.children.count > maxChildren { node["more_children"] = d.children.count - maxChildren; nodes[nodes.count - 1] = node }
        for child in kids.reversed() { stack.append((child, ref, item.depth + 1, clip, free)) }
    }
    return ["gen": gen, "pid": Int(pid), "nodes": nodes, "truncated": truncated,
            "ms": Int(Date().timeIntervalSince(started) * 1000)]
}

// MARK: - act

func axPerform(_ p: Params) throws -> Any {
    guard let ref = p["ref"] as? String else { throw RPCError("bad_params", "ref required") }
    let action = p["action"] as? String ?? (kAXPressAction as String)
    let el = try store.get(ref)
    let err = AXUIElementPerformAction(el, action as CFString)
    guard err == .success else { throw RPCError("ax_error", "\(action) failed on \(ref): AXError \(err.rawValue)") }
    return ["ok": true]
}

func axSet(_ p: Params) throws -> Any {
    guard let ref = p["ref"] as? String else { throw RPCError("bad_params", "ref required") }
    let attr = p["attribute"] as? String ?? (kAXValueAttribute as String)
    let el = try store.get(ref)
    let value: CFTypeRef
    switch p["value"] {
    case let b as Bool: value = (b ? kCFBooleanTrue : kCFBooleanFalse)!
    case let n as NSNumber: value = n
    case let s as String: value = s as CFString
    default: throw RPCError("bad_params", "value must be string, number or bool")
    }
    let err = AXUIElementSetAttributeValue(el, attr as CFString, value)
    guard err == .success else { throw RPCError("ax_error", "set \(attr) failed on \(ref): AXError \(err.rawValue)") }
    return ["ok": true]
}

/// Select a range of text in an editable element (UTF-16 offsets, as AppKit counts them); length 0 = put the cursor.
func axSetRange(_ p: Params) throws -> Any {
    guard let ref = p["ref"] as? String, let loc = p["location"] as? Int else { throw RPCError("bad_params", "ref, location required") }
    let el = try store.get(ref)
    var range = CFRange(location: loc, length: p["length"] as? Int ?? 0)
    guard let value = AXValueCreate(.cfRange, &range) else { throw RPCError("ax_error", "cannot make a range") }
    let err = AXUIElementSetAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, value)
    guard err == .success else { throw RPCError("ax_error", "set selection failed on \(ref): AXError \(err.rawValue)") }
    return ["ok": true]
}

/// The element that has keyboard focus, system-wide.
/// The element the keyboard is actually going to, asked of the system rather than guessed from what is in
/// front. Typing is the one action whose target is invisible in any one app's tree: it lands wherever this
/// says, which may be a sheet, another app, or nothing at all.
///
/// `secure_input` travels with the answer because the caller needs it to decide whether reading the value
/// back is allowed at all, and asking for it separately is a second round trip on a state that changes with
/// focus — which is exactly what this call is reporting.
func axFocused(_ p: Params) throws -> Any {
    let sys = AXUIElementCreateSystemWide()
    let secure = IsSecureEventInputEnabled()
    // No per-call timeout here. Setting one on the system-wide element sets the *global default* for every
    // AXUIElement afterwards, so a cap meant for this one call would quietly shorten every later snapshot
    // and never be put back. main.swift sets that global once, at 0.5 s.
    guard let v = axAttr(sys, kAXFocusedUIElementAttribute) else {
        return ["focused": NSNull(), "secure_input": secure]
    }
    let el = v as! AXUIElement
    var pid: pid_t = 0
    AXUIElementGetPid(el, &pid)
    // `value` is what the element *holds* — a password, a whole document. A caller that only needs to know
    // where the cursor is says so, rather than asking for one character of everything and getting a role
    // truncated to "A…".
    var node = describe(el, textLimit: p["max_text"] as? Int ?? 200,
                        withActions: p["actions"] as? Bool ?? false).node
    if !(p["value"] as? Bool ?? true) {
        node["value"] = nil
        node["selected_text"] = nil
    }
    node["ref"] = store.addTransient(el)
    var out: [String: Any] = ["focused": node, "pid": Int(pid), "secure_input": secure]
    // which app the cursor is in: the caller has a pid and would otherwise list every running app to name it
    if let app = NSRunningApplication(processIdentifier: pid) {
        out["app"] = app.localizedName ?? app.bundleIdentifier ?? ""
        out["bundle_id"] = app.bundleIdentifier ?? ""
    }
    return out
}

// MARK: - wait for UI events

private final class WaitBox {
    var fired: [(String, Double)] = []
    let t0 = Date()
}

private let observerCallback: AXObserverCallback = { _, _, name, refcon in
    guard let refcon else { return }
    let box = Unmanaged<WaitBox>.fromOpaque(refcon).takeUnretainedValue()
    box.fired.append((name as String, Date().timeIntervalSince(box.t0)))
}

/// A cheap digest of what the focused window shows (roles, titles, values), for apps that change their UI
/// without posting Accessibility notifications (many SwiftUI apps).
func windowFingerprintPublic(pid: pid_t, maxNodes: Int) -> Int { windowFingerprint(pid: pid, maxNodes: maxNodes) }

private func windowFingerprint(pid: pid_t, maxNodes: Int) -> Int {
    let app = AXUIElementCreateApplication(pid)
    var hasher = Hasher()
    var root: AXUIElement?
    for attr in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
        if let w = axAttr(app, attr) { root = (w as! AXUIElement); break }
    }
    if let front = axAttr(app, kAXFocusedUIElementAttribute) {   // focus moving counts as a change too
        hasher.combine(CFHash(front as! AXUIElement))
    }
    guard let root else { return hasher.finalize() }
    var stack = [root]
    var n = 0
    let attrs = [kAXRoleAttribute, kAXTitleAttribute, kAXValueAttribute, kAXChildrenAttribute] as CFArray
    while let el = stack.popLast(), n < maxNodes {
        n += 1
        var raw: CFArray?
        AXUIElementCopyMultipleAttributeValues(el, attrs, AXCopyMultipleAttributeOptions(rawValue: 0), &raw)
        let vals = (raw as? [CFTypeRef]) ?? []
        for v in vals.prefix(3) {
            if let str = v as? String { hasher.combine(str) } else if let num = v as? NSNumber { hasher.combine(num) }
        }
        if vals.count > 3, let kids = vals[3] as? [AXUIElement] { stack.append(contentsOf: kids.reversed()) }
    }
    return hasher.finalize()
}

/// Block until one of ``notifications`` fires in app ``pid`` (or an app activates/launches) — or, with
/// ``poll_ms``, until the focused window's content changes — then keep collecting for ``settle_ms`` so the
/// caller sees the UI after it settled. Returns what fired and when.
func axWait(_ p: Params) throws -> Any {
    guard let pidNum = p["pid"] as? Int else { throw RPCError("bad_params", "pid required") }
    let timeout = Double(p["timeout_ms"] as? Int ?? 3000) / 1000
    let settle = Double(p["settle_ms"] as? Int ?? 150) / 1000
    let pollEvery = Double(p["poll_ms"] as? Int ?? 0) / 1000
    let pollNodes = p["poll_nodes"] as? Int ?? 400
    let baseline = p["baseline"] as? Int
    let quiet = Double(p["quiet_ms"] as? Int ?? 0) / 1000   // after a change: wait until nothing changes for this long
    let names = p["notifications"] as? [String] ?? [
        kAXFocusedWindowChangedNotification, kAXFocusedUIElementChangedNotification, kAXWindowCreatedNotification,
        kAXValueChangedNotification, kAXTitleChangedNotification, kAXUIElementDestroyedNotification,
        kAXMenuOpenedNotification, kAXSelectedChildrenChangedNotification, kAXLayoutChangedNotification,
    ].map { $0 as String }
    let pid = pid_t(pidNum)
    let box = WaitBox()
    var observer: AXObserver?
    guard AXObserverCreate(pid, observerCallback, &observer) == .success, let observer else {
        throw RPCError("ax_error", "cannot observe pid \(pid)")
    }
    let app = AXUIElementCreateApplication(pid)
    let refcon = Unmanaged.passUnretained(box).toOpaque()
    for n in names { AXObserverAddNotification(observer, app, n as CFString, refcon) }
    CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
    let center = NSWorkspace.shared.notificationCenter
    let tokens = [NSWorkspace.didActivateApplicationNotification, NSWorkspace.didLaunchApplicationNotification].map { name in
        center.addObserver(forName: name, object: nil, queue: nil) { note in
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            box.fired.append(("\(name.rawValue):\(app?.processIdentifier ?? 0)", Date().timeIntervalSince(box.t0)))
        }
    }
    defer {
        for n in names { AXObserverRemoveNotification(observer, app, n as CFString) }
        CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
        tokens.forEach(center.removeObserver)
    }
    var firstAt: Date?
    // the caller may pass the fingerprint taken just before acting; otherwise take it now
    let before = pollEvery > 0 ? (baseline ?? windowFingerprint(pid: pid, maxNodes: pollNodes)) : 0
    var lastPoll = Date()
    var lastPrint = before
    var stableSince: Date?
    var settledQuiet = false
    while true {
        let now = Date()
        if pollEvery > 0, now.timeIntervalSince(lastPoll) >= pollEvery {
            lastPoll = now
            let fp = windowFingerprint(pid: pid, maxNodes: pollNodes)
            if firstAt == nil, fp != before { box.fired.append(("content_changed", now.timeIntervalSince(box.t0))) }
            if fp != lastPrint { lastPrint = fp; stableSince = now } else if stableSince == nil { stableSince = now }
        }
        if firstAt == nil, !box.fired.isEmpty { firstAt = now; stableSince = now }
        if let f = firstAt {
            if quiet > 0, pollEvery > 0 {
                // settled = the window has not changed for `quiet` (a page loading, a sheet sliding in keep it busy)
                if let s0 = stableSince, now.timeIntervalSince(s0) >= quiet, now.timeIntervalSince(f) >= settle { settledQuiet = true; break }
            } else if now.timeIntervalSince(f) >= settle { break }
        }
        if now.timeIntervalSince(box.t0) >= timeout { break }
        CFRunLoopRunInMode(.defaultMode, 0.02, true)
    }
    var seen = Set<String>()
    let events = box.fired.filter { seen.insert($0.0).inserted }.map { ["name": $0.0, "ms": safeInt($0.1 * 1000)] }
    return ["events": events, "timed_out": box.fired.isEmpty, "settled": settledQuiet, "ms": Int(Date().timeIntervalSince(box.t0) * 1000)]
}

/// Read one attribute (to verify an action, e.g. that a text field now holds what was typed).
func axGet(_ p: Params) throws -> Any {
    guard let ref = p["ref"] as? String else { throw RPCError("bad_params", "ref required") }
    let attr = p["attribute"] as? String ?? (kAXValueAttribute as String)
    let el = try store.get(ref)
    guard let v = axAttr(el, attr) else { return ["value": NSNull()] }
    if CFGetTypeID(v) == CFBooleanGetTypeID() { return ["value": CFBooleanGetValue((v as! CFBoolean))] }
    if let n = v as? NSNumber { return ["value": n] }
    if let s = v as? String { return ["value": s] }
    return ["value": "\(v)"]
}


/// Double → Int that cannot trap: non-finite values become 0, huge ones are clamped.
func safeInt(_ d: Double) -> Int { d.isFinite ? Int(max(min(d, 1e12), -1e12)) : 0 }
func safeInt(_ d: CGFloat) -> Int { safeInt(Double(d)) }
