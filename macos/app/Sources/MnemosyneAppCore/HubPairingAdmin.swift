import Foundation

public struct HubPairingClaim: Codable, Equatable, Identifiable, Sendable {
    public let schemaVersion: Int
    public let claimID: String
    public let invitationID: String
    public let pairingID: String
    public let displayName: String
    public let reportingNodeID: String
    public let serviceVersion: String
    public let platform: String
    public let protocolVersion: Int
    public let state: String
    public let claimedAt: Double
    public let expiresAt: Double

    public var id: String { claimID }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case claimID = "claim_id"
        case invitationID = "invitation_id"
        case pairingID = "pairing_id"
        case displayName = "display_name"
        case reportingNodeID = "reporting_node_id"
        case serviceVersion = "service_version"
        case platform
        case protocolVersion = "protocol_version"
        case state
        case claimedAt = "claimed_at"
        case expiresAt = "expires_at"
    }
}

public struct HubPairingClaimList: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let claims: [HubPairingClaim]

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case claims
    }
}

public struct HubPairingEnrollment: Codable, Equatable, Identifiable, Sendable {
    public let schemaVersion: Int
    public let pairingID: String
    public let reportingNodeID: String
    public let displayName: String
    public let platform: String
    public let serviceVersion: String
    public let protocolVersion: Int
    public let serviceClass: String
    public let state: String
    public let hubEnabled: Bool
    public let credentialGeneration: Int?
    public let createdAt: Double
    public let updatedAt: Double
    public let revokedAt: Double?
    public let failureCode: String?

    public var id: String { pairingID }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case pairingID = "pairing_id"
        case reportingNodeID = "reporting_node_id"
        case displayName = "display_name"
        case platform
        case serviceVersion = "service_version"
        case protocolVersion = "protocol_version"
        case serviceClass = "service_class"
        case state
        case hubEnabled = "hub_enabled"
        case credentialGeneration = "credential_generation"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case revokedAt = "revoked_at"
        case failureCode = "failure_code"
    }
}

public struct HubPairingEnrollmentList: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let enrollments: [HubPairingEnrollment]

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case enrollments
    }
}

public enum HubPairingAdminError: Error, Equatable, LocalizedError {
    case invalidConfiguration
    case invalidClaimID
    case invalidPairingID
    case invalidPIN
    case unavailable
    case invalidResponse
    case rejected(statusCode: Int, code: String)

    public var errorDescription: String? {
        switch self {
        case .invalidConfiguration:
            "The local Hub administration connection is not configured safely."
        case .invalidClaimID, .invalidPairingID:
            "The Hub returned an invalid pairing identity."
        case .invalidPIN:
            "Enter the six-digit code shown on the Mac."
        case .unavailable:
            "The local Hub service is unavailable."
        case .invalidResponse:
            "The local Hub returned an invalid administration response."
        case let .rejected(statusCode, code):
            switch code {
            case "pairing_presence_pin_rejected":
                "That code did not match. Check the six digits shown on the Mac."
            case "pairing_claim_expired", "pairing_expired":
                "That pairing request expired. Request to join again from the Mac."
            case "pairing_claim_unknown":
                "That pairing request is no longer pending. Refresh the list and try again."
            default:
                statusCode == 401
                    ? "The local Hub rejected its private administration credential."
                    : "The local Hub rejected the pairing operation (\(code))."
            }
        }
    }
}

/// A loopback-only client for the Hub's small native pairing surface.
///
/// The private admin bearer is read just-in-time by Hub Mode and is never
/// stored in SwiftUI state. Requests ignore ambient proxies and refuse HTTP
/// redirects so the bearer cannot leave the local Hub boundary.
public struct HubPairingAdminClient: Sendable {
    public static let localBaseURL = URL(string: "http://127.0.0.1:17400")!

    private let baseURL: URL
    private let adminKey: String

    public init(
        adminKey: String,
        baseURL: URL = HubPairingAdminClient.localBaseURL
    ) throws {
        guard
            !adminKey.isEmpty,
            adminKey.utf8.count <= 4_096,
            !adminKey.contains(where: { $0.isNewline }),
            let components = URLComponents(
                url: baseURL,
                resolvingAgainstBaseURL: false
            ),
            components.scheme == "http",
            components.host == "127.0.0.1",
            components.port == 17_400,
            components.user == nil,
            components.password == nil,
            components.query == nil,
            components.fragment == nil,
            components.path.isEmpty || components.path == "/"
        else {
            throw HubPairingAdminError.invalidConfiguration
        }
        self.baseURL = baseURL
        self.adminKey = adminKey
    }

