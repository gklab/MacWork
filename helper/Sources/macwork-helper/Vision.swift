import AppKit
import ScreenCaptureKit
import Vision

/// Vision fallback for UIs the Accessibility tree cannot describe: capture ONE window (ScreenCaptureKit) and
/// read it on-device (Vision OCR). Pixels never leave this process unless a caller asks for a PNG on disk.

/// Run async work from the (blocking) main thread by spinning the run loop until it finishes.
/// Wait for an async call from a synchronous handler.
///
/// This used to spin: `while result == nil { CFRunLoopRunInMode(.defaultMode, 0.01, true) }`. On the main
/// thread that at least pumped a run loop. Since the slow captures became `offMain` the caller is a
/// connection's reader thread, which has no run loop — `CFRunLoopRunInMode` makes one, finds nothing to
/// do and returns at once, so it burned a core for the whole timeout. The write and the read of `result`
/// were also unsynchronised across two threads, which is a data race whatever it appears to do.
///
/// A semaphore is the whole of it: it blocks without spinning, it works on any thread, and it is the
/// handshake the two sides actually need.
func runAsync<T>(timeout: Double, _ body: @escaping () async throws -> T) throws -> T {
    let done = DispatchSemaphore(value: 0)
    let box = Box<Result<T, Error>>()
    Task.detached {
        do { box.set(.success(try await body())) } catch { box.set(.failure(error)) }
        done.signal()
    }
    guard done.wait(timeout: .now() + timeout) == .success, let r = box.get() else {
        throw RPCError("timeout", "capture timed out")
    }
    return try r.get()
}

/// One value, handed from the task that produced it to the thread waiting for it.
private final class Box<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: T?
    func set(_ v: T) { lock.lock(); value = v; lock.unlock() }
    func get() -> T? { lock.lock(); defer { lock.unlock() }; return value }
}

private func frameParam(_ p: Params, _ key: String) -> CGRect? {
    guard let a = p[key] as? [Any], a.count == 4 else { return nil }
    let v = a.map { ($0 as? NSNumber)?.doubleValue ?? 0 }
    return CGRect(x: v[0], y: v[1], width: v[2], height: v[3])
}

/// Which languages to read a screen in: the ones this Mac is set to, kept to those the recogniser supports.
/// A fixed pair (zh-Hans, en-US, as it was) reads a Korean, Thai or Greek interface as noise.
func ocrLanguages() -> [String] {
    let probe = VNRecognizeTextRequest()
    probe.recognitionLevel = .accurate
    let supported = Set((try? probe.supportedRecognitionLanguages()) ?? [])
    var out: [String] = []
    for want in Locale.preferredLanguages {
        // "zh-Hans-CN" -> "zh-Hans-CN", "zh-Hans", "zh": the recogniser lists "zh-Hans", the Mac says the
        // longer thing. Failing all of those, any variant of the same language will do ("en-CN" -> "en-US").
        var parts = want.split(separator: "-").map(String.init)
        var hit: String?
        while hit == nil && !parts.isEmpty {
            let tag = parts.joined(separator: "-")
            hit = supported.contains(tag) ? tag : nil
            if hit == nil { parts.removeLast() }
        }
        if hit == nil, let language = want.split(separator: "-").first {
            hit = supported.sorted().first { $0.hasPrefix(language + "-") }
        }
        if let hit, !out.contains(hit) { out.append(hit) }
    }
    if !out.contains("en-US"), supported.contains("en-US") { out.append("en-US") }   // the fallback of last resort
    return out.isEmpty ? ["en-US"] : out
}

/// Which of ``screens`` a window is on, for its backing scale: the one holding its centre, else the one it
/// overlaps most, else none.
///
/// ``frame`` is the window's frame as CoreGraphics and ScreenCaptureKit report it — origin at the top-left of
/// the primary display, y downwards — and ``screens`` are AppKit's, origin at the bottom-left of the primary
/// display, y upwards. The two were compared as if they were one space. On the primary display they
/// coincide by accident (both span the same y range), so it worked there; a window on a display above the
/// primary has a negative y in one and a y above the primary's height in the other, matched no screen, and
/// was captured at the primary's scale — every OCR box, and every click derived from one, off by that ratio.
func screenIndex(forWindow frame: CGRect, screens: [CGRect]) -> Int? {
    guard let primary = screens.first else { return nil }        // AppKit puts the primary display first, at (0, 0)
    let flipped = CGRect(x: frame.minX, y: primary.maxY - frame.maxY, width: frame.width, height: frame.height)
    let centre = CGPoint(x: flipped.midX, y: flipped.midY)
    if let i = screens.firstIndex(where: { $0.contains(centre) }) { return i }
    let overlaps = screens.map { s -> CGFloat in
        let r = s.intersection(flipped)
        return r.isNull ? 0 : r.width * r.height
    }
    guard let best = overlaps.indices.max(by: { overlaps[$0] < overlaps[$1] }), overlaps[best] > 0 else { return nil }
    return best
}

