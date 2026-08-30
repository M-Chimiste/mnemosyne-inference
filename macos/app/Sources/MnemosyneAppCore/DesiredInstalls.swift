import Foundation

/// The closed DesiredInstall v1 states emitted by the Mac's local journal.
public enum DesiredInstallState: String, Codable, CaseIterable, Sendable {
    case received
    case awaitingLocalApproval = "awaiting_local_approval"
    case accepted
    case downloading
    case verifying
    case downloadedUnregistered = "downloaded_unregistered"
    case registered
    case completed
    case refused
    case cancelled
    case failed

    public var isTerminal: Bool {
        switch self {
        case .completed, .refused, .cancelled, .failed:
            true
        default:
            false
        }
    }
}

public enum DesiredInstallDesiredState: String, Codable, Sendable {
    case run
    case cancel
}

/// The exact path-free Mac inventory basis selected by the Hub.
public struct DesiredInstallRecommendationBasis: Codable, Equatable, Sendable {
    public let inventoryInstanceID: String
    public let inventorySequence: Int64

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case inventoryInstanceID = "inventory_instance_id"
        case inventorySequence = "inventory_sequence"
    }

    public init(inventoryInstanceID: String, inventorySequence: Int64) {
        self.inventoryInstanceID = inventoryInstanceID
        self.inventorySequence = inventorySequence
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "desired install recommendation basis"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        inventoryInstanceID = try values.decode(
            String.self,
            forKey: .inventoryInstanceID
        )
        inventorySequence = try values.decode(
            Int64.self,
            forKey: .inventorySequence
        )
        try requireCanonicalUUID(
            inventoryInstanceID,
            codingPath: decoder.codingPath,
            field: "inventory_instance_id"
        )
        try requireInteger(
            inventorySequence,
            in: 0 ... 9_007_199_254_740_991,
            codingPath: decoder.codingPath,
            field: "inventory_sequence"
        )
    }
}

/// One exact, signed-catalog DesiredInstall intent.
///
/// This model deliberately has no filesystem path, bookmark, credential, or
/// node locator property. Unknown members are rejected so a future service
/// cannot accidentally surface one through this UI contract.
public struct DesiredInstallJob: Codable, Equatable, Sendable, Identifiable {
    public let schemaVersion: Int
    public let jobID: String
    public let jobRevision: Int
    public let idempotencyKey: String
    public let desiredState: DesiredInstallDesiredState
    public let createdAt: Double
    public let expiresAt: Double
    public let validForSeconds: Int
    public let pairingID: String
    public let credentialGeneration: Int
    public let recommendationBasis: DesiredInstallRecommendationBasis
    public let catalogVersion: String
    public let catalogDigest: String
    public let logicalModelID: String
    public let recipeID: String
    public let artifactID: String
    public let engine: InferenceEngine
    public let capabilities: [String]
    public let guaranteedContextTokens: Int?
    public let alias: String?
    public let storageLocationID: String
    public let storageBindingGeneration: Int

    public var id: String { jobID }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case jobID = "job_id"
        case jobRevision = "job_revision"
        case idempotencyKey = "idempotency_key"
        case desiredState = "desired_state"
        case createdAt = "created_at"
        case expiresAt = "expires_at"
        case validForSeconds = "valid_for_seconds"
        case pairingID = "pairing_id"
        case credentialGeneration = "credential_generation"
        case recommendationBasis = "recommendation_basis"
        case catalogVersion = "catalog_version"
        case catalogDigest = "catalog_digest"
        case logicalModelID = "logical_model_id"
        case recipeID = "recipe_id"
        case artifactID = "artifact_id"
        case engine
        case capabilities
        case guaranteedContextTokens = "guaranteed_context_tokens"
        case alias
        case storageLocationID = "storage_location_id"
        case storageBindingGeneration = "storage_binding_generation"
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "desired install job"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        jobID = try values.decode(String.self, forKey: .jobID)
        jobRevision = try values.decode(Int.self, forKey: .jobRevision)
        idempotencyKey = try values.decode(String.self, forKey: .idempotencyKey)
        desiredState = try values.decode(
            DesiredInstallDesiredState.self,
            forKey: .desiredState
        )
        createdAt = try values.decode(Double.self, forKey: .createdAt)
        expiresAt = try values.decode(Double.self, forKey: .expiresAt)
        validForSeconds = try values.decode(Int.self, forKey: .validForSeconds)
        pairingID = try values.decode(String.self, forKey: .pairingID)
        credentialGeneration = try values.decode(
            Int.self,
            forKey: .credentialGeneration
        )
        recommendationBasis = try values.decode(
            DesiredInstallRecommendationBasis.self,
            forKey: .recommendationBasis
        )
        catalogVersion = try values.decode(String.self, forKey: .catalogVersion)
        catalogDigest = try values.decode(String.self, forKey: .catalogDigest)
        logicalModelID = try values.decode(String.self, forKey: .logicalModelID)
        recipeID = try values.decode(String.self, forKey: .recipeID)
        artifactID = try values.decode(String.self, forKey: .artifactID)
        engine = try values.decode(InferenceEngine.self, forKey: .engine)
        capabilities = try values.decode([String].self, forKey: .capabilities)
        guaranteedContextTokens = try values.decodeIfPresent(
            Int.self,
            forKey: .guaranteedContextTokens
        )
        alias = try values.decodeIfPresent(String.self, forKey: .alias)
        storageLocationID = try values.decode(
            String.self,
            forKey: .storageLocationID
        )
        storageBindingGeneration = try values.decode(
            Int.self,
            forKey: .storageBindingGeneration
        )

