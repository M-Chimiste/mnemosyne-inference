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
    public let diagnostic: String?
    public let startupError: String?

    public init(
        status: String?,
        residentAlias: String?,
        residentModel: String?,
        residentEngine: String?,
        inFlightRequests: Int?,
        tokenSidecar: TokenSidecarSnapshot?,
        diagnostic: String? = nil,
        startupError: String? = nil
    ) {
        self.status = status
        self.residentAlias = residentAlias
        self.residentModel = residentModel
        self.residentEngine = residentEngine
        self.inFlightRequests = inFlightRequests
        self.tokenSidecar = tokenSidecar
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
        case diagnostic
        case startupError = "startup_error"
    }
}
