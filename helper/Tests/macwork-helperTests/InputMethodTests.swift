import CoreGraphics
import XCTest

@testable import macwork_helper

/// Input methods: handing the keyboard back only once the keys have landed, which windows are theirs, and what
/// they are still composing.
///
/// The person's input source went back as soon as the last key was posted, and an app still a few keys behind
/// read those through the input method: 21 of 265 typing steps left the end of the text composing. The waits
/// here run on a scripted focused element and a clock moved by hand; no key is posted and no input source is
/// selected. An input method's candidate panel was also taken for another app's prompt: which processes belong
/// to an input method is asked of Text Input Sources and LaunchServices, by bundle, never by name.
final class InputMethodTests: XCTestCase {
    // MARK: - the wait

    /// A focused element that shows what it is scripted to, one look after another (the last one stays), and a
    /// clock that moves only when the wait pauses. 1/64 s a pause keeps every time exact.
    private final class Element {
        var t: TimeInterval = 0
        let looks: [CaretLook]
        var n = 0
        init(_ looks: [CaretLook]) { self.looks = looks }
        func look() -> CaretLook {
            defer { n += 1 }
            return looks[min(n, looks.count - 1)]
        }
        func wait(tail: String, expectAt: Int?, before: Caret?, ceiling: TimeInterval = 1, quiet: TimeInterval = 0.25) -> Landing {
            awaitLanding(tail: tail, expectAt: expectAt, before: before, ceiling: ceiling, quiet: quiet,
                         now: { self.t }, pause: 1.0 / 64, sleep: { self.t += $0 }, look: look)
        }
    }

    private func at(_ location: Int, _ before: String = "") -> CaretLook { .text(Caret(location: location, before: before)) }

    func testTheKeyboardIsHandedBackOnlyOnceTheLastKeysHaveArrived() {
        let e = Element([at(1, "h"), at(2, "he"), at(3, "hel"), at(4, "hell"), at(5, "hello")])
        XCTAssertEqual(e.wait(tail: "hello", expectAt: 5, before: Caret(location: 0)), .read)
        XCTAssertEqual(e.n, 5, "read on the fifth look, not while 'hell' was all there was")
        let shouting = Element([at(5, "HELLO")])
        XCTAssertEqual(shouting.wait(tail: "hello", expectAt: 5, before: Caret(location: 0)), .read,
                       "the same letters in another case are the same keys")
    }

    func testTextAlreadyThereIsNotTakenForTheKeysArriving() {
        // typed again after select-all: the document already says "hello", the keys have not arrived yet
        let again = Element([at(0), at(0), at(1, "h"), at(2, "he"), at(3, "hel"), at(4, "hell"), at(5, "hello")])
        XCTAssertEqual(again.wait(tail: "hello", expectAt: 5, before: Caret(location: 0)), .read)
        XCTAssertEqual(again.n, 7)
        // appended to a document that already ends with it: "hello" sits before the caret from the start
        let appended = Element([at(5, "hello"), at(5, "hello"), at(6, "elloh"), at(7, "llohe"), at(8, "lohel"),
                                at(9, "ohell"), at(10, "hello")])
        XCTAssertEqual(appended.wait(tail: "hello", expectAt: 10, before: Caret(location: 5, before: "hello")), .read)
        XCTAssertEqual(appended.n, 7, "not at the first look, where the old text looked like the new")
        // an element with no insertion point, that already shows the text: read once its value has changed
        let shown = { (v: String) in CaretLook.text(Caret(location: nil, value: v)) }
        let valueOnly = Element([shown("hello"), shown("hello"), shown("hello h"), shown("hello hello")])
        XCTAssertEqual(valueOnly.wait(tail: "hello", expectAt: nil, before: Caret(location: nil, value: "hello")), .read)
        XCTAssertEqual(valueOnly.n, 4, "not while the value was still what it was before the keys")
    }

    func testAPrefixThatMatchesIsNotTakenForTheWholeText() {
        // "abab" typed after "ab": two keys in, the four characters before the caret already read "abab"
        let e = Element([at(3, "aba"), at(4, "abab"), at(5, "baba"), at(6, "abab")])
        XCTAssertEqual(e.wait(tail: "abab", expectAt: 6, before: Caret(location: 2, before: "ab")), .read)
        XCTAssertEqual(e.n, 4)
    }

    func testAnAppThatChangesWhatWasTypedIsBelievedOnceItStopsChanging() {
        // "hi" in straight quotes, turned into curly ones as it is typed: the tail never shows
        let e = Element([at(1, "\u{201C}"), at(2, "\u{201C}h"), at(3, "\u{201C}hi"), at(4, "\u{201C}hi\u{201D}")])
        XCTAssertEqual(e.wait(tail: "\"hi\"", expectAt: 4, before: Caret(location: 0)), .settled)
        XCTAssertEqual(e.t, 3.0 / 64 + 0.25, "a quiet time after the last change, well before the ceiling")
    }

    func testAnElementWithNoTextGetsTheQuietTimeNotTheCeiling() {
        let e = Element([.none])
        XCTAssertEqual(e.wait(tail: "hello", expectAt: nil, before: nil), .unreadable)
        XCTAssertEqual(e.t, 0.25)
    }

    func testItGivesUpAtTheCeiling() {
        let e = Element([at(0)])
        XCTAssertEqual(e.wait(tail: "hello", expectAt: 5, before: Caret(location: 0)), .timeout)
        XCTAssertEqual(e.t, 1.0)
    }

