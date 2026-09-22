import ApplicationServices
import XCTest

@testable import macwork_helper

/// Element references: a snapshot's live for a number of generations, the focused element's in a ring of
/// its own. Reading the focus used to open a generation each time, so polling it while a field took focus
/// pushed out the trees the engine still held.
final class StoreTests: XCTestCase {
    func testTheFocusedElementDoesNotPushSnapshotsOut() {
        let store = AXStore(keepGenerations: 2)
        let el = AXUIElementCreateSystemWide()
        let kept = store.add(el, gen: store.newGeneration(), n: 0)
        for _ in 0..<200 { _ = store.addTransient(el) }
        XCTAssertNoThrow(try store.get(kept), "two hundred focus reads later, the snapshot is still there")
    }

    func testTransientReferencesExpireAmongThemselves() {
        let store = AXStore()
        let el = AXUIElementCreateSystemWide()
        let first = store.addTransient(el)
        for _ in 0..<store.keepTransient { _ = store.addTransient(el) }
        XCTAssertThrowsError(try store.get(first))
        XCTAssertNoThrow(try store.get(store.addTransient(el)))
    }

    func testSnapshotsStillExpireByGeneration() {
        let store = AXStore(keepGenerations: 2)
        let el = AXUIElementCreateSystemWide()
        let old = store.add(el, gen: store.newGeneration(), n: 0)
        _ = store.newGeneration(); _ = store.newGeneration()
        XCTAssertThrowsError(try store.get(old))
    }
}
