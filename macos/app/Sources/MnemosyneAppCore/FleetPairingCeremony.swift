import Foundation

/// The only secret-bearing payload sent by the signed menu app during pairing.
///
/// The value deliberately provides redacted descriptions and reflection so an
/// accidental diagnostic does not reveal the invitation secret. Its custom
/// encoder is the sole place where that secret is exposed, directly into the
/// bounded loopback control request body.
public struct FleetPairingControlRequest: Encodable, Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public static let supportedSchemaVersion = 1

    public let schemaVersion: Int
    public let invitationID: String
    public let hubOrigin: String
    public let locator: String
    private let pairingSecret: String

    public init(
        schemaVersion: Int = FleetPairingControlRequest.supportedSchemaVersion,
        invitationID: String,
        pairingSecret: String,
        hubOrigin: String,
        locator: String
    ) {
        self.schemaVersion = schemaVersion
        self.invitationID = invitationID
        self.pairingSecret = pairingSecret
        self.hubOrigin = hubOrigin
        self.locator = locator
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion
        case invitationID
        case pairingSecret
        case hubOrigin
        case locator
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(schemaVersion, forKey: .schemaVersion)
        try values.encode(invitationID, forKey: .invitationID)
        try values.encode(pairingSecret, forKey: .pairingSecret)
        try values.encode(hubOrigin, forKey: .hubOrigin)
        try values.encode(locator, forKey: .locator)
    }

    public static func == (
        lhs: FleetPairingControlRequest,
        rhs: FleetPairingControlRequest
    ) -> Bool {
        lhs.schemaVersion == rhs.schemaVersion
            && lhs.invitationID == rhs.invitationID
            && lhs.pairingSecret == rhs.pairingSecret
            && lhs.hubOrigin == rhs.hubOrigin
            && lhs.locator == rhs.locator
    }

    public var description: String {
        "FleetPairingControlRequest(schemaVersion: \(schemaVersion), "
            + "invitationID: \(invitationID), pairingSecret: <redacted>, "
            + "hubOrigin: \(hubOrigin), locator: <redacted>)"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(
            self,
            children: [
                "schemaVersion": schemaVersion,
                "invitationID": invitationID,
                "pairingSecret": "<redacted>",
                "hubOrigin": hubOrigin,
                "locator": "<redacted>",
            ],
            displayStyle: .struct
        )
    }
}

/// Secret-free request for a short-code ceremony. The locator is redacted from
/// diagnostics because it is transport metadata, not browser-facing state.
public struct FleetPairingPresenceRequest: Encodable, Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public static let supportedSchemaVersion = 1

    public let schemaVersion: Int
    public let requestID: String
    public let hubOrigin: String
    public let transport: String
    private let locator: String

    public init(
        schemaVersion: Int = supportedSchemaVersion,
        requestID: String,
        hubOrigin: String,
        locator: String,
        transport: String = "tailscale"
    ) {
        self.schemaVersion = schemaVersion
        self.requestID = requestID
        self.hubOrigin = hubOrigin
        self.locator = locator
        self.transport = transport
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion
        case requestID
        case hubOrigin
        case locator
        case transport
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(schemaVersion, forKey: .schemaVersion)
        try values.encode(requestID, forKey: .requestID)
        try values.encode(hubOrigin, forKey: .hubOrigin)
        try values.encode(locator, forKey: .locator)
        try values.encode(transport, forKey: .transport)
    }

    public var description: String {
        "FleetPairingPresenceRequest(schemaVersion: \(schemaVersion), "
            + "requestID: \(requestID), hubOrigin: \(hubOrigin), "
            + "locator: <redacted>, transport: \(transport))"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(
            self,
            children: [
                "schemaVersion": schemaVersion,
                "requestID": requestID,
                "hubOrigin": hubOrigin,
                "locator": "<redacted>",
                "transport": transport,
            ],
            displayStyle: .struct
        )
    }
}

