import Foundation

public struct ContextWindowContract: Codable, Equatable, Sendable {
    public let mode: String
    public let nativeTokens: Int?
    public let configuredTokens: Int?
    public let effectiveTokens: Int?
    public let verifiedTokens: Int?
    public let guaranteedTokens: Int?
    public let source: String
    public let confidence: String
}

public struct ContextWindowCandidate: Codable, Equatable, Sendable, Identifiable {
    public let engine: InferenceEngine
    public let targetFingerprint: String
    public let mode: String
    public let nativeTokens: Int?
    public let configuredTokens: Int?
    public let effectiveTokens: Int?
    public let verifiedTokens: Int?
    public let guaranteedTokens: Int?
    public let source: String
    public let confidence: String

    public var id: String { "\(engine.rawValue):\(targetFingerprint)" }
}

public struct ContextWindowModel: Codable, Equatable, Sendable, Identifiable {
    public let alias: String
    public let candidates: [ContextWindowCandidate]

    public var id: String { alias }
}

public struct ContextWindowRecord: Codable, Equatable, Sendable, Identifiable {
    public let createdAt: Double
    public let alias: String
    public let engine: InferenceEngine
    public let targetFingerprint: String
    public let runtimeFingerprint: String
    public let systemFingerprint: String
    public let suiteVersion: Int
    public let requestedTokens: Int
    public let verifiedTokens: Int
    public let promptTokens: Int

    public var id: String {
        "\(alias):\(engine.rawValue):\(targetFingerprint):\(createdAt)"
    }
}

public struct ContextWindowSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let models: [ContextWindowModel]
    public let records: [ContextWindowRecord]
}

public struct ContextProfileRun: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let alias: String
    public let policy: ModelContextSettings
    public let results: [ContextWindowRecord]
    public let failures: [EngineBenchmarkFailure]
    public let contexts: [ContextWindowModel]
}

struct RunContextProfileRequest: Codable, Equatable, Sendable {
    let targetTokens: Int?
}
