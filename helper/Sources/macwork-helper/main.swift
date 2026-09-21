import AppKit
import ApplicationServices
import Carbon.HIToolbox

/// macwork-helper: the hands and eyes of macwork. It owns the macOS permissions (Accessibility, Screen
/// Recording, Automation) so they are granted once to one signed app, and exposes plain primitives over
/// newline-delimited JSON-RPC. It decides nothing.
///
///   macwork-helper --stdio              serve stdin/stdout (development: inherits the terminal's permissions)
///   macwork-helper --socket <path>      serve a 0600 Unix socket (installed .app, launched by the engine)

let version = "0.1.0"
let dispatcher = Dispatcher()

/// Whether the session is at the lock screen / login window (nothing can be operated then).
func screenLocked() -> Bool {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (d["CGSSessionScreenIsLocked"] as? Bool) == true || (d["kCGSSessionOnConsoleKey"] as? Bool) == false
}

dispatcher.register("ping") { _ in
    ["version": version, "pid": Int(getpid()), "ax_trusted": AXIsProcessTrusted(),
     "screen_capture": CGPreflightScreenCaptureAccess(), "screen_locked": screenLocked(),
     "secure_input": IsSecureEventInputEnabled(), "methods": dispatcher.methods]
}
dispatcher.register("session.state") { _ in
    ["screen_locked": screenLocked(), "frontmost": (NSWorkspace.shared.frontmostApplication?.bundleIdentifier as Any?) ?? NSNull()]
}
dispatcher.register("permissions.request") { p in
    switch p["kind"] as? String ?? "accessibility" {
    case "accessibility":
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        return ["granted": AXIsProcessTrustedWithOptions(opts)]
    case "screen_capture": return ["granted": CGRequestScreenCaptureAccess()]
    case let k: throw RPCError("bad_params", "unknown permission \(k)")
    }
}
dispatcher.register("apps.running", appsRunning)
dispatcher.register("apps.frontmost", appsFrontmost)
dispatcher.register("apps.activate", appsActivate)
dispatcher.register("apps.quit", appsQuit)
dispatcher.register("ax.set_range", axSetRange)
dispatcher.register("input.drag", inputDrag)
dispatcher.register("input.scroll", inputScroll)
dispatcher.register("screen.windows", screenWindows)
dispatcher.register("apps.installed", offMain: true, appsInstalled)
dispatcher.register("apps.openers", offMain: true, appsOpeners)
dispatcher.register("ax.snapshot", axSnapshot)
dispatcher.register("ax.perform", axPerform)
dispatcher.register("ax.set", axSet)
dispatcher.register("ax.get", axGet)
dispatcher.register("ax.focused", axFocused)
dispatcher.register("ax.extras_owners", axExtrasOwners)
dispatcher.register("ax.wait", axWait)
dispatcher.register("ax.fingerprint", { p in
    guard let pid = p["pid"] as? Int else { throw RPCError("bad_params", "pid required") }
    return ["fingerprint": windowFingerprintPublic(pid: pid_t(pid), maxNodes: p["poll_nodes"] as? Int ?? 400)]
})
dispatcher.register("input.key", inputKey)
dispatcher.register("input.type", inputType)
dispatcher.register("input.click", inputClick)
dispatcher.register("input.idle", inputIdle)
// off the main thread: a hold blocks for as long as it lasts, and `input.release_all` on a second
// connection has to be able to get in and end it
dispatcher.register("input.hold", offMain: true, inputHold)
dispatcher.register("input.release_all", offMain: true, inputReleaseAll)
dispatcher.register("services.perform", servicesPerform)
dispatcher.register("file.read_text", offMain: true, fileReadText)
dispatcher.register("file.trash", offMain: true, fileTrash)
dispatcher.register("clipboard.read", clipboardRead)
dispatcher.register("clipboard.write", clipboardWrite)
dispatcher.register("script.applescript", scriptRun)
dispatcher.register("nl.entities", offMain: true, nlEntities)
dispatcher.register("nl.detect", offMain: true, nlDetect)
dispatcher.register("screen.ocr", offMain: true, screenOCR)
dispatcher.register("system.locale") { _ in
    ["locale": Locale.current.identifier, "languages": Array(Locale.preferredLanguages.prefix(8)),
     "ocr_languages": ocrLanguages(),
     "region": Locale.current.region?.identifier as Any? ?? NSNull()]
}
dispatcher.register("screen.capture", offMain: true, screenCapture)
dispatcher.register("events.watch", eventsWatch)
dispatcher.register("events.poll", eventsPoll)
dispatcher.register("llm.available", llmAvailable)
dispatcher.register("llm.generate", offMain: true, llmGenerate)

// A client that goes away mid-reply must not take the helper with it: the default action for SIGPIPE is
// to terminate the process, and every reply is a write to a socket somebody else owns.
signal(SIGPIPE, SIG_IGN)
// Nothing is left held, whichever way this process ends (see Hold.swift).
atexit { holds.releaseAll() }
private let endings = [SIGTERM, SIGINT, SIGHUP].map { sig -> DispatchSourceSignal in
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
    src.setEventHandler { holds.releaseAll(); exit(0) }
    src.resume()
    return src
}
AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 0.5)  // one hung app must not hang us
NSApplication.shared.setActivationPolicy(.prohibited)

let args = CommandLine.arguments
if args.contains("--version") {
    print(version)
    exit(0)
}
if let i = args.firstIndex(of: "--socket"), i + 1 < args.count {
    let server = SocketServer(path: args[i + 1], dispatcher: dispatcher)
    do { try server.start() } catch { FileHandle.standardError.write(Data("\(error)\n".utf8)); exit(1) }
} else {
    Connection(input: .standardInput, output: .standardOutput, dispatcher: dispatcher, onClose: { holds.releaseAll(); exit(0) }).start()
}
RunLoop.main.run()
