import Foundation

public protocol ControlAPI: NativeLifecycleAuthorizationServicing, Sendable {
    func status() async throws -> ServiceSnapshot
    func fleetPairing() async throws -> FleetPairingSnapshot
    func refreshFleetPairingAttempt() async throws -> FleetPairingSnapshot
    func requestFleetPairing(
        _ request: FleetPairingPresenceRequest
    ) async throws -> FleetPairingPresenceResponse
    func beginFleetPairing(
        _ request: FleetPairingControlRequest
    ) async throws -> FleetPairingOperationResponse
    func resumeFleetPairing(
        _ request: FleetPairingControlRequest
    ) async throws -> FleetPairingOperationResponse
    func discardRejectedFleetPairingAttempt() async throws -> FleetPairingSnapshot
    func discardTerminalFleetPairingAttempt() async throws -> FleetPairingSnapshot
    func revokeFleetPairing(
        requestID: String
    ) async throws -> FleetPairingManagementResponse
    func fleetParticipation() async throws -> FleetParticipationSnapshot
    func setFleetParticipation(enabled: Bool) async throws -> FleetParticipationSnapshot
    func desiredInstalls(
        offset: Int, limit: Int
    ) async throws -> DesiredInstallListSnapshot
    func desiredInstall(
        jobID: String
    ) async throws -> DesiredInstallDetailSnapshot
    func refuseDesiredInstall(
        jobID: String, jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot
    func approveDesiredInstall(
        jobID: String, jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot
    func cancelDesiredInstall(
        jobID: String, jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot
    func nativeLifecycleStatus() async throws -> NativeLifecycleStatusSnapshot
    func previewNativeUninstall(
        retentionMode: NativeLifecycleRetentionMode
    ) async throws -> NativeLifecycleUninstallPreview
    func prepareNativeUninstall(
        transactionID: String,
        retentionMode: NativeLifecycleRetentionMode
    ) async throws -> NativeLifecyclePrepareResponse
    func nativeLifecycleTransaction(
        transactionID: String
    ) async throws -> NativeLifecycleTransaction
    func nativeLifecycleAuthorizationStatus(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationStatus
    func performNativeLifecycleAuthorization(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationResponse
    func previewNativeMigration() async throws -> NativeLifecycleMigrationPreview
    func readiness() async throws -> ReadinessSnapshot
    func reconcile() async throws -> ServiceSnapshot
    func selfTest(
        model: String,
        includeVision: Bool,
        unloadAfter: Bool
    ) async throws -> ModelSelfTestResult
    func models() async throws -> ModelCatalogSnapshot
    func benchmarks(alias: String?) async throws -> EngineBenchmarkSnapshot
    func runBenchmark(alias: String, sampleRuns: Int) async throws -> EngineBenchmarkRun
    func contexts(alias: String?) async throws -> ContextWindowSnapshot
    func profileContext(alias: String, targetTokens: Int?) async throws -> ContextProfileRun
    func load(model: String) async throws -> ServiceSnapshot
    func unload() async throws
    func storageLocations() async throws -> StorageSnapshot
    func inspectStorage(path: String, bookmarkData: Data?) async throws -> StorageStatus
    func searchLibrary(query: String) async throws -> [LibraryModel]
    func libraryFiles(
        repoId: String, engine: InferenceEngine, revision: String?
    ) async throws -> [LibraryModel]
    func libraryDetails(
        repoId: String,
        engine: InferenceEngine,
        filename: String?,
        revision: String?
    ) async throws -> LibraryModelDetails
    func localModelSources() async throws -> [LocalModelSource]
    func scanLocalModels(path: String, bookmarkData: Data?) async throws -> LocalModelScanSnapshot
    func importLocalModels(
        _ request: LocalModelImportRequest
    ) async throws -> LocalModelImportResult
    func modelInstalls() async throws -> [ModelInstall]
    func modelInstallHistory() async throws -> [ModelInstall]
    func startModelInstall(_ request: StartModelInstallRequest) async throws -> ModelInstall
    func cancelModelInstall(id: String) async throws -> ModelInstall
    func retryModelInstall(id: String) async throws -> ModelInstall
    func dismissModelInstall(id: String) async throws
    func deleteManagedModel(
        alias: String, revision: String
    ) async throws -> ConfigurationSaveResult
    func deleteManagedModel(
        alias: String, revision: String, installationID: String?
    ) async throws -> ConfigurationSaveResult
    func runtimeUpdates(refresh: Bool) async throws -> RuntimeUpdateSnapshot
    func checkRuntimeUpdates() async throws -> RuntimeUpdateSnapshot
    func installRuntimeUpdate(
        engine: InferenceEngine, version: String?
    ) async throws -> RuntimeUpdateSnapshot
    func installRuntimeUpdate(
        engine: InferenceEngine, version: String?, channel: String?
    ) async throws -> RuntimeUpdateSnapshot
    func rollbackRuntimeUpdate(engine: InferenceEngine) async throws -> RuntimeUpdateSnapshot
    func omlxCacheHealth() async throws -> OMLXCacheHealth
    func resetOMLXCache() async throws -> OMLXCacheResetResult
    func configuration() async throws -> ConfigurationSnapshot
    func saveConfiguration(
        _ settings: NativeSettings, revision: String
    ) async throws -> ConfigurationSaveResult
}

public extension ControlAPI {
    func refreshFleetPairingAttempt() async throws -> FleetPairingSnapshot {
        try await fleetPairing()
    }

    func requestFleetPairing(
        _ request: FleetPairingPresenceRequest
    ) async throws -> FleetPairingPresenceResponse {
        throw FleetPairingAPIError(
            statusCode: 0,
            code: "pairing_local_control_required",
            retryable: false
        )
    }

    func discardRejectedFleetPairingAttempt() async throws -> FleetPairingSnapshot {
        throw FleetPairingAPIError(
            statusCode: 0,
            code: "pairing_local_control_required",
            retryable: false
        )
    }

    func discardTerminalFleetPairingAttempt() async throws -> FleetPairingSnapshot {
        try await discardRejectedFleetPairingAttempt()
    }

    func revokeFleetPairing(
        requestID: String
    ) async throws -> FleetPairingManagementResponse {
        throw FleetPairingAPIError(
            statusCode: 0,
            code: "pairing_local_control_required",
            retryable: false
        )
    }

    func desiredInstalls(
        offset: Int,
        limit: Int
    ) async throws -> DesiredInstallListSnapshot {
        throw DesiredInstallRequestError.unsupportedByClient
    }

    func desiredInstall(
        jobID: String
    ) async throws -> DesiredInstallDetailSnapshot {
        throw DesiredInstallRequestError.unsupportedByClient
    }

    func refuseDesiredInstall(
        jobID: String,
        jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot {
        throw DesiredInstallRequestError.unsupportedByClient
    }

    func approveDesiredInstall(
        jobID: String,
        jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot {
        throw DesiredInstallRequestError.unsupportedByClient
    }

    func cancelDesiredInstall(
        jobID: String,
        jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot {
        throw DesiredInstallRequestError.unsupportedByClient
    }

    func nativeLifecycleStatus() async throws -> NativeLifecycleStatusSnapshot {
        throw NativeLifecycleRequestError.unsupportedByClient
    }

    func previewNativeUninstall(
        retentionMode: NativeLifecycleRetentionMode
    ) async throws -> NativeLifecycleUninstallPreview {
        throw NativeLifecycleRequestError.unsupportedByClient
    }

    func prepareNativeUninstall(
        transactionID: String,
        retentionMode: NativeLifecycleRetentionMode
    ) async throws -> NativeLifecyclePrepareResponse {
        throw NativeLifecycleRequestError.unsupportedByClient
    }

    func nativeLifecycleTransaction(
        transactionID: String
    ) async throws -> NativeLifecycleTransaction {
        throw NativeLifecycleRequestError.unsupportedByClient
    }

    func previewNativeMigration() async throws -> NativeLifecycleMigrationPreview {
        throw NativeLifecycleRequestError.unsupportedByClient
    }

    func nativeLifecycleAuthorizationStatus(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationStatus {
        throw NativeLifecycleRequestError.unsupportedByClient
    }

    func performNativeLifecycleAuthorization(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationResponse {
        throw NativeLifecycleRequestError.unsupportedByClient
    }

    func modelInstallHistory() async throws -> [ModelInstall] {
        try await modelInstalls()
    }

    func deleteManagedModel(
        alias: String,
        revision: String,
        installationID: String?
    ) async throws -> ConfigurationSaveResult {
        guard installationID == nil else {
            throw ModelCleanupAPICompatibilityError
                .exactInstallationIdentityUnsupported
        }
        return try await deleteManagedModel(alias: alias, revision: revision)
    }

    func installRuntimeUpdate(
        engine: InferenceEngine,
        version: String?,
        channel: String?
    ) async throws -> RuntimeUpdateSnapshot {
        guard channel == nil || channel == "official" else {
            throw RuntimeUpdateAPICompatibilityError
                .managedChannelsUnsupported
        }
        return try await installRuntimeUpdate(engine: engine, version: version)
    }
}

public enum ModelCleanupAPICompatibilityError: Error, Equatable, LocalizedError {
    case exactInstallationIdentityUnsupported

    public var errorDescription: String? {
        "This control service client cannot send an exact managed installation identity. Update Unified Inference before cleaning up this managed model."
    }
}

public enum RuntimeUpdateAPICompatibilityError: Error, Equatable, LocalizedError {
    case managedChannelsUnsupported

    public var errorDescription: String? {
        "This control service client cannot select a managed runtime channel. Update Unified Inference before installing this preview."
    }
}

public enum ControlAPIError: Error, Equatable, LocalizedError {
    case invalidResponse
    case unexpectedStatus(Int)
    case rejected(Int, String)
    case unsupportedConfigurationSchema(Int)

    public var errorDescription: String? {
        switch self {
        case .invalidResponse:
            "The Mnemosyne control service returned an invalid response."
        case let .unexpectedStatus(status):
            "The Mnemosyne control service returned HTTP \(status)."
        case let .rejected(_, detail):
            detail
        case let .unsupportedConfigurationSchema(version):
            "This app supports configuration schema \(NativeSettings.supportedSchemaVersion), but the service returned schema \(version). Update Unified Inference before saving settings."
        }
    }
}

public struct ConfigurationSaveResult: Codable, Equatable, Sendable {
    public let saved: Bool
    public let applied: Bool
    public let restartRequired: Bool
    public let modelCount: Int
    public let revision: String
    public let config: NativeSettings
    public let deletedFiles: Bool?
    public let filesDisposition: String?
}

struct DeleteManagedModelRequest: Codable, Equatable, Sendable {
    let revision: String
    let installationId: String?

    init(revision: String, installationId: String? = nil) {
        self.revision = revision
        self.installationId = installationId
    }
}

struct ModelInstallEvidenceSnapshot: Decodable, Equatable, Sendable {
    let schemaVersion: Int
    let installs: [ModelInstall]
}

public struct ConfigurationSnapshot: Codable, Equatable, Sendable {
    public let config: NativeSettings
    public let revision: String
    public let appliedRevision: String
    public let restartRequired: Bool

    public init(
        config: NativeSettings,
        revision: String,
        appliedRevision: String? = nil,
        restartRequired: Bool = false
    ) {
        self.config = config
        self.revision = revision
        self.appliedRevision = appliedRevision ?? revision
        self.restartRequired = restartRequired
    }

    private enum CodingKeys: String, CodingKey {
        case config
        case revision
        case appliedRevision
        case restartRequired
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        config = try container.decode(NativeSettings.self, forKey: .config)
        revision = try container.decode(String.self, forKey: .revision)
        appliedRevision = try container.decodeIfPresent(
            String.self,
            forKey: .appliedRevision
        ) ?? revision
        restartRequired = try container.decodeIfPresent(
            Bool.self,
            forKey: .restartRequired
        ) ?? false
    }
}

private struct LocalModelScanRequest: Codable {
    let path: String
    let bookmarkData: Data?
}

private struct StorageInspectRequest: Codable {
    let path: String
    let bookmarkData: Data?
}

struct SaveConfigurationRequest: Codable, Equatable {
    let config: NativeSettings
    let revision: String
}

private struct APIErrorPayload: Decodable {
    let detail: String
}

private enum ModelInstallHistoryError: Error, LocalizedError {
    case boundReached

    var errorDescription: String? {
        "Install history reached its safety bound and cannot prove a unique cleanup identity."
    }
}

private struct FleetPairingErrorEnvelope: Decodable {
    struct Detail: Decodable {
        let code: String
        let retryable: Bool
    }

    let detail: Detail
}

private struct NativeLifecycleErrorEnvelope: Decodable {
    struct Detail: Decodable {
        let code: String
    }

    let detail: Detail
}

private final class FleetPairingNoRedirectDelegate: NSObject,
    URLSessionTaskDelegate, @unchecked Sendable
{
    static let shared = FleetPairingNoRedirectDelegate()

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

public struct ControlAPIClient: ControlAPI, Sendable {
    public static let defaultBaseURL = ControlConnectionConfiguration.defaultBaseURL

    public let baseURL: URL
    private let session: URLSession
    private let authorizationHeader: String?

    public init(
        baseURL: URL = ControlAPIClient.defaultBaseURL,
        session: URLSession = .shared,
        adminPassword: String? = nil
    ) {
        self.baseURL = baseURL
        self.session = session
        if let adminPassword, !adminPassword.isEmpty {
            let value = Data("admin:\(adminPassword)".utf8).base64EncodedString()
            authorizationHeader = "Basic \(value)"
        } else {
            authorizationHeader = nil
        }
    }

    public func endpointURL(_ path: String) -> URL {
        let normalizedPath = path.hasPrefix("/") ? String(path.dropFirst()) : path
        return baseURL.appending(path: normalizedPath)
    }

    public func status() async throws -> ServiceSnapshot {
        let request = makeRequest(path: "/manager/status")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(ServiceSnapshot.self, from: data)
    }

    public func fleetParticipation() async throws -> FleetParticipationSnapshot {
        let request = fleetParticipationRequest()
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(
            FleetParticipationSnapshot.self,
            from: data
        )
    }

    public func fleetPairing() async throws -> FleetPairingSnapshot {
        let request = fleetPairingRequest()
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(FleetPairingSnapshot.self, from: data)
    }

    public func refreshFleetPairingAttempt() async throws
        -> FleetPairingSnapshot
    {
        let request = fleetPairingRefreshRequest()
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(FleetPairingSnapshot.self, from: data)
    }

    public func requestFleetPairing(
        _ payload: FleetPairingPresenceRequest
    ) async throws -> FleetPairingPresenceResponse {
        try await sendFleetPairingPresenceRequest(
            fleetPairingPresenceRequest(payload)
        )
    }

    public func beginFleetPairing(
        _ payload: FleetPairingControlRequest
    ) async throws -> FleetPairingOperationResponse {
        try await sendFleetPairingRequest(
            fleetPairingBeginRequest(payload)
        )
    }

    public func resumeFleetPairing(
        _ payload: FleetPairingControlRequest
    ) async throws -> FleetPairingOperationResponse {
        try await sendFleetPairingRequest(
            fleetPairingResumeRequest(payload)
        )
    }

    public func discardRejectedFleetPairingAttempt() async throws
        -> FleetPairingSnapshot
    {
        let request = fleetPairingDiscardRejectedRequest()
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(FleetPairingSnapshot.self, from: data)
    }

    public func discardTerminalFleetPairingAttempt() async throws
        -> FleetPairingSnapshot
    {
        let request = fleetPairingDiscardTerminalRequest()
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(FleetPairingSnapshot.self, from: data)
    }

    public func revokeFleetPairing(
        requestID: String
    ) async throws -> FleetPairingManagementResponse {
        let request = try fleetPairingRevokeRequest(requestID: requestID)
        return try await sendFleetPairingManagementRequest(request)
    }

    func fleetPairingRequest() -> URLRequest {
        makeRequest(path: "/manager/fleet/pairing")
    }

    func fleetPairingRefreshRequest() -> URLRequest {
        makeRequest(
            path: "/manager/fleet/pairing/refresh",
            method: "POST"
        )
    }

    func fleetPairingPresenceRequest(
        _ payload: FleetPairingPresenceRequest
    ) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/fleet/pairing/request",
            method: "POST"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(payload)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    func fleetPairingBeginRequest(
        _ payload: FleetPairingControlRequest
    ) throws -> URLRequest {
        try makeFleetPairingMutationRequest(
            path: "/manager/fleet/pairing/begin",
            payload: payload
        )
    }

    func fleetPairingResumeRequest(
        _ payload: FleetPairingControlRequest
    ) throws -> URLRequest {
        try makeFleetPairingMutationRequest(
            path: "/manager/fleet/pairing/resume",
            payload: payload
        )
    }

    func fleetPairingDiscardRejectedRequest() -> URLRequest {
        makeRequest(
            path: "/manager/fleet/pairing/discard-rejected",
            method: "POST"
        )
    }

    func fleetPairingDiscardTerminalRequest() -> URLRequest {
        makeRequest(
            path: "/manager/fleet/pairing/discard-terminal",
            method: "POST"
        )
    }

    func fleetPairingRevokeRequest(requestID: String) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/fleet/pairing/revoke",
            method: "POST"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            FleetPairingManagementRequest(requestID: requestID)
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    public func setFleetParticipation(
        enabled: Bool
    ) async throws -> FleetParticipationSnapshot {
        let request = try setFleetParticipationRequest(enabled: enabled)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(
            FleetParticipationSnapshot.self,
            from: data
        )
    }

    func fleetParticipationRequest() -> URLRequest {
        makeRequest(path: "/manager/fleet/participation")
    }

    func setFleetParticipationRequest(enabled: Bool) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/fleet/participation",
            method: "PUT"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            SetFleetParticipationRequest(enabled: enabled)
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    public func desiredInstalls(
        offset: Int = 0,
        limit: Int = 100
    ) async throws -> DesiredInstallListSnapshot {
        let request = try desiredInstallListRequest(offset: offset, limit: limit)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(
            DesiredInstallListSnapshot.self,
            from: data
        )
    }

    public func desiredInstall(
        jobID: String
    ) async throws -> DesiredInstallDetailSnapshot {
        let request = try desiredInstallReadRequest(jobID: jobID)
        return try await sendDesiredInstallRequest(request)
    }

    public func refuseDesiredInstall(
        jobID: String,
        jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot {
        try await mutateDesiredInstall(
            jobID: jobID,
            jobRevision: jobRevision,
            action: "refuse"
        )
    }

    public func approveDesiredInstall(
        jobID: String,
        jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot {
        try await mutateDesiredInstall(
            jobID: jobID,
            jobRevision: jobRevision,
            action: "approve"
        )
    }

    public func cancelDesiredInstall(
        jobID: String,
        jobRevision: Int
    ) async throws -> DesiredInstallDetailSnapshot {
        try await mutateDesiredInstall(
            jobID: jobID,
            jobRevision: jobRevision,
            action: "cancel"
        )
    }

    public func nativeLifecycleStatus() async throws
        -> NativeLifecycleStatusSnapshot
    {
        let request = nativeLifecycleStatusRequest()
        return try await sendNativeLifecycleRequest(
            request,
            as: NativeLifecycleStatusSnapshot.self
        )
    }

    public func previewNativeUninstall(
        retentionMode: NativeLifecycleRetentionMode
    ) async throws -> NativeLifecycleUninstallPreview {
        let request = try nativeUninstallPreviewRequest(
            retentionMode: retentionMode
        )
        return try await sendNativeLifecycleRequest(
            request,
            as: NativeLifecycleUninstallPreview.self
        )
    }

    public func prepareNativeUninstall(
        transactionID: String,
        retentionMode: NativeLifecycleRetentionMode
    ) async throws -> NativeLifecyclePrepareResponse {
        let request = try nativeUninstallPrepareRequest(
            transactionID: transactionID,
            retentionMode: retentionMode
        )
        return try await sendNativeLifecycleRequest(
            request,
            as: NativeLifecyclePrepareResponse.self
        )
    }

    public func nativeLifecycleTransaction(
        transactionID: String
    ) async throws -> NativeLifecycleTransaction {
        let request = try nativeLifecycleTransactionRequest(
            transactionID: transactionID
        )
        return try await sendNativeLifecycleRequest(
            request,
            as: NativeLifecycleTransaction.self
        )
    }

    public func nativeLifecycleAuthorizationStatus(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationStatus {
        let request = try nativeLifecycleAuthorizationStatusRequest(
            transactionID: transactionID
        )
        return try await sendNativeLifecycleRequest(
            request,
            as: NativeLifecycleAuthorizationStatus.self
        )
    }

    public func performNativeLifecycleAuthorization(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationResponse {
        let request = try nativeLifecycleAuthorizationPerformRequest(
            transactionID: transactionID
        )
        return try await sendNativeLifecycleRequest(
            request,
            as: NativeLifecycleAuthorizationResponse.self
        )
    }

    public func previewNativeMigration() async throws
        -> NativeLifecycleMigrationPreview
    {
        let request = nativeMigrationPreviewRequest()
        return try await sendNativeLifecycleRequest(
            request,
            as: NativeLifecycleMigrationPreview.self
        )
    }

    func nativeLifecycleStatusRequest() -> URLRequest {
        makeRequest(path: "/manager/native-lifecycle")
    }

    func nativeUninstallPreviewRequest(
        retentionMode: NativeLifecycleRetentionMode
    ) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/native-lifecycle/uninstall/preview",
            method: "POST"
        )
        request.httpBody = try JSONEncoder().encode(
            NativeLifecycleUninstallPreviewRequest(
                retentionMode: retentionMode
            )
        )
        request.timeoutInterval = 3 * 60
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    func nativeUninstallPrepareRequest(
        transactionID: String,
        retentionMode: NativeLifecycleRetentionMode
    ) throws -> URLRequest {
        let payload = try NativeLifecycleUninstallPrepareRequest(
            transactionID: transactionID,
            retentionMode: retentionMode
        )
        var request = makeRequest(
            path: "/manager/native-lifecycle/uninstall/prepare",
            method: "POST"
        )
        request.httpBody = try JSONEncoder().encode(payload)
        request.timeoutInterval = 3 * 60
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    func nativeLifecycleTransactionRequest(
        transactionID: String
    ) throws -> URLRequest {
        guard UUID(uuidString: transactionID)?.uuidString.lowercased()
            == transactionID
        else {
            throw NativeLifecycleRequestError.invalidTransactionID
        }
        return makeRequest(
            path: "/manager/native-lifecycle/transactions/\(transactionID)"
        )
    }

    func nativeMigrationPreviewRequest() -> URLRequest {
        makeRequest(path: "/manager/native-lifecycle/migration/preview")
    }

    func nativeLifecycleAuthorizationStatusRequest(
        transactionID: String
    ) throws -> URLRequest {
        try requireNativeLifecycleTransactionID(transactionID)
        return makeRequest(
            path: "/manager/native-lifecycle/transactions/\(transactionID)/authorization"
        )
    }

    func nativeLifecycleAuthorizationPerformRequest(
        transactionID: String
    ) throws -> URLRequest {
        try requireNativeLifecycleTransactionID(transactionID)
        var request = makeRequest(
            path: "/manager/native-lifecycle/transactions/\(transactionID)/authorization/perform",
            method: "POST"
        )
        request.httpBody = try JSONEncoder().encode(
            NativeLifecycleAuthorizationChallengeRequest()
        )
        request.timeoutInterval =
            LifecycleHelperProtocolV2.maximumAuthorizationLifetime + 5
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func requireNativeLifecycleTransactionID(
        _ transactionID: String
    ) throws {
        guard UUID(uuidString: transactionID)?.uuidString.lowercased()
            == transactionID
        else {
            throw NativeLifecycleRequestError.invalidTransactionID
        }
    }

    func desiredInstallListRequest(
        offset: Int,
        limit: Int
    ) throws -> URLRequest {
        guard 0 ... 10_000 ~= offset else {
            throw DesiredInstallRequestError.invalidPageOffset
        }
        guard 1 ... 256 ~= limit else {
            throw DesiredInstallRequestError.invalidPageLimit
        }
        var components = URLComponents(
            url: endpointURL("/manager/fleet/desired-installs"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "offset", value: String(offset)),
            URLQueryItem(name: "limit", value: String(limit)),
        ]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        return request
    }

    func desiredInstallReadRequest(jobID: String) throws -> URLRequest {
        try validateDesiredInstallJobID(jobID)
        return makeRequest(
            path: "/manager/fleet/desired-installs/\(jobID)"
        )
    }

    func desiredInstallMutationRequest(
        jobID: String,
        jobRevision: Int,
        action: String
    ) throws -> URLRequest {
        try validateDesiredInstallJobID(jobID)
        guard ["approve", "cancel", "refuse"].contains(action) else {
            throw DesiredInstallRequestError.unsupportedByClient
        }
        let payload = try DesiredInstallMutationRequest(
            jobRevision: jobRevision
        )
        var request = makeRequest(
            path: "/manager/fleet/desired-installs/\(jobID)/\(action)",
            method: "POST"
        )
        request.httpBody = try JSONEncoder().encode(payload)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    public func benchmarks(alias: String? = nil) async throws -> EngineBenchmarkSnapshot {
        var components = URLComponents(
            url: endpointURL("/manager/benchmarks"),
            resolvingAgainstBaseURL: false
        )!
        if let alias {
            components.queryItems = [URLQueryItem(name: "alias", value: alias)]
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            EngineBenchmarkSnapshot.self,
            from: data
        )
    }

    public func runBenchmark(
        alias: String,
        sampleRuns: Int
    ) async throws -> EngineBenchmarkRun {
        let request = try benchmarkRequest(alias: alias, sampleRuns: sampleRuns)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            EngineBenchmarkRun.self,
            from: data
        )
    }

    func benchmarkRequest(alias: String, sampleRuns: Int) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/benchmarks/\(alias)",
            method: "POST"
        )
        request.timeoutInterval = 60 * 60
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            RunEngineBenchmarkRequest(
                warmupRuns: 1,
                sampleRuns: min(max(sampleRuns, 1), 20),
                maxTokens: 128
            )
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    public func contexts(alias: String? = nil) async throws -> ContextWindowSnapshot {
        var components = URLComponents(
            url: endpointURL("/manager/contexts"),
            resolvingAgainstBaseURL: false
        )!
        if let alias {
            components.queryItems = [URLQueryItem(name: "alias", value: alias)]
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            ContextWindowSnapshot.self,
            from: data
        )
    }

    public func profileContext(
        alias: String,
        targetTokens: Int? = nil
    ) async throws -> ContextProfileRun {
        let request = try contextProfileRequest(alias: alias, targetTokens: targetTokens)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            ContextProfileRun.self,
            from: data
        )
    }

    func contextProfileRequest(alias: String, targetTokens: Int?) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/contexts/\(alias)/profile",
            method: "POST"
        )
        request.timeoutInterval = 60 * 60
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            RunContextProfileRequest(targetTokens: targetTokens)
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    public func readiness() async throws -> ReadinessSnapshot {
        let request = makeRequest(path: "/manager/readiness")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            ReadinessSnapshot.self,
            from: data
        )
    }

    public func reconcile() async throws -> ServiceSnapshot {
        let request = makeRequest(path: "/manager/reconcile", method: "POST")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(ServiceSnapshot.self, from: data)
    }

    public func selfTest(
        model: String,
        includeVision: Bool = true,
        unloadAfter: Bool = false
    ) async throws -> ModelSelfTestResult {
        let request = try selfTestRequest(
            model: model,
            includeVision: includeVision,
            unloadAfter: unloadAfter
        )
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            ModelSelfTestResult.self,
            from: data
        )
    }

    func selfTestRequest(
        model: String,
        includeVision: Bool,
        unloadAfter: Bool
    ) throws -> URLRequest {
        var request = makeRequest(path: "/manager/self-test", method: "POST")
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            ModelSelfTestRequest(
                model: model,
                includeVision: includeVision,
                unloadAfter: unloadAfter
            )
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    public func models() async throws -> ModelCatalogSnapshot {
        let request = makeRequest(path: "/manager/models")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(ModelCatalogSnapshot.self, from: data)
    }

    public func load(model: String) async throws -> ServiceSnapshot {
        let request = try loadRequest(model: model)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(ServiceSnapshot.self, from: data)
    }

    public func unload() async throws {
        let request = makeRequest(path: "/manager/unload", method: "POST")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
    }

    public func storageLocations() async throws -> StorageSnapshot {
        let request = makeRequest(path: "/manager/storage")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(StorageSnapshot.self, from: data)
    }

    public func inspectStorage(
        path: String,
        bookmarkData: Data? = nil
    ) async throws -> StorageStatus {
        var request = makeRequest(path: "/manager/storage/inspect", method: "POST")
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            StorageInspectRequest(path: path, bookmarkData: bookmarkData)
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(StorageStatus.self, from: data)
    }

    public func searchLibrary(
        query: String
    ) async throws -> [LibraryModel] {
        let request = librarySearchRequest(query: query)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(LibraryModelsSnapshot.self, from: data).models
    }

    public func libraryFiles(
        repoId: String,
        engine: InferenceEngine,
        revision: String? = nil
    ) async throws -> [LibraryModel] {
        let request = libraryFilesRequest(
            repoId: repoId,
            engine: engine,
            revision: revision
        )
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(LibraryModelsSnapshot.self, from: data).models
    }

    public func libraryDetails(
        repoId: String,
        engine: InferenceEngine,
        filename: String? = nil,
        revision: String? = nil
    ) async throws -> LibraryModelDetails {
        var components = URLComponents(
            url: endpointURL("/manager/model-library/details"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "engine", value: engine.rawValue),
            URLQueryItem(name: "repo_id", value: repoId),
        ]
        if let filename, !filename.isEmpty {
            components.queryItems?.append(
                URLQueryItem(name: "filename", value: filename)
            )
        }
        if let revision, !revision.isEmpty {
            components.queryItems?.append(
                URLQueryItem(name: "revision", value: revision)
            )
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            LibraryModelDetails.self,
            from: data
        )
    }

    public func localModelSources() async throws -> [LocalModelSource] {
        let request = localModelSourcesRequest()
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(LocalModelSourcesSnapshot.self, from: data).sources
    }

    public func scanLocalModels(
        path: String,
        bookmarkData: Data? = nil
    ) async throws -> LocalModelScanSnapshot {
        let request = try localModelScanRequest(path: path, bookmarkData: bookmarkData)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(LocalModelScanSnapshot.self, from: data)
    }

    public func importLocalModels(
        _ payload: LocalModelImportRequest
    ) async throws -> LocalModelImportResult {
        let request = try localModelImportRequest(payload)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(LocalModelImportResult.self, from: data)
    }

    public func modelInstalls() async throws -> [ModelInstall] {
        let request = makeRequest(path: "/manager/model-library/installs")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(ModelInstallsSnapshot.self, from: data).installs
    }

    public func modelInstallHistory() async throws -> [ModelInstall] {
        let request = modelInstallHistoryRequest()
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        let installs = try JSONDecoder.nativeSettingsDecoder()
            .decode(ModelInstallEvidenceSnapshot.self, from: data).installs
        guard installs.count < 500 else {
            throw ModelInstallHistoryError.boundReached
        }
        return installs
    }

    func modelInstallHistoryRequest() -> URLRequest {
        var components = URLComponents(
            url: endpointURL("/manager/model-library/install-evidence"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "limit", value: "500")]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        return request
    }

    public func startModelInstall(
        _ payload: StartModelInstallRequest
    ) async throws -> ModelInstall {
        let request = try startModelInstallRequest(payload)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(ModelInstall.self, from: data)
    }

    func startModelInstallRequest(
        _ payload: StartModelInstallRequest
    ) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/model-library/installs",
            method: "POST"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(payload)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    public func cancelModelInstall(id: String) async throws -> ModelInstall {
        try await mutateInstall(id: id, action: "cancel")
    }

    public func retryModelInstall(id: String) async throws -> ModelInstall {
        try await mutateInstall(id: id, action: "retry")
    }

    public func dismissModelInstall(id: String) async throws {
        let request = dismissModelInstallRequest(id: id)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
    }

    public func deleteManagedModel(
        alias: String,
        revision: String
    ) async throws -> ConfigurationSaveResult {
        try await deleteManagedModel(
            alias: alias,
            revision: revision,
            installationID: nil
        )
    }

    public func deleteManagedModel(
        alias: String,
        revision: String,
        installationID: String?
    ) async throws -> ConfigurationSaveResult {
        let request = try deleteManagedModelRequest(
            alias: alias,
            revision: revision,
            installationID: installationID
        )
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(ConfigurationSaveResult.self, from: data)
    }

    public func runtimeUpdates(refresh: Bool = false) async throws -> RuntimeUpdateSnapshot {
        var components = URLComponents(
            url: endpointURL("/manager/runtime-updates"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "refresh", value: refresh ? "true" : "false"),
        ]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        return try await sendRuntimeUpdateRequest(request)
    }

    public func checkRuntimeUpdates() async throws -> RuntimeUpdateSnapshot {
        let request = makeRequest(path: "/manager/runtime-updates/check", method: "POST")
        return try await sendRuntimeUpdateRequest(request)
    }

    public func installRuntimeUpdate(
        engine: InferenceEngine,
        version: String?
    ) async throws -> RuntimeUpdateSnapshot {
        try await installRuntimeUpdate(
            engine: engine,
            version: version,
            channel: nil
        )
    }

    public func installRuntimeUpdate(
        engine: InferenceEngine,
        version: String?,
        channel: String?
    ) async throws -> RuntimeUpdateSnapshot {
        let request = try runtimeInstallRequest(
            engine: engine,
            version: version,
            channel: channel
        )
        return try await sendRuntimeUpdateRequest(request)
    }

    public func rollbackRuntimeUpdate(
        engine: InferenceEngine
    ) async throws -> RuntimeUpdateSnapshot {
        let request = makeRequest(
            path: "/manager/runtime-updates/\(engine.rawValue)/rollback",
            method: "POST"
        )
        return try await sendRuntimeUpdateRequest(request)
    }

    public func omlxCacheHealth() async throws -> OMLXCacheHealth {
        let request = makeRequest(path: "/manager/engines/omlx/cache")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            OMLXCacheHealth.self,
            from: data
        )
    }

    public func resetOMLXCache() async throws -> OMLXCacheResetResult {
        var request = makeRequest(
            path: "/manager/engines/omlx/cache/reset",
            method: "POST"
        )
        request.timeoutInterval = 15 * 60
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(
            OMLXCacheResetResult.self,
            from: data
        )
    }

    public func configuration() async throws -> ConfigurationSnapshot {
        let request = makeRequest(path: "/manager/config")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(ConfigurationSnapshot.self, from: data)
    }

    public func saveConfiguration(
        _ settings: NativeSettings,
        revision: String
    ) async throws -> ConfigurationSaveResult {
        let request = try configurationSaveRequest(
            settings: settings,
            revision: revision
        )
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(ConfigurationSaveResult.self, from: data)
    }

    func loadRequest(model: String) throws -> URLRequest {
        let body = try JSONEncoder().encode(LoadModelRequest(model: model))
        var request = makeRequest(path: "/manager/load", method: "POST")
        request.httpBody = body
        request.timeoutInterval = 15 * 60
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    func configurationSaveRequest(
        settings: NativeSettings,
        revision: String
    ) throws -> URLRequest {
        guard settings.schemaVersion <= NativeSettings.supportedSchemaVersion else {
            throw ControlAPIError.unsupportedConfigurationSchema(settings.schemaVersion)
        }
        let body = try JSONEncoder.nativeSettingsEncoder().encode(
            SaveConfigurationRequest(config: settings, revision: revision)
        )
        var request = makeRequest(path: "/manager/config", method: "PUT")
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    func localModelSourcesRequest() -> URLRequest {
        makeRequest(path: "/manager/model-library/local-sources")
    }

    func librarySearchRequest(query: String) -> URLRequest {
        var components = URLComponents(
            url: endpointURL("/manager/model-library/search"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "q", value: query)]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        return request
    }

    func libraryFilesRequest(
        repoId: String,
        engine: InferenceEngine,
        revision: String?
    ) -> URLRequest {
        var components = URLComponents(
            url: endpointURL("/manager/model-library/files"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "engine", value: engine.rawValue),
            URLQueryItem(name: "repo_id", value: repoId),
        ]
        if let revision, !revision.isEmpty {
            components.queryItems?.append(URLQueryItem(name: "revision", value: revision))
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        return request
    }

    func localModelScanRequest(
        path: String,
        bookmarkData: Data? = nil
    ) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/model-library/local-scan",
            method: "POST"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            LocalModelScanRequest(path: path, bookmarkData: bookmarkData)
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 5 * 60
        return request
    }

    func localModelImportRequest(
        _ payload: LocalModelImportRequest
    ) throws -> URLRequest {
        var request = makeRequest(
            path: "/manager/model-library/imports",
            method: "POST"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(payload)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 5 * 60
        return request
    }

    func runtimeInstallRequest(
        engine: InferenceEngine,
        version: String?,
        channel: String? = nil
    ) throws -> URLRequest {
        let body = try JSONEncoder.nativeSettingsEncoder().encode(
            InstallRuntimeUpdateRequest(version: version, channel: channel)
        )
        var request = makeRequest(
            path: "/manager/runtime-updates/\(engine.rawValue)/install",
            method: "POST"
        )
        request.timeoutInterval = 6 * 60 * 60
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func validate(_ response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw ControlAPIError.invalidResponse
        }
        guard 200 ..< 300 ~= http.statusCode else {
            if let payload = try? JSONDecoder().decode(APIErrorPayload.self, from: data) {
                throw ControlAPIError.rejected(http.statusCode, payload.detail)
            }
            throw ControlAPIError.unexpectedStatus(http.statusCode)
        }
    }

    private func makeFleetPairingMutationRequest(
        path: String,
        payload: FleetPairingControlRequest
    ) throws -> URLRequest {
        var request = makeRequest(path: path, method: "POST")
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(payload)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func sendFleetPairingRequest(
        _ request: URLRequest
    ) async throws -> FleetPairingOperationResponse {
        guard isLoopbackControlOrigin else {
            throw FleetPairingAPIError(
                statusCode: 0,
                code: "pairing_local_control_required",
                retryable: false
            )
        }
        let (data, response) = try await session.data(
            for: request,
            delegate: FleetPairingNoRedirectDelegate.shared
        )
        guard let http = response as? HTTPURLResponse else {
            throw ControlAPIError.invalidResponse
        }
        guard 200 ..< 300 ~= http.statusCode else {
            if let envelope = try? JSONDecoder().decode(
                FleetPairingErrorEnvelope.self,
                from: data
            ) {
                throw FleetPairingAPIError(
                    statusCode: http.statusCode,
                    code: envelope.detail.code,
                    retryable: envelope.detail.retryable
                )
            }
            throw ControlAPIError.unexpectedStatus(http.statusCode)
        }
        return try JSONDecoder().decode(
            FleetPairingOperationResponse.self,
            from: data
        )
    }

    private func sendFleetPairingPresenceRequest(
        _ request: URLRequest
    ) async throws -> FleetPairingPresenceResponse {
        guard isLoopbackControlOrigin else {
            throw FleetPairingAPIError(
                statusCode: 0,
                code: "pairing_local_control_required",
                retryable: false
            )
        }
        let (data, response) = try await session.data(
            for: request,
            delegate: FleetPairingNoRedirectDelegate.shared
        )
        guard let http = response as? HTTPURLResponse else {
            throw ControlAPIError.invalidResponse
        }
        guard 200 ..< 300 ~= http.statusCode else {
            if let envelope = try? JSONDecoder().decode(
                FleetPairingErrorEnvelope.self,
                from: data
            ) {
                throw FleetPairingAPIError(
                    statusCode: http.statusCode,
                    code: envelope.detail.code,
                    retryable: envelope.detail.retryable
                )
            }
            throw ControlAPIError.unexpectedStatus(http.statusCode)
        }
        return try JSONDecoder().decode(
            FleetPairingPresenceResponse.self,
            from: data
        )
    }

    private func sendFleetPairingManagementRequest(
        _ request: URLRequest
    ) async throws -> FleetPairingManagementResponse {
        guard isLoopbackControlOrigin else {
            throw FleetPairingAPIError(
                statusCode: 0,
                code: "pairing_local_control_required",
                retryable: false
            )
        }
        let (data, response) = try await session.data(
            for: request,
            delegate: FleetPairingNoRedirectDelegate.shared
        )
        guard let http = response as? HTTPURLResponse else {
            throw ControlAPIError.invalidResponse
        }
        guard 200 ..< 300 ~= http.statusCode else {
            if let envelope = try? JSONDecoder().decode(
                FleetPairingErrorEnvelope.self,
                from: data
            ) {
                throw FleetPairingAPIError(
                    statusCode: http.statusCode,
                    code: envelope.detail.code,
                    retryable: envelope.detail.retryable
                )
            }
            throw ControlAPIError.unexpectedStatus(http.statusCode)
        }
        return try JSONDecoder().decode(
            FleetPairingManagementResponse.self,
            from: data
        )
    }

    private func mutateDesiredInstall(
        jobID: String,
        jobRevision: Int,
        action: String
    ) async throws -> DesiredInstallDetailSnapshot {
        let request = try desiredInstallMutationRequest(
            jobID: jobID,
            jobRevision: jobRevision,
            action: action
        )
        return try await sendDesiredInstallRequest(request)
    }

    private func sendDesiredInstallRequest(
        _ request: URLRequest
    ) async throws -> DesiredInstallDetailSnapshot {
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(
            DesiredInstallDetailSnapshot.self,
            from: data
        )
    }

    private func sendNativeLifecycleRequest<Response: Decodable>(
        _ request: URLRequest,
        as type: Response.Type
    ) async throws -> Response {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw ControlAPIError.invalidResponse
        }
        guard 200 ..< 300 ~= http.statusCode else {
            if let envelope = try? JSONDecoder().decode(
                NativeLifecycleErrorEnvelope.self,
                from: data
            ), envelope.detail.code.range(
                of: #"^[a-z][a-z0-9_]{0,127}$"#,
                options: .regularExpression
            ) != nil {
                throw NativeLifecycleAPIError(
                    statusCode: http.statusCode,
                    code: envelope.detail.code
                )
            }
            throw ControlAPIError.unexpectedStatus(http.statusCode)
        }
        return try JSONDecoder().decode(type, from: data)
    }

    private var isLoopbackControlOrigin: Bool {
        switch baseURL.host?.lowercased() {
        case "127.0.0.1", "::1", "localhost":
            true
        default:
            false
        }
    }

    private func mutateInstall(id: String, action: String) async throws -> ModelInstall {
        let encodedID = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let request = makeRequest(
            path: "/manager/model-library/installs/\(encodedID)/\(action)",
            method: "POST"
        )
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(ModelInstall.self, from: data)
    }

    func dismissModelInstallRequest(id: String) -> URLRequest {
        let encodedID = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        return makeRequest(
            path: "/manager/model-library/installs/\(encodedID)",
            method: "DELETE"
        )
    }

    func deleteManagedModelRequest(
        alias: String,
        revision: String,
        installationID: String? = nil
    ) throws -> URLRequest {
        let encodedAlias =
            alias.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? alias
        var request = makeRequest(
            path: "/manager/models/\(encodedAlias)",
            method: "DELETE"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            DeleteManagedModelRequest(
                revision: revision,
                installationId: installationID
            )
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func sendRuntimeUpdateRequest(
        _ request: URLRequest
    ) async throws -> RuntimeUpdateSnapshot {
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder()
            .decode(RuntimeUpdateSnapshot.self, from: data)
    }

    private func makeRequest(path: String, method: String = "GET") -> URLRequest {
        var request = URLRequest(url: endpointURL(path))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
        return request
    }

    private func addAuthorization(to request: inout URLRequest) {
        if let authorizationHeader {
            request.setValue(authorizationHeader, forHTTPHeaderField: "Authorization")
        }
    }
}
