import Foundation

public enum LifecycleExecutionProtocolError: Error, Equatable, Sendable {
    case malformed
    case oversized
    case invalidAuthority
}

public enum LifecycleExecutionDirectionV2: String, Codable, Sendable {
    case forward
    case rollback
    case manualRecovery = "manual_recovery"
}

public enum LifecycleExecutionMessageTypeV2: String, Codable, Sendable {
    case register
    case registered
    case start
    case observe
    case apply
    case finalize
    case refused
}

public enum LifecycleEffectKindV2: String, Codable, Sendable {
    case preflightCandidate = "preflight_candidate"
    case drainInference = "drain_inference"
    case captureRollback = "capture_rollback"
    case stopPredecessor = "stop_predecessor"
    case installCandidate = "install_candidate"
    case startCandidate = "start_candidate"
    case validateCandidate = "validate_candidate"
    case commitCandidate = "commit_candidate"
    case restorePredecessor = "restore_predecessor"
    case quiesceService = "quiesce_service"
    case resolveOutbox = "resolve_outbox"
    case resolvePairing = "resolve_pairing"
    case resolveExclusiveWeight = "resolve_exclusive_weight"
    case resolveRuntimeMember = "resolve_runtime_member"
    case unregisterAgent = "unregister_agent"
    case unregisterLoginItem = "unregister_login_item"
    case resolveStateMember = "resolve_state_member"
    case quarantineApplication = "quarantine_application"
    case removeApplication = "remove_application"
    case finalizeUninstall = "finalize_uninstall"
    case cleanupRecoveryClone = "cleanup_recovery_clone"
}

public enum LifecycleEffectObservationV2: String, Codable, Sendable {
    case needsAction = "needs_action"
    case effectSatisfied = "effect_satisfied"
    case retryableNotReady = "retryable_not_ready"
    case conflict
    case unavailable
}

public enum LifecycleEffectReceiptStatusV2: String, Codable, Sendable {
    case observed
    case applyStarted = "apply_started"
    case applied
    case finalized
    case refused
}

public struct LifecycleRunnerRegistrationV2: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let messageType: LifecycleExecutionMessageTypeV2
    public let transactionID: String
    public let grantID: String
    public let grantDigest: String
    public let runnerSessionID: String
    public let sequence: Int
    public let nonce: String
    public let runnerIdentifier: String
    public let runnerBuildDigest: String
    public let runnerIdentityDigest: String
    public let teamIdentifier: String
    public let codeRequirementDigest: String
    public let requestedLeaseSeconds: Int

    public init(
        protocolVersion: Int = 2,
        messageType: LifecycleExecutionMessageTypeV2 = .register,
        transactionID: String,
        grantID: String,
        grantDigest: String,
        runnerSessionID: String,
        sequence: Int,
        nonce: String,
        runnerIdentifier: String = LifecycleExecutionProtocolV2.runnerIdentifier,
        runnerBuildDigest: String,
        runnerIdentityDigest: String,
        teamIdentifier: String,
        codeRequirementDigest: String,
        requestedLeaseSeconds: Int
    ) {
        self.protocolVersion = protocolVersion
        self.messageType = messageType
        self.transactionID = transactionID
        self.grantID = grantID
        self.grantDigest = grantDigest
        self.runnerSessionID = runnerSessionID
        self.sequence = sequence
        self.nonce = nonce
        self.runnerIdentifier = runnerIdentifier
        self.runnerBuildDigest = runnerBuildDigest
        self.runnerIdentityDigest = runnerIdentityDigest
        self.teamIdentifier = teamIdentifier
        self.codeRequirementDigest = codeRequirementDigest
        self.requestedLeaseSeconds = requestedLeaseSeconds
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion = "protocol_version"
        case messageType = "message_type"
        case transactionID = "transaction_id"
        case grantID = "grant_id"
        case grantDigest = "grant_digest"
        case runnerSessionID = "runner_session_id"
        case sequence
        case nonce
        case runnerIdentifier = "runner_identifier"
        case runnerBuildDigest = "runner_build_digest"
        case runnerIdentityDigest = "runner_identity_digest"
        case teamIdentifier = "team_identifier"
        case codeRequirementDigest = "code_requirement_digest"
        case requestedLeaseSeconds = "requested_lease_seconds"
    }
}