/// The loopback service returns this hidden invitation only to the signed app.
/// UI code displays the PIN and retains the strong secret only in memory.
public struct FleetPairingPresenceResponse: Decodable, Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public let schemaVersion: Int
    public let presencePIN: String
    public let expiresAt: Double
    public let invitationID: String
    public let hubOrigin: String
    public let locator: String
    private let pairingSecret: String

    public init(
        schemaVersion: Int = 1,
        presencePIN: String,
        expiresAt: Double,
        invitationID: String,
        pairingSecret: String,
        hubOrigin: String,
        locator: String
    ) {
        self.schemaVersion = schemaVersion
        self.presencePIN = presencePIN
        self.expiresAt = expiresAt
        self.invitationID = invitationID
        self.pairingSecret = pairingSecret
        self.hubOrigin = hubOrigin
        self.locator = locator
    }

    public func pairingControlRequest() -> FleetPairingControlRequest {
        FleetPairingControlRequest(
            invitationID: invitationID,
            pairingSecret: pairingSecret,
            hubOrigin: hubOrigin,
            locator: locator
        )
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case presencePIN = "presence_pin"
        case expiresAt = "expires_at"
        case invitationID = "invitation_id"
        case pairingSecret = "pairing_secret"
        case hubOrigin = "hub_origin"
        case locator
    }

    public var description: String {
        "FleetPairingPresenceResponse(schemaVersion: \(schemaVersion), "
            + "presencePIN: <redacted>, invitationID: \(invitationID), "
            + "pairingSecret: <redacted>, hubOrigin: \(hubOrigin), "
            + "locator: <redacted>)"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(
            self,
            children: [
                "schemaVersion": schemaVersion,
                "presencePIN": "<redacted>",
                "invitationID": invitationID,
                "pairingSecret": "<redacted>",
                "hubOrigin": hubOrigin,
                "locator": "<redacted>",
            ],
            displayStyle: .struct
        )
    }
}

/// Secret-free progress journal returned by the local control service.
public struct FleetPairingWorkflowSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int?
    public let available: Bool
    public let phase: String?
    public let attemptID: String?
    public let invitationID: String?
    public let claimRequestID: String?
    public let provisionRequestID: String?
    public let activationRequestID: String?
    public let claimID: String?
    public let pairingID: String?
    public let reportingNodeID: String?
    public let credentialGeneration: Int?
    public let expiresAt: Double?
    public let lastErrorCode: String?
    public let updatedAt: Double?

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case available
        case phase
        case attemptID = "attempt_id"
        case invitationID = "invitation_id"
        case claimRequestID = "claim_request_id"
        case provisionRequestID = "provision_request_id"
        case activationRequestID = "activation_request_id"
        case claimID = "claim_id"
        case pairingID = "pairing_id"
        case reportingNodeID = "reporting_node_id"
        case credentialGeneration = "credential_generation"
        case expiresAt = "expires_at"
        case lastErrorCode = "last_error_code"
        case updatedAt = "updated_at"
    }
}

/// Result of beginning or resuming the local, restart-safe pairing workflow.
public struct FleetPairingOperationResponse: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let accepted: Bool
    public let nextAction: String?
    public let workflow: FleetPairingWorkflowSnapshot?
    public let pairing: FleetPairingSnapshot?

    public var effectiveWorkflow: FleetPairingWorkflowSnapshot? {
        workflow ?? pairing?.workflow
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case accepted
        case nextAction = "next_action"
        case workflow
        case pairing
    }
}

/// Idempotent request to revoke this Mac's dynamic Hub enrollment.
public struct FleetPairingManagementRequest: Codable, Equatable, Sendable {
    public static let supportedSchemaVersion = 1

    public let schemaVersion: Int
    public let requestID: String

    public init(
        schemaVersion: Int = supportedSchemaVersion,
        requestID: String
    ) {
        self.schemaVersion = schemaVersion
        self.requestID = requestID
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case requestID = "request_id"
    }
}

