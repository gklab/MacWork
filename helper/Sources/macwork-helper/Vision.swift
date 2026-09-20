import AppKit
import ScreenCaptureKit
import Vision

/// Vision fallback for UIs the Accessibility tree cannot describe: capture ONE window (ScreenCaptureKit) and
/// read it on-device (Vision OCR). Pixels never leave this process unless a caller asks for a PNG on disk.

/// Run async work from the (blocking) main thread by spinning the run loop until it finishes.
func runAsync<T>(timeout: Double, _ body: @escaping () async throws -> T) throws -> T {
    var result: Result<T, Error>?
    Task.detached {
        do { result = .success(try await body()) } catch { result = .failure(error) }
    }
    let deadline = Date().addingTimeInterval(timeout)
    while result == nil && Date() < deadline { CFRunLoopRunInMode(.defaultMode, 0.01, true) }
    guard let r = result else { throw RPCError("timeout", "capture timed out") }
    return try r.get()
}

private func frameParam(_ p: Params, _ key: String) -> CGRect? {
    guard let a = p[key] as? [Any], a.count == 4 else { return nil }
    let v = a.map { ($0 as? NSNumber)?.doubleValue ?? 0 }
    return CGRect(x: v[0], y: v[1], width: v[2], height: v[3])
}

/// The on-screen window of ``pid`` closest to ``near`` (the AX frame), or its largest one.
private func captureWindow(pid: pid_t, near: CGRect?) throws -> (CGImage, CGRect) {
    try runAsync(timeout: 8) {
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
        let wins = content.windows.filter { $0.owningApplication?.processID == pid && $0.windowLayer == 0 && $0.frame.width > 40 }
        func d(_ a: CGRect, _ b: CGRect) -> CGFloat { abs(a.minX - b.minX) + abs(a.minY - b.minY) + abs(a.width - b.width) + abs(a.height - b.height) }
        let pick = near.flatMap { f in wins.min(by: { d($0.frame, f) < d($1.frame, f) }) }
            ?? wins.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height })
        guard let win = pick else { throw RPCError("no_window", "no on-screen window for pid \(pid)") }
        // the screen the window's centre is on: "the first screen it touches" gets a window straddling a
        // Retina and a non-Retina display captured at the wrong scale, and every OCR box lands off by a factor
        let centre = CGPoint(x: win.frame.midX, y: win.frame.midY)
        func overlap(_ s: NSScreen) -> CGFloat {
            let r = s.frame.intersection(win.frame)
            return r.isNull ? 0 : r.width * r.height
        }
        let screen = NSScreen.screens.first(where: { $0.frame.contains(centre) })
            ?? NSScreen.screens.max(by: { overlap($0) < overlap($1) })
        let scale = screen?.backingScaleFactor ?? 2
        let cfg = SCStreamConfiguration()
        cfg.width = max(1, safeInt(win.frame.width * scale))
        cfg.height = max(1, safeInt(win.frame.height * scale))
        cfg.showsCursor = false
        let img = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(desktopIndependentWindow: win), configuration: cfg)
        return (img, win.frame)
    }
}

private func savePNG(_ img: CGImage, _ path: String) throws {
    let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
        throw RPCError("io", "cannot write \(path)")
    }
    CGImageDestinationAddImage(dest, img, nil)
    guard CGImageDestinationFinalize(dest) else { throw RPCError("io", "cannot write \(path)") }
}

/// screen.ocr {pid, near?: [x,y,w,h], languages?, fast?, min_conf?} -> {frame, boxes: [{text, conf, frame}], ms}
func screenOCR(_ p: Params) throws -> Any {
    guard let pidNum = p["pid"] as? Int else { throw RPCError("bad_params", "pid required") }
    let t0 = Date()
    let (img, frame) = try captureWindow(pid: pid_t(pidNum), near: frameParam(p, "near"))
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = (p["fast"] as? Bool ?? false) ? .fast : .accurate
    req.recognitionLanguages = p["languages"] as? [String] ?? ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: img).perform([req])
    let minConf = (p["min_conf"] as? NSNumber)?.floatValue ?? 0.3
    var boxes: [[String: Any]] = []
    for o in req.results ?? [] {
        guard let top = o.topCandidates(1).first, top.confidence >= minConf else { continue }
        let b = o.boundingBox   // normalized, origin bottom-left
        let x = frame.minX + b.minX * frame.width
        let y = frame.minY + (1 - b.maxY) * frame.height
        boxes.append(["text": top.string, "conf": Double(top.confidence),
                      "frame": [safeInt(x), safeInt(y), safeInt(b.width * frame.width), safeInt(b.height * frame.height)]])
    }
    return ["frame": [safeInt(frame.minX), safeInt(frame.minY), safeInt(frame.width), safeInt(frame.height)], "boxes": boxes,
            "ms": Int(Date().timeIntervalSince(t0) * 1000)]
}

/// screen.capture {pid, near?, crop?: [x,y,w,h] in screen points, path} -> {path, frame}. For local describers only.
func screenCapture(_ p: Params) throws -> Any {
    guard let pidNum = p["pid"] as? Int, let path = p["path"] as? String else { throw RPCError("bad_params", "pid and path required") }
    var (img, frame) = try captureWindow(pid: pid_t(pidNum), near: frameParam(p, "near"))
    if let crop = frameParam(p, "crop") {
        let sx = CGFloat(img.width) / frame.width, sy = CGFloat(img.height) / frame.height
        let r = CGRect(x: (crop.minX - frame.minX) * sx, y: (crop.minY - frame.minY) * sy, width: crop.width * sx, height: crop.height * sy)
        guard let c = img.cropping(to: r.integral) else { throw RPCError("bad_params", "crop outside the window") }
        img = c
    }
    try savePNG(img, path)
    return ["path": path, "frame": [safeInt(frame.minX), safeInt(frame.minY), safeInt(frame.width), safeInt(frame.height)]]
}
