import CoreGraphics
import XCTest

@testable import macwork_helper

/// Which display a window is on. Window frames come from CoreGraphics (origin top-left of the primary
/// display, y down); screen frames come from AppKit (origin bottom-left of the primary display, y up). The
/// two agree on the primary display and nowhere else.
final class ScreenTests: XCTestCase {
    let primary = CGRect(x: 0, y: 0, width: 1920, height: 1080)
    let above = CGRect(x: 0, y: 1080, width: 2560, height: 1440)       // AppKit: stacked on top of the primary
    let right = CGRect(x: 1920, y: 0, width: 1440, height: 900)

    func testAWindowOnThePrimaryDisplay() {
        XCTAssertEqual(screenIndex(forWindow: CGRect(x: 100, y: 100, width: 800, height: 600), screens: [primary, above, right]), 0)
    }

    func testAWindowOnTheDisplayAboveHasANegativeYInCoreGraphics() {
        // 400 px below the top of the upper display: CoreGraphics counts down from the primary's top edge
        let win = CGRect(x: 100, y: -1000, width: 800, height: 600)
        XCTAssertEqual(screenIndex(forWindow: win, screens: [primary, above, right]), 1)
        XCTAssertEqual(screenIndex(forWindow: win, screens: [primary]), nil, "no such display: no scale to borrow")
    }

    func testAWindowOnTheDisplayToTheRight() {
        XCTAssertEqual(screenIndex(forWindow: CGRect(x: 2000, y: 100, width: 400, height: 300), screens: [primary, above, right]), 2)
    }

    func testAWindowStraddlingTwoDisplaysGoesWhereMostOfItIs() {
        // its centre is in the gap the right display does not cover (y 900..1080 in AppKit terms)
        let win = CGRect(x: 1700, y: 0, width: 400, height: 300)
        XCTAssertEqual(screenIndex(forWindow: win, screens: [primary, right]), 0)
        let mostlyRight = CGRect(x: 1800, y: 0, width: 800, height: 300)
        XCTAssertEqual(screenIndex(forWindow: mostlyRight, screens: [primary, right]), 1)
    }

    func testNoScreensNoAnswer() {
        XCTAssertNil(screenIndex(forWindow: CGRect(x: 0, y: 0, width: 10, height: 10), screens: []))
    }

    // MARK: - the window server's list

    /// One window as CGWindowListCopyWindowInfo describes it. Made by hand: nothing on this Mac is listed.
    private func window(pid: Int, id: Int, layer: Int = 0, frame: [Double] = [100, 100, 800, 600],
                        title: String? = nil) -> [String: Any] {
        var w: [String: Any] = [kCGWindowOwnerPID as String: pid, kCGWindowNumber as String: id,
                                kCGWindowLayer as String: layer, kCGWindowOwnerName as String: "Example",
                                kCGWindowAlpha as String: 1.0,
                                kCGWindowBounds as String: ["X": frame[0], "Y": frame[1], "Width": frame[2], "Height": frame[3]]]
        if let title { w[kCGWindowName as String] = title }
        return w
    }

    private func entry(_ w: [String: Any], only pid: Int? = nil, onScreen: Set<Int>? = nil,
                       imPids: Set<Int> = []) -> [String: Any]? {
        windowEntry(w, onScreen: onScreen, regular: { $0 == 42 }, pidFilter: pid, imPids: imPids)
    }

    func testTheOrdinaryLevelsAreTheAppWindowLevels() {
        // normal 0, floating 3 and modal panel 8 are where an app's own windows live; the main menu 24, status
        // items 25 and pop-up menus 101 are not, though an app owns windows there too
        for key in [CGWindowLevelKey.normalWindow, .floatingWindow, .modalPanelWindow] {
            XCTAssertTrue(ordinaryLevel(Int(CGWindowLevelForKey(key))), "level \(CGWindowLevelForKey(key))")
        }
        for key in [CGWindowLevelKey.mainMenuWindow, .statusWindow, .popUpMenuWindow] {
            XCTAssertFalse(ordinaryLevel(Int(CGWindowLevelForKey(key))), "level \(CGWindowLevelForKey(key))")
        }
        let panel = entry(window(pid: 42, id: 7, layer: Int(CGWindowLevelForKey(.floatingWindow))))
        let statusItem = entry(window(pid: 42, id: 8, layer: Int(CGWindowLevelForKey(.statusWindow))))
        XCTAssertEqual(panel?["ordinary"] as? Bool, true, "each window says so")
        XCTAssertEqual(statusItem?["ordinary"] as? Bool, false)
    }

    func testATitleIsGivenOnlyForTheAppAskedAbout() {
        let w = window(pid: 42, id: 501, title: "Budget")
        XCTAssertNil(entry(w)?["title"], "the list of every window names nobody's documents")
        XCTAssertEqual(entry(w, only: 42)?["title"] as? String, "Budget", "the one app asked about gets its titles")
        XCTAssertNil(entry(window(pid: 43, id: 502, title: "Inbox"), only: 42),
                     "another app's window is not listed, title and all, when one app is asked about")
        XCTAssertNil(entry(window(pid: 42, id: 503, title: ""), only: 42)?["title"], "an empty name is no title")
    }

    func testThePidFilterKeepsOnlyThatProcess() {
        let list = [window(pid: 42, id: 501), window(pid: 43, id: 601, layer: 8), window(pid: 42, id: 502, layer: 3)]
        let kept = list.compactMap { entry($0, only: 42) }
        XCTAssertEqual(kept.compactMap { $0["id"] as? Int }, [501, 502], "that process's windows, front to back")
        XCTAssertEqual(list.compactMap { entry($0) }.count, 3, "and every window without the filter")

        // what each entry carries: today's fields, plus ordinary and input_method
        let e = entry(window(pid: 43, id: 601, layer: 8, frame: [10, 20, 300, 200]), onScreen: [501], imPids: [43])
        XCTAssertEqual(e?["pid"] as? Int, 43)
        XCTAssertEqual(e?["id"] as? Int, 601)
        XCTAssertEqual(e?["owner"] as? String, "Example")
        XCTAssertEqual(e?["layer"] as? Int, 8)
        XCTAssertEqual(e?["frame"] as? [Int], [10, 20, 300, 200])
        XCTAssertEqual(e?["regular"] as? Bool, false)
        XCTAssertEqual(e?["alpha"] as? Double, 1.0)
        XCTAssertEqual(e?["on_screen"] as? Bool, false, "listed with every window, and not on this Space")
        XCTAssertEqual(e?["ordinary"] as? Bool, true)
        XCTAssertEqual(e?["input_method"] as? Bool, true)
        XCTAssertEqual(entry(window(pid: 42, id: 501), onScreen: [501])?["on_screen"] as? Bool, true)
        XCTAssertEqual(entry(window(pid: 42, id: 501))?["input_method"] as? Bool, false)
    }
}
