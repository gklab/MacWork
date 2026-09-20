import AppKit
import ApplicationServices

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
     "screen_capture": CGPreflightScreenCaptureAccess(), "screen_locked": screenLocked(), "methods": dispatcher.methods]
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
dispatcher.register("screen.windows", screenWindows)
dispatcher.register("apps.installed", appsInstalled)
dispatcher.register("ax.snapshot", axSnapshot)
dispatcher.register("ax.perform", axPerform)
dispatcher.register("ax.set", axSet)
dispatcher.register("ax.get", axGet)
dispatcher.register("ax.focused", axFocused)
dispatcher.register("ax.wait", axWait)
dispatcher.register("ax.fingerprint", { p in
    guard let pid = p["pid"] as? Int else { throw RPCError("bad_params", "pid required") }
    return ["fingerprint": windowFingerprintPublic(pid: pid_t(pid), maxNodes: p["poll_nodes"] as? Int ?? 400)]
})
dispatcher.register("input.key", inputKey)
dispatcher.register("input.type", inputType)
dispatcher.register("input.click", inputClick)
dispatcher.register("input.idle", inputIdle)
dispatcher.register("script.applescript", scriptRun)
dispatcher.register("nl.entities", nlEntities)
dispatcher.register("screen.ocr", screenOCR)
dispatcher.register("screen.capture", screenCapture)
dispatcher.register("llm.available", llmAvailable)
dispatcher.register("llm.generate", llmGenerate)

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
    Connection(input: .standardInput, output: .standardOutput, dispatcher: dispatcher, onClose: { exit(0) }).start()
}
RunLoop.main.run()