public struct LifecycleRunnerRegisteredV2: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let messageType: LifecycleExecutionMessageTypeV2
    public let transactionID: String
    public let grantID: String
    public let grantDigest: String
    public let runnerSessionID: String
    public let sequence: Int
    public let nonce: String
    public let requestNonce: String
    public let leaseID: String
    public let leaseEpoch: Int
    public let leaseExpiresAt: Int

    public init(
        protocolVersion: Int = 2,
        messageType: LifecycleExecutionMessageTypeV2 = .registered,
        transactionID: String,
        grantID: String,
        grantDigest: String,
        runnerSessionID: String,
        sequence: Int,
        nonce: String,
        requestNonce: String,
        leaseID: String,
        leaseEpoch: Int,
        leaseExpiresAt: Int
    ) {
        self.protocolVersion = protocolVersion
        self.messageType = messageType
        self.transactionID = transactionID
        self.grantID = grantID
        self.grantDigest = grantDigest
        self.runnerSessionID = runnerSessionID
        self.sequence = sequence
        self.nonce = nonce
        self.requestNonce = requestNonce
        self.leaseID = leaseID
        self.leaseEpoch = leaseEpoch
        self.leaseExpiresAt = leaseExpiresAt
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion = "protocol_version"
        case messageType = "message_type"
        case transactionID = "transaction_id"
        case grantID = "grant_id"
        case grantDigest = "grant_digest"
        case runnerSessionID = "runner_session_id"
        case sequence
        case nonce
        case requestNonce = "request_nonce"
        case leaseID = "lease_id"
        case leaseEpoch = "lease_epoch"
        case leaseExpiresAt = "lease_expires_at"
    }
}

public struct LifecycleExecutionStartV2: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let messageType: LifecycleExecutionMessageTypeV2
    public let transactionID: String
    public let grantID: String
    public let grantDigest: String
    public let runnerSessionID: String
    public let leaseID: String
    public let leaseEpoch: Int
    public let sequence: Int
    public let nonce: String
    public let direction: LifecycleExecutionDirectionV2
    public let executionManifestDigest: String
    public let recoveryCloneIdentityDigest: String
    public let authorizationDigest: String
    public let authorizationSessionID: String

    public init(
        protocolVersion: Int = 2,
        messageType: LifecycleExecutionMessageTypeV2 = .start,
        transactionID: String,
        grantID: String,
        grantDigest: String,
        runnerSessionID: String,
        leaseID: String,
        leaseEpoch: Int,
        sequence: Int,
        nonce: String,
        direction: LifecycleExecutionDirectionV2,
        executionManifestDigest: String,
        recoveryCloneIdentityDigest: String,
        authorizationDigest: String,
        authorizationSessionID: String
    ) {
        self.protocolVersion = protocolVersion
        self.messageType = messageType
        self.transactionID = transactionID
        self.grantID = grantID
        self.grantDigest = grantDigest
        self.runnerSessionID = runnerSessionID
        self.leaseID = leaseID
        self.leaseEpoch = leaseEpoch
        self.sequence = sequence
        self.nonce = nonce
        self.direction = direction
        self.executionManifestDigest = executionManifestDigest
        self.recoveryCloneIdentityDigest = recoveryCloneIdentityDigest
        self.authorizationDigest = authorizationDigest
        self.authorizationSessionID = authorizationSessionID
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion = "protocol_version"
        case messageType = "message_type"
        case transactionID = "transaction_id"
        case grantID = "grant_id"
        case grantDigest = "grant_digest"
        case runnerSessionID = "runner_session_id"
        case leaseID = "lease_id"
        case leaseEpoch = "lease_epoch"
        case sequence
        case nonce
        case direction
        case executionManifestDigest = "execution_manifest_digest"
        case recoveryCloneIdentityDigest = "recovery_clone_identity_digest"
        case authorizationDigest = "authorization_digest"
        case authorizationSessionID = "authorization_session_id"
    }
}