        try requireSchemaVersion(schemaVersion, codingPath: decoder.codingPath)
        try requireCanonicalUUID(
            jobID,
            codingPath: decoder.codingPath,
            field: "job_id"
        )
        try requireJobRevision(jobRevision, codingPath: decoder.codingPath)
        try requireCanonicalUUID(
            idempotencyKey,
            codingPath: decoder.codingPath,
            field: "idempotency_key"
        )
        try requireTimestamp(
            createdAt,
            codingPath: decoder.codingPath,
            field: "created_at"
        )
        try requireTimestamp(
            expiresAt,
            codingPath: decoder.codingPath,
            field: "expires_at"
        )
        try requireInteger(
            validForSeconds,
            in: 1 ... 604_800,
            codingPath: decoder.codingPath,
            field: "valid_for_seconds"
        )
        guard abs(expiresAt - (createdAt + Double(validForSeconds))) <= 0.000_001 else {
            throw invalidValue(
                "expires_at does not match the bounded lifetime",
                codingPath: decoder.codingPath
            )
        }
        try requireCanonicalUUID(
            pairingID,
            codingPath: decoder.codingPath,
            field: "pairing_id"
        )
        try requireInteger(
            credentialGeneration,
            in: 1 ... 2_147_483_647,
            codingPath: decoder.codingPath,
            field: "credential_generation"
        )
        for (field, value) in [
            ("catalog_version", catalogVersion),
            ("logical_model_id", logicalModelID),
            ("recipe_id", recipeID),
            ("artifact_id", artifactID),
        ] {
            try requireSafeIdentifier(
                value,
                codingPath: decoder.codingPath,
                field: field
            )
        }
        try requireSHA256(
            catalogDigest,
            codingPath: decoder.codingPath,
            field: "catalog_digest"
        )
        try requireCapabilities(capabilities, codingPath: decoder.codingPath)
        if let guaranteedContextTokens {
            try requireInteger(
                guaranteedContextTokens,
                in: 1 ... 100_000_000,
                codingPath: decoder.codingPath,
                field: "guaranteed_context_tokens"
            )
        }
        if let alias {
            try requireSafeAlias(alias, codingPath: decoder.codingPath)
        }
        try requireCanonicalUUID(
            storageLocationID,
            codingPath: decoder.codingPath,
            field: "storage_location_id"
        )
        try requireInteger(
            storageBindingGeneration,
            in: 1 ... 2_147_483_647,
            codingPath: decoder.codingPath,
            field: "storage_binding_generation"
        )
    }
}