    func testAnAppTooBusyToAnswerIsWaitedForUntilTheCeiling() {
        // the first key shows, then the app is too busy to answer: it may still be reading the rest
        let busy = Element([at(1, "h"), .busy])
        XCTAssertEqual(busy.wait(tail: "hello", expectAt: 5, before: Caret(location: 0)), .timeout)
        XCTAssertEqual(busy.t, 1.0, "not settled a quiet time after the first key")
        let never = Element([.busy])
        XCTAssertEqual(never.wait(tail: "hello", expectAt: 5, before: Caret(location: 0)), .timeout)
        XCTAssertEqual(never.t, 1.0, "not unreadable after the quiet time")
    }

    func testTheTailIsWhatFollowsTheLastReturnOrTab() {
        XCTAssertEqual(keyTail("name\tvalue\nhello world"), "lo world")
        XCTAssertEqual(keyTail("hi"), "hi")
        XCTAssertEqual(keyTail("line\n"), "")
        XCTAssertEqual(landingTail("hello world"), "lo world")
        XCTAssertEqual(landingTail("name\tvalue"), "", "a text holding a Tab or a Return never reads")
    }

    // MARK: - typing, in order

    /// The keyboard and the focused element, made by hand: what typing did, in order.
    private final class Keyboard {
        var events: [String] = []
        var t: TimeInterval = 0
        let layout: LayoutSwitch
        let looks: [CaretLook]
        var n = 0
        init(_ layout: LayoutSwitch, _ looks: [CaretLook] = [.none]) { self.layout = layout; self.looks = looks }
        var effects: TypeEffects {
            TypeEffects(selectASCII: { self.events.append("select"); return self.layout },
                        restore: { self.events.append("restore") },
                        key: { _, _ in self.events.append("key") },
                        paste: { self.events.append("paste \($0)") },
                        unicode: { self.events.append("unicode \($0)") },
                        after: { self.events.append("after") },
                        look: { _ in
                            self.events.append("look")
                            defer { self.n += 1 }
                            return self.looks[min(self.n, self.looks.count - 1)]
                        },
                        now: { self.t },
                        sleep: { self.events.append("sleep"); self.t += $0 })
        }
        var steps: [String] { events.filter { $0 != "sleep" } }
    }

    private func keys(_ text: String) -> [Character: (CGKeyCode, Bool)] {
        Dictionary(uniqueKeysWithValues: Set(text).enumerated().map { ($1, (CGKeyCode($0), false)) })
    }

    private func type(_ text: String, _ kb: Keyboard) -> [String: Any] {
        typeText(text, keys: keys(text), nonAscii: "paste", ceiling: 1, quiet: 0.25, fx: kb.effects)
    }

    func testTheInputSourceIsRestoredOnlyAfterTheKeysHaveLanded() {
        // an input method was current: the caret is read while the switch settles, then the keys, then they are
        // marked ours, then the element is watched until they show, and only then is the input source put back
        let kb = Keyboard(.inEffect, [at(0), at(1, "h"), at(2, "hi")])
        let reply = type("hi", kb)
        XCTAssertEqual(kb.steps, ["select", "look", "key", "key", "after", "look", "look", "restore"])
        XCTAssertEqual(reply["landed"] as? String, "read")
        XCTAssertEqual(reply["switched"] as? Bool, true)
        XCTAssertEqual(Double(reply["handback_ms"] as? Int ?? -1), 15, accuracy: 1, "one pause between the two looks")

        // no switch was needed: no look and no wait, and nothing to put back
        let plain = Keyboard(.notNeeded)
        let r2 = type("hi", plain)
        XCTAssertEqual(plain.events, ["select", "key", "key", "after"])
        XCTAssertEqual(r2["landed"] as? String, "no_switch")
        XCTAssertEqual(r2["switched"] as? Bool, false)

        // the layout did not take: the text is pasted, and nothing is waited for
        let stuck = Keyboard(.didNotTake)
        let r3 = type("hi", stuck)
        XCTAssertEqual(stuck.steps, ["select", "paste hi", "after", "restore"])
        XCTAssertEqual(r3["method"] as? String, "paste")

        // nothing to type: nothing selected, nothing waited for, and still marked ours exactly once
        let empty = Keyboard(.inEffect)
        _ = type("", empty)
        XCTAssertEqual(empty.events, ["after"])
    }

    func testATextEndingInReturnIsNotReadWhenTheCaretFirstMoves() {
        // the caret moves as soon as the first key lands; the rest are still on their way
        let kb = Keyboard(.inEffect, [at(0), at(1), at(2), at(3), at(4), at(5)])
        let reply = type("line\n", kb)
        XCTAssertEqual(reply["landed"] as? String, "settled", "it ends once nothing changes, never as read")
        XCTAssertGreaterThan(kb.steps.filter { $0 == "look" }.count, 6, "not at the first move")
        XCTAssertEqual(kb.steps.last, "restore")
    }

    func testARepeatedLineIsNotTakenForTheEnd() {
        // "abc\nabc": once the first line lands, "abc" sits before the caret, and the second is still to come
        let kb = Keyboard(.inEffect, [at(0), at(3, "abc"), at(4, "bc\n"), at(5, "c\na"), at(6, "\nab"), at(7, "abc")])
        let reply = type("abc\nabc", kb)
        XCTAssertEqual(reply["landed"] as? String, "settled")
        XCTAssertGreaterThan(kb.steps.filter { $0 == "look" }.count, 6, "not when the first line looked like the end")
    }

    // MARK: - which processes are an input method's

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

    // MARK: - what is still being composed

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