public struct LifecycleExecutionObserveV2: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let messageType: LifecycleExecutionMessageTypeV2
    public let transactionID: String
    public let grantID: String
    public let grantDigest: String
    public let runnerSessionID: String
    public let leaseID: String
    public let leaseEpoch: Int
    public let sequence: Int
    public let nonce: String
    public let effectID: String
    public let effectKind: LifecycleEffectKindV2
    public let targetDigest: String
    public let attempt: Int
    public let priorReceiptDigest: String?

    public init(
        protocolVersion: Int = 2,
        messageType: LifecycleExecutionMessageTypeV2 = .observe,
        transactionID: String,
        grantID: String,
        grantDigest: String,
        runnerSessionID: String,
        leaseID: String,
        leaseEpoch: Int,
        sequence: Int,
        nonce: String,
        effectID: String,
        effectKind: LifecycleEffectKindV2,
        targetDigest: String,
        attempt: Int,
        priorReceiptDigest: String?
    ) {
        self.protocolVersion = protocolVersion
        self.messageType = messageType
        self.transactionID = transactionID
        self.grantID = grantID
        self.grantDigest = grantDigest
        self.runnerSessionID = runnerSessionID
        self.leaseID = leaseID
        self.leaseEpoch = leaseEpoch
        self.sequence = sequence
        self.nonce = nonce
        self.effectID = effectID
        self.effectKind = effectKind
        self.targetDigest = targetDigest
        self.attempt = attempt
        self.priorReceiptDigest = priorReceiptDigest
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion = "protocol_version"
        case messageType = "message_type"
        case transactionID = "transaction_id"
        case grantID = "grant_id"
        case grantDigest = "grant_digest"
        case runnerSessionID = "runner_session_id"
        case leaseID = "lease_id"
        case leaseEpoch = "lease_epoch"
        case sequence
        case nonce
        case effectID = "effect_id"
        case effectKind = "effect_kind"
        case targetDigest = "target_digest"
        case attempt
        case priorReceiptDigest = "prior_receipt_digest"
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(protocolVersion, forKey: .protocolVersion)
        try container.encode(messageType, forKey: .messageType)
        try container.encode(transactionID, forKey: .transactionID)
        try container.encode(grantID, forKey: .grantID)
        try container.encode(grantDigest, forKey: .grantDigest)
        try container.encode(runnerSessionID, forKey: .runnerSessionID)
        try container.encode(leaseID, forKey: .leaseID)
        try container.encode(leaseEpoch, forKey: .leaseEpoch)
        try container.encode(sequence, forKey: .sequence)
        try container.encode(nonce, forKey: .nonce)
        try container.encode(effectID, forKey: .effectID)
        try container.encode(effectKind, forKey: .effectKind)
        try container.encode(targetDigest, forKey: .targetDigest)
        try container.encode(attempt, forKey: .attempt)
        if let priorReceiptDigest {
            try container.encode(priorReceiptDigest, forKey: .priorReceiptDigest)
        } else {
            try container.encodeNil(forKey: .priorReceiptDigest)
        }
    }
}

