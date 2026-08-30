import Foundation
import Testing

@testable import MnemosyneAppCore

private let authorizationTransactionID =
    "66666666-6666-4666-8666-666666666666"

private actor FakeLifecycleAuthorizationService:
    NativeLifecycleAuthorizationServicing
{
    let response: NativeLifecycleAuthorizationResponse
    let failure: Failure?
    private(set) var performed = 0

    enum Failure: Error, Equatable {
        case unavailable
    }

    init(
        response: NativeLifecycleAuthorizationResponse,
        failure: Failure? = nil
    ) {
        self.response = response
        self.failure = failure
    }

    func performNativeLifecycleAuthorization(
        transactionID: String
    ) async throws -> NativeLifecycleAuthorizationResponse {
        performed += 1
        if let failure {
            throw failure
        }
        return response
    }

    func count() -> Int {
        performed
    }
}

@Test("Lifecycle authorization is performed by the loopback service")
func nativeLifecycleAuthorizationHappyFlow() async throws {
    let service = try FakeLifecycleAuthorizationService(
        response: decodeAuthorizationResponse()
    )
    let result = try await NativeLifecycleAuthorizationSession(
        service: service
    ).authorize(transactionID: authorizationTransactionID)

    #expect(result.authorized)
    #expect(result.transaction.phase == .authorized)
    #expect(await service.count() == 1)
}

@Test("Service helper transport failure is surfaced without a menu helper")
func nativeLifecycleAuthorizationServiceFailure() async throws {
    let service = try FakeLifecycleAuthorizationService(
        response: decodeAuthorizationResponse(),
        failure: .unavailable
    )
    await #expect(throws: FakeLifecycleAuthorizationService.Failure.unavailable) {
        _ = try await NativeLifecycleAuthorizationSession(
            service: service
        ).authorize(transactionID: authorizationTransactionID)
    }
    #expect(await service.count() == 1)
}

private func decodeAuthorizationResponse() throws
    -> NativeLifecycleAuthorizationResponse
{
    let transaction = authorizationTransactionObject()
    let data = try JSONSerialization.data(withJSONObject: [
        "schema_version": 2,
        "authorized": true,
        "replayed": false,
        "execution_available": false,
        "transaction": transaction,
    ])
    return try JSONDecoder().decode(
        NativeLifecycleAuthorizationResponse.self,
        from: data
    )
}

private func authorizationTransactionObject() -> [String: Any] {
    let plan: [String: Any] = [
        "schema_version": 2,
        "kind": "uninstall",
        "transaction_id": authorizationTransactionID,
        "retention_mode": "app_only",
        "product": [
            "application_name": "Unified Inference.app",
            "application_bundle_id": "com.mnemosyne.inference.menu",
            "launch_agent_label": "com.mnemosyne.inference.agent",
            "service_code_requirement_id": "com.mnemosyne.inference.service",
        ],
        "token_outbox_count": 0,
        "outbox_decision": "preserve_with_state",
        "hub_revocation_state": "not_requested",
        "components": [
            ["kind": "application", "disposition": "remove_exact"],
            ["kind": "launch_agent", "disposition": "remove_exact"],
            ["kind": "private_state", "disposition": "retain"],
            ["kind": "managed_runtimes", "disposition": "retain"],
            ["kind": "security_scopes", "disposition": "retain"],
            ["kind": "pairing_state", "disposition": "retain"],
        ],
        "retention_manifest": [
            "schema_version": 1,
            "digest": String(repeating: "a", count: 64),
            "item_count": 0,
            "retained_count": 0,
            "trash_count": 0,
        ],
    ]
    return [
        "schema_version": 2,
        "contract_version": 2,
        "transaction_id": authorizationTransactionID,
        "kind": "uninstall",
        "phase": "authorized",
        "terminal": false,
        "needs_recovery": true,
        "created_at": 1_788_027_600.0,
        "updated_at": 1_788_027_601.0,
        "error_code": NSNull(),
        "plan": plan,
    ]
}