public struct FleetPairingManagementResult: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let state: String
    public let pairingID: String?
    public let reportingNodeID: String?
    public let credentialGeneration: Int?

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case state
        case pairingID = "pairing_id"
        case reportingNodeID = "reporting_node_id"
        case credentialGeneration = "credential_generation"
    }
}

public struct FleetPairingManagementResponse: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let result: FleetPairingManagementResult
    public let pairing: FleetPairingSnapshot

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case result
        case pairing
    }
}

public enum FleetPairingCeremonyOperation: String, Equatable, Sendable {
    case begin
    case resume
}

public struct FleetPairingCeremonySubmission: Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public let operation: FleetPairingCeremonyOperation
    public let request: FleetPairingControlRequest

    public var description: String {
        "FleetPairingCeremonySubmission(operation: \(operation.rawValue), "
            + "request: \(request))"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(
            self,
            children: [
                "operation": operation.rawValue,
                "request": request.description,
            ],
            displayStyle: .struct
        )
    }
}

public enum FleetPairingCeremonyStage: String, Equatable, Sendable {
    case collecting
    case submitting
    case awaitingApproval
    case activating
    case paired
    case blocked
    case failed
}

public enum FleetPairingCeremonyFailure: Equatable, Sendable {
    case localServiceUnavailable
    case invitationRejected
    case invitationExpired
    case staticCredentialsPresent
    case stateConflict
    case invalidResponse
}

/// A fixed-code pairing failure. The server's free-form message is
/// deliberately not retained or surfaced by the app.
public struct FleetPairingAPIError: Error, Equatable, LocalizedError, Sendable {
    public let statusCode: Int
    public let code: String
    public let retryable: Bool

    public init(statusCode: Int, code: String, retryable: Bool) {
        self.statusCode = statusCode
        self.code = Self.allowedCodes.contains(code)
            ? code : "pairing_invalid_response"
        self.retryable = retryable
    }

    public var errorDescription: String? {
        switch code {
        case "pairing_static_credentials_present":
            "Existing static Fleet credentials require explicit migration."
        case "pairing_expired":
            "The pairing invitation expired."
        case "pairing_remote_attempt_terminal":
            "The Hub confirmed that this pairing attempt is no longer active."
        case "pairing_claim_rejected", "pairing_activation_rejected":
            "The Hub rejected this pairing request."
        case "pairing_hub_unavailable":
            "The Hub is temporarily unavailable."
        case "pairing_management_outcome_unknown":
            "The Hub may have accepted the removal. Retry the exact request."
        case "pairing_management_rejected":
            "The Hub rejected this enrollment-management request."
        case "pairing_local_control_required":
            "Pairing requires this Mac's loopback control service."
        case "pairing_invalid_response":
            "The pairing service returned an invalid response."
        default:
            "The pairing request could not continue."
        }
    }

    private static let allowedCodes: Set<String> = [
        "pairing_no_attempt",
        "pairing_payload_mismatch",
        "pairing_static_credentials_present",
        "pairing_local_identity_invalid",
        "pairing_state_conflict",
        "pairing_hub_unavailable",
        "pairing_hub_redirect_refused",
        "pairing_hub_response_too_large",
        "pairing_hub_response_invalid",
        "pairing_claim_rejected",
        "pairing_approval_pending",
        "pairing_remote_attempt_terminal",
        "pairing_activation_rejected",
        "pairing_management_rejected",
        "pairing_management_outcome_unknown",
        "pairing_expired",
        "pairing_local_control_required",
        "pairing_invalid_response",
    ]
}

public enum FleetPairingCeremonyInputError: Error, Equatable, LocalizedError {
    case incompleteInvitation

    public var errorDescription: String? {
        "Enter a valid invitation ID, pairing secret, Hub HTTPS origin, and this Mac's pool address."
    }
}

