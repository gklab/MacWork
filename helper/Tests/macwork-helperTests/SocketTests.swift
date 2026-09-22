import XCTest

@testable import macwork_helper

/// Who may connect. The socket file's mode is a wall around the path; `peerIsUs` is the check at the door,
/// and it asks the kernel rather than anything on the wire.
final class SocketTests: XCTestCase {
    func testAConnectionFromThisUserIsUs() {
        var fds: [Int32] = [0, 0]
        XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &fds), 0)
        defer { close(fds[0]); close(fds[1]) }
        XCTAssertTrue(peerIsUs(fds[0]))
        XCTAssertTrue(peerIsUs(fds[1]))
    }

    func testAnUnconnectedDescriptorIsNobody() {
        XCTAssertFalse(peerIsUs(-1))
        let plain = open("/dev/null", O_RDONLY)
        defer { close(plain) }
        XCTAssertFalse(peerIsUs(plain), "not a socket: no peer to ask about")
    }
}
