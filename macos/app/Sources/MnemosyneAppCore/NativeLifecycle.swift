import Foundation

/// The three closed retention choices supported by native lifecycle v2.
public enum NativeLifecycleRetentionMode: String, Codable, CaseIterable,
    Identifiable, Sendable
{
    case appOnly = "app_only"
    case keepWeights = "remove_state_runtimes_keep_weights"
    case removeExclusiveManaged = "remove_exclusive_managed"

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .appOnly:
            "Remove app only"
        case .keepWeights:
            "Remove app and managed runtimes"
        case .removeExclusiveManaged:
            "Remove app, runtimes, and managed weights"
        }
    }

    public var effectDescription: String {
        switch self {
        case .appOnly:
            "The exact app and background-agent identities would be removed. The private .env, settings, local usage database, token outbox, managed runtimes, storage grants, pool pairing, and every model weight would be retained for reinstall."
        case .keepWeights:
            "The exact app, background-agent identities, and proven managed engine runtimes would be removed. The private .env, settings, local usage database, token outbox, storage grants, pool pairing, and every model weight would be retained for reinstall."
        case .removeExclusiveManaged:
            "The same app, agent, and managed-runtime effects apply. The private .env and accounting recovery data remain. Only model payloads with fresh, exclusive managed ownership proof would be eligible for Trash; imported, LM Studio, external, shared, ambiguous, and unproven weights stay in place."
        }
    }
}

public enum NativeLifecycleKind: String, Decodable, Sendable {
    case migration
    case uninstall
}

public enum NativeLifecycleComponentKind: String, Decodable, CaseIterable,
    Identifiable, Sendable
{
    case application
    case launchAgent = "launch_agent"
    case privateState = "private_state"
    case managedRuntimes = "managed_runtimes"
    case securityScopes = "security_scopes"
    case pairingState = "pairing_state"

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .application: "Application"
        case .launchAgent: "Background service registration"
        case .privateState: "Private settings and state"
        case .managedRuntimes: "Managed engine runtimes"
        case .securityScopes: "Storage access grants"
        case .pairingState: "Hub pairing state"
        }
    }
}

public enum NativeLifecycleComponentDisposition: String, Decodable, Sendable {
    case retain
    case removeExact = "remove_exact"
    case removeProvenMembers = "remove_proven_members"
    case replaceExact = "replace_exact"

    public var title: String {
        switch self {
        case .retain: "Retain"
        case .removeExact: "Remove exact identity"
        case .removeProvenMembers: "Remove proven members only"
        case .replaceExact: "Replace exact identity"
        }
    }
}

public enum NativeLifecycleOutboxDecision: String, Decodable, Sendable {
    case preserveWithState = "preserve_with_state"
    case emptyConfirmed = "empty_confirmed"
    case recoveryCapsule = "recovery_capsule"
    case explicitAbandonment = "explicit_abandonment"

    public var title: String {
        switch self {
        case .preserveWithState: "Retain with private state"
        case .emptyConfirmed: "Confirmed empty"
        case .recoveryCapsule: "Preserve in recovery capsule"
        case .explicitAbandonment: "Explicitly abandon"
        }
    }
}

public enum NativeLifecycleHubRevocationState: String, Decodable, Sendable {
    case notRequested = "not_requested"
    case confirmed
    case pendingOffline = "pending_offline"

    public var title: String {
        switch self {
        case .notRequested: "Not requested"
        case .confirmed: "Confirmed"
        case .pendingOffline: "Pending while Hub is offline"
        }
    }
}

public enum NativeLifecyclePhase: String, Decodable, Sendable {
    case discovered
    case helperStaged = "helper_staged"
    case authorized
    case preflighted
    case drained
    case snapshotted
    case predecessorStopped = "predecessor_stopped"
    case candidateInstalled = "candidate_installed"
    case candidateStarted = "candidate_started"
    case validated
    case committed
    case restored
    case prepared
    case serviceQuiesced = "service_quiesced"
    case outboxResolved = "outbox_resolved"
    case hubResolved = "hub_resolved"
    case agentUnregistered = "agent_unregistered"
    case menuLoginUnregistered = "menu_login_unregistered"
    case applicationQuarantined = "application_quarantined"
    case applicationRemoved = "application_removed"
    case weightsResolved = "weights_resolved"
    case runtimesResolved = "runtimes_resolved"
    case stateResolved = "state_resolved"
    case completed
    case manualRecovery = "manual_recovery"