/// Path-free progress and final outcome for one exact job revision.
public struct DesiredInstallAcknowledgement: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let jobID: String
    public let jobRevision: Int
    public let installationID: String?
    public let state: DesiredInstallState
    public let bytesDownloaded: Int64
    public let totalBytes: Int64?
    public let updatedAt: Double

    /// A bounded machine-readable code. Unknown future codes are preserved so
    /// an older app can report them without inventing a misleading outcome.
    public let resultCode: String?

    public var progressFraction: Double? {
        guard let totalBytes, totalBytes > 0 else { return nil }
        return min(1, Double(bytesDownloaded) / Double(totalBytes))
    }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case jobID = "job_id"
        case jobRevision = "job_revision"
        case installationID = "installation_id"
        case state
        case bytesDownloaded = "bytes_downloaded"
        case totalBytes = "total_bytes"
        case updatedAt = "updated_at"
        case resultCode = "result_code"
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "desired install acknowledgement"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        jobID = try values.decode(String.self, forKey: .jobID)
        jobRevision = try values.decode(Int.self, forKey: .jobRevision)
        installationID = try values.decodeIfPresent(
            String.self,
            forKey: .installationID
        )
        state = try values.decode(DesiredInstallState.self, forKey: .state)
        bytesDownloaded = try values.decode(
            Int64.self,
            forKey: .bytesDownloaded
        )
        totalBytes = try values.decodeIfPresent(Int64.self, forKey: .totalBytes)
        updatedAt = try values.decode(Double.self, forKey: .updatedAt)
        resultCode = try values.decodeIfPresent(String.self, forKey: .resultCode)

        try requireSchemaVersion(schemaVersion, codingPath: decoder.codingPath)
        try requireCanonicalUUID(
            jobID,
            codingPath: decoder.codingPath,
            field: "job_id"
        )
        try requireJobRevision(jobRevision, codingPath: decoder.codingPath)
        if let installationID {
            try requireCanonicalUUID(
                installationID,
                codingPath: decoder.codingPath,
                field: "installation_id"
            )
        }
        try requireInteger(
            bytesDownloaded,
            in: 0 ... 1_152_921_504_606_846_976,
            codingPath: decoder.codingPath,
            field: "bytes_downloaded"
        )
        if let totalBytes {
            try requireInteger(
                totalBytes,
                in: 0 ... 1_152_921_504_606_846_976,
                codingPath: decoder.codingPath,
                field: "total_bytes"
            )
            guard bytesDownloaded <= totalBytes else {
                throw invalidValue(
                    "bytes_downloaded exceeds total_bytes",
                    codingPath: decoder.codingPath
                )
            }
        }
        try requireTimestamp(
            updatedAt,
            codingPath: decoder.codingPath,
            field: "updated_at"
        )
        if let resultCode {
            try requireResultCode(resultCode, codingPath: decoder.codingPath)
        }
    }
}

/// Local actions are the current authority for controls shown by the app.
/// Top-level availability flags remain diagnostic compatibility signals only.
public struct DesiredInstallLocalActions: Codable, Equatable, Sendable {
    public let refusalAvailable: Bool
    public let approvalAvailable: Bool
    public let cancellationAvailable: Bool

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case refusalAvailable = "refusal_available"
        case approvalAvailable = "approval_available"
        case cancellationAvailable = "cancellation_available"
    }

    public init(
        refusalAvailable: Bool,
        approvalAvailable: Bool,
        cancellationAvailable: Bool = false
    ) {
        self.refusalAvailable = refusalAvailable
        self.approvalAvailable = approvalAvailable
        self.cancellationAvailable = cancellationAvailable
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "desired install local actions"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        refusalAvailable = try values.decode(
            Bool.self,
            forKey: .refusalAvailable
        )
        approvalAvailable = try values.decode(
            Bool.self,
            forKey: .approvalAvailable
        )
        cancellationAvailable = try values.decodeIfPresent(
            Bool.self,
            forKey: .cancellationAvailable
        ) ?? false
    }
}

public struct DesiredInstallItem: Codable, Equatable, Sendable, Identifiable {
    public let job: DesiredInstallJob
    public let acknowledgement: DesiredInstallAcknowledgement
    public let localActions: DesiredInstallLocalActions

    public var id: String { job.jobID }

    public var canApprove: Bool {
        hasExactAcknowledgement
            && job.desiredState == .run
            && acknowledgement.state == .awaitingLocalApproval
            && localActions.approvalAvailable
    }

    public var canRefuse: Bool {
        hasExactAcknowledgement
            && job.desiredState == .run
            && !acknowledgement.state.isTerminal
            && localActions.refusalAvailable
    }

    public var canCancel: Bool {
        hasExactAcknowledgement
            && !acknowledgement.state.isTerminal
            && localActions.cancellationAvailable
    }

