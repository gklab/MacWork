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
}
