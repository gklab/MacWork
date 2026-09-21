import XCTest

@testable import macwork_helper

/// The handlers themselves.
///
/// Thirteen tests over 1800 lines exercised the keyboard table and the input tracker and not one of the
/// forty RPCs — so reverting the keyboard-layout fix left them all green while every other handler was
/// unchecked. Most need Accessibility or Screen Recording and cannot run here. These do not: they read
/// the file system, the text detectors, a directory listing, and the event ring buffer.
final class HandlerTests: XCTestCase {
    private func box() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("macwork-tests-\(getpid())-\(counter())")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private static var n = 0
    private func counter() -> Int { HandlerTests.n += 1; return HandlerTests.n }

    // MARK: - file.read_text

    func testReadsAPlainTextFileWhateverItIsNamed() throws {
        let dir = try box()
        for name in ["notes.txt", "发票.txt", "청구서.txt", "no-extension"] {
            let f = dir.appendingPathComponent(name)
            try "the quick brown fox".write(to: f, atomically: true, encoding: .utf8)
            let out = try fileReadText(["path": f.path]) as? [String: Any]
            XCTAssertEqual(out?["text"] as? String, "the quick brown fox", name)
        }
    }

    func testRefusesSomethingThatIsNotText() throws {
        let dir = try box()
        let f = dir.appendingPathComponent("blob.bin")
        try Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10]).write(to: f)
        XCTAssertThrowsError(try fileReadText(["path": f.path]))
    }

    func testAMissingFileIsAnErrorAndNotAnEmptyString() {
        XCTAssertThrowsError(try fileReadText(["path": "/nowhere/at/all.txt"]))
    }

    func testItSaysHowMuchItLeftOut() throws {
        let dir = try box()
        let f = dir.appendingPathComponent("long.txt")
        try String(repeating: "x", count: 5000).write(to: f, atomically: true, encoding: .utf8)
        let out = try fileReadText(["path": f.path, "max_chars": 100]) as? [String: Any]
        XCTAssertEqual((out?["text"] as? String)?.count, 100)
        XCTAssertEqual(out?["truncated"] as? Bool, true)
    }

    // MARK: - nl.detect (the system's own detectors, not a hand-written pattern)

    func testFindsAPhoneNumberAndALink() throws {
        let out = try nlDetect(["texts": ["call +1 (415) 555-0134 or see https://example.com/x"],
                                "kinds": ["PHONE", "LINK"]]) as? [[[String: Any]]]
        let kinds = Set((out?.first ?? []).compactMap { $0["type"] as? String })
        XCTAssertTrue(kinds.contains("PHONE"), "\(kinds)")
        XCTAssertTrue(kinds.contains("LINK"), "\(kinds)")
    }

    func testItOnlyLooksForWhatItWasAskedFor() throws {
        // the default is PHONE and ADDRESS; a link is not personal data and is not hunted for unasked
        let out = try nlDetect(["texts": ["see https://example.com/x"]]) as? [[[String: Any]]]
        XCTAssertEqual(out?.first?.count, 0)
    }

    func testOrdinaryProseHasNothingInIt() throws {
        let out = try nlDetect(["texts": ["open the file and read the first paragraph"]]) as? [[[String: Any]]]
        XCTAssertEqual(out?.first?.count, 0)
    }

    func testOneAnswerPerTextInTheOrderGiven() throws {
        let out = try nlDetect(["texts": ["nothing here", "ring 13800138000", "nor here"]]) as? [[[String: Any]]]
        XCTAssertEqual(out?.count, 3)
        XCTAssertEqual(out?[0].count, 0)
        XCTAssertGreaterThan(out?[1].count ?? 0, 0)
    }

    // MARK: - apps.installed

    func testFindsAnAppWhereverItIsPutRatherThanInAFixedList() throws {
        let dir = try box()
        let app = dir.appendingPathComponent("Nobody Has This.app/Contents")
        try FileManager.default.createDirectory(at: app, withIntermediateDirectories: true)
        try """
        {"CFBundleIdentifier": "com.example.nobody", "CFBundleName": "Nobody Has This"}
        """.write(to: app.appendingPathComponent("Info.json"), atomically: true, encoding: .utf8)
        let plist: [String: Any] = ["CFBundleIdentifier": "com.example.nobody", "CFBundleName": "Nobody Has This"]
        try (try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0))
            .write(to: app.appendingPathComponent("Info.plist"))

        let out = try appsInstalled(["dirs": [dir.path], "source": "dirs"]) as? [[String: Any]]
        XCTAssertTrue((out ?? []).contains { $0["bundle_id"] as? String == "com.example.nobody" },
                      "an app in an unusual place was not found: \(out ?? [])")
    }

    func testABundleInsideAnotherBundleIsNotSomethingAPersonOpens() throws {
        let dir = try box()
        let inner = dir.appendingPathComponent("Outer.app/Contents/Library/Inner.app/Contents")
        try FileManager.default.createDirectory(at: inner, withIntermediateDirectories: true)
        let out = try appsInstalled(["dirs": [dir.path], "source": "dirs"]) as? [[String: Any]]
        XCTAssertFalse((out ?? []).contains { ($0["path"] as? String)?.contains("Inner.app") == true })
    }

    // MARK: - input.scroll: what it refuses

    func testScrollNeedsSomewhereToPointAndSomethingToDo() {
        XCTAssertThrowsError(try inputScroll(["dy": 3]))                  // no point
        XCTAssertThrowsError(try inputScroll(["x": 10, "y": 10]))         // no distance
    }

    // MARK: - events

    func testTheRingBufferHandsBackWhatHappenedAfterASequenceNumber() throws {
        EventLog.shared.add("test.one", ["n": 1])
        let first = EventLog.shared.poll(after: 0)
        let seq = first["seq"] as? Int ?? 0
        EventLog.shared.add("test.two", ["n": 2])
        let next = EventLog.shared.poll(after: seq)
        let kinds = ((next["events"] as? [[String: Any]]) ?? []).compactMap { $0["kind"] as? String }
        XCTAssertEqual(kinds, ["test.two"], "polling after a sequence number replayed what was already seen")
    }

    func testItSaysWhatItStillHoldsSoAClientCanTellAGapFromQuiet() throws {
        let out = EventLog.shared.poll(after: 0)
        XCTAssertNotNil(out["oldest"], "a client cannot tell 'nothing happened' from 'you were away too long'")
        XCTAssertNotNil(out["dropped"])
    }

    func testWatchingSaysWhatItCanReportRatherThanLeavingTheCallerToGuess() throws {
        let out = try eventsWatch(["apps": false, "screen_lock": false]) as? [String: Any]
        let kinds = out?["kinds"] as? [String] ?? []
        XCTAssertTrue(kinds.contains("file.changed") && kinds.contains("app.launched"), "\(kinds)")
        _ = try eventsWatch(["stop": true])
    }

    func testWatchingAPathThatIsNotThereIsRefusedRatherThanSilentlyIgnored() {
        XCTAssertThrowsError(try eventsWatch(["apps": false, "screen_lock": false,
                                              "paths": ["/nowhere/at/all/\(counter())"]]))
    }

    // MARK: - runAsync

    func testAnAsyncCallThatFinishesComesBack() throws {
        XCTAssertEqual(try runAsync(timeout: 2) { 42 }, 42)
    }

    func testOneThatDoesNotFinishTimesOutInsteadOfSpinningForever() {
        let started = Date()
        XCTAssertThrowsError(try runAsync(timeout: 0.2) {
            try await Task.sleep(nanoseconds: 5_000_000_000)
            return 1
        })
        XCTAssertLessThan(Date().timeIntervalSince(started), 2.0, "it waited far longer than its timeout")
    }

    func testAnErrorInsideComesBackAsAnError() {
        struct Boom: Error {}
        XCTAssertThrowsError(try runAsync(timeout: 2) { throw Boom() })
    }
}