    public var title: String {
        rawValue.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

public struct NativeLifecycleComponentPlan: Decodable, Equatable, Identifiable,
    Sendable
{
    public let kind: NativeLifecycleComponentKind
    public let disposition: NativeLifecycleComponentDisposition

    public var id: NativeLifecycleComponentKind { kind }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case kind
        case disposition
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle component"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        kind = try values.decode(NativeLifecycleComponentKind.self, forKey: .kind)
        disposition = try values.decode(
            NativeLifecycleComponentDisposition.self,
            forKey: .disposition
        )
    }
}

public struct NativeLifecycleRetentionManifestSummary: Decodable, Equatable,
    Sendable
{
    public let schemaVersion: Int
    public let itemCount: Int
    public let retainedCount: Int
    public let trashCount: Int

    // This receipt binds the exact private manifest during preparation. It is
    // intentionally not exposed to the Settings module or rendered in UI.
    private let receiptDigest: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case receiptDigest = "digest"
        case itemCount = "item_count"
        case retainedCount = "retained_count"
        case trashCount = "trash_count"
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle retention summary"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        receiptDigest = try values.decode(String.self, forKey: .receiptDigest)
        itemCount = try values.decode(Int.self, forKey: .itemCount)
        retainedCount = try values.decode(Int.self, forKey: .retainedCount)
        trashCount = try values.decode(Int.self, forKey: .trashCount)
        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
        try requireNativeLifecycleDigest(
            receiptDigest,
            codingPath: decoder.codingPath,
            field: "retention receipt"
        )
        guard 0 ... 100_000 ~= itemCount,
              0 ... itemCount ~= retainedCount,
              0 ... itemCount ~= trashCount,
              retainedCount + trashCount == itemCount
        else {
            throw nativeLifecycleInvalid(
                "Invalid retention counts",
                codingPath: decoder.codingPath
            )
        }
    }
}

