import XCTest

@testable import macwork_helper

/// Nothing is ever left held.
///
/// A key that stays down after the engine has crashed or been interrupted keeps acting on the person's Mac
/// with nobody deciding anything. These tests run against a fake world — no key is pressed on this Mac — and
/// check the order things go down and come up, the ceiling, and every way a hold can end.
final class HoldTests: XCTestCase {
    private final class World {
        var log: [String] = []
        var clock: TimeInterval = 100
        var userTouchedAt: TimeInterval?            // when the person did something, if they did
        var onSleep: ((World) -> Void)?
        var sleeps = 0

        func make() -> Holds.World {
            Holds.World(
                key: { code, down, flags in self.log.append("\(down ? "down" : "up") key\(code)\(flags.contains(.maskShift) ? "+shift" : "")") },
                button: { name, down in self.log.append("\(down ? "down" : "up") \(name)") },
                move: { dx, dy, dragging in self.log.append("move \(dx),\(dy)\(dragging.map { " dragging \($0)" } ?? "")") },
                sleep: { s in self.sleeps += 1; self.clock += s; self.onSleep?(self) },
                now: { self.clock },
                userIdle: { _, _ in self.userTouchedAt.map { self.clock - $0 } ?? 1_000 })
        }
    }

    private func request(keys: [(CGKeyCode, CGEventFlags?)] = [], buttons: [String] = [], dx: Int = 0, dy: Int = 0,
                         ms: Int, yield: Bool = true) -> Holds.Request {
        var r = Holds.Request()
        r.keys = keys.map { (code: $0.0, flag: $0.1) }
        r.buttons = buttons; r.dx = dx; r.dy = dy; r.ms = ms; r.yieldToUser = yield
        return r
    }

    func testItGoesDownWaitsAndComesUpInReverse() {
        let w = World()
        let holds = Holds(world: w.make())
        let out = holds.hold(request(keys: [(56, .maskShift), (13, nil)], ms: 500))

        XCTAssertEqual(w.log, ["down key56+shift", "down key13+shift", "up key13+shift", "up key56"])
        XCTAssertEqual(out["ended"] as? String, "completed")
        XCTAssertEqual(out["held_ms"] as? Int, 500)
        XCTAssertFalse(holds.anythingDown)
    }

    func testTheCeilingHoldsWhateverIsAsked() {
        let w = World()
        let out = Holds(world: w.make()).hold(request(keys: [(13, nil)], ms: 3_600_000))
        XCTAssertEqual(out["held_ms"] as? Int, Holds.hardCeilingMs)
        XCTAssertEqual(w.log.last, "up key13")
    }

    func testThePersonReachingForTheMacEndsIt() {
        let w = World()
        w.onSleep = { world in if world.sleeps == 10 { world.userTouchedAt = world.clock } }
        let out = Holds(world: w.make()).hold(request(keys: [(13, nil)], ms: 5_000))

        XCTAssertEqual(out["ended"] as? String, "user")
        XCTAssertLessThan(out["held_ms"] as! Int, 200)
        XCTAssertEqual(w.log, ["down key13", "up key13"], "and the key came up")
    }

    func testWhatThePersonDidBeforeItBeganIsNotThemComingBack() {
        let w = World()
        w.userTouchedAt = 99.9                       // a moment before the hold started
        let out = Holds(world: w.make()).hold(request(keys: [(13, nil)], ms: 300))
        XCTAssertEqual(out["ended"] as? String, "completed")
    }

    func testAnExclusiveEngineIsNotInterrupted() {
        let w = World()
        w.onSleep = { world in world.userTouchedAt = world.clock }
        let out = Holds(world: w.make()).hold(request(keys: [(13, nil)], ms: 300, yield: false))
        XCTAssertEqual(out["ended"] as? String, "completed")
    }

    func testReleaseAllFromElsewhereEndsAHoldInProgress() {
        let w = World()
        var holds: Holds!
        w.onSleep = { world in if world.sleeps == 5 { holds.releaseAll() } }    // as `input.release_all` on a second connection would
        holds = Holds(world: w.make())
        let out = holds.hold(request(keys: [(13, nil)], buttons: ["left"], ms: 5_000))

        XCTAssertEqual(out["ended"] as? String, "released")
        XCTAssertEqual(w.log, ["down key13", "down left", "up left", "up key13"], "released once, not twice")
    }

    func testReleasingWithNothingHeldDoesNothing() {
        let w = World()
        let holds = Holds(world: w.make())
        holds.releaseAll(); holds.releaseAll()
        XCTAssertEqual(w.log, [])
    }

    func testMovementIsSpreadOverTheHoldAndAddsUpExactly() {
        let w = World()
        let out = Holds(world: w.make()).hold(request(dx: 301, dy: -7, ms: 100))
        let moves = w.log.filter { $0.hasPrefix("move") }.map { $0.dropFirst(5).split(separator: ",").map { Int($0)! } }

        XCTAssertGreaterThan(moves.count, 5, "in steps: a view that tracks the pointer sees one jump as one event")
        XCTAssertEqual(moves.map { $0[0] }.reduce(0, +), 301)
        XCTAssertEqual(moves.map { $0[1] }.reduce(0, +), -7)
        XCTAssertEqual(out["moved"] as? [Int], [301, -7])
    }

    func testMovingWithAButtonDownIsADrag() {
        let w = World()
        _ = Holds(world: w.make()).hold(request(buttons: ["left"], dx: 40, ms: 50))
        XCTAssertTrue(w.log.contains { $0.hasPrefix("move") && $0.hasSuffix("dragging left") })
        XCTAssertEqual(w.log.first, "down left")
        XCTAssertEqual(w.log.last, "up left")
    }

    func testAMoveOfNoLengthStillArrives() {
        let w = World()
        _ = Holds(world: w.make()).hold(request(dx: 50, ms: 0))
        XCTAssertEqual(w.log, ["move 50,0"])
    }

    // MARK: - the request

    func testAHoldMustSayHowLong() {
        XCTAssertThrowsError(try inputHold(["keys": ["w"]]))
        XCTAssertThrowsError(try inputHold(["ms": 100]), "and what")
        XCTAssertThrowsError(try inputHold(["keys": ["no-such-key"], "ms": 100]))
        XCTAssertThrowsError(try inputHold(["buttons": ["fourth"], "ms": 100]))
    }
}
