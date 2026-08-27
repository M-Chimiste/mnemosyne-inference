import Foundation

public struct EngineBenchmarkRecord: Codable, Equatable, Sendable, Identifiable {
    public let createdAt: Double
    public let alias: String
    public let endpoint: String
    public let engine: InferenceEngine
    public let targetFingerprint: String
    public let runtimeFingerprint: String
    public let systemFingerprint: String
    public let configRevision: String
    public let suiteVersion: Int
    public let successfulSamples: Int
    public let failedSamples: Int
    public let p50TtftMs: Double?
    public let p50TotalMs: Double?
    public let p50OutputTps: Double?
    public let successRate: Double

    public var id: String {
        "\(alias):\(engine.rawValue):\(targetFingerprint):\(createdAt)"
    }
}

public struct EngineBenchmarkDecision: Codable, Equatable, Sendable, Identifiable {
    public let alias: String
    public let mode: String
    public let selectedEngine: InferenceEngine
    public let selectedTargetFingerprint: String
    public let fallbackEngine: InferenceEngine
    public let reason: String
    public let score: Double?

    public var id: String { alias }
}

public struct EngineBenchmarkSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let records: [EngineBenchmarkRecord]
    public let decisions: [EngineBenchmarkDecision]
}

public struct EngineBenchmarkRun: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let alias: String
    public let policy: ModelSelectionSettings
    public let results: [EngineBenchmarkRecord]
    public let failures: [EngineBenchmarkFailure]
    public let decision: EngineBenchmarkDecision
}

public struct EngineBenchmarkFailure: Codable, Equatable, Sendable {
    public let engine: InferenceEngine
    public let code: String
    public let detail: String
}

struct RunEngineBenchmarkRequest: Codable, Equatable, Sendable {
    let warmupRuns: Int
    let sampleRuns: Int
    let maxTokens: Int
}
