import Foundation

public struct CoreReadiness: Codable, Equatable, Sendable {
    public let ready: Bool
    public let state: String
    public let diagnostic: String?
    public let startupError: String?
    public let residentAlias: String?
    public let inFlightRequests: Int
    public let queuedRequests: Int
    public let omlxModelDirectorySyncPending: Bool
}

public struct EngineReadiness: Codable, Equatable, Identifiable, Sendable {
    public let engine: InferenceEngine
    public let releaseTier: String
    public let enabled: Bool
    public let installed: Bool
    public let installedVersion: String?
    public let installedPath: String?
    public let serviceState: String
    public let authoritative: Bool
    public let residentModels: [String]
    public let ready: Bool
    public let diagnostic: String?

    public var id: InferenceEngine { engine }
    public var isStable: Bool { releaseTier == "stable" }
}

public struct ReadinessStorage: Codable, Equatable, Identifiable, Sendable {
    public let name: String
    public let path: String
    public let available: Bool
    public let writable: Bool
    public let volumeMatches: Bool
    public let freeBytes: Int64?
    public let diagnostic: String?

    public var id: String { name }
}

public struct ReadinessModels: Codable, Equatable, Sendable {
    public let configured: Int
    public let callable: Int
}

public struct ReadinessDownloads: Codable, Equatable, Sendable {
    public let active: Int
}

public struct UsageReadiness: Codable, Equatable, Sendable {
    public let enabled: Bool
    public let nodeId: String
    public let nodeIdSource: String
    public let writerReady: Bool
    public let outboxPending: Int
    public let outboxDepth: Int
    public let lastFlushAt: Double?
    public let lastFlushCount: Int
    public let lastError: String?
}

public struct ReadinessSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let productVersion: String?
    public let core: CoreReadiness
    public let engines: [EngineReadiness]
    public let storage: [ReadinessStorage]
    public let models: ReadinessModels
    public let downloads: ReadinessDownloads
    public let usage: UsageReadiness
    public let readyForInference: Bool
}

public struct ModelSelfTestResult: Codable, Equatable, Sendable {
    public struct Usage: Codable, Equatable, Sendable {
        public let promptTokens: Int
        public let completionTokens: Int
        public let totalTokens: Int
    }

    public let schemaVersion: Int
    public let success: Bool
    public let model: String
    public let engine: InferenceEngine
    public let releaseTier: String
    public let endpoint: String
    public let vision: Bool
    public let responsePreview: String?
    public let responseMs: Double
    public let usage: Usage?
    public let usageRecorded: Bool?
    public let usageDelivery: UsageReadiness

    public var completesGuidedSetup: Bool {
        success && usage != nil && usageRecorded == true
    }
}

struct ModelSelfTestRequest: Codable, Equatable, Sendable {
    let model: String
    let includeVision: Bool
    let unloadAfter: Bool
}