    private var hasExactAcknowledgement: Bool {
        acknowledgement.jobID == job.jobID
            && acknowledgement.jobRevision == job.jobRevision
    }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case job
        case acknowledgement
        case localActions = "local_actions"
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "desired install item"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        job = try values.decode(DesiredInstallJob.self, forKey: .job)
        acknowledgement = try values.decode(
            DesiredInstallAcknowledgement.self,
            forKey: .acknowledgement
        )
        localActions = try values.decode(
            DesiredInstallLocalActions.self,
            forKey: .localActions
        )
        guard hasExactAcknowledgement else {
            throw invalidValue(
                "desired install acknowledgement identity does not match the job",
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct DesiredInstallListSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let executorAvailable: Bool
    public let approvalAvailable: Bool
    public let offset: Int
    public let limit: Int
    public let total: Int
    public let items: [DesiredInstallItem]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case executorAvailable = "executor_available"
        case approvalAvailable = "approval_available"
        case offset
        case limit
        case total
        case items
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "desired install list"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        executorAvailable = try values.decode(Bool.self, forKey: .executorAvailable)
        approvalAvailable = try values.decode(Bool.self, forKey: .approvalAvailable)
        offset = try values.decode(Int.self, forKey: .offset)
        limit = try values.decode(Int.self, forKey: .limit)
        total = try values.decode(Int.self, forKey: .total)
        items = try values.decode([DesiredInstallItem].self, forKey: .items)

        try requireSchemaVersion(schemaVersion, codingPath: decoder.codingPath)
        try requireInteger(
            offset,
            in: 0 ... 10_000,
            codingPath: decoder.codingPath,
            field: "offset"
        )
        try requireInteger(
            limit,
            in: 1 ... 256,
            codingPath: decoder.codingPath,
            field: "limit"
        )
        try requireInteger(
            total,
            in: 0 ... 10_000,
            codingPath: decoder.codingPath,
            field: "total"
        )
        guard items.count <= limit, Set(items.map(\.id)).count == items.count else {
            throw invalidValue(
                "desired install list bounds or identities are invalid",
                codingPath: decoder.codingPath
            )
        }
    }
}

public struct DesiredInstallDetailSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let executorAvailable: Bool
    public let approvalAvailable: Bool
    public let item: DesiredInstallItem

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case executorAvailable = "executor_available"
        case approvalAvailable = "approval_available"
        case item
    }

    public init(from decoder: Decoder) throws {
        try rejectUnknownKeys(
            decoder,
            allowed: CodingKeys.allRawValues,
            context: "desired install detail"
        )
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        executorAvailable = try values.decode(Bool.self, forKey: .executorAvailable)
        approvalAvailable = try values.decode(Bool.self, forKey: .approvalAvailable)
        item = try values.decode(DesiredInstallItem.self, forKey: .item)
        try requireSchemaVersion(schemaVersion, codingPath: decoder.codingPath)
    }
}

public struct DesiredInstallMutationRequest: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let jobRevision: Int

    public init(schemaVersion: Int = 1, jobRevision: Int) throws {
        guard schemaVersion == 1 else {
            throw DesiredInstallRequestError.unsupportedSchemaVersion
        }
        try validateDesiredInstallJobRevision(jobRevision)
        self.schemaVersion = schemaVersion
        self.jobRevision = jobRevision
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case jobRevision = "job_revision"
    }
}

/// Input failures are resolved before a URL is built, so a path or stale
/// noncanonical identifier can never become part of a control request.
public enum DesiredInstallRequestError: Error, Equatable, LocalizedError {
    case unsupportedByClient
    case unsupportedSchemaVersion
    case invalidJobID
    case invalidJobRevision
    case invalidPageOffset
    case invalidPageLimit

    public var errorDescription: String? {
        switch self {
        case .unsupportedByClient:
            "This control service client does not support desired installs."
        case .unsupportedSchemaVersion:
            "The desired install request schema is not supported."
        case .invalidJobID:
            "The desired install job identity is invalid."
        case .invalidJobRevision:
            "The desired install job revision is invalid."
        case .invalidPageOffset, .invalidPageLimit:
            "The desired install page bounds are invalid."
        }
    }
}

/// Transient, path-free state for a native desired-install view.
///
/// Mutation buttons derive from each item's current local action flags and
/// exact acknowledgement revision. The top-level compatibility flags are
/// retained for diagnostics but never grant authority on their own.
public struct DesiredInstallViewModel: Equatable, Sendable {
    public private(set) var executorAvailable = false
    public private(set) var approvalAvailable = false
    public private(set) var offset = 0
    public private(set) var limit = 100
    public private(set) var total = 0
    public private(set) var items: [DesiredInstallItem] = []

    public init() {}

    public mutating func apply(_ snapshot: DesiredInstallListSnapshot) {
        executorAvailable = snapshot.executorAvailable
        approvalAvailable = snapshot.approvalAvailable
        offset = snapshot.offset
        limit = snapshot.limit
        total = snapshot.total
        items = snapshot.items
    }

    public mutating func apply(_ snapshot: DesiredInstallDetailSnapshot) {
        executorAvailable = snapshot.executorAvailable
        approvalAvailable = snapshot.approvalAvailable
        if let index = items.firstIndex(where: { $0.job.jobID == snapshot.item.job.jobID }) {
            guard snapshot.item.job.jobRevision >= items[index].job.jobRevision else {
                return
            }
            items[index] = snapshot.item
        } else {
            items.append(snapshot.item)
            total = max(total, items.count)
        }
    }