    public func pendingClaimsRequest() -> URLRequest {
        var request = authorizedRequest(path: "fleet/api/v1/pairing/claims")
        var components = URLComponents(
            url: request.url!,
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [URLQueryItem(name: "limit", value: "1000")]
        request.url = components.url!
        return request
    }

    public func enrollmentsRequest() -> URLRequest {
        authorizedRequest(path: "fleet/api/v1/pairing/enrollments")
    }

    public func approvePresenceRequest(
        claimID: String,
        pin: String,
        requestID: String = UUID().uuidString.lowercased()
    ) throws -> URLRequest {
        guard Self.isCanonicalUUID(claimID) else {
            throw HubPairingAdminError.invalidClaimID
        }
        guard Self.isCanonicalUUID(requestID) else {
            throw HubPairingAdminError.invalidClaimID
        }
        guard Self.isPIN(pin) else { throw HubPairingAdminError.invalidPIN }
        return try jsonRequest(
            path: "fleet/api/v1/pairing/claims/\(claimID)/approve-presence",
            method: "POST",
            body: PresenceApproval(
                schemaVersion: 1,
                requestID: requestID,
                presencePIN: pin,
                serviceClass: "primary",
                hubEnabled: false
            )
        )
    }

    public func setEnrollmentEnabledRequest(
        pairingID: String,
        enabled: Bool,
        requestID: String = UUID().uuidString.lowercased()
    ) throws -> URLRequest {
        guard Self.isCanonicalUUID(pairingID) else {
            throw HubPairingAdminError.invalidPairingID
        }
        guard Self.isCanonicalUUID(requestID) else {
            throw HubPairingAdminError.invalidPairingID
        }
        return try jsonRequest(
            path: "fleet/api/v1/pairing/enrollments/\(pairingID)/enabled",
            method: "PUT",
            body: EnrollmentPolicy(
                schemaVersion: 1,
                requestID: requestID,
                enabled: enabled
            )
        )
    }

    public func pendingClaims() async throws -> [HubPairingClaim] {
        let response: HubPairingClaimList = try await perform(
            pendingClaimsRequest()
        )
        guard response.schemaVersion == 1 else {
            throw HubPairingAdminError.invalidResponse
        }
        return response.claims
    }

    public func enrollments() async throws -> [HubPairingEnrollment] {
        let response: HubPairingEnrollmentList = try await perform(
            enrollmentsRequest()
        )
        guard response.schemaVersion == 1 else {
            throw HubPairingAdminError.invalidResponse
        }
        return response.enrollments
    }

    public func approvePresence(
        claimID: String,
        pin: String
    ) async throws -> HubPairingEnrollment {
        try await perform(
            approvePresenceRequest(claimID: claimID, pin: pin)
        )
    }

    public func setEnrollmentEnabled(
        pairingID: String,
        enabled: Bool
    ) async throws -> HubPairingEnrollment {
        try await perform(
            setEnrollmentEnabledRequest(
                pairingID: pairingID,
                enabled: enabled
            )
        )
    }

    private func authorizedRequest(path: String) -> URLRequest {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = "GET"
        request.timeoutInterval = 5
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.setValue("Bearer \(adminKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private func jsonRequest<Body: Encodable>(
        path: String,
        method: String,
        body: Body
    ) throws -> URLRequest {
        var request = authorizedRequest(path: path)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        request.httpBody = try encoder.encode(body)
        return request
    }

    private func perform<Response: Decodable>(
        _ request: URLRequest
    ) async throws -> Response {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        let delegate = HubPairingNoRedirectDelegate()
        let session = URLSession(
            configuration: configuration,
            delegate: delegate,
            delegateQueue: nil
        )
        defer { session.invalidateAndCancel() }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw HubPairingAdminError.unavailable
        }
        guard
            data.count <= 1_048_576,
            let http = response as? HTTPURLResponse
        else {
            throw HubPairingAdminError.invalidResponse
        }
        guard (200 ..< 300).contains(http.statusCode) else {
            throw HubPairingAdminError.rejected(
                statusCode: http.statusCode,
                code: Self.errorCode(in: data, statusCode: http.statusCode)
            )
        }
        do {
            return try JSONDecoder().decode(Response.self, from: data)
        } catch {
            throw HubPairingAdminError.invalidResponse
        }
    }

    private static func errorCode(in data: Data, statusCode: Int) -> String {
        guard
            let envelope = try? JSONDecoder().decode(
                HubPairingErrorEnvelope.self,
                from: data
            ),
            let value = envelope.detail?.code ?? envelope.errorCode,
            !value.isEmpty,
            value.utf8.count <= 128,
            value.unicodeScalars.allSatisfy({
                $0.value == 95
                    || (48 ... 57).contains($0.value)
                    || (97 ... 122).contains($0.value)
            })
        else { return "hub_admin_http_\(statusCode)" }
        return value
    }

    private static func isCanonicalUUID(_ value: String) -> Bool {
        value.utf8.count == 36
            && value == value.lowercased()
            && UUID(uuidString: value) != nil
    }

    private static func isPIN(_ value: String) -> Bool {
        value.utf8.count == 6
            && value.utf8.allSatisfy { (48 ... 57).contains($0) }
    }
}

private struct PresenceApproval: Encodable {
    let schemaVersion: Int
    let requestID: String
    let presencePIN: String
    let serviceClass: String
    let hubEnabled: Bool

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case requestID = "request_id"
        case presencePIN = "presence_pin"
        case serviceClass = "service_class"
        case hubEnabled = "hub_enabled"
    }
}

private struct EnrollmentPolicy: Encodable {
    let schemaVersion: Int
    let requestID: String
    let enabled: Bool

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case requestID = "request_id"
        case enabled
    }
}

private struct HubPairingErrorEnvelope: Decodable {
    struct Detail: Decodable { let code: String? }

    let detail: Detail?
    let errorCode: String?

    private enum CodingKeys: String, CodingKey {
        case detail
        case errorCode = "error_code"
    }
}

private final class HubPairingNoRedirectDelegate: NSObject,
    URLSessionTaskDelegate,
    @unchecked Sendable
{
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
