import Foundation

public struct TokenSidecarSnapshot: Codable, Equatable, Sendable {
    public let enabled: Bool?
    public let nodeId: String?
    public let nodeIdSource: String?
    public let outboxDepth: Int?
    public let lastFlushAt: Double?

    public init(
        enabled: Bool?,
        nodeId: String?,
        nodeIdSource: String?,
        outboxDepth: Int?,
        lastFlushAt: Double?
    ) {
        self.enabled = enabled
        self.nodeId = nodeId
        self.nodeIdSource = nodeIdSource
        self.outboxDepth = outboxDepth
        self.lastFlushAt = lastFlushAt
    }

    enum CodingKeys: String, CodingKey {
        case enabled
        case nodeId = "node_id"
        case nodeIdSource = "node_id_source"
        case outboxDepth = "outbox_depth"
        case lastFlushAt = "last_flush_at"
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

    public init(
        status: String?,
        residentAlias: String?,
        residentModel: String?,
        residentEngine: String?,
        inFlightRequests: Int?,
        tokenSidecar: TokenSidecarSnapshot?
    ) {
        self.status = status
        self.residentAlias = residentAlias
        self.residentModel = residentModel
        self.residentEngine = residentEngine
        self.inFlightRequests = inFlightRequests
        self.tokenSidecar = tokenSidecar
    }

    enum CodingKeys: String, CodingKey {
        case status
        case residentAlias = "resident_alias"
        case residentModel = "resident_model"
        case residentEngine = "resident_engine"
        case inFlightRequests = "in_flight_requests"
        case tokenSidecar = "token_sidecar"
    }
}