/// The on-screen window of ``pid`` closest to ``near`` (the AX frame), or its largest one.
private func captureWindow(pid: pid_t, near: CGRect?, maxWidth: CGFloat? = nil) throws -> (CGImage, CGRect) {
    try runAsync(timeout: 8) {
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
        let wins = content.windows.filter { $0.owningApplication?.processID == pid && $0.windowLayer == 0 && $0.frame.width > 40 }
        func d(_ a: CGRect, _ b: CGRect) -> CGFloat { abs(a.minX - b.minX) + abs(a.minY - b.minY) + abs(a.width - b.width) + abs(a.height - b.height) }
        let pick = near.flatMap { f in wins.min(by: { d($0.frame, f) < d($1.frame, f) }) }
            ?? wins.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height })
        guard let win = pick else { throw RPCError("no_window", "no on-screen window for pid \(pid)") }
        // the screen the window's centre is on: "the first screen it touches" gets a window straddling a
        // Retina and a non-Retina display captured at the wrong scale, and every OCR box lands off by a factor
        let screens = NSScreen.screens
        let screen = screenIndex(forWindow: win.frame, screens: screens.map { $0.frame }).map { screens[$0] }
        var scale = screen?.backingScaleFactor ?? 2
        if let w = maxWidth { scale = min(scale, w / win.frame.width) }   // a glance needs no detail: let the capture shrink it
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
    req.recognitionLanguages = (p["languages"] as? [String]).flatMap { $0.isEmpty ? nil : $0 } ?? ocrLanguages()
    // off: it "corrects" towards the chosen languages, and what is being read is interface labels, file
    // names and code identifiers — the things a language model is most confident about and most wrong about
    req.usesLanguageCorrection = p["correct"] as? Bool ?? false
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
    // Which way a line of this language runs. Reading right-to-left text left-to-right gives the words back
    // in the wrong order, which is not "slightly off" — it is a different sentence.
    let rtl = req.recognitionLanguages.contains { NSLocale.characterDirection(forLanguage: $0) == .rightToLeft }
    return ["frame": [safeInt(frame.minX), safeInt(frame.minY), safeInt(frame.width), safeInt(frame.height)], "boxes": boxes,
            "direction": rtl ? "rtl" : "ltr", "languages": req.recognitionLanguages,
            "ms": Int(Date().timeIntervalSince(t0) * 1000)]
}

/// screen.capture {pid, near?, crop?: [x,y,w,h] in screen points, path} -> {path, frame}. For local describers only.
/// What a window looks like, coarsely: an n×n grid of brightness, one byte a cell.
///
/// A person who presses a key looks at the screen to see whether anything happened. For a window that
/// describes itself to Accessibility the engine can do the same from the tree; for one that draws itself —
/// a game, a map, a canvas, a video — the tree says nothing and the picture is all there is. Two glances,
/// before and after, say how much of the window changed and roughly where. That is a measurement, not an
/// interpretation: nothing here knows what the picture is *of*.
///
/// The pointer is left out of the capture, so moving it does not count as the window changing.
func glanceGrid(_ img: CGImage, n: Int) -> [UInt8] {
    var px = [UInt8](repeating: 0, count: n * n)
    px.withUnsafeMutableBytes { buf in
        // drawing into an n×n grey context *is* the averaging: each cell is the mean brightness under it
        if let ctx = CGContext(data: buf.baseAddress, width: n, height: n, bitsPerComponent: 8, bytesPerRow: n,
                               space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue) {
            ctx.interpolationQuality = .medium
            ctx.draw(img, in: CGRect(x: 0, y: 0, width: n, height: n))
        }
    }
    return px
}

func screenGlance(_ p: Params) throws -> Any {
    guard let pidNum = p["pid"] as? Int else { throw RPCError("bad_params", "pid required") }
    let n = max(4, min(p["grid"] as? Int ?? 32, 64))
    let (img, frame) = try captureWindow(pid: pid_t(pidNum), near: frameParam(p, "near"), maxWidth: CGFloat(n * 8))
    return ["grid": n, "cells": Data(glanceGrid(img, n: n)).base64EncodedString(),
            "frame": [safeInt(frame.minX), safeInt(frame.minY), safeInt(frame.width), safeInt(frame.height)]]
}

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
