import Foundation

public protocol ControlAPI: Sendable {
    func status() async throws -> ServiceSnapshot
    func models() async throws -> ModelCatalogSnapshot
    func load(model: String) async throws -> ServiceSnapshot
    func unload() async throws
    func validateConfiguration(_ configYAML: String) async throws -> ConfigurationValidation
    func reloadConfiguration() async throws -> ConfigurationReloadResult
}

public enum ControlAPIError: Error, Equatable, LocalizedError {
    case invalidResponse
    case unexpectedStatus(Int)
    case rejected(Int, String)

    public var errorDescription: String? {
        switch self {
        case .invalidResponse:
            "The Mnemosyne control service returned an invalid response."
        case let .unexpectedStatus(status):
            "The Mnemosyne control service returned HTTP \(status)."
        case let .rejected(_, detail):
            detail
        }
    }
}

public struct ConfigurationValidation: Codable, Equatable, Sendable {
    public let valid: Bool
    public let modelCount: Int

    enum CodingKeys: String, CodingKey {
        case valid
        case modelCount = "model_count"
    }
}

public struct ConfigurationReloadResult: Codable, Equatable, Sendable {
    public let reloaded: Bool
}

struct ConfigurationValidationRequest: Codable, Equatable {
    let configYAML: String

    enum CodingKeys: String, CodingKey {
        case configYAML = "config_yaml"
    }
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

    public func validateConfiguration(
        _ configYAML: String
    ) async throws -> ConfigurationValidation {
        let request = try configurationValidationRequest(configYAML: configYAML)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(ConfigurationValidation.self, from: data)
    }

    public func reloadConfiguration() async throws -> ConfigurationReloadResult {
        let request = makeRequest(path: "/manager/reload", method: "POST")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try JSONDecoder().decode(ConfigurationReloadResult.self, from: data)
    }

    func loadRequest(model: String) throws -> URLRequest {
        let body = try JSONEncoder().encode(LoadModelRequest(model: model))
        var request = makeRequest(path: "/manager/load", method: "POST")
        request.httpBody = body
        request.timeoutInterval = 15 * 60
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    func configurationValidationRequest(configYAML: String) throws -> URLRequest {
        let body = try JSONEncoder().encode(
            ConfigurationValidationRequest(configYAML: configYAML)
        )
        var request = makeRequest(path: "/manager/config/validate", method: "POST")
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