/// Ephemeral state for the Settings pairing ceremony.
///
/// No invitation material is written to disk. Once submitted, the visible
/// secret field is emptied and the complete payload is retained privately only
/// so the same open Settings view can resume after Hub approval. `cancel()` and
/// `viewDidDisappear()` discard every copy owned by this state machine.
public struct FleetPairingCeremonyState: Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public private(set) var stage: FleetPairingCeremonyStage = .collecting
    public private(set) var invitationID = ""
    public private(set) var hubOrigin = ""
    public private(set) var locator = ""
    public private(set) var workflowPhase: String?
    public private(set) var statusText = "Not paired"
    public private(set) var nextActionText =
        "Enter the invitation details created by your Hub administrator."

    private var pairingSecretEntry = ""
    private var retainedRequest: FleetPairingControlRequest?
    private var durableAttemptExists = false
    private var presenceCeremony = false

    public init() {}

    public var pairingSecretForSecureEntry: String { pairingSecretEntry }

    public var hasSecretInMemory: Bool {
        !pairingSecretEntry.isEmpty || retainedRequest != nil
    }

    public var isPresenceCeremony: Bool { presenceCeremony }

    public var showsInvitationEntry: Bool {
        retainedRequest == nil && stage != .paired && stage != .blocked
    }

    public var canSubmit: Bool {
        guard showsInvitationEntry,
              canonicalInvitationID(invitationID),
              validPairingSecret(pairingSecretEntry),
              validOrigin(hubOrigin, schemes: ["https"]),
              validOrigin(locator, schemes: ["http", "https"])
        else {
            return false
        }
        return true
    }

    public var canResumeWithoutReentry: Bool {
        retainedRequest != nil && stage != .submitting && stage != .paired
    }

    public mutating func setInvitationID(_ value: String) {
        guard showsInvitationEntry else { return }
        invitationID = value
    }

    public mutating func setPairingSecret(_ value: String) {
        guard showsInvitationEntry else { return }
        pairingSecretEntry = value
    }

    public mutating func setHubOrigin(_ value: String) {
        guard showsInvitationEntry else { return }
        hubOrigin = value
    }

    public mutating func setLocator(_ value: String) {
        guard showsInvitationEntry else { return }
        locator = value
    }

    public mutating func prepareSubmission() throws
        -> FleetPairingCeremonySubmission
    {
        let request: FleetPairingControlRequest
        if let retainedRequest {
            request = retainedRequest
        } else {
            guard canSubmit else {
                throw FleetPairingCeremonyInputError.incompleteInvitation
            }
            request = FleetPairingControlRequest(
                invitationID: invitationID.lowercased(),
                pairingSecret: pairingSecretEntry,
                hubOrigin: canonicalOrigin(hubOrigin),
                locator: canonicalOrigin(locator)
            )
            self.retainedRequest = request
            invitationID.removeAll(keepingCapacity: false)
            pairingSecretEntry.removeAll(keepingCapacity: false)
            hubOrigin.removeAll(keepingCapacity: false)
            locator.removeAll(keepingCapacity: false)
        }

        let operation: FleetPairingCeremonyOperation = durableAttemptExists
            ? .resume : .begin
        stage = .submitting
        statusText = operation == .begin ? "Submitting claim" : "Resuming pairing"
        nextActionText = "Wait for the local service and Hub to respond."
        return FleetPairingCeremonySubmission(
            operation: operation,
            request: request
        )
    }

    public mutating func preparePresenceSubmission(
        _ response: FleetPairingPresenceResponse
    ) throws -> FleetPairingCeremonySubmission {
        clearInvitationMaterial()
        retainedRequest = response.pairingControlRequest()
        presenceCeremony = true
        durableAttemptExists = false
        stage = .collecting
        return try prepareSubmission()
    }

    public mutating func apply(_ response: FleetPairingOperationResponse) {
        durableAttemptExists = true
        if response.pairing?.state == "paired"
            || response.effectiveWorkflow?.phase == "complete"
        {
            complete()
            return
        }
        let phase = response.effectiveWorkflow?.phase
        workflowPhase = phase
        if response.nextAction == "resume_after_approval"
            || phase == "awaiting_approval"
        {
            stage = .awaitingApproval
            statusText = "Waiting for Hub approval"
            nextActionText = presenceCeremony
                ? "Enter the displayed code in the Hub. This Mac will finish automatically."
                : "Approve this Mac in the Hub, then return here and choose Resume Pairing."
        } else {
            stage = .activating
            statusText = phaseLabel(phase)
            nextActionText = "Resume this exact pairing attempt if it does not complete."
        }
    }

    public mutating func synchronize(with snapshot: FleetPairingSnapshot) {
        if snapshot.state == "paired" {
            complete()
            return
        }
        if snapshot.legacyCredentialsPresent == true {
            clearInvitationMaterial()
            durableAttemptExists = false
            stage = .blocked
            workflowPhase = snapshot.workflow?.phase
            statusText = "Static enrollment configured"
            nextActionText =
                "Migrate or remove the existing static Fleet enrollment before pairing with the Hub."
            return
        }
        if !snapshot.available || snapshot.workflow?.available == false {
            clearInvitationMaterial()
            stage = .blocked
            workflowPhase = snapshot.workflow?.phase
            statusText = "Pairing unavailable"
            nextActionText =
                "Update or repair the local service before starting a pairing ceremony."
            return
        }

        if snapshot.state == "revoked" {
            clearInvitationMaterial()
            workflowPhase = snapshot.workflow?.phase
            if snapshot.selfRevoke != nil {
                durableAttemptExists = true
                stage = .blocked
                statusText = "Hub removal needs confirmation"
                nextActionText =
                    "Retry Removal to finish the exact durable request. Pooled routing remains denied locally."
            } else {
                durableAttemptExists = false
                stage = .collecting
                statusText = "Enrollment removed"
                nextActionText =
                    "Use a new Hub invitation whenever you want to enroll this Mac again."
            }
            return
        }

        let phase = snapshot.workflow?.phase
        workflowPhase = phase
        durableAttemptExists = phase != nil || snapshot.state == "pending"
        if durableAttemptExists {
            if snapshot.workflow?.lastErrorCode
                == "pairing_remote_attempt_terminal"
            {
                stage = .failed
                statusText = "Pairing attempt expired"
                nextActionText =
                    "Discard this stale attempt, then request a new pairing code."
            } else if phase == "awaiting_approval" {
                stage = .awaitingApproval
                statusText = "Waiting for Hub approval"
            } else {
                stage = .activating
                statusText = phaseLabel(phase)
            }
            if retainedRequest == nil {
                nextActionText =
                    "Re-enter the original invitation details to resume; this app does not store them."
            } else if presenceCeremony && phase == "awaiting_approval" {
                nextActionText =
                    "Enter the displayed code in the Hub. This Mac will finish automatically."
            } else {
                nextActionText = "Resume this exact pairing attempt."
            }
        } else if snapshot.state == "recovery_required" {
            stage = .failed
            statusText = "Pairing needs recovery"
            nextActionText =
                "Re-enter the original invitation details to retry the durable attempt."
        } else {
            stage = .collecting
            statusText = "Not paired"
            nextActionText =
                "Enter the invitation details created by your Hub administrator."
        }
    }

    public mutating func recordFailure(_ failure: FleetPairingCeremonyFailure) {
        stage = failure == .staticCredentialsPresent ? .blocked : .failed
        switch failure {
        case .localServiceUnavailable:
            statusText = "Pairing service unavailable"
            nextActionText = "Try the same request again when the service is reachable."
        case .invitationRejected:
            statusText = "Invitation rejected"
            nextActionText = "Verify the invitation details with your Hub administrator."
        case .invitationExpired:
            statusText = "Invitation expired"
            nextActionText = "Ask your Hub administrator to create a new invitation."
        case .staticCredentialsPresent:
            clearInvitationMaterial()
            statusText = "Static enrollment configured"
            nextActionText =
                "Migrate or remove the existing static Fleet enrollment before pairing with the Hub."
        case .stateConflict:
            statusText = "Pairing state changed"
            nextActionText = retainedRequest == nil
                ? "Refresh status, then re-enter the original details if the attempt is still pending."
                : "Refresh status, then resume the same attempt."
        case .invalidResponse:
            statusText = "Invalid pairing response"
            nextActionText = "Update the app and local service, then try again."
        }
    }

    public mutating func cancel() {
        clearInvitationMaterial()
        presenceCeremony = false
        workflowPhase = nil
        durableAttemptExists = false
        stage = .collecting
        statusText = "Pairing details cleared"
        nextActionText =
            "Re-enter the original details to resume any attempt already recorded by the service."
    }

    public mutating func viewDidDisappear() {
        cancel()
    }

    public var description: String {
        "FleetPairingCeremonyState(stage: \(stage.rawValue), "
            + "workflowPhase: \(workflowPhase ?? "none"), "
            + "secretInMemory: <redacted>)"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(
            self,
            children: [
                "stage": stage.rawValue,
                "workflowPhase": workflowPhase ?? "none",
                "secretInMemory": "<redacted>",
            ],
            displayStyle: .struct
        )
    }

    private mutating func complete() {
        let completedWithPresence = presenceCeremony
        clearInvitationMaterial()
        durableAttemptExists = true
        workflowPhase = "complete"
        stage = .paired
        statusText = "Paired with Hub"
        nextActionText = completedWithPresence
            ? "This Mac is enrolled. The Hub is finishing enablement and model publication."
            : "The Hub must enable this Mac before it can receive pooled requests."
    }

    private mutating func clearInvitationMaterial() {
        invitationID.removeAll(keepingCapacity: false)
        pairingSecretEntry.removeAll(keepingCapacity: false)
        hubOrigin.removeAll(keepingCapacity: false)
        locator.removeAll(keepingCapacity: false)
        retainedRequest = nil
    }
}