private struct NativeLifecycleProductIdentity: Decodable, Equatable, Sendable {
    let applicationName: String
    let applicationBundleID: String
    let launchAgentLabel: String
    let serviceCodeRequirementID: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case applicationName = "application_name"
        case applicationBundleID = "application_bundle_id"
        case launchAgentLabel = "launch_agent_label"
        case serviceCodeRequirementID = "service_code_requirement_id"
    }

    init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle product identity"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        applicationName = try values.decode(String.self, forKey: .applicationName)
        applicationBundleID = try values.decode(String.self, forKey: .applicationBundleID)
        launchAgentLabel = try values.decode(String.self, forKey: .launchAgentLabel)
        serviceCodeRequirementID = try values.decode(
            String.self,
            forKey: .serviceCodeRequirementID
        )
        guard applicationName == "Unified Inference.app",
              applicationBundleID == "com.mnemosyne.inference.menu",
              launchAgentLabel == "com.mnemosyne.inference.agent",
              serviceCodeRequirementID == "com.mnemosyne.inference.service"
        else {
            throw nativeLifecycleInvalid(
                "Unexpected native product identity",
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct NativeLifecycleUninstallPlan: Decodable, Equatable, Sendable {
    public let schemaVersion: Int
    public let transactionID: String
    public let retentionMode: NativeLifecycleRetentionMode
    public let tokenOutboxCount: Int
    public let outboxDecision: NativeLifecycleOutboxDecision
    public let hubRevocationState: NativeLifecycleHubRevocationState
    public let components: [NativeLifecycleComponentPlan]
    public let retentionManifest: NativeLifecycleRetentionManifestSummary

    private let product: NativeLifecycleProductIdentity

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case kind
        case transactionID = "transaction_id"
        case retentionMode = "retention_mode"
        case product
        case tokenOutboxCount = "token_outbox_count"
        case outboxDecision = "outbox_decision"
        case hubRevocationState = "hub_revocation_state"
        case components
        case retentionManifest = "retention_manifest"
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native uninstall plan"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        let kind = try values.decode(NativeLifecycleKind.self, forKey: .kind)
        transactionID = try values.decode(String.self, forKey: .transactionID)
        retentionMode = try values.decode(
            NativeLifecycleRetentionMode.self,
            forKey: .retentionMode
        )
        product = try values.decode(
            NativeLifecycleProductIdentity.self,
            forKey: .product
        )
        tokenOutboxCount = try values.decode(Int.self, forKey: .tokenOutboxCount)
        outboxDecision = try values.decode(
            NativeLifecycleOutboxDecision.self,
            forKey: .outboxDecision
        )
        hubRevocationState = try values.decode(
            NativeLifecycleHubRevocationState.self,
            forKey: .hubRevocationState
        )
        components = try values.decode(
            [NativeLifecycleComponentPlan].self,
            forKey: .components
        )
        retentionManifest = try values.decode(
            NativeLifecycleRetentionManifestSummary.self,
            forKey: .retentionManifest
        )

        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
        guard kind == .uninstall else {
            throw nativeLifecycleInvalid(
                "Expected an uninstall plan",
                codingPath: decoder.codingPath
            )
        }
        try requireNativeLifecycleUUID(
            transactionID,
            codingPath: decoder.codingPath,
            field: "transaction_id"
        )
        guard tokenOutboxCount >= 0,
              components.count == NativeLifecycleComponentKind.allCases.count,
              Set(components.map(\.kind)) == Set(NativeLifecycleComponentKind.allCases)
        else {
            throw nativeLifecycleInvalid(
                "Invalid native uninstall bounds",
                codingPath: decoder.codingPath
            )
        }
    }

    /// Compares every path-free effect plus the private-manifest receipt while
    /// deliberately excluding the newly minted transaction identity.
    public func hasSamePreparedEffects(
        as other: NativeLifecycleUninstallPlan
    ) -> Bool {
        retentionMode == other.retentionMode
            && product == other.product
            && tokenOutboxCount == other.tokenOutboxCount
            && outboxDecision == other.outboxDecision
            && hubRevocationState == other.hubRevocationState
            && components == other.components
            && retentionManifest == other.retentionManifest
    }
}

public struct NativeLifecycleUninstallPreview: Decodable, Equatable, Sendable {
    public let schemaVersion: Int
    public let preparable: Bool
    public let executionAvailable: Bool
    public let plan: NativeLifecycleUninstallPlan

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case preparable
        case executionAvailable = "execution_available"
        case plan
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native uninstall preview"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        preparable = try values.decode(Bool.self, forKey: .preparable)
        executionAvailable = try values.decode(Bool.self, forKey: .executionAvailable)
        plan = try values.decode(NativeLifecycleUninstallPlan.self, forKey: .plan)
        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
    }
}

public struct NativeLifecycleMigrationPlan: Decodable, Equatable, Sendable {
    public let schemaVersion: Int
    public let transactionID: String
    public let legacySidecarState: String

    private let product: NativeLifecycleProductIdentity
    private let predecessorBuildDigest: String
    private let candidateBuildDigest: String
    private let retentionContract: [String: String]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case kind
        case transactionID = "transaction_id"
        case product
        case predecessorBuildDigest = "predecessor_build_digest"
        case candidateBuildDigest = "candidate_build_digest"
        case legacySidecar = "legacy_sidecar"
        case retentionContract = "retention_contract"
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native migration plan"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        let kind = try values.decode(NativeLifecycleKind.self, forKey: .kind)
        transactionID = try values.decode(String.self, forKey: .transactionID)
        product = try values.decode(
            NativeLifecycleProductIdentity.self,
            forKey: .product
        )
        predecessorBuildDigest = try values.decode(
            String.self,
            forKey: .predecessorBuildDigest
        )
        candidateBuildDigest = try values.decode(
            String.self,
            forKey: .candidateBuildDigest
        )
        let legacy = try values.decode(
            NativeLifecycleLegacySidecar.self,
            forKey: .legacySidecar
        )
        legacySidecarState = legacy.state
        retentionContract = try values.decode(
            [String: String].self,
            forKey: .retentionContract
        )
        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
        guard kind == .migration,
              Set(retentionContract.keys) == [
                "private_state", "managed_runtimes", "security_scopes",
                "pairing_state", "weights",
              ],
              retentionContract.values.allSatisfy({ $0 == "retain" })
        else {
            throw nativeLifecycleInvalid(
                "Invalid migration retention contract",
                codingPath: decoder.codingPath
            )
        }
        try requireNativeLifecycleUUID(
            transactionID,
            codingPath: decoder.codingPath,
            field: "transaction_id"
        )
        try requireNativeLifecycleDigest(
            predecessorBuildDigest,
            codingPath: decoder.codingPath,
            field: "predecessor build"
        )
        try requireNativeLifecycleDigest(
            candidateBuildDigest,
            codingPath: decoder.codingPath,
            field: "candidate build"
        )
        guard ["absent", "inheritance_durably_validated"].contains(
            legacySidecarState
        ) else {
            throw nativeLifecycleInvalid(
                "Invalid legacy sidecar state",
                codingPath: decoder.codingPath
            )
        }
    }
}

