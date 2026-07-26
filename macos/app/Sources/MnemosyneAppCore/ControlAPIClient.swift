import Foundation

public protocol ControlAPI: Sendable {
    func status() async throws -> ServiceSnapshot
    func models() async throws -> ModelCatalogSnapshot
    func load(model: String) async throws -> ServiceSnapshot
    func unload() async throws
    func storageLocations() async throws -> StorageSnapshot
    func inspectStorage(path: String, bookmarkData: Data?) async throws -> StorageStatus
    func searchLibrary(query: String, engine: InferenceEngine) async throws -> [LibraryModel]
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
    func startModelInstall(_ request: StartModelInstallRequest) async throws -> ModelInstall
    func cancelModelInstall(id: String) async throws -> ModelInstall
    func retryModelInstall(id: String) async throws -> ModelInstall
    func dismissModelInstall(id: String) async throws
    func deleteManagedModel(
        alias: String, revision: String
    ) async throws -> ConfigurationSaveResult
    func runtimeUpdates(refresh: Bool) async throws -> RuntimeUpdateSnapshot
    func checkRuntimeUpdates() async throws -> RuntimeUpdateSnapshot
    func installRuntimeUpdate(
        engine: InferenceEngine, version: String?
    ) async throws -> RuntimeUpdateSnapshot
    func rollbackRuntimeUpdate(engine: InferenceEngine) async throws -> RuntimeUpdateSnapshot
    func configuration() async throws -> ConfigurationSnapshot
    func saveConfiguration(
        _ settings: NativeSettings, revision: String
    ) async throws -> ConfigurationSaveResult
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
}

struct DeleteManagedModelRequest: Codable, Equatable, Sendable {
    let revision: String
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
        query: String,
        engine: InferenceEngine
    ) async throws -> [LibraryModel] {
        var components = URLComponents(
            url: endpointURL("/manager/model-library/search"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "engine", value: engine.rawValue),
            URLQueryItem(name: "q", value: query),
        ]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        addAuthorization(to: &request)
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

    public func startModelInstall(
        _ payload: StartModelInstallRequest
    ) async throws -> ModelInstall {
        var request = makeRequest(path: "/manager/model-library/installs", method: "POST")
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(payload)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder.nativeSettingsDecoder().decode(ModelInstall.self, from: data)
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
        let request = try deleteManagedModelRequest(
            alias: alias,
            revision: revision
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
        let request = try runtimeInstallRequest(engine: engine, version: version)
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
        version: String?
    ) throws -> URLRequest {
        let body = try JSONEncoder.nativeSettingsEncoder().encode(
            InstallRuntimeUpdateRequest(version: version)
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
        revision: String
    ) throws -> URLRequest {
        let encodedAlias =
            alias.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? alias
        var request = makeRequest(
            path: "/manager/models/\(encodedAlias)",
            method: "DELETE"
        )
        request.httpBody = try JSONEncoder.nativeSettingsEncoder().encode(
            DeleteManagedModelRequest(revision: revision)
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
