import Foundation

/// Newline-delimited JSON-RPC: {"id", "method", "params"} -> {"id", "result"} | {"id", "error": {"code", "message"}}.
///
/// Requests run one at a time on the main thread, because Accessibility and the run loop live there. A few do
/// not: enumerating every installed app shells out to `mdfind`, reading a PDF is PDFKit, recognising text is
/// Vision. Those held the main thread for as long as they took, and measurably: the first look of a session
/// spent 1.8 s inside whichever provider happened to be running while a background app enumeration had the
/// thread — the enumeration was moved off the critical path and then blocked it anyway. Such a method is
/// registered `offMain` and runs on the connection's own reader thread, so a second connection gets on with
/// its work. Ordering within one connection is unchanged: that thread still handles its requests in turn.

struct RPCError: Error {
    let code: String
    let message: String
    init(_ code: String, _ message: String) { self.code = code; self.message = message }
}

typealias Params = [String: Any]
typealias Handler = (Params) throws -> Any

final class Dispatcher {
    private var handlers: [String: Handler] = [:]
    private var offMain: Set<String> = []

    /// `offMain`: this handler touches no Accessibility API, no run loop and no shared element store, and it
    /// is slow enough that holding the main thread for it is felt. Anything else stays on the main thread.
    func register(_ method: String, offMain: Bool = false, _ handler: @escaping Handler) {
        handlers[method] = handler
        if offMain { self.offMain.insert(method) }
    }
    var methods: [String] { handlers.keys.sorted() }

    /// Handle one request line; always returns a response line.
    func handle(line: Data) -> Data {
        var id: Any = NSNull()
        let response: [String: Any]
        do {
            guard let obj = try JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                throw RPCError("bad_request", "request must be a JSON object")
            }
            id = obj["id"] ?? NSNull()
            guard let method = obj["method"] as? String, let handler = handlers[method] else {
                throw RPCError("no_method", "unknown method \(obj["method"] ?? "")")
            }
            let params = obj["params"] as? Params ?? [:]
            let result = offMain.contains(method) ? try handler(params)
                                                  : try DispatchQueue.main.sync { try handler(params) }
            response = ["id": id, "result": result]
        } catch let e as RPCError {
            response = ["id": id, "error": ["code": e.code, "message": e.message]]
        } catch {
            response = ["id": id, "error": ["code": "internal", "message": "\(error)"]]
        }
        let data = (try? JSONSerialization.data(withJSONObject: sanitize(response))) ??
            Data(#"{"id":null,"error":{"code":"encode","message":"unencodable result"}}"#.utf8)
        return data + Data([0x0A])
    }
}

/// JSONSerialization only takes plain types; make anything else a string.
func sanitize(_ v: Any) -> Any {
    switch v {
    case let d as [String: Any]: return d.mapValues(sanitize)
    case let a as [Any]: return a.map(sanitize)
    case is String, is NSNumber, is NSNull, is Int, is Double, is Bool: return v
    default: return "\(v)"
    }
}

/// Serve one byte stream (stdin/stdout or a socket connection).
final class Connection {
    private let input: FileHandle
    private let output: FileHandle
    private let dispatcher: Dispatcher
    private let onClose: () -> Void

    init(input: FileHandle, output: FileHandle, dispatcher: Dispatcher, onClose: @escaping () -> Void) {
        self.input = input; self.output = output; self.dispatcher = dispatcher; self.onClose = onClose
    }

    func start() {
        Thread.detachNewThread { [self] in
            var buffer = Data()
            while true {
                let chunk = input.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let nl = buffer.firstIndex(of: 0x0A) {
                    let line = buffer[buffer.startIndex..<nl]
                    buffer.removeSubrange(buffer.startIndex...nl)
                    if line.isEmpty { continue }
                    // handle() hops to the main thread itself, for every method that needs it.
                    // `write(contentsOf:)`, not `write(_:)`: the old one raises an ObjC exception on a
                    // broken pipe rather than throwing, so a client that went away mid-reply took the
                    // helper with it. (SIGPIPE is ignored in main.swift for the same reason.)
                    do {
                        try self.output.write(contentsOf: self.dispatcher.handle(line: Data(line)))
                    } catch {
                        break      // the other end is gone; this connection is over, the helper is not
                    }
                }
            }
            DispatchQueue.main.async { self.onClose() }
        }
    }
}

/// Unix-domain socket server: one connection per client, each serving its requests in turn.
final class SocketServer {
    let path: String
    private let dispatcher: Dispatcher
    private var fd: Int32 = -1

    init(path: String, dispatcher: Dispatcher) { self.path = path; self.dispatcher = dispatcher }

    func start() throws {
        unlink(path)
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw RPCError("socket", "socket() failed") }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else { throw RPCError("socket", "path too long") }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in bytes.enumerated() { raw[i] = UInt8(bitPattern: b) }
        }
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let ok = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, size) } }
        guard ok == 0 else { throw RPCError("socket", "bind(\(path)) failed: \(errno)") }
        chmod(path, 0o600)  // only this user may drive the Mac
        guard listen(fd, 8) == 0 else { throw RPCError("socket", "listen failed") }
        Thread.detachNewThread { [self] in
            while true {
                let client = accept(fd, nil, nil)
                if client < 0 { continue }
                let h = FileHandle(fileDescriptor: client, closeOnDealloc: true)
                Connection(input: h, output: h, dispatcher: dispatcher, onClose: {}).start()
            }
        }
    }
}