private struct NativeLifecycleLegacySidecar: Decodable, Equatable, Sendable {
    let state: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case label
        case state
    }

    init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "legacy sidecar migration state"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let label = try values.decode(String.self, forKey: .label)
        state = try values.decode(String.self, forKey: .state)
        guard label == "com.athena.token-sidecar" else {
            throw nativeLifecycleInvalid(
                "Invalid legacy sidecar identity",
                codingPath: decoder.codingPath
            )
        }
    }
}

public enum NativeLifecyclePlan: Decodable, Equatable, Sendable {
    case migration(NativeLifecycleMigrationPlan)
    case uninstall(NativeLifecycleUninstallPlan)

    public var kind: NativeLifecycleKind {
        switch self {
        case .migration: .migration
        case .uninstall: .uninstall
        }
    }

    public var uninstall: NativeLifecycleUninstallPlan? {
        guard case let .uninstall(plan) = self else { return nil }
        return plan
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: KindCodingKeys.self)
        switch try values.decode(NativeLifecycleKind.self, forKey: .kind) {
        case .migration:
            self = .migration(try NativeLifecycleMigrationPlan(from: decoder))
        case .uninstall:
            self = .uninstall(try NativeLifecycleUninstallPlan(from: decoder))
        }
    }

    private enum KindCodingKeys: String, CodingKey {
        case kind
    }
}

