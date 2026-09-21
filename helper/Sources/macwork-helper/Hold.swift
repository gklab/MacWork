import AppKit
import Carbon.HIToolbox

/// Input that lasts: a key or a mouse button held for a time, the pointer moved *by* an amount.
///
/// `input.key` is a tap and `input.click` is a click at a place, which is all a document needs. Anything
/// that is steered rather than operated — a game, a map, a 3D view, a canvas that pans while a key is down —
/// reads how long a key stays down and how far the mouse moved, not where it is.
///
/// One rule shapes everything here: **nothing is ever left held.** A key that stays down after the engine
/// has crashed, timed out or been interrupted keeps acting on the person's Mac with nobody deciding
/// anything. So there is no "key down" call and no "key up" call to forget: the only primitive is a hold of
/// stated length, carried out and released inside one request. It has a hard ceiling whatever is asked for,
/// it is released on every way out of the function, and again when the connection or the process ends.
final class Holds {
    /// The ways out to the world, injected so the ordering and the guarantees can be tested without
    /// pressing anything on a real keyboard.
    struct World {
        var key: (_ code: CGKeyCode, _ down: Bool, _ flags: CGEventFlags) -> Void
        var button: (_ name: String, _ down: Bool) -> Void
        var move: (_ dx: Int, _ dy: Int, _ dragging: String?) -> Void
        var sleep: (_ seconds: Double) -> Void
        var now: () -> TimeInterval
        /// Seconds since the person last did something *of a kind this hold is not itself producing*.
        var userIdle: (_ watchKeys: Bool, _ watchPointer: Bool) -> Double
    }

    struct Request {
        var keys: [(code: CGKeyCode, flag: CGEventFlags?)] = []
        var buttons: [String] = []
        var dx = 0, dy = 0
        var ms = 0
        var yieldToUser = true
    }

    static let hardCeilingMs = 10_000      // whatever the caller asks for. A budget, not a judgement
    static let tick = 0.008                // pointer movement is spread over the hold at about 120 Hz

    private let world: World
    private let lock = NSLock()
    private var downKeys: [(code: CGKeyCode, flag: CGEventFlags?)] = []
    private var downButtons: [String] = []
    private var abort = false

    init(world: World) { self.world = world }

    /// Hold, wait, release. Returns how long it was really held and why it ended.
    func hold(_ r: Request) -> [String: Any] {
        let wanted = Double(max(0, min(r.ms, Holds.hardCeilingMs))) / 1000
        lock.lock(); abort = false; lock.unlock()
        var flags = CGEventFlags()
        let started = world.now()
        defer { releaseAll() }                        // every way out of here, including the ones not thought of
        for k in r.keys {                             // modifiers first, so the keys after them carry the flag
            if let f = k.flag { flags.insert(f) }
            world.key(k.code, true, flags)
            lock.lock(); downKeys.append(k); lock.unlock()
        }
        for b in r.buttons {
            world.button(b, true)
            lock.lock(); downButtons.append(b); lock.unlock()
        }
        let dragging = r.buttons.first
        var ended = "completed"
        var movedX = 0, movedY = 0
        while true {
            let elapsed = world.now() - started
            let part = wanted > 0 ? min(1, elapsed / wanted) : 1
            let toX = Int((Double(r.dx) * part).rounded()), toY = Int((Double(r.dy) * part).rounded())
            if toX != movedX || toY != movedY {
                world.move(toX - movedX, toY - movedY, dragging)
                movedX = toX; movedY = toY
            }
            if wanted - elapsed < 0.0005 { break }        // not `>=`: a remainder too small to move a clock never ends
            lock.lock(); let stop = abort; lock.unlock()
            if stop { ended = "released"; break }
            // The person reaching for the Mac ends it at once. Only kinds of input this hold is not itself
            // producing are watched: a held key makes the system send repeats, and those are not the person.
            if r.yieldToUser && world.userIdle(r.keys.isEmpty, r.buttons.isEmpty && r.dx == 0 && r.dy == 0) < elapsed {
                ended = "user"; break
            }
            world.sleep(min(Holds.tick, wanted - elapsed))
        }
        return ["ok": true, "held_ms": Int(((world.now() - started) * 1000).rounded()), "ended": ended,
                "moved": [movedX, movedY], "asked_ms": r.ms, "ceiling_ms": Holds.hardCeilingMs]
    }

    /// Let go of everything, in the reverse of the order it went down. Safe to call at any time, from any
    /// thread, any number of times; a hold in progress ends at its next tick.
    func releaseAll() {
        lock.lock()
        abort = true
        let keys = downKeys, buttons = downButtons
        downKeys = []; downButtons = []
        lock.unlock()
        for b in buttons.reversed() { world.button(b, false) }
        var flags = CGEventFlags()
        for k in keys { if let f = k.flag { flags.insert(f) } }
        for k in keys.reversed() {
            if let f = k.flag { flags.remove(f) }
            world.key(k.code, false, flags)
        }
    }

