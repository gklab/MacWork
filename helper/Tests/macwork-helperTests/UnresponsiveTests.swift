import ApplicationServices
import XCTest

@testable import macwork_helper

/// An app that did not answer Accessibility is believed silent only until it is asked again.
///
/// One timeout used to blank every snapshot of the app for 20 s, restarted by every later timeout and ended by
/// nothing else, so an app that answered a moment after launching read as having no window for 20 s. The clock,
/// the question asked again, whether the app is launching and whether it is still running are all injected
/// here: nothing is asked of a real app.
final class UnresponsiveTests: XCTestCase {
    /// A Mac made by hand: a clock moved by hand, an app that answers or not, and every question it was asked.
    private final class Mac {
        var t: TimeInterval = 1_000
        var answer: AXError = .success
        var launching = false
        var running: Set<pid_t> = [7, 8]
        var asked: [(el: AXUIElement, attr: String, at: TimeInterval)] = []
        var lines: [String] = []
        func tick(_ s: TimeInterval) { t += s }
    }

    private func marks(_ mac: Mac) -> Unresponsive {
        Unresponsive(now: { mac.t },
                     ask: { el, attr in mac.asked.append((el, attr, mac.t)); return mac.answer },
                     isLaunching: { _ in mac.launching },
                     isRunning: { mac.running.contains($0) },
                     say: { mac.lines.append($0) })
    }

    private let app7 = AXUIElementCreateApplication(7)
    private let app8 = AXUIElementCreateApplication(8)

    private func entry(_ u: Unresponsive, _ pid: pid_t) -> [String: Any]? { u.report()[String(pid)] as? [String: Any] }

    /// When the questions were asked, seconds after `start`.
    private func times(_ mac: Mac, since start: TimeInterval) -> [TimeInterval] { mac.asked.map { $0.at - start } }

    func testATimeoutIsBelievedOnlyUntilTheAppIsAskedAgain() {
        let mac = Mac()
        let u = marks(mac)
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        XCTAssertTrue(u.skip(7), "just timed out: believed")
        XCTAssertEqual(mac.asked.count, 0, "and nothing is asked while it is believed")

        mac.tick(0.6)
        mac.answer = .success
        XCTAssertFalse(u.skip(7), "0.6 s later it is asked again, answers, and is read")
        XCTAssertEqual(mac.asked.count, 1)
        XCTAssertFalse(u.skip(7))
        XCTAssertEqual(mac.asked.count, 1, "an app that answered is not asked again")
        XCTAssertEqual(entry(u, 7)?["cleared_by"] as? String, "reask", "ping says what ended the mark")
        XCTAssertEqual(mac.lines.count, 2, "one line for the mark and one for its end: \(mac.lines)")
    }

    func testAnyAnswerFromTheAppEndsTheMark() {
        let answers: [AXError] = [.success, .noValue, .attributeUnsupported, .parameterizedAttributeUnsupported,
                                  .actionUnsupported, .notImplemented]
        for answer in answers {
            let mac = Mac()
            let u = marks(mac)
            u.saw(.cannotComplete, app7, kAXWindowsAttribute)
            u.saw(answer, app7, kAXRoleAttribute)          // some other call, answered
            XCTAssertFalse(u.skip(7), "AXError \(answer.rawValue) is an answer: the app is not silent")
            XCTAssertEqual(mac.asked.count, 0, "an answer already seen needs no question")
            XCTAssertEqual(entry(u, 7)?["cleared_by"] as? String, "answer")
        }
    }

    func testAnAppThatNeverAnswersIsAskedLessAndLessOften() {
        let mac = Mac()
        mac.answer = .cannotComplete
        let u = marks(mac)
        let start = mac.t
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        let tick = 0.25
        for _ in 0..<Int(60 / tick) {                      // a look every 0.25 s for 60 s
            mac.tick(tick)
            XCTAssertTrue(u.skip(7), "it never answers, so it is never read")
        }
        let asked = times(mac, since: start)
        XCTAssertLessThanOrEqual(asked.count, 8, "\(asked)")
        XCTAssertEqual(asked, [0.5, 1.5, 3.5, 7.5, 15.5, 31.5, 51.5], "the wait doubles from 0.5 s up to 20 s")
        let gaps = zip(asked.dropFirst(), asked).map { $0 - $1 }
        XCTAssertEqual(gaps, gaps.sorted(), "the gaps never shrink: \(gaps)")
        XCTAssertLessThanOrEqual(gaps.max() ?? 0, 20 + tick, "and none is longer than the old 20 s: \(gaps)")
    }

    func testALaunchingAppIsAskedAgainAtTheFirstInterval() {
        let mac = Mac()
        mac.answer = .cannotComplete
        mac.launching = true
        let u = marks(mac)
        let start = mac.t
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        for _ in 0..<20 {                                  // 5 s of looks, 0.25 s apart
            mac.tick(0.25)
            _ = u.skip(7)
        }
        let asked = times(mac, since: start)
        XCTAssertEqual(asked.count, 10, "\(asked)")
        XCTAssertEqual(Set(zip(asked.dropFirst(), asked).map { $0 - $1 }), [0.5], "starting, not stuck: never backed off")
    }