public struct NativeLifecycleTransaction: Decodable, Equatable, Identifiable,
    Sendable
{
    public let schemaVersion: Int
    public let contractVersion: Int
    public let transactionID: String
    public let kind: NativeLifecycleKind
    public let phase: NativeLifecyclePhase
    public let terminal: Bool
    public let needsRecovery: Bool
    public let createdAt: Double
    public let updatedAt: Double
    public let errorCode: String?
    public let plan: NativeLifecyclePlan

    public var id: String { transactionID }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case contractVersion = "contract_version"
        case transactionID = "transaction_id"
        case kind
        case phase
        case terminal
        case needsRecovery = "needs_recovery"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case errorCode = "error_code"
        case plan
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle transaction"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        contractVersion = try values.decodeIfPresent(
            Int.self,
            forKey: .contractVersion
        ) ?? 1
        transactionID = try values.decode(String.self, forKey: .transactionID)
        kind = try values.decode(NativeLifecycleKind.self, forKey: .kind)
        phase = try values.decode(NativeLifecyclePhase.self, forKey: .phase)
        terminal = try values.decode(Bool.self, forKey: .terminal)
        needsRecovery = try values.decode(Bool.self, forKey: .needsRecovery)
        createdAt = try values.decode(Double.self, forKey: .createdAt)
        updatedAt = try values.decode(Double.self, forKey: .updatedAt)
        errorCode = try values.decodeIfPresent(String.self, forKey: .errorCode)
        plan = try values.decode(NativeLifecyclePlan.self, forKey: .plan)
        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
        try requireNativeLifecycleUUID(
            transactionID,
            codingPath: decoder.codingPath,
            field: "transaction_id"
        )
        guard kind == plan.kind,
              transactionID == planTransactionID(plan),
              1 ... 2 ~= contractVersion,
              terminal != needsRecovery,
              createdAt.isFinite,
              updatedAt.isFinite,
              0 ... 4_102_444_800 ~= createdAt,
              createdAt ... 4_102_444_800 ~= updatedAt
        else {
            throw nativeLifecycleInvalid(
                "Invalid native lifecycle transaction",
                codingPath: decoder.codingPath
            )
        }
        if let errorCode {
            try requireNativeLifecycleErrorCode(
                errorCode,
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct NativeLifecycleStatusSnapshot: Decodable, Equatable, Sendable {
    public let schemaVersion: Int
    public let available: Bool
    public let errorCode: String?
    public let executionAvailable: Bool
    public let authorizationAvailable: Bool
    public let authorizationPendingCount: Int
    public let migrationPreviewAvailable: Bool
    public let incompleteCount: Int
    public let incomplete: [NativeLifecycleTransaction]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case available
        case errorCode = "error_code"
        case executionAvailable = "execution_available"
        case authorizationAvailable = "authorization_available"
        case authorizationPendingCount = "authorization_pending_count"
        case migrationPreviewAvailable = "migration_preview_available"
        case incompleteCount = "incomplete_count"
        case incomplete
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle status"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        available = try values.decode(Bool.self, forKey: .available)
        errorCode = try values.decodeIfPresent(String.self, forKey: .errorCode)
        executionAvailable = try values.decode(Bool.self, forKey: .executionAvailable)
        authorizationAvailable = try values.decodeIfPresent(
            Bool.self,
            forKey: .authorizationAvailable
        ) ?? false
        authorizationPendingCount = try values.decodeIfPresent(
            Int.self,
            forKey: .authorizationPendingCount
        ) ?? 0
        migrationPreviewAvailable = try values.decode(
            Bool.self,
            forKey: .migrationPreviewAvailable
        )
        incompleteCount = try values.decode(Int.self, forKey: .incompleteCount)
        incomplete = try values.decode(
            [NativeLifecycleTransaction].self,
            forKey: .incomplete
        )
        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
        guard 0 ... 1_024 ~= incompleteCount,
              0 ... incompleteCount ~= authorizationPendingCount,
              incompleteCount == incomplete.count,
              Set(incomplete.map(\.transactionID)).count == incomplete.count,
              available == (errorCode == nil)
        else {
            throw nativeLifecycleInvalid(
                "Invalid native lifecycle status",
                codingPath: decoder.codingPath
            )
        }
        if let errorCode {
            try requireNativeLifecycleErrorCode(
                errorCode,
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct NativeLifecyclePrepareResponse: Decodable, Equatable, Sendable {
    public let schemaVersion: Int
    public let prepared: Bool
    public let replayed: Bool
    public let executionAvailable: Bool
    public let transaction: NativeLifecycleTransaction

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case prepared
        case replayed
        case executionAvailable = "execution_available"
        case transaction
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle prepare response"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        prepared = try values.decode(Bool.self, forKey: .prepared)
        replayed = try values.decode(Bool.self, forKey: .replayed)
        executionAvailable = try values.decode(Bool.self, forKey: .executionAvailable)
        transaction = try values.decode(
            NativeLifecycleTransaction.self,
            forKey: .transaction
        )
        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
    }
}

public struct NativeLifecycleMigrationPreview: Decodable, Equatable, Sendable {
    public let schemaVersion: Int
    public let preparable: Bool
    public let executionAvailable: Bool
    public let plan: NativeLifecycleMigrationPlan

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case preparable
        case executionAvailable = "execution_available"
        case plan
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native migration preview"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        preparable = try values.decode(Bool.self, forKey: .preparable)
        executionAvailable = try values.decode(Bool.self, forKey: .executionAvailable)
        plan = try values.decode(NativeLifecycleMigrationPlan.self, forKey: .plan)
        try requireNativeLifecycleSchema(schemaVersion, codingPath: decoder.codingPath)
    }
}

public enum NativeLifecycleAuthorizationState: String, Decodable, Sendable {
    case unavailable
    case ready
    case challengePending = "challenge_pending"
    case challengeExpired = "challenge_expired"
    case challengeCancelled = "challenge_cancelled"
    case authorized
}

public struct NativeLifecycleAuthorizationStatus: Decodable, Equatable,
    Sendable
{
    public let schemaVersion: Int
    public let transactionID: String
    public let phase: NativeLifecyclePhase
    public let state: NativeLifecycleAuthorizationState
    public let canRequest: Bool
    public let executionAvailable: Bool

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case transactionID = "transaction_id"
        case phase
        case state
        case canRequest = "can_request"
        case executionAvailable = "execution_available"
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle authorization status"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        transactionID = try values.decode(String.self, forKey: .transactionID)
        phase = try values.decode(NativeLifecyclePhase.self, forKey: .phase)
        state = try values.decode(
            NativeLifecycleAuthorizationState.self,
            forKey: .state
        )
        canRequest = try values.decode(Bool.self, forKey: .canRequest)
        executionAvailable = try values.decode(
            Bool.self,
            forKey: .executionAvailable
        )
        try requireNativeLifecycleSchema(
            schemaVersion,
            codingPath: decoder.codingPath
        )
        try requireNativeLifecycleUUID(
            transactionID,
            codingPath: decoder.codingPath,
            field: "transaction_id"
        )
        guard !executionAvailable,
              canRequest == (
                  phase == .helperStaged
                      && [.ready, .challengeExpired, .challengeCancelled]
                          .contains(state)
              )
        else {
            throw nativeLifecycleInvalid(
                "Invalid native lifecycle authorization status",
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct NativeLifecycleAuthorizationChallengeResponse: Decodable,
    Equatable, Sendable
{
    public let schemaVersion: Int
    public let transactionID: String
    public let authorizationAvailable: Bool
    public let executionAvailable: Bool
    public let replayed: Bool
    public let challenge: LifecycleHelperChallengeV2

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case transactionID = "transaction_id"
        case authorizationAvailable = "authorization_available"
        case executionAvailable = "execution_available"
        case replayed
        case challenge
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle authorization challenge response"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        transactionID = try values.decode(String.self, forKey: .transactionID)
        authorizationAvailable = try values.decode(
            Bool.self,
            forKey: .authorizationAvailable
        )
        executionAvailable = try values.decode(
            Bool.self,
            forKey: .executionAvailable
        )
        replayed = try values.decode(Bool.self, forKey: .replayed)
        challenge = try values.decode(
            LifecycleHelperChallengeV2.self,
            forKey: .challenge
        )
        try requireNativeLifecycleSchema(
            schemaVersion,
            codingPath: decoder.codingPath
        )
        try requireNativeLifecycleUUID(
            transactionID,
            codingPath: decoder.codingPath,
            field: "transaction_id"
        )
        do {
            try LifecycleHelperProtocolV2.validate(
                challenge,
                now: Date().timeIntervalSince1970
            )
        } catch {
            throw nativeLifecycleInvalid(
                "Invalid lifecycle helper challenge",
                codingPath: decoder.codingPath
            )
        }
        guard authorizationAvailable,
              !executionAvailable,
              challenge.transactionID == transactionID
        else {
            throw nativeLifecycleInvalid(
                "Mismatched lifecycle helper challenge",
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct NativeLifecycleAuthorizationResponse: Decodable, Equatable,
    Sendable
{
    public let schemaVersion: Int
    public let authorized: Bool
    public let replayed: Bool
    public let executionAvailable: Bool
    public let transaction: NativeLifecycleTransaction

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case authorized
        case replayed
        case executionAvailable = "execution_available"
        case transaction
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle authorization response"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        authorized = try values.decode(Bool.self, forKey: .authorized)
        replayed = try values.decode(Bool.self, forKey: .replayed)
        executionAvailable = try values.decode(
            Bool.self,
            forKey: .executionAvailable
        )
        transaction = try values.decode(
            NativeLifecycleTransaction.self,
            forKey: .transaction
        )
        try requireNativeLifecycleSchema(
            schemaVersion,
            codingPath: decoder.codingPath
        )
        guard authorized,
              !executionAvailable,
              transaction.phase == .authorized
        else {
            throw nativeLifecycleInvalid(
                "Invalid native lifecycle authorization",
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct NativeLifecycleAuthorizationCancellationResponse: Decodable,
    Equatable, Sendable
{
    public let schemaVersion: Int
    public let transactionID: String
    public let cancelled: Bool
    public let replayed: Bool
    public let executionAvailable: Bool

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case transactionID = "transaction_id"
        case cancelled
        case replayed
        case executionAvailable = "execution_available"
    }

    public init(from decoder: Decoder) throws {
        try rejectNativeLifecycleUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "native lifecycle authorization cancellation"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        transactionID = try values.decode(String.self, forKey: .transactionID)
        cancelled = try values.decode(Bool.self, forKey: .cancelled)
        replayed = try values.decode(Bool.self, forKey: .replayed)
        executionAvailable = try values.decode(
            Bool.self,
            forKey: .executionAvailable
        )
        try requireNativeLifecycleSchema(
            schemaVersion,
            codingPath: decoder.codingPath
        )
        try requireNativeLifecycleUUID(
            transactionID,
            codingPath: decoder.codingPath,
            field: "transaction_id"
        )
        guard cancelled, !executionAvailable else {
            throw nativeLifecycleInvalid(
                "Invalid lifecycle authorization cancellation",
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct NativeLifecycleAuthorizationChallengeRequest: Encodable,
    Equatable, Sendable
{
    public let schemaVersion = 2

    public init() {}

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
    }
}

public struct NativeLifecycleAuthorizationCancelRequest: Encodable,
    Equatable, Sendable
{
    public let schemaVersion = 2
    public let nonce: String
    public let sessionID: String

    public init(nonce: String, sessionID: String) throws {
        guard UUID(uuidString: nonce)?.uuidString.lowercased() == nonce,
              UUID(uuidString: sessionID)?.uuidString.lowercased() == sessionID
        else {
            throw NativeLifecycleRequestError.invalidAuthorizationIdentity
        }
        self.nonce = nonce
        self.sessionID = sessionID
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case nonce
        case sessionID = "session_id"
    }
}

public struct NativeLifecycleUninstallPreviewRequest: Encodable, Equatable,
    Sendable
{
    public let schemaVersion = 1
    public let retentionMode: NativeLifecycleRetentionMode

    public init(retentionMode: NativeLifecycleRetentionMode) {
        self.retentionMode = retentionMode
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case retentionMode = "retention_mode"
    }
}

public struct NativeLifecycleUninstallPrepareRequest: Encodable, Equatable,
    Sendable
{
    public let schemaVersion = 1
    public let transactionID: String
    public let retentionMode: NativeLifecycleRetentionMode

    public init(
        transactionID: String,
        retentionMode: NativeLifecycleRetentionMode
    ) throws {
        guard UUID(uuidString: transactionID)?.uuidString.lowercased()
            == transactionID
        else {
            throw NativeLifecycleRequestError.invalidTransactionID
        }
        self.transactionID = transactionID
        self.retentionMode = retentionMode
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case transactionID = "transaction_id"
        case retentionMode = "retention_mode"
    }
}

public enum NativeLifecycleRequestError: Error, Equatable, LocalizedError {
    case unsupportedByClient
    case invalidTransactionID
    case invalidAuthorizationIdentity
    case changedBeforePreparation
    case invalidPreparedTransaction
    case invalidAuthorizationResponse

    public var errorDescription: String? {
        switch self {
        case .unsupportedByClient:
            "This control service client does not support lifecycle planning."
        case .invalidTransactionID:
            "The lifecycle transaction identity is invalid."
        case .invalidAuthorizationIdentity:
            "The lifecycle helper session identity is invalid."
        case .changedBeforePreparation:
            "Local lifecycle evidence changed. Review the refreshed plan before preparing it."
        case .invalidPreparedTransaction:
            "The control service did not confirm the exact prepared lifecycle plan."
        case .invalidAuthorizationResponse:
            "The control service did not confirm the exact authorized lifecycle transaction."
        }
    }
}

public struct NativeLifecycleAPIError: Error, Equatable, LocalizedError,
    Sendable
{
    public let statusCode: Int
    public let code: String

    public init(statusCode: Int, code: String) {
        self.statusCode = statusCode
        self.code = code
    }

    public var errorDescription: String? {
        switch code {
        case "native_lifecycle_outbox_blocked":
            "Token usage is still queued, so private-state removal cannot be prepared. Let reporting drain, then refresh."
        case "native_lifecycle_storage_authority_stale":
            "The registered storage authority changed. Refresh storage before reviewing removal."
        case "native_lifecycle_inventory_timeout":
            "Lifecycle inventory timed out. Check that configured storage volumes are available, then refresh."
        case "native_lifecycle_inventory_unavailable":
            "Lifecycle inventory is unavailable. Check configured storage and try again."
        case "native_lifecycle_migration_evidence_unavailable":
            "No signed migration candidate with rollback evidence is available in this release."
        case "native_lifecycle_journal_unavailable":
            "The private lifecycle journal is unavailable."
        case "native_lifecycle_helper_authority_cancelled":
            "Lifecycle authorization was cancelled. No lifecycle effect was performed."
        case "native_lifecycle_helper_authority_expired":
            "Lifecycle authorization expired before it could be recorded. Try again."
        case "native_lifecycle_helper_authority_replayed":
            "That lifecycle authorization was already consumed and cannot be replayed."
        case "native_lifecycle_helper_authority_mismatch",
             "native_lifecycle_helper_authority_conflict":
            "The staged lifecycle authority changed. Refresh before trying again."
        case "native_lifecycle_helper_authority_unavailable":
            "Owner authorization requires a sealed Developer ID build of Unified Inference."
        case "native_lifecycle_helper_authority_invalid":
            "The lifecycle helper returned an invalid receipt. No lifecycle effect was performed."
        default:
            "Lifecycle planning is unavailable (HTTP \(statusCode))."
        }
    }
}

private struct NativeLifecycleAnyCodingKey: CodingKey, Hashable {
    let stringValue: String
    let intValue: Int?

    init?(stringValue: String) {
        self.stringValue = stringValue
        intValue = nil
    }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

private extension CaseIterable where Self: CodingKey {
    static var allRawValues: Set<String> {
        Set(allCases.map(\.stringValue))
    }
}

private func rejectNativeLifecycleUnknownKeys(
    _ decoder: Decoder,
    allowed: Set<String>,
    context: String
) throws {
    let values = try decoder.container(keyedBy: NativeLifecycleAnyCodingKey.self)
    let unknown = Set(values.allKeys.map(\.stringValue)).subtracting(allowed)
    guard unknown.isEmpty else {
        throw DecodingError.dataCorrupted(
            .init(
                codingPath: decoder.codingPath,
                debugDescription: "Unexpected member in \(context)."
            )
        )
    }
}

private func requireNativeLifecycleSchema(
    _ value: Int,
    codingPath: [any CodingKey]
) throws {
    guard 1 ... 2 ~= value else {
        throw nativeLifecycleInvalid(
            "Unsupported native lifecycle schema",
            codingPath: codingPath
        )
    }
}

private func requireNativeLifecycleUUID(
    _ value: String,
    codingPath: [any CodingKey],
    field: String
) throws {
    guard UUID(uuidString: value)?.uuidString.lowercased() == value else {
        throw nativeLifecycleInvalid("Invalid \(field)", codingPath: codingPath)
    }
}

private func requireNativeLifecycleDigest(
    _ value: String,
    codingPath: [any CodingKey],
    field: String
) throws {
    guard value.range(
        of: #"^[0-9a-f]{64}$"#,
        options: .regularExpression
    ) != nil else {
        throw nativeLifecycleInvalid("Invalid \(field)", codingPath: codingPath)
    }
}

private func requireNativeLifecycleErrorCode(
    _ value: String,
    codingPath: [any CodingKey]
) throws {
    guard value.range(
        of: #"^[a-z][a-z0-9_]{0,127}$"#,
        options: .regularExpression
    ) != nil else {
        throw nativeLifecycleInvalid(
            "Invalid lifecycle error code",
            codingPath: codingPath
        )
    }
}

private func planTransactionID(_ plan: NativeLifecyclePlan) -> String {
    switch plan {
    case let .migration(value): value.transactionID
    case let .uninstall(value): value.transactionID
    }
}

private func nativeLifecycleInvalid(
    _ description: String,
    codingPath: [any CodingKey]
) -> DecodingError {
    .dataCorrupted(
        .init(codingPath: codingPath, debugDescription: description)
    )
}
