import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

/// Apple's on-device language model (macOS 26+, Apple Intelligence-eligible Macs). Swift-only API, so it lives
/// in the helper; the engine uses it as an optional planner that never leaves the Mac.

func llmAvailable(_ p: Params) throws -> Any {
    #if canImport(FoundationModels)
    if #available(macOS 26.0, *) {
        if case .available = SystemLanguageModel.default.availability { return ["available": true] }
        return ["available": false, "reason": "\(SystemLanguageModel.default.availability)"]
    }
    #endif
    return ["available": false, "reason": "needs macOS 26 with FoundationModels"]
}

func llmGenerate(_ p: Params) throws -> Any {
    guard let prompt = p["prompt"] as? String else { throw RPCError("bad_params", "prompt required") }
    let instructions = p["instructions"] as? String ?? ""
    #if canImport(FoundationModels)
    if #available(macOS 26.0, *) {
        guard case .available = SystemLanguageModel.default.availability else {
            throw RPCError("unavailable", "\(SystemLanguageModel.default.availability)")
        }
        let text: String = try runAsync(timeout: Double(p["timeout_s"] as? Int ?? 120)) {
            let session = LanguageModelSession(instructions: instructions)
            return try await session.respond(to: prompt).content
        }
        return ["text": text]
    }
    #endif
    throw RPCError("unavailable", "needs macOS 26 with FoundationModels")
}
