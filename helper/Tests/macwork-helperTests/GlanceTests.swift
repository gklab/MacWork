import CoreGraphics
import XCTest

@testable import macwork_helper

/// The coarse look at a window: each cell is the mean brightness of what is under it. Pictures are made in
/// memory here — nothing on this Mac's screen is captured.
final class GlanceTests: XCTestCase {
    private func picture(width: Int, height: Int, _ paint: (CGContext) -> Void) -> CGImage {
        let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(CGColor(gray: 0, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        paint(ctx)
        return ctx.makeImage()!
    }

    func testTheSamePictureGivesTheSameGrid() {
        let a = picture(width: 640, height: 480) { $0.setFillColor(CGColor(gray: 1, alpha: 1)); $0.fill(CGRect(x: 10, y: 10, width: 200, height: 100)) }
        XCTAssertEqual(glanceGrid(a, n: 32), glanceGrid(a, n: 32))
        XCTAssertEqual(glanceGrid(a, n: 32).count, 32 * 32)
    }

    func testAChangeShowsWhereItIs() {
        let blank = picture(width: 640, height: 640) { _ in }
        // CoreGraphics draws from the bottom left; the grid reads from the top left like everything else here
        let corner = picture(width: 640, height: 640) { $0.setFillColor(CGColor(gray: 1, alpha: 1)); $0.fill(CGRect(x: 0, y: 480, width: 160, height: 160)) }
        let a = glanceGrid(blank, n: 8), b = glanceGrid(corner, n: 8)
        let changed = (0..<64).filter { abs(Int(a[$0]) - Int(b[$0])) > 2 }
        XCTAssertEqual(Set(changed), [0, 1, 8, 9], "a bright square in the top-left quarter of the top-left quarter")
    }

    func testSomethingSmallerThanACellStillMovesIt() {
        let blank = picture(width: 640, height: 640) { _ in }
        let dot = picture(width: 640, height: 640) { $0.setFillColor(CGColor(gray: 1, alpha: 1)); $0.fill(CGRect(x: 300, y: 300, width: 40, height: 40)) }
        let a = glanceGrid(blank, n: 8), b = glanceGrid(dot, n: 8)
        XCTAssertTrue((0..<64).contains { abs(Int(a[$0]) - Int(b[$0])) > 2 })
    }

    func testTheGridSizeIsKeptWithinReason() {
        XCTAssertThrowsError(try screenGlance([:]), "a pid is required")
    }
}