    func testAskingNowIgnoresTheWait() {
        let mac = Mac()
        let u = marks(mac)
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        XCTAssertTrue(u.skip(7))
        XCTAssertEqual(mac.asked.count, 0)
        XCTAssertFalse(u.skip(7, askNow: true), "asked at once, and it answered")
        XCTAssertEqual(mac.asked.count, 1)
        XCTAssertEqual(mac.asked.first?.at, mac.t, "no time passed: the wait was not waited out")
    }

    func testOneAppNotAnsweringLeavesTheOthersAlone() {
        let mac = Mac()
        let u = marks(mac)
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        XCTAssertFalse(u.skip(8), "another app is read as ever")
        XCTAssertEqual(mac.asked.count, 0, "and nothing is asked on its behalf")
        u.saw(.success, app8, kAXFocusedWindowAttribute)
        XCTAssertTrue(u.skip(7), "an answer from another app ends nobody else's mark")
        XCTAssertNil(entry(u, 8))
    }

    func testATimeoutFromAnotherCallRefreshesTheMarkButNeverGrowsTheWait() {
        let mac = Mac()
        mac.answer = .cannotComplete
        let u = marks(mac)
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        mac.tick(0.25)
        u.saw(.cannotComplete, app7, kAXWindowsAttribute)   // another call timed out
        mac.tick(0.25)
        XCTAssertTrue(u.skip(7))
        XCTAssertEqual(mac.asked.count, 0, "0.5 s after the mark but 0.25 s after the latest timeout: still waiting")
        mac.tick(0.25)
        XCTAssertTrue(u.skip(7))
        XCTAssertEqual(mac.asked.count, 1, "0.5 s after the latest timeout: asked again, and no answer")
        XCTAssertEqual(entry(u, 7)?["wait_s"] as? Double, 1.0, "the question's own timeout doubled the wait")
        for _ in 0..<5 {
            mac.tick(0.125)
            u.saw(.cannotComplete, app7, kAXWindowsAttribute)
        }
        XCTAssertEqual(entry(u, 7)?["wait_s"] as? Double, 1.0, "other calls' timeouts never grow it")
        XCTAssertEqual(entry(u, 7)?["age_ms"] as? Int, 0, "but each one restarts it")
        XCTAssertEqual(mac.asked.count, 1)
    }

    func testAnExitedAppsMarkIsDropped() {
        let mac = Mac()
        let u = marks(mac)
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        u.saw(.invalidUIElement, app7, kAXFocusedWindowAttribute)
        XCTAssertTrue(u.skip(7), "an element gone from an app still running says nothing about the app")

        mac.running.remove(7)
        u.saw(.invalidUIElement, app7, kAXFocusedWindowAttribute)
        XCTAssertFalse(u.skip(7), "the app has exited: there is nobody left to be silent")
        XCTAssertEqual(mac.asked.count, 0)
        XCTAssertNil(entry(u, 7), "and ping no longer lists it")
    }

    func testAFailureKeepsTheMarkWithoutGrowingIt() {
        let mac = Mac()
        let u = marks(mac)
        u.saw(.cannotComplete, app7, kAXFocusedWindowAttribute)
        u.saw(.failure, app7, kAXRoleAttribute)
        u.saw(.apiDisabled, app7, kAXRoleAttribute)
        XCTAssertTrue(u.skip(7), "neither an answer nor a timeout: the mark stands")
        XCTAssertEqual(mac.asked.count, 0)

        mac.tick(0.5)
        mac.answer = .failure
        XCTAssertTrue(u.skip(7), "asked again and it failed: still no answer")
        XCTAssertEqual(mac.asked.count, 1)
        XCTAssertEqual(entry(u, 7)?["wait_s"] as? Double, 0.5, "a failure is not a timeout: the wait is not grown")
        XCTAssertTrue(u.skip(7))
        XCTAssertEqual(mac.asked.count, 1, "and the question is not repeated on every call")
    }

    func testTheReaskIsTheAttributeThatTimedOut() {
        let mac = Mac()
        let u = marks(mac)
        // No window can be made here without Accessibility: the system-wide element stands in for a window of
        // pid 7. What matters is that it, and not the app, is what gets asked again.
        let window = AXUIElementCreateSystemWide()
        u.saw(.cannotComplete, window, kAXChildrenAttribute, pid: 7)
        u.saw(.cannotComplete, app7, kAXRoleAttribute)      // later timeouts do not change the question
        mac.tick(0.5)
        _ = u.skip(7)
        XCTAssertEqual(mac.asked.count, 1)
        guard let q = mac.asked.first else { return }
        XCTAssertTrue(CFEqual(q.el, window), "the element that timed out is asked")
        XCTAssertFalse(CFEqual(q.el, app7), "not the app, which a toolkit answers while its windows time out")
        XCTAssertEqual(q.attr, kAXChildrenAttribute as String, "and the attribute that timed out, not the role")
    }
}
