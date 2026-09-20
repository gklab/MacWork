import Carbon.HIToolbox
import XCTest

@testable import macwork_helper

/// Which key to press for a given character.
///
/// The name-to-keycode table holds ANSI *positions*. On a German, French or JIS keyboard those positions
/// hold different characters, so `cmd+[` pressed whatever sits where an American keyboard has `[` — on a
/// German layout, `ü`. Characters go through the current layout instead; only keys that are in the same
/// place on every keyboard come from the table.
final class KeyboardTests: XCTestCase {
    func testNamedKeysComeFromTheFixedTable() {
        XCTAssertEqual(keyCode(for: "return")?.code, CGKeyCode(kVK_Return))
        XCTAssertEqual(keyCode(for: "tab")?.code, CGKeyCode(kVK_Tab))
        XCTAssertEqual(keyCode(for: "pagedown")?.code, CGKeyCode(kVK_PageDown))
        XCTAssertEqual(keyCode(for: "f7")?.code, CGKeyCode(kVK_F7))
    }

    func testACharacterIsResolvedThroughTheLayout() {
        // whatever the layout, asking for "a" must land on the key that produces "a"
        guard let a = keyCode(for: "a") else { return XCTFail("no key produces 'a' on this layout") }
        XCTAssertFalse(a.shift, "a lowercase letter needs no shift")
        XCTAssertEqual(a.code, asciiKeyMap()["a"]?.0)
    }

    func testAShiftedCharacterSaysSo() {
        guard let colon = keyCode(for: ":") else { return }   // not on every layout without shift
        if let plain = asciiKeyMap()[":"] {
            XCTAssertEqual(colon.shift, plain.1)
        }
    }

    func testAnUnknownNameIsRefusedRatherThanGuessed() {
        XCTAssertNil(keyCode(for: "nosuchkey"))
        XCTAssertNil(keyCode(for: ""))
    }
}