public struct LifecycleExecutionApplyV2: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let messageType: LifecycleExecutionMessageTypeV2
    public let transactionID: String
    public let grantID: String
    public let grantDigest: String
    public let runnerSessionID: String
    public let leaseID: String
    public let leaseEpoch: Int
    public let sequence: Int
    public let nonce: String
    public let effectID: String
    public let effectKind: LifecycleEffectKindV2
    public let targetDigest: String
    public let attempt: Int
    public let observationReceiptDigest: String

    public init(
        protocolVersion: Int = 2,
        messageType: LifecycleExecutionMessageTypeV2 = .apply,
        transactionID: String,
        grantID: String,
        grantDigest: String,
        runnerSessionID: String,
        leaseID: String,
        leaseEpoch: Int,
        sequence: Int,
        nonce: String,
        effectID: String,
        effectKind: LifecycleEffectKindV2,
        targetDigest: String,
        attempt: Int,
        observationReceiptDigest: String
    ) {
        self.protocolVersion = protocolVersion
        self.messageType = messageType
        self.transactionID = transactionID
        self.grantID = grantID
        self.grantDigest = grantDigest
        self.runnerSessionID = runnerSessionID
        self.leaseID = leaseID
        self.leaseEpoch = leaseEpoch
        self.sequence = sequence
        self.nonce = nonce
        self.effectID = effectID
        self.effectKind = effectKind
        self.targetDigest = targetDigest
        self.attempt = attempt
        self.observationReceiptDigest = observationReceiptDigest
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion = "protocol_version"
        case messageType = "message_type"
        case transactionID = "transaction_id"
        case grantID = "grant_id"
        case grantDigest = "grant_digest"
        case runnerSessionID = "runner_session_id"
        case leaseID = "lease_id"
        case leaseEpoch = "lease_epoch"
        case sequence
        case nonce
        case effectID = "effect_id"
        case effectKind = "effect_kind"
        case targetDigest = "target_digest"
        case attempt
        case observationReceiptDigest = "observation_receipt_digest"
    }
}

public struct LifecycleExecutionFinalizeV2: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let messageType: LifecycleExecutionMessageTypeV2
    public let transactionID: String
    public let grantID: String
    public let grantDigest: String
    public let runnerSessionID: String
    public let leaseID: String
    public let leaseEpoch: Int
    public let sequence: Int
    public let nonce: String
    public let direction: LifecycleExecutionDirectionV2
    public let finalReceiptDigest: String

    public init(
        protocolVersion: Int = 2,
        messageType: LifecycleExecutionMessageTypeV2 = .finalize,
        transactionID: String,
        grantID: String,
        grantDigest: String,
        runnerSessionID: String,
        leaseID: String,
        leaseEpoch: Int,
        sequence: Int,
        nonce: String,
        direction: LifecycleExecutionDirectionV2,
        finalReceiptDigest: String
    ) {
        self.protocolVersion = protocolVersion
        self.messageType = messageType
        self.transactionID = transactionID
        self.grantID = grantID
        self.grantDigest = grantDigest
        self.runnerSessionID = runnerSessionID
        self.leaseID = leaseID
        self.leaseEpoch = leaseEpoch
        self.sequence = sequence
        self.nonce = nonce
        self.direction = direction
        self.finalReceiptDigest = finalReceiptDigest
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion = "protocol_version"
        case messageType = "message_type"
        case transactionID = "transaction_id"
        case grantID = "grant_id"
        case grantDigest = "grant_digest"
        case runnerSessionID = "runner_session_id"
        case leaseID = "lease_id"
        case leaseEpoch = "lease_epoch"
        case sequence
        case nonce
        case direction
        case finalReceiptDigest = "final_receipt_digest"
    }
}

public struct LifecycleExecutionRefusedV2: Codable, Equatable, Sendable {
    public let protocolVersion: Int
    public let messageType: LifecycleExecutionMessageTypeV2
    public let transactionID: String
    public let grantID: String
    public let runnerSessionID: String
    public let sequence: Int
    public let nonce: String
    public let requestNonce: String
    public let errorCode: String

    public init(
        transactionID: String,
        grantID: String,
        runnerSessionID: String,
        sequence: Int,
        nonce: String,
        requestNonce: String,
        errorCode: String
    ) {
        protocolVersion = 2
        messageType = .refused
        self.transactionID = transactionID
        self.grantID = grantID
        self.runnerSessionID = runnerSessionID
        self.sequence = sequence
        self.nonce = nonce
        self.requestNonce = requestNonce
        self.errorCode = errorCode
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion = "protocol_version"
        case messageType = "message_type"
        case transactionID = "transaction_id"
        case grantID = "grant_id"
        case runnerSessionID = "runner_session_id"
        case sequence
        case nonce
        case requestNonce = "request_nonce"
        case errorCode = "error_code"
    }
}

