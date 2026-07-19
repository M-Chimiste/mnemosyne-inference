import Foundation

public protocol ControlAPI: Sendable {
    func status() async throws -> ServiceSnapshot
    func models() async throws -> ModelCatalogSnapshot
    func load(model: String) async throws -> ServiceSnapshot
    func unload() async throws
}

public enum ControlAPIError: Error, Equatable, LocalizedError {
    case invalidResponse
    case unexpectedStatus(Int)

    public var errorDescription: String? {
        switch self {
        case .invalidResponse:
            "The Mnemosyne control service returned an invalid response."
        case let .unexpectedStatus(status):
            "The Mnemosyne control service returned HTTP \(status)."
        }
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
        try validate(response)
        return try JSONDecoder().decode(ServiceSnapshot.self, from: data)
    }

    public func models() async throws -> ModelCatalogSnapshot {
        let request = makeRequest(path: "/manager/models")
        let (data, response) = try await session.data(for: request)
        try validate(response)
        return try JSONDecoder().decode(ModelCatalogSnapshot.self, from: data)
    }

    public func load(model: String) async throws -> ServiceSnapshot {
        let request = try loadRequest(model: model)
        let (data, response) = try await session.data(for: request)
        try validate(response)
        return try JSONDecoder().decode(ServiceSnapshot.self, from: data)
    }

    public func unload() async throws {
        let request = makeRequest(path: "/manager/unload", method: "POST")
        let (_, response) = try await session.data(for: request)
        try validate(response)
    }

    func loadRequest(model: String) throws -> URLRequest {
        let body = try JSONEncoder().encode(LoadModelRequest(model: model))
        var request = makeRequest(path: "/manager/load", method: "POST")
        request.httpBody = body
        request.timeoutInterval = 15 * 60
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    private func validate(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else {
            throw ControlAPIError.invalidResponse
        }
        guard 200 ..< 300 ~= http.statusCode else {
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
