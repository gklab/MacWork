import AppKit
import CoreServices

/// Things the Mac itself announces.
///
/// The engine could be asked to do something and could be resumed, but nothing ever *woke* it: the daemon
/// sat there and a task only ever began because a person typed one. What is missing is not a scheduler —
/// macOS has one — but the other half: "when this happens, do that".
///
/// Every event here is one the system publishes about itself: an app started or came forward, the screen
/// locked, a folder changed, the clipboard was replaced. Nothing knows which apps or folders matter; the
/// subscription says which to watch and that comes from the person's own configuration.
///
/// Delivery is by polling rather than by pushing. The protocol is one reply per request, and a client that
/// asks every couple of seconds costs nothing measurable, while pushing would mean a second protocol, an
/// ordering problem and a way for a slow reader to wedge the helper. Events are kept in a ring buffer with
/// a sequence number, so a client that was away for a while sees what it missed — up to the buffer's size,
/// which it is told about rather than left to guess.
final class EventLog {
    static let shared = EventLog()

    private let lock = NSLock()
    private var events: [[String: Any]] = []
    private var nextSeq = 1
    private var capacity = 512
    private var dropped = 0

    /// Subscriptions, kept so that watching twice does not deliver twice.
    private var workspaceTokens: [NSObjectProtocol] = []
    private var distributedTokens: [NSObjectProtocol] = []
    private var streams: [String: FSEventStreamRef] = [:]
    private var clipboardTimer: Timer?
    private var lastChangeCount = NSPasteboard.general.changeCount

    func add(_ kind: String, _ detail: [String: Any]) {
        lock.lock()
        defer { lock.unlock() }
        var e: [String: Any] = ["seq": nextSeq, "kind": kind, "at": Date().timeIntervalSince1970]
        e.merge(detail) { a, _ in a }
        events.append(e)
        nextSeq += 1
        if events.count > capacity {
            dropped += events.count - capacity
            events.removeFirst(events.count - capacity)
        }
    }

    func poll(after: Int) -> [String: Any] {
        lock.lock()
        defer { lock.unlock() }
        let out = events.filter { ($0["seq"] as? Int ?? 0) > after }
        // `oldest` lets a client tell "nothing happened" from "you were away too long": if what it last saw
        // has fallen out of the buffer, it knows it has a gap rather than silently missing the reason it woke.
        return ["events": out, "seq": nextSeq - 1, "dropped": dropped,
                "oldest": events.first?["seq"] as? Int ?? nextSeq]
    }

    // MARK: - subscribing

    func watchApps() {
        guard workspaceTokens.isEmpty else { return }
        let nc = NSWorkspace.shared.notificationCenter
        let kinds: [(NSNotification.Name, String)] = [
            (NSWorkspace.didLaunchApplicationNotification, "app.launched"),
            (NSWorkspace.didTerminateApplicationNotification, "app.quit"),
            (NSWorkspace.didActivateApplicationNotification, "app.activated"),
            (NSWorkspace.didHideApplicationNotification, "app.hidden"),
            (NSWorkspace.didWakeNotification, "screen.woke"),
            (NSWorkspace.screensDidSleepNotification, "screen.slept"),
            (NSWorkspace.didMountNotification, "volume.mounted"),
            (NSWorkspace.didUnmountNotification, "volume.unmounted"),
        ]
        for (name, kind) in kinds {
            workspaceTokens.append(nc.addObserver(forName: name, object: nil, queue: .main) { note in
                var detail: [String: Any] = [:]
                if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication {
                    detail["app"] = app.localizedName ?? ""
                    detail["bundle_id"] = app.bundleIdentifier ?? ""
                    detail["pid"] = Int(app.processIdentifier)
                }
                if let url = note.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL { detail["path"] = url.path }
                EventLog.shared.add(kind, detail)
            })
        }
    }

    func watchScreenLock() {
        guard distributedTokens.isEmpty else { return }
        let dnc = DistributedNotificationCenter.default()
        for (name, kind) in [("com.apple.screenIsLocked", "screen.locked"),
                             ("com.apple.screenIsUnlocked", "screen.unlocked")] {
            distributedTokens.append(dnc.addObserver(forName: .init(name), object: nil, queue: .main) { _ in
                EventLog.shared.add(kind, [:])
            })
        }
    }

