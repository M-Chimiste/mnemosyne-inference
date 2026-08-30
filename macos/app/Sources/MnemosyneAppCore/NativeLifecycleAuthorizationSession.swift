import Foundation

public protocol NativeLifecycleAuthorizationServicing: Sendable {
    func performNativeLifecycleAuthorization(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationResponse
}

/// Requests the service-owned helper ceremony. The menu process must not
/// create the helper socketpair: the helper's sealed peer manifest authorizes
/// only the bundled service Python as its direct peer.
public struct NativeLifecycleAuthorizationSession: Sendable {
    private let service: any NativeLifecycleAuthorizationServicing

    public init(
        service: any NativeLifecycleAuthorizationServicing
    ) {
        self.service = service
    }

    public func authorize(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationResponse {
        guard UUID(uuidString: transactionID)?.uuidString.lowercased()
            == transactionID
        else {
            throw NativeLifecycleRequestError.invalidTransactionID
        }
        let accepted = try await service.performNativeLifecycleAuthorization(
            transactionID: transactionID
        )
        guard accepted.authorized,
              accepted.transaction.transactionID == transactionID,
              accepted.transaction.phase == .authorized,
              !accepted.executionAvailable
        else {
            throw NativeLifecycleRequestError.invalidAuthorizationResponse
        }
        return accepted
    }
}