    public func item(jobID: String, jobRevision: Int) -> DesiredInstallItem? {
        items.first {
            $0.job.jobID == jobID && $0.job.jobRevision == jobRevision
        }
    }
}

func validateDesiredInstallJobID(_ value: String) throws {
    guard UUID(uuidString: value)?.uuidString.lowercased() == value else {
        throw DesiredInstallRequestError.invalidJobID
    }
}

func validateDesiredInstallJobRevision(_ value: Int) throws {
    guard 1 ... 2_147_483_647 ~= value else {
        throw DesiredInstallRequestError.invalidJobRevision
    }
}

private struct AnyDesiredInstallCodingKey: CodingKey, Hashable {
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

private func rejectUnknownKeys(
    _ decoder: Decoder,
    allowed: Set<String>,
    context: String
) throws {
    let values = try decoder.container(keyedBy: AnyDesiredInstallCodingKey.self)
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

private func requireSchemaVersion(
    _ value: Int,
    codingPath: [any CodingKey]
) throws {
    guard value == 1 else {
        throw invalidValue("Unsupported schema_version", codingPath: codingPath)
    }
}

private func requireCanonicalUUID(
    _ value: String,
    codingPath: [any CodingKey],
    field: String
) throws {
    guard UUID(uuidString: value)?.uuidString.lowercased() == value else {
        throw invalidValue("Invalid \(field)", codingPath: codingPath)
    }
}

private func requireJobRevision(
    _ value: Int,
    codingPath: [any CodingKey]
) throws {
    guard 1 ... 2_147_483_647 ~= value else {
        throw invalidValue("Invalid job_revision", codingPath: codingPath)
    }
}

private func requireTimestamp(
    _ value: Double,
    codingPath: [any CodingKey],
    field: String
) throws {
    guard value.isFinite, 0 ... 4_102_444_800 ~= value else {
        throw invalidValue("Invalid \(field)", codingPath: codingPath)
    }
}

private func requireInteger<T: BinaryInteger>(
    _ value: T,
    in range: ClosedRange<T>,
    codingPath: [any CodingKey],
    field: String
) throws {
    guard range.contains(value) else {
        throw invalidValue("Invalid \(field)", codingPath: codingPath)
    }
}

private func requireSafeIdentifier(
    _ value: String,
    codingPath: [any CodingKey],
    field: String
) throws {
    guard value.utf8.count <= 128,
        value.range(
            of: #"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$"#,
            options: .regularExpression
        ) != nil
    else {
        throw invalidValue("Invalid \(field)", codingPath: codingPath)
    }
}

private func requireSHA256(
    _ value: String,
    codingPath: [any CodingKey],
    field: String
) throws {
    guard value.range(
        of: #"^sha256:[0-9a-f]{64}$"#,
        options: .regularExpression
    ) != nil else {
        throw invalidValue("Invalid \(field)", codingPath: codingPath)
    }
}

private func requireCapabilities(
    _ values: [String],
    codingPath: [any CodingKey]
) throws {
    let allowed: Set<String> = [
        "chat/completions",
        "completions",
        "embeddings",
        "images/generations",
        "messages",
        "rerank",
        "responses",
    ]
    guard !values.isEmpty,
        values.count <= allowed.count,
        values == Array(Set(values)).sorted(),
        values.allSatisfy(allowed.contains)
    else {
        throw invalidValue("Invalid capabilities", codingPath: codingPath)
    }
}

private func requireSafeAlias(
    _ value: String,
    codingPath: [any CodingKey]
) throws {
    guard !value.isEmpty,
        value.unicodeScalars.count <= 128,
        value.range(
            of: #"^[^/\\\u0000-\u001f]{1,128}$"#,
            options: .regularExpression
        ) != nil
    else {
        throw invalidValue("Invalid alias", codingPath: codingPath)
    }
}

private func requireResultCode(
    _ value: String,
    codingPath: [any CodingKey]
) throws {
    guard value.range(
        of: #"^[a-z][a-z0-9_]{0,127}$"#,
        options: .regularExpression
    ) != nil else {
        throw invalidValue("Invalid result_code", codingPath: codingPath)
    }
}

private func invalidValue(
    _ description: String,
    codingPath: [any CodingKey]
) -> DecodingError {
    .dataCorrupted(
        .init(codingPath: codingPath, debugDescription: description)
    )
}
