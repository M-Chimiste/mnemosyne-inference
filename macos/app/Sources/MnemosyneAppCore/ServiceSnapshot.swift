import Foundation

public struct TokenSidecarSnapshot: Codable, Equatable, Sendable {
    public let enabled: Bool?
    public let nodeId: String?
    public let nodeIdSource: String?
    public let outboxDepth: Int?
    public let lastFlushAt: Double?
    public let writerReady: Bool?
    public let lastError: String?

    public init(
        enabled: Bool?,
        nodeId: String?,
        nodeIdSource: String?,
        outboxDepth: Int?,
        lastFlushAt: Double?,
        writerReady: Bool? = nil,
        lastError: String? = nil
    ) {
        self.enabled = enabled
        self.nodeId = nodeId
        self.nodeIdSource = nodeIdSource
        self.outboxDepth = outboxDepth
        self.lastFlushAt = lastFlushAt
        self.writerReady = writerReady
        self.lastError = lastError
    }

    enum CodingKeys: String, CodingKey {
        case enabled
        case nodeId = "node_id"
        case nodeIdSource = "node_id_source"
        case outboxDepth = "outbox_depth"
        case lastFlushAt = "last_flush_at"
        case writerReady = "writer_ready"
        case lastError = "last_error"
    }
}

public struct ModelPerformanceSnapshot: Codable, Equatable, Sendable {
    public let alias: String
    public let engine: String
    public let requests: Int
    public let errors: Int
    public let coldStarts: Int
    public let averageAdmissionMs: Double?
    public let averageFirstByteMs: Double?
    public let averageTotalMs: Double?
    public let averageOutputTokensPerSecond: Double?
    public let p50TotalMs: Double?
    public let p95TotalMs: Double?

    enum CodingKeys: String, CodingKey {
        case alias
        case engine
        case requests
        case errors
        case coldStarts = "cold_starts"
        case averageAdmissionMs = "average_admission_ms"
        case averageFirstByteMs = "average_first_byte_ms"
        case averageTotalMs = "average_total_ms"
        case averageOutputTokensPerSecond = "average_output_tokens_per_second"
        case p50TotalMs = "p50_total_ms"
        case p95TotalMs = "p95_total_ms"
    }
}

public struct PerformanceSnapshot: Codable, Equatable, Sendable {
    public let windowLimit: Int
    public let sampleCount: Int
    public let oldestObservedAt: Double?
    public let newestObservedAt: Double?
    public let byModel: [ModelPerformanceSnapshot]

    enum CodingKeys: String, CodingKey {
        case windowLimit = "window_limit"
        case sampleCount = "sample_count"
        case oldestObservedAt = "oldest_observed_at"
        case newestObservedAt = "newest_observed_at"
        case byModel = "by_model"
    }
}

/// Deliberately small view of `/manager/status`.
///
/// Every field is optional so the menu app remains compatible while the native
/// service grows its status payload. Unknown fields are ignored by `Codable`.
public struct ServiceSnapshot: Codable, Equatable, Sendable {
    public let status: String?
    public let residentAlias: String?
    public let residentModel: String?
    public let residentEngine: String?
    public let inFlightRequests: Int?
    public let tokenSidecar: TokenSidecarSnapshot?
    public let performance: PerformanceSnapshot?
    public let diagnostic: String?
    public let startupError: String?

    public init(
        status: String?,
        residentAlias: String?,
        residentModel: String?,
        residentEngine: String?,
        inFlightRequests: Int?,
        tokenSidecar: TokenSidecarSnapshot?,
        performance: PerformanceSnapshot? = nil,
        diagnostic: String? = nil,
        startupError: String? = nil
    ) {
        self.status = status
        self.residentAlias = residentAlias
        self.residentModel = residentModel
        self.residentEngine = residentEngine
        self.inFlightRequests = inFlightRequests
        self.tokenSidecar = tokenSidecar
        self.performance = performance
        self.diagnostic = diagnostic
        self.startupError = startupError
    }

    enum CodingKeys: String, CodingKey {
        case status
        case residentAlias = "resident_alias"
        case residentModel = "resident_model"
        case residentEngine = "resident_engine"
        case inFlightRequests = "in_flight_requests"
        case tokenSidecar = "token_sidecar"
        case performance
        case diagnostic
        case startupError = "startup_error"
    }
}
