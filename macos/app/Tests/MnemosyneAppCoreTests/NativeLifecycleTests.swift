import Foundation
import Testing
@testable import MnemosyneAppCore

private let lifecycleTransactionID =
    "44444444-4444-4444-8444-444444444444"
private let alternateLifecycleTransactionID =
    "55555555-5555-4555-8555-555555555555"

@Test("Native lifecycle previews expose only path-free effects and counts")
func nativeLifecyclePreviewDecoding() throws {
    let preview = try decodeUninstallPreview(
        plan: uninstallPlanObject(
            transactionID: lifecycleTransactionID,
            retentionMode: "remove_exclusive_managed",
            receiptDigest: String(repeating: "a", count: 64),
            retainedCount: 3,
            trashCount: 1
        )
    )

    #expect(preview.schemaVersion == 1)
    #expect(preview.preparable)
    #expect(!preview.executionAvailable)
    #expect(preview.plan.retentionMode == .removeExclusiveManaged)
    #expect(preview.plan.tokenOutboxCount == 0)
    #expect(preview.plan.retentionManifest.itemCount == 4)
    #expect(preview.plan.retentionManifest.retainedCount == 3)
    #expect(preview.plan.retentionManifest.trashCount == 1)
    #expect(preview.plan.components.count == 6)
    #expect(
        preview.plan.components.first { $0.kind == .securityScopes }?
            .disposition == .retain
    )
    #expect(
        preview.plan.components.first { $0.kind == .managedRuntimes }?
            .disposition == .removeProvenMembers
    )
}

@Test("Native lifecycle responses reject unmodeled path and bookmark members")
func nativeLifecycleResponseRejectsSensitiveMembers() throws {
    var plan = uninstallPlanObject(transactionID: lifecycleTransactionID)
    plan["exact_path"] = "/Volumes/Athena/private/model.gguf"
    plan["bookmark_data"] = "secret-bookmark"

    #expect(throws: DecodingError.self) {
        _ = try decodeUninstallPreview(plan: plan)
    }

    var componentPlan = uninstallPlanObject(transactionID: lifecycleTransactionID)
    var components = try #require(
        componentPlan["components"] as? [[String: Any]]
    )
    components[0]["authority"] = String(repeating: "f", count: 64)
    componentPlan["components"] = components
    #expect(throws: DecodingError.self) {
        _ = try decodeUninstallPreview(plan: componentPlan)
    }
}

@Test("Lifecycle confirmation fences the exact private-manifest receipt")
func nativeLifecycleEffectFence() throws {
    let confirmed = try decodeUninstallPreview(
        plan: uninstallPlanObject(
            transactionID: lifecycleTransactionID,
            receiptDigest: String(repeating: "a", count: 64)
        )
    )
    let freshSameEffects = try decodeUninstallPreview(
        plan: uninstallPlanObject(
            transactionID: alternateLifecycleTransactionID,
            receiptDigest: String(repeating: "a", count: 64)
        )
    )
    let freshDifferentManifest = try decodeUninstallPreview(
        plan: uninstallPlanObject(
            transactionID: alternateLifecycleTransactionID,
            receiptDigest: String(repeating: "b", count: 64)
        )
    )

    #expect(
        confirmed.plan.hasSamePreparedEffects(as: freshSameEffects.plan)
    )
    #expect(
        !confirmed.plan.hasSamePreparedEffects(as: freshDifferentManifest.plan)
    )
}

@Test("Native lifecycle status and prepared transactions remain path-free")
func nativeLifecycleStatusAndTransactionDecoding() throws {
    let plan = uninstallPlanObject(transactionID: lifecycleTransactionID)
    let transaction: [String: Any] = [
        "schema_version": 1,
        "transaction_id": lifecycleTransactionID,
        "kind": "uninstall",
        "phase": "prepared",
        "terminal": false,
        "needs_recovery": true,
        "created_at": 1_788_027_600.0,
        "updated_at": 1_788_027_601.0,
        "error_code": NSNull(),
        "plan": plan,
    ]
    let statusData = try JSONSerialization.data(
        withJSONObject: [
            "schema_version": 1,
            "available": true,
            "error_code": NSNull(),
            "execution_available": false,
            "migration_preview_available": false,
            "incomplete_count": 1,
            "incomplete": [transaction],
        ]
    )
    let status = try JSONDecoder().decode(
        NativeLifecycleStatusSnapshot.self,
        from: statusData
    )

    #expect(status.available)
    #expect(!status.executionAvailable)
    #expect(!status.migrationPreviewAvailable)
    #expect(status.incompleteCount == 1)
    #expect(status.incomplete.first?.phase == .prepared)
    #expect(status.incomplete.first?.plan.uninstall?.retentionMode == .appOnly)

    let prepareData = try JSONSerialization.data(
        withJSONObject: [
            "schema_version": 1,
            "prepared": true,
            "replayed": false,
            "execution_available": false,
            "transaction": transaction,
        ]
    )
    let prepared = try JSONDecoder().decode(
        NativeLifecyclePrepareResponse.self,
        from: prepareData
    )
    #expect(prepared.prepared)
    #expect(prepared.transaction == status.incomplete.first)
}

