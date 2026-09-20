import XCTest

@testable import macwork_helper

/// Which thread a request runs on.
///
/// Everything ran on the main thread, because Accessibility and the run loop live there. A few methods need
/// neither — enumerating installed apps shells out to `mdfind`, reading a PDF is PDFKit — and they are slow
/// enough that holding the main thread for them was felt on the other side: the first look of a session
/// spent 1.8 s inside whichever provider happened to be running while a background app enumeration had the
/// thread. Those are registered `offMain`.
///
/// `handle` is called from a connection's reader thread and never from the main thread — it blocks on
/// `DispatchQueue.main.sync`, so calling it on main would deadlock. These tests call it the way a connection
/// does, and let the main thread service the hop by waiting on an expectation.
final class RPCTests: XCTestCase {
    private func reply(_ d: Dispatcher, _ method: String, _ params: [String: Any] = [:]) -> [String: Any] {
        let line = try! JSONSerialization.data(withJSONObject: ["id": 1, "method": method, "params": params])
        var out: [String: Any] = [:]
        let done = expectation(description: method)
        DispatchQueue.global().async {
            let data = d.handle(line: line)
            out = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            done.fulfill()
        }
        wait(for: [done], timeout: 5)
        return out
    }

    func testAnOrdinaryMethodStillRunsOnTheMainThread() {
        let d = Dispatcher()
        d.register("plain") { _ in ["main": Thread.isMainThread] }
        let result = reply(d, "plain")["result"] as? [String: Any]
        XCTAssertEqual(result?["main"] as? Bool, true)
    }

    func testAnOffMainMethodDoesNotTakeTheMainThread() {
        let d = Dispatcher()
        d.register("slow", offMain: true) { _ in ["main": Thread.isMainThread] }
        let result = reply(d, "slow")["result"] as? [String: Any]
        XCTAssertEqual(result?["main"] as? Bool, false)
    }

    func testAnOffMainMethodRunsWhileTheMainThreadIsBusy() {
        // the whole point: one connection's slow work must not stop another's
        let d = Dispatcher()
        d.register("slow", offMain: true) { _ in ["ok": true] }
        let blocked = expectation(description: "main is busy")
        let answered = expectation(description: "answered anyway")
        DispatchQueue.main.async {
            Thread.sleep(forTimeInterval: 0.4)      // the main thread is in a long Accessibility call
            blocked.fulfill()
        }
        DispatchQueue.global().async {
            _ = d.handle(line: try! JSONSerialization.data(withJSONObject: ["id": 2, "method": "slow"]))
            answered.fulfill()
        }
        wait(for: [answered], timeout: 0.35)        // before the main thread is free again
        wait(for: [blocked], timeout: 2)
    }

    func testAnOffMainFailureIsStillAnRPCError() {
        let d = Dispatcher()
        d.register("bad", offMain: true) { _ in throw RPCError("nope", "not today") }
        let error = reply(d, "bad")["error"] as? [String: Any]
        XCTAssertEqual(error?["code"] as? String, "nope")
    }

    func testAnUnknownMethodIsRefusedRatherThanIgnored() {
        XCTAssertEqual((reply(Dispatcher(), "nothing")["error"] as? [String: Any])?["code"] as? String, "no_method")
    }

    func testAResultThatJSONCannotHoldIsMadeIntoOne() {
        // handlers hand back whatever AppKit gave them; an unencodable value must not lose the whole reply
        let d = Dispatcher()
        d.register("odd") { _ in ["url": URL(fileURLWithPath: "/tmp/x"), "n": 1] }
        let result = reply(d, "odd")["result"] as? [String: Any]
        XCTAssertEqual(result?["n"] as? Int, 1)
        XCTAssertNotNil(result?["url"] as? String)
    }
}
