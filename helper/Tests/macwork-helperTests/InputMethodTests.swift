import XCTest

@testable import macwork_helper

/// Input methods: which windows are theirs.
///
/// An input method's candidate panel was taken for another app's prompt: 48 looks in 27 tasks had it in
/// covered_by, with 168 candidates offered as options. Which processes belong to an input method is asked of
/// Text Input Sources and LaunchServices, by bundle, never by name. The processes here are made by hand.
final class InputMethodTests: XCTestCase {
    func testInputMethodProcessesAreFoundByBundleAndByBeingInsideIt() {
        let bundle = "/Library/Input Methods/Example.app"
        let running = [
            // the input method itself, by its bundle identifier
            RunningProcess(pid: 870, bundleId: "com.example.inputmethod.Example", bundlePath: bundle,
                           executablePath: bundle + "/Contents/MacOS/Example"),
            // its services, a bundle of their own inside it
            RunningProcess(pid: 806, bundleId: "com.example.inputmethod.Example.Services",
                           bundlePath: bundle + "/Contents/Helpers/Services.app",
                           executablePath: bundle + "/Contents/Helpers/Services.app/Contents/MacOS/Services"),
            // a helper binary with no bundle, inside it
            RunningProcess(pid: 807, bundleId: nil, bundlePath: nil, executablePath: bundle + "/Contents/MacOS/helper"),
            // an unrelated app
            RunningProcess(pid: 500, bundleId: "com.example.Editor", bundlePath: "/Applications/Editor.app",
                           executablePath: "/Applications/Editor.app/Contents/MacOS/Editor"),
            // a bundle whose path merely starts with the same characters
            RunningProcess(pid: 501, bundleId: "com.example.Other", bundlePath: "/Library/Input Methods/Example.app2",
                           executablePath: "/Library/Input Methods/Example.app2/Contents/MacOS/Other"),
        ]
        let found = inputMethodPids(bundleIds: ["com.example.inputmethod.Example"], bundlePaths: [bundle], running: running)
        XCTAssertEqual(found, [870, 806, 807])
        XCTAssertEqual(inputMethodPids(bundleIds: [], bundlePaths: [], running: running), [], "no input method, no process")
    }

    func testAMarkedRangeIsCountedInUTF16Units() {
        // an NSTextView composing "lo" after "😀hel" says {5, 2}: the emoji is two units, not one character
        XCTAssertEqual(markedSubstring("😀hello", location: 5, length: 2), "lo")
        XCTAssertEqual(markedSubstring("😀hello", location: 0, length: 2), "😀")
        XCTAssertEqual(markedSubstring("😀hello", location: 7, length: 0), "", "nothing composing at the end")
        XCTAssertNil(markedSubstring("😀hello", location: 6, length: 2), "a range past the end is no text")
        XCTAssertNil(markedSubstring("hello", location: -1, length: 2))
        XCTAssertNil(markedSubstring("hello", location: 2, length: -1))
    }
}