public enum LifecycleExecutionMessageV2: Equatable, Sendable {
    case register(LifecycleRunnerRegistrationV2)
    case registered(LifecycleRunnerRegisteredV2)
    case start(LifecycleExecutionStartV2)
    case observe(LifecycleExecutionObserveV2)
    case apply(LifecycleExecutionApplyV2)
    case finalize(LifecycleExecutionFinalizeV2)
    case refused(LifecycleExecutionRefusedV2)
}

public enum LifecycleRunnerInertAdapterV2 {
    public static let refusalCode = "runner_adapter_unavailable"

    public static func refusal(
        for message: LifecycleExecutionMessageV2,
        refusalNonce: String
    ) throws -> LifecycleExecutionMessageV2 {
        try LifecycleExecutionProtocolV2.validate(message)
        guard case let .register(registration) = message else {
            throw LifecycleExecutionProtocolError.invalidAuthority
        }
        let refusal = LifecycleExecutionRefusedV2(
            transactionID: registration.transactionID,
            grantID: registration.grantID,
            runnerSessionID: registration.runnerSessionID,
            sequence: registration.sequence,
            nonce: refusalNonce,
            requestNonce: registration.nonce,
            errorCode: refusalCode
        )
        let result = LifecycleExecutionMessageV2.refused(refusal)
        try LifecycleExecutionProtocolV2.validate(result)
        return result
    }

    public static func refusalFrame(
        for requestFrame: Data,
        refusalNonce: String
    ) throws -> Data {
        let request = try LifecycleExecutionProtocolV2.decodeFrame(requestFrame)
        return try LifecycleExecutionProtocolV2.encodeFrame(
            refusal(for: request, refusalNonce: refusalNonce)
        )
    }
}

public enum LifecycleExecutionProtocolV2 {
    public static let protocolVersion = 2
    public static let runnerIdentifier = "com.mnemosyne.inference.lifecycle-runner"
    public static let maximumJSONBytes = 32 * 1024
    public static let maximumFrameBytes = maximumJSONBytes + 4
    public static let maximumLeaseSeconds = 60

    private static let refusalCodes: Set<String> = [
        "runner_adapter_unavailable",
        "execution_disabled",
        "execution_grant_invalid",
        "execution_lease_conflict",
        "execution_not_ready",
        "execution_protocol_invalid",
    ]

    public static func encodeFrame(_ message: LifecycleExecutionMessageV2) throws -> Data {
        try validate(message)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let payload: Data
        do {
            switch message {
            case let .register(value): payload = try encoder.encode(value)
            case let .registered(value): payload = try encoder.encode(value)
            case let .start(value): payload = try encoder.encode(value)
            case let .observe(value): payload = try encoder.encode(value)
            case let .apply(value): payload = try encoder.encode(value)
            case let .finalize(value): payload = try encoder.encode(value)
            case let .refused(value): payload = try encoder.encode(value)
            }
        } catch {
            throw LifecycleExecutionProtocolError.malformed
        }
        guard !payload.isEmpty, payload.count <= maximumJSONBytes else {
            throw LifecycleExecutionProtocolError.oversized
        }
        var length = UInt32(payload.count).bigEndian
        var frame = Data(bytes: &length, count: MemoryLayout<UInt32>.size)
        frame.append(payload)
        return frame
    }