    var anythingDown: Bool { lock.lock(); defer { lock.unlock() }; return !downKeys.isEmpty || !downButtons.isEmpty }
}

// MARK: - the real world

private let modifierKeys: [String: (CGKeyCode, CGEventFlags)] = [
    "shift": (CGKeyCode(kVK_Shift), .maskShift), "ctrl": (CGKeyCode(kVK_Control), .maskControl),
    "control": (CGKeyCode(kVK_Control), .maskControl), "alt": (CGKeyCode(kVK_Option), .maskAlternate),
    "option": (CGKeyCode(kVK_Option), .maskAlternate), "opt": (CGKeyCode(kVK_Option), .maskAlternate),
    "cmd": (CGKeyCode(kVK_Command), .maskCommand), "command": (CGKeyCode(kVK_Command), .maskCommand),
]

private let mouseButtons: [String: (CGMouseButton, CGEventType, CGEventType, CGEventType)] = [
    "left": (.left, .leftMouseDown, .leftMouseUp, .leftMouseDragged),
    "right": (.right, .rightMouseDown, .rightMouseUp, .rightMouseDragged),
    "middle": (.center, .otherMouseDown, .otherMouseUp, .otherMouseDragged),
]

private func pointerNow() -> CGPoint { CGEvent(source: nil)?.location ?? .zero }

let holds = Holds(world: Holds.World(
    key: { code, down, flags in
        input.before(); defer { input.after() }
        let e = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: code, keyDown: down)
        e?.flags = flags
        e?.post(tap: .cghidEventTap)
    },
    button: { name, down in
        guard let b = mouseButtons[name] else { return }
        input.before(); defer { input.after() }
        CGEvent(mouseEventSource: nil, mouseType: down ? b.1 : b.2, mouseCursorPosition: pointerNow(),
                mouseButton: b.0)?.post(tap: .cghidEventTap)
    },
    move: { dx, dy, dragging in
        input.before(); defer { input.after() }
        // Both are set: a view that follows the pointer reads the position, a view that has captured it
        // (a first-person camera) reads only the deltas.
        let at = pointerNow()
        let b = dragging.flatMap { mouseButtons[$0] }
        let e = CGEvent(mouseEventSource: nil, mouseType: b?.3 ?? .mouseMoved,
                        mouseCursorPosition: CGPoint(x: at.x + CGFloat(dx), y: at.y + CGFloat(dy)),
                        mouseButton: b?.0 ?? .left)
        e?.setIntegerValueField(.mouseEventDeltaX, value: Int64(dx))
        e?.setIntegerValueField(.mouseEventDeltaY, value: Int64(dy))
        e?.post(tap: .cghidEventTap)
    },
    sleep: { Thread.sleep(forTimeInterval: $0) },
    now: { ProcessInfo.processInfo.systemUptime },
    userIdle: { watchKeys, watchPointer in
        var kinds: [CGEventType] = []
        if watchKeys { kinds += [.keyDown] }
        if watchPointer { kinds += [.mouseMoved, .leftMouseDown, .rightMouseDown, .scrollWheel] }
        return kinds.map { CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: $0) }.min()
            ?? Double.greatestFiniteMagnitude
    }
))

/// `input.hold` — {keys: ["shift", "w"], buttons: ["left"], move: {dx, dy}, ms, yield_to_user}
func inputHold(_ p: Params) throws -> Any {
    var r = Holds.Request()
    for name in (p["keys"] as? [String] ?? []).map({ $0.lowercased() }) {
        if let m = modifierKeys[name] { r.keys.insert((m.0, m.1), at: r.keys.filter { $0.flag != nil }.count); continue }
        guard let k = keyCode(for: name) else { throw RPCError("bad_params", "unknown key \(name)") }
        r.keys.append((k.code, nil))
    }
    for name in (p["buttons"] as? [String] ?? []).map({ $0.lowercased() }) {
        guard mouseButtons[name] != nil else { throw RPCError("bad_params", "unknown mouse button \(name)") }
        r.buttons.append(name)
    }
    if let m = p["move"] as? [String: Any] {
        func num(_ k: String) -> Int { (m[k] as? Int) ?? Int((m[k] as? Double) ?? 0) }
        r.dx = num("dx"); r.dy = num("dy")
    }
    guard !r.keys.isEmpty || !r.buttons.isEmpty || r.dx != 0 || r.dy != 0 else {
        throw RPCError("bad_params", "keys, buttons or move required")
    }
    guard let ms = (p["ms"] as? Int) ?? (p["ms"] as? Double).map({ Int($0) }), ms >= 0 else {
        throw RPCError("bad_params", "ms required: a hold has a stated length")
    }
    r.ms = ms
    r.yieldToUser = p["yield_to_user"] as? Bool ?? true
    if !r.keys.isEmpty { try requireKeyboard() }
    return holds.hold(r)
}

func inputReleaseAll(_ p: Params) throws -> Any {
    let was = holds.anythingDown
    holds.releaseAll()
    return ["ok": true, "was_holding": was]
}