    func watchClipboard(every seconds: Double) {
        guard clipboardTimer == nil else { return }
        // The pasteboard announces nothing; its change count is the only signal there is, so this is the one
        // source here that has to be looked at rather than waited for.
        clipboardTimer = Timer.scheduledTimer(withTimeInterval: max(0.25, seconds), repeats: true) { _ in
            let now = NSPasteboard.general.changeCount
            guard now != self.lastChangeCount else { return }
            self.lastChangeCount = now
            // the shape of it, never the contents: a password manager puts real secrets here
            EventLog.shared.add("clipboard.changed",
                                ["change_count": now, "types": (NSPasteboard.general.types ?? []).map { $0.rawValue }])
        }
    }

    func watch(path: String) throws {
        let full = (path as NSString).expandingTildeInPath
        guard streams[full] == nil else { return }
        var context = FSEventStreamContext(version: 0, info: nil, retain: nil, release: nil, copyDescription: nil)
        // `kFSEventStreamCreateFlagUseCFTypes` is what makes `paths` a CFArray. Without it the callback is
        // handed a C `char **`, and reading that as an NSArray is undefined behaviour — it took the whole
        // helper down on the first file that changed. The client restarted it without a word, so the
        // symptom was "file events never arrive, and the events that had arrived disappeared".
        let callback: FSEventStreamCallback = { _, _, count, paths, _, _ in
            guard let list = unsafeBitCast(paths, to: NSArray.self) as? [String] else { return }
            for p in list.prefix(count) { EventLog.shared.add("file.changed", ["path": p]) }
        }
        guard let stream = FSEventStreamCreate(kCFAllocatorDefault, callback, &context, [full] as CFArray,
                                               FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1.0,
                                               FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents |
                                                                        kFSEventStreamCreateFlagUseCFTypes |
                                                                        kFSEventStreamCreateFlagNoDefer)) else {
            throw RPCError("watch_failed", "could not watch \(full)")
        }
        FSEventStreamSetDispatchQueue(stream, DispatchQueue.main)
        guard FSEventStreamStart(stream) else {
            FSEventStreamInvalidate(stream)
            throw RPCError("watch_failed", "could not start watching \(full)")
        }
        streams[full] = stream
    }

    func unwatchAll() {
        let nc = NSWorkspace.shared.notificationCenter
        workspaceTokens.forEach { nc.removeObserver($0) }
        distributedTokens.forEach { DistributedNotificationCenter.default().removeObserver($0) }
        workspaceTokens = []
        distributedTokens = []
        for (_, s) in streams { FSEventStreamStop(s); FSEventStreamInvalidate(s) }
        streams = [:]
        clipboardTimer?.invalidate()
        clipboardTimer = nil
    }

    var watching: [String: Any] {
        ["apps": !workspaceTokens.isEmpty, "screen_lock": !distributedTokens.isEmpty,
         "clipboard": clipboardTimer != nil, "paths": Array(streams.keys), "capacity": capacity]
    }
}

/// Everything this helper can report, asked of it rather than written down by the caller.
let eventKinds = ["app.launched", "app.quit", "app.activated", "app.hidden",
                  "screen.woke", "screen.slept", "screen.locked", "screen.unlocked",
                  "volume.mounted", "volume.unmounted", "file.changed", "clipboard.changed"]

func eventsWatch(_ p: Params) throws -> Any {
    if p["stop"] as? Bool == true {
        EventLog.shared.unwatchAll()
        return ["watching": EventLog.shared.watching, "kinds": eventKinds]
    }
    if p["apps"] as? Bool ?? true { EventLog.shared.watchApps() }
    if p["screen_lock"] as? Bool ?? true { EventLog.shared.watchScreenLock() }
    if let every = p["clipboard_every_s"] as? Double, every > 0 { EventLog.shared.watchClipboard(every: every) }
    for path in p["paths"] as? [String] ?? [] { try EventLog.shared.watch(path: path) }
    return ["watching": EventLog.shared.watching, "kinds": eventKinds]
}

func eventsPoll(_ p: Params) throws -> Any {
    EventLog.shared.poll(after: p["after"] as? Int ?? 0)
}