    public static func decodeFrame(_ frame: Data) throws -> LifecycleExecutionMessageV2 {
        guard frame.count >= 4 else { throw LifecycleExecutionProtocolError.malformed }
        let length = frame.prefix(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard length > 0, length <= maximumJSONBytes,
              frame.count == Int(length) + 4
        else {
            throw LifecycleExecutionProtocolError.oversized
        }
        let payload = Data(frame.dropFirst(4))
        let object: [String: Any]
        do {
            guard let parsed = try JSONSerialization.jsonObject(with: payload) as? [String: Any] else {
                throw LifecycleExecutionProtocolError.malformed
            }
            object = parsed
        } catch let error as LifecycleExecutionProtocolError {
            throw error
        } catch {
            throw LifecycleExecutionProtocolError.malformed
        }
        guard let rawType = object["message_type"] as? String,
              let type = LifecycleExecutionMessageTypeV2(rawValue: rawType)
        else {
            throw LifecycleExecutionProtocolError.malformed
        }
        let decoder = JSONDecoder()
        let message: LifecycleExecutionMessageV2
        do {
            switch type {
            case .register:
                try exactKeys(payload, LifecycleRunnerRegistrationV2.CodingKeys.allCases)
                message = .register(try decoder.decode(LifecycleRunnerRegistrationV2.self, from: payload))
            case .registered:
                try exactKeys(payload, LifecycleRunnerRegisteredV2.CodingKeys.allCases)
                message = .registered(try decoder.decode(LifecycleRunnerRegisteredV2.self, from: payload))
            case .start:
                try exactKeys(payload, LifecycleExecutionStartV2.CodingKeys.allCases)
                message = .start(try decoder.decode(LifecycleExecutionStartV2.self, from: payload))
            case .observe:
                try exactKeys(payload, LifecycleExecutionObserveV2.CodingKeys.allCases)
                message = .observe(try decoder.decode(LifecycleExecutionObserveV2.self, from: payload))
            case .apply:
                try exactKeys(payload, LifecycleExecutionApplyV2.CodingKeys.allCases)
                message = .apply(try decoder.decode(LifecycleExecutionApplyV2.self, from: payload))
            case .finalize:
                try exactKeys(payload, LifecycleExecutionFinalizeV2.CodingKeys.allCases)
                message = .finalize(try decoder.decode(LifecycleExecutionFinalizeV2.self, from: payload))
            case .refused:
                try exactKeys(payload, LifecycleExecutionRefusedV2.CodingKeys.allCases)
                message = .refused(try decoder.decode(LifecycleExecutionRefusedV2.self, from: payload))
            }
        } catch let error as LifecycleExecutionProtocolError {
            throw error
        } catch {
            throw LifecycleExecutionProtocolError.malformed
        }
        try validate(message)
        return message
    }

    public static func validate(_ message: LifecycleExecutionMessageV2) throws {
        switch message {
        case let .register(value):
            try common(
                protocolVersion: value.protocolVersion,
                actualType: value.messageType,
                expectedType: .register,
                transactionID: value.transactionID,
                grantID: value.grantID,
                runnerSessionID: value.runnerSessionID,
                sequence: value.sequence,
                nonce: value.nonce
            )
            guard validDigest(value.grantDigest),
                  value.runnerIdentifier == runnerIdentifier,
                  validDigest(value.runnerBuildDigest),
                  validDigest(value.runnerIdentityDigest),
                  validTeam(value.teamIdentifier),
                  validDigest(value.codeRequirementDigest),
                  5 ... maximumLeaseSeconds ~= value.requestedLeaseSeconds
            else { throw LifecycleExecutionProtocolError.invalidAuthority }
        case let .registered(value):
            try common(value, expectedType: .registered)
            guard validDigest(value.grantDigest), canonicalUUID(value.requestNonce),
                  canonicalUUID(value.leaseID), positive(value.leaseEpoch),
                  positive(value.leaseExpiresAt)
            else { throw LifecycleExecutionProtocolError.invalidAuthority }
        case let .start(value):
            try common(value, expectedType: .start)
            guard validDigest(value.grantDigest), canonicalUUID(value.leaseID),
                  positive(value.leaseEpoch), validDigest(value.executionManifestDigest),
                  validDigest(value.recoveryCloneIdentityDigest),
                  validDigest(value.authorizationDigest),
                  canonicalUUID(value.authorizationSessionID)
            else { throw LifecycleExecutionProtocolError.invalidAuthority }
        case let .observe(value):
            try common(value, expectedType: .observe)
            guard validLeaseEffect(
                grantDigest: value.grantDigest,
                leaseID: value.leaseID,
                leaseEpoch: value.leaseEpoch,
                effectID: value.effectID,
                targetDigest: value.targetDigest,
                attempt: value.attempt
            ), value.priorReceiptDigest.map(validDigest) ?? true
            else { throw LifecycleExecutionProtocolError.invalidAuthority }
        case let .apply(value):
            try common(value, expectedType: .apply)
            guard validLeaseEffect(
                grantDigest: value.grantDigest,
                leaseID: value.leaseID,
                leaseEpoch: value.leaseEpoch,
                effectID: value.effectID,
                targetDigest: value.targetDigest,
                attempt: value.attempt
            ), validDigest(value.observationReceiptDigest)
            else { throw LifecycleExecutionProtocolError.invalidAuthority }
        case let .finalize(value):
            try common(value, expectedType: .finalize)
            guard validDigest(value.grantDigest), canonicalUUID(value.leaseID),
                  positive(value.leaseEpoch), validDigest(value.finalReceiptDigest)
            else { throw LifecycleExecutionProtocolError.invalidAuthority }
        case let .refused(value):
            try common(value, expectedType: .refused)
            guard canonicalUUID(value.requestNonce), refusalCodes.contains(value.errorCode)
            else { throw LifecycleExecutionProtocolError.invalidAuthority }
        }
    }

    private static func common<T>(
        _ value: T,
        expectedType: LifecycleExecutionMessageTypeV2
    ) throws where T: LifecycleExecutionCommonV2 {
        try common(
            protocolVersion: value.protocolVersion,
            actualType: value.messageType,
            expectedType: expectedType,
            transactionID: value.transactionID,
            grantID: value.grantID,
            runnerSessionID: value.runnerSessionID,
            sequence: value.sequence,
            nonce: value.nonce
        )
    }

    private static func common(
        protocolVersion: Int,
        actualType: LifecycleExecutionMessageTypeV2,
        expectedType: LifecycleExecutionMessageTypeV2,
        transactionID: String,
        grantID: String,
        runnerSessionID: String,
        sequence: Int,
        nonce: String
    ) throws {
        guard protocolVersion == self.protocolVersion,
              actualType == expectedType,
              canonicalUUID(transactionID), canonicalUUID(grantID),
              canonicalUUID(runnerSessionID), positive(sequence),
              sequence <= 1_000_000, canonicalUUID(nonce)
        else { throw LifecycleExecutionProtocolError.invalidAuthority }
    }

    private static func validLeaseEffect(
        grantDigest: String,
        leaseID: String,
        leaseEpoch: Int,
        effectID: String,
        targetDigest: String,
        attempt: Int
    ) -> Bool {
        validDigest(grantDigest) && canonicalUUID(leaseID)
            && positive(leaseEpoch) && canonicalUUID(effectID)
            && validDigest(targetDigest) && 1 ... 1_024 ~= attempt
    }

    private static func validDigest(_ value: String) -> Bool {
        value.count == 71 && value.hasPrefix("sha256:")
            && value.dropFirst(7).allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }

    private static func canonicalUUID(_ value: String) -> Bool {
        guard let parsed = UUID(uuidString: value) else { return false }
        return parsed.uuidString.lowercased() == value
    }

    private static func validTeam(_ value: String) -> Bool {
        value.count == 10 && !["ADHOC00000", "UNSIGNED00", "NOTSET0000"].contains(value)
            && value.allSatisfy { $0.isUppercase || $0.isNumber }
    }

    private static func positive(_ value: Int) -> Bool { value > 0 }

    private static func exactKeys<K: CodingKey & CaseIterable>(
        _ data: Data,
        _ keys: [K]
    ) throws {
        let expected = Set(keys.map(\.stringValue))
        let observed = try topLevelKeys(data)
        guard observed.count == expected.count, Set(observed) == expected else {
            throw LifecycleExecutionProtocolError.malformed
        }
    }

    // A small top-level scanner is used in addition to JSONSerialization so
    // duplicate keys cannot be hidden by Foundation's last-value behavior.
    private static func topLevelKeys(_ data: Data) throws -> [String] {
        let bytes = Array(data)
        var cursor = 0
        func whitespace() {
            while cursor < bytes.count, [9, 10, 13, 32].contains(bytes[cursor]) { cursor += 1 }
        }
        func stringRange() throws -> Range<Int> {
            guard cursor < bytes.count, bytes[cursor] == 34 else {
                throw LifecycleExecutionProtocolError.malformed
            }
            let start = cursor
            cursor += 1
            var escaped = false
            while cursor < bytes.count {
                let byte = bytes[cursor]
                cursor += 1
                if escaped { escaped = false }
                else if byte == 92 { escaped = true }
                else if byte == 34 { return start ..< cursor }
                else if byte < 32 { throw LifecycleExecutionProtocolError.malformed }
            }
            throw LifecycleExecutionProtocolError.malformed
        }
        func skipValue() throws {
            whitespace()
            guard cursor < bytes.count else { throw LifecycleExecutionProtocolError.malformed }
            if bytes[cursor] == 34 { _ = try stringRange(); return }
            var depth = 0
            var inString = false
            var escaped = false
            while cursor < bytes.count {
                let byte = bytes[cursor]
                if inString {
                    cursor += 1
                    if escaped { escaped = false }
                    else if byte == 92 { escaped = true }
                    else if byte == 34 { inString = false }
                } else if byte == 34 { inString = true; cursor += 1 }
                else if byte == 123 || byte == 91 { depth += 1; cursor += 1 }
                else if byte == 125 || byte == 93 {
                    if depth == 0 { return }
                    depth -= 1; cursor += 1
                } else if depth == 0, byte == 44 { return }
                else { cursor += 1 }
            }
        }
        whitespace()
        guard cursor < bytes.count, bytes[cursor] == 123 else {
            throw LifecycleExecutionProtocolError.malformed
        }
        cursor += 1
        var keys: [String] = []
        while true {
            whitespace()
            if cursor < bytes.count, bytes[cursor] == 125 { cursor += 1; break }
            let range = try stringRange()
            guard let key = try? JSONDecoder().decode(String.self, from: Data(bytes[range])) else {
                throw LifecycleExecutionProtocolError.malformed
            }
            keys.append(key)
            whitespace()
            guard cursor < bytes.count, bytes[cursor] == 58 else {
                throw LifecycleExecutionProtocolError.malformed
            }
            cursor += 1
            try skipValue()
            whitespace()
            if cursor < bytes.count, bytes[cursor] == 44 { cursor += 1; continue }
            if cursor < bytes.count, bytes[cursor] == 125 { cursor += 1; break }
            throw LifecycleExecutionProtocolError.malformed
        }
        whitespace()
        guard cursor == bytes.count else { throw LifecycleExecutionProtocolError.malformed }
        return keys
    }
}

private protocol LifecycleExecutionCommonV2 {
    var protocolVersion: Int { get }
    var messageType: LifecycleExecutionMessageTypeV2 { get }
    var transactionID: String { get }
    var grantID: String { get }
    var runnerSessionID: String { get }
    var sequence: Int { get }
    var nonce: String { get }
}

extension LifecycleRunnerRegisteredV2: LifecycleExecutionCommonV2 {}
extension LifecycleExecutionStartV2: LifecycleExecutionCommonV2 {}
extension LifecycleExecutionObserveV2: LifecycleExecutionCommonV2 {}
extension LifecycleExecutionApplyV2: LifecycleExecutionCommonV2 {}
extension LifecycleExecutionFinalizeV2: LifecycleExecutionCommonV2 {}
extension LifecycleExecutionRefusedV2: LifecycleExecutionCommonV2 {}
