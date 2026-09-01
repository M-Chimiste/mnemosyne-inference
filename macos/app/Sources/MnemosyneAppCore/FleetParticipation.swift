import Foundation

/// The local Mac's durable participation state in the Mnemosyne pool.
///
/// Pausing pool participation does not unregister the Mac or stop its local
/// inference service. A draining Mac finishes requests that Fleet already
/// routed to it before becoming paused.
public struct FleetParticipationSnapshot: Codable, Equatable, Sendable {
    public let enabled: Bool
    public let state: String
    public let activeRequests: Int
    public let updatedAt: Double

    public init(
        enabled: Bool,
        state: String,
        activeRequests: Int,
        updatedAt: Double
    ) {
        self.enabled = enabled
        self.state = state
        self.activeRequests = activeRequests
        self.updatedAt = updatedAt
    }

    private enum CodingKeys: String, CodingKey {
        case enabled
        case state
        case activeRequests = "active_requests"
        case updatedAt = "updated_at"
    }
}

public struct SetFleetParticipationRequest: Codable, Equatable, Sendable {
    public let enabled: Bool

    public init(enabled: Bool) {
        self.enabled = enabled
    }
}

/// Secret-free enrollment state returned by the local control service.
///
/// Pairing identity is deliberately separate from participation: a paired Mac
/// can be paused, and an unpaired Mac continues serving ordinary local calls.
public struct FleetPairingSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let available: Bool
    public let state: String
    public let deviceId: String?
    public let pairingId: String?
    public let reportingNodeId: String?
    public let credentialGeneration: Int?
    public let credentialsConfigured: Bool?
    public let legacyCredentialsPresent: Bool?
    public let lastErrorCode: String?
    public let updatedAt: Double?
    public let pairedAt: Double?
    public let revokedAt: Double?
    public let selfRevoke: FleetPairingSelfRevokeSnapshot?
    public let workflow: FleetPairingWorkflowSnapshot?

    public var permitsParticipationControl: Bool {
        state == "paired" || legacyCredentialsPresent == true
    }

    /// A conclusive Hub refusal before claim creation is the only pending
    /// ceremony that can be safely discarded without remote coordination.
    public var canDiscardRejectedAttempt: Bool {
        state == "pending"
            && credentialsConfigured != true
            && pairingId == nil
            && workflow?.phase == "claiming"
            && workflow?.claimID == nil
            && workflow?.pairingID == nil
            && workflow?.credentialGeneration == nil
            && workflow?.lastErrorCode == "pairing_claim_rejected"
    }

    /// Whether the pairing transaction, rather than the generic credential
    /// editor, owns the Fleet snapshot and dispatch credential slots.
    ///
    /// A pending or degraded transaction owns those slots before or while its
    /// durable credential write is being reconciled. Revocation retains that
    /// ownership until the pairing is explicitly forgotten. The explicit
    /// legacy flag keeps manually configured static nodes editable.
    public var ownsFleetCredentials: Bool {
        guard legacyCredentialsPresent != true else { return false }
        if credentialsConfigured == true {
            return true
        }
        return ["pending", "paired", "recovery_required", "revoked"]
            .contains(state)
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case available
        case state
        case deviceId = "device_id"
        case pairingId = "pairing_id"
        case reportingNodeId = "reporting_node_id"
        case credentialGeneration = "credential_generation"
        case credentialsConfigured = "credentials_configured"
        case legacyCredentialsPresent = "legacy_credentials_present"
        case lastErrorCode = "last_error_code"
        case updatedAt = "updated_at"
        case pairedAt = "paired_at"
        case revokedAt = "revoked_at"
        case selfRevoke = "self_revoke"
        case workflow
    }
}

/// Secret-free identity of an in-flight or completed local self-revocation.
///
/// The request ID is intentionally returned by the loopback service so the
/// menu app can replay the exact operation after either process restarts. It
/// is not a Fleet credential and grants no remote authority.
public struct FleetPairingSelfRevokeSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let requestID: String
    public let phase: String

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case requestID = "request_id"
        case phase
    }
}