private func canonicalInvitationID(_ value: String) -> Bool {
    let normalized = value.lowercased()
    guard value == value.trimmingCharacters(in: .whitespacesAndNewlines),
          value.count == 36,
          let parsed = UUID(uuidString: value)
    else {
        return false
    }
    return parsed.uuidString.lowercased() == normalized
}

private func validPairingSecret(_ value: String) -> Bool {
    value == value.trimmingCharacters(in: .whitespacesAndNewlines)
        && (32 ... 4_096).contains(value.count)
        && !value.contains(where: { "\r\n\0".contains($0) })
}

private func validOrigin(_ value: String, schemes: Set<String>) -> Bool {
    guard value == value.trimmingCharacters(in: .whitespacesAndNewlines),
          value.count <= 2_048,
          let components = URLComponents(string: value),
          let scheme = components.scheme?.lowercased(),
          schemes.contains(scheme),
          components.host != nil,
          components.user == nil,
          components.password == nil,
          components.query == nil,
          components.fragment == nil,
          components.path.isEmpty || components.path == "/"
    else {
        return false
    }
    return true
}

private func canonicalOrigin(_ value: String) -> String {
    var components = URLComponents(string: value)!
    components.path = ""
    return components.string ?? value
}

private func phaseLabel(_ phase: String?) -> String {
    switch phase {
    case "claiming": "Submitting claim"
    case "awaiting_approval": "Waiting for Hub approval"
    case "staging": "Installing paired credentials"
    case "activation_pending": "Verifying this Mac"
    case "complete": "Paired with Hub"
    default: "Pairing in progress"
    }
}