@Test("Native lifecycle v2 decodes helper-stage contract and version")
func nativeLifecycleV2TransactionDecoding() throws {
    var plan = uninstallPlanObject(transactionID: lifecycleTransactionID)
    plan["schema_version"] = 2
    var retention = try #require(
        plan["retention_manifest"] as? [String: Any]
    )
    retention["schema_version"] = 1
    plan["retention_manifest"] = retention
    let data = try JSONSerialization.data(
        withJSONObject: [
            "schema_version": 2,
            "contract_version": 2,
            "transaction_id": lifecycleTransactionID,
            "kind": "uninstall",
            "phase": "helper_staged",
            "terminal": false,
            "needs_recovery": true,
            "created_at": 1_788_027_600.0,
            "updated_at": 1_788_027_601.0,
            "error_code": NSNull(),
            "plan": plan,
        ]
    )
    let transaction = try JSONDecoder().decode(
        NativeLifecycleTransaction.self,
        from: data
    )

    #expect(transaction.schemaVersion == 2)
    #expect(transaction.contractVersion == 2)
    #expect(transaction.phase == .helperStaged)
    #expect(transaction.plan.uninstall?.schemaVersion == 2)
}

@Test("Native lifecycle client requests use closed authenticated bodies")
func nativeLifecycleRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let status = client.nativeLifecycleStatusRequest()
    #expect(
        status.url?.absoluteString
            == "http://localhost:17321/manager/native-lifecycle"
    )
    #expect(status.httpMethod == "GET")
    #expect(
        status.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )

    let preview = try client.nativeUninstallPreviewRequest(
        retentionMode: .keepWeights
    )
    #expect(
        preview.url?.absoluteString
            == "http://localhost:17321/manager/native-lifecycle/uninstall/preview"
    )
    #expect(preview.httpMethod == "POST")
    let previewBody = try #require(preview.httpBody)
    let previewObject = try #require(
        JSONSerialization.jsonObject(with: previewBody) as? [String: Any]
    )
    #expect(previewObject.count == 2)
    #expect(previewObject["schema_version"] as? Int == 1)
    #expect(
        previewObject["retention_mode"] as? String
            == "remove_state_runtimes_keep_weights"
    )

    let prepare = try client.nativeUninstallPrepareRequest(
        transactionID: lifecycleTransactionID,
        retentionMode: .keepWeights
    )
    let prepareBody = try #require(prepare.httpBody)
    let prepareObject = try #require(
        JSONSerialization.jsonObject(with: prepareBody) as? [String: Any]
    )
    #expect(prepareObject.count == 3)
    #expect(prepareObject["transaction_id"] as? String == lifecycleTransactionID)
    #expect(prepareObject["path"] == nil)
    #expect(prepareObject["digest"] == nil)

    let read = try client.nativeLifecycleTransactionRequest(
        transactionID: lifecycleTransactionID
    )
    #expect(
        read.url?.absoluteString
            == "http://localhost:17321/manager/native-lifecycle/transactions/\(lifecycleTransactionID)"
    )

    let perform = try client.nativeLifecycleAuthorizationPerformRequest(
        transactionID: lifecycleTransactionID
    )
    #expect(
        perform.url?.absoluteString
            == "http://localhost:17321/manager/native-lifecycle/transactions/\(lifecycleTransactionID)/authorization/perform"
    )
    #expect(perform.httpMethod == "POST")
    let performBody = try #require(perform.httpBody)
    let performObject = try #require(
        JSONSerialization.jsonObject(with: performBody) as? [String: Any]
    )
    #expect(performObject.count == 1)
    #expect(performObject["schema_version"] as? Int == 2)
    #expect(perform.timeoutInterval > 120)
}

@Test("Native lifecycle transaction paths require canonical UUIDs")
func nativeLifecycleRequestValidation() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!
    )
    #expect(throws: NativeLifecycleRequestError.invalidTransactionID) {
        _ = try client.nativeLifecycleTransactionRequest(
            transactionID: "../../private-state"
        )
    }
    #expect(throws: NativeLifecycleRequestError.invalidTransactionID) {
        _ = try client.nativeUninstallPrepareRequest(
            transactionID:
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".uppercased(),
            retentionMode: .appOnly
        )
    }
}

private func decodeUninstallPreview(
    plan: [String: Any]
) throws -> NativeLifecycleUninstallPreview {
    let data = try JSONSerialization.data(
        withJSONObject: [
            "schema_version": 1,
            "preparable": true,
            "execution_available": false,
            "plan": plan,
        ]
    )
    return try JSONDecoder().decode(
        NativeLifecycleUninstallPreview.self,
        from: data
    )
}

private func uninstallPlanObject(
    transactionID: String,
    retentionMode: String = "app_only",
    receiptDigest: String = String(repeating: "a", count: 64),
    retainedCount: Int = 4,
    trashCount: Int = 0
) -> [String: Any] {
    let removeRuntimes = retentionMode != "app_only"
    return [
        "schema_version": 1,
        "kind": "uninstall",
        "transaction_id": transactionID,
        "retention_mode": retentionMode,
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
            ["kind": "managed_runtimes", "disposition": removeRuntimes ? "remove_proven_members" : "retain"],
            ["kind": "security_scopes", "disposition": "retain"],
            ["kind": "pairing_state", "disposition": "retain"],
        ],
        "retention_manifest": [
            "schema_version": 1,
            "digest": receiptDigest,
            "item_count": retainedCount + trashCount,
            "retained_count": retainedCount,
            "trash_count": trashCount,
        ],
    ]
}
