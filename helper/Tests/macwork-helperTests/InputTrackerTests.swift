import XCTest

@testable import macwork_helper

/// Telling the user's input apart from the engine's own.
///
/// Events posted to the HID tap land in the same stream the system measures idle time from — measured on a
/// real Mac, one synthetic mouse move took the idle reading from 25.97 s to 0.15 s. So the raw figure says
/// "the user is busy" every time the engine presses a key. The clock and the raw reading are injected here,
/// so the arithmetic is checked without a real HID stream.
final class InputTrackerTests: XCTestCase {
    /// A fake HID stream: `uptime` moves by hand, `lastEvent` is when something last went into it.
    private final class Stream {
        var uptime: TimeInterval = 1_000
        var lastEvent: TimeInterval = 970          // the user last did something 30 s ago
        func idle() -> Double { uptime - lastEvent }
        func tick(_ seconds: TimeInterval) { uptime += seconds }
        func event() { lastEvent = uptime }        // anything at all, ours or theirs
    }

    private func tracker(_ s: Stream) -> InputTracker {
        InputTracker(now: { s.uptime }, hidIdle: { s.idle() })
    }

    func testOurOwnTypingDoesNotLookLikeTheUserComingBack() {
        let stream = Stream()
        let input = tracker(stream)
        XCTAssertEqual(input.snapshot().user, 30, accuracy: 0.001)

        input.before()                       // the engine is about to type
        stream.tick(0.1)
        stream.event()                       // its keystroke lands in the HID stream
        input.after()
        stream.tick(0.4)

        let seen = input.snapshot()
        XCTAssertEqual(seen.hid, 0.4, accuracy: 0.001, "the raw reading is reset by our own key")
        XCTAssertTrue(seen.ours, "and it must be recognised as ours")
        XCTAssertEqual(seen.user, 30.5, accuracy: 0.001, "while the user has still not touched anything")
    }

    func testTheUserTouchingSomethingAfterwardsIsSeenAtOnce() {
        let stream = Stream()
        let input = tracker(stream)

        input.before()
        stream.tick(0.1)
        stream.event()
        input.after()

        stream.tick(5)
        stream.event()                       // now the user really does press a key
        stream.tick(0.2)

        let seen = input.snapshot()
        XCTAssertFalse(seen.ours)
        XCTAssertEqual(seen.user, 0.2, accuracy: 0.001, "the engine must yield immediately")
    }

    func testIdleKeepsGrowingWhileTheEngineWorksOnItsOwn() {
        let stream = Stream()
        let input = tracker(stream)
        for _ in 0..<10 {                    // ten steps of typing, nobody else touching the Mac
            input.before()
            stream.tick(0.05)
            stream.event()
            input.after()
            stream.tick(0.95)
        }
        XCTAssertEqual(input.snapshot().user, 40, accuracy: 0.01,
                       "30 s before the run plus the 10 s it took: the user has been away the whole time")
    }
}
