import Foundation
import Testing
@testable import MnemosyneAppCore

private let desiredJobID = "77777777-7777-4777-8777-777777777777"

@Test("Desired-install snapshots decode only opaque placement and progress identities")
func desiredInstallSnapshotDecoding() throws {
    let snapshot = try decodeDesiredInstallList(
        executorAvailable: true,
        approvalAvailable: true,
        item: desiredInstallItem(
            state: "downloading",
            installationID: "33333333-3333-4333-8333-333333333333",
            bytesDownloaded: 72_000_000_000,
            totalBytes: 144_000_000_000,
            resultCode: "future_catalog_policy",
            cancellationAvailable: true
        )
    )

    let item = try #require(snapshot.items.first)
    #expect(snapshot.schemaVersion == 1)
    #expect(snapshot.executorAvailable)
    #expect(snapshot.approvalAvailable)
    #expect(item.job.jobID == desiredJobID)
    #expect(item.job.jobRevision == 1)
    #expect(item.job.engine == .omlx)
    #expect(item.job.storageLocationID == "22222222-2222-4222-8222-222222222222")
    #expect(item.job.recommendationBasis.inventorySequence == 42)
    #expect(item.acknowledgement.installationID == "33333333-3333-4333-8333-333333333333")
    #expect(item.acknowledgement.bytesDownloaded == 72_000_000_000)
    #expect(item.acknowledgement.progressFraction == 0.5)
    #expect(item.acknowledgement.resultCode == "future_catalog_policy")
    #expect(item.canCancel)
    #expect(!item.canApprove)
    #expect(!item.canRefuse)
}

@Test("Older local-action snapshots default cancellation closed")
func desiredInstallCancellationDefaultsClosed() throws {
    let snapshot = try decodeDesiredInstallList(
        executorAvailable: false,
        approvalAvailable: false,
        item: desiredInstallItem(
            state: "awaiting_local_approval",
            resultCode: "local_approval_required",
            approvalAvailable: true,
            includeCancellationAvailability: false
        )
    )

    let item = try #require(snapshot.items.first)
    #expect(item.canApprove)
    #expect(!item.canCancel)
    #expect(!item.localActions.cancellationAvailable)
    // Top-level flags remain diagnostic; exact per-item authority controls UI.
    #expect(!snapshot.executorAvailable)
    #expect(!snapshot.approvalAvailable)
}

@Test("Desired-install responses reject unmodeled path-bearing members")
func desiredInstallResponseRejectsPaths() throws {
    var item = desiredInstallItem(state: "awaiting_local_approval")
    var job = try #require(item["job"] as? [String: Any])
    job["destination"] = "/Volumes/Athena/private/model"
    item["job"] = job
    let data = try desiredInstallListData(item: item)

    #expect(throws: DecodingError.self) {
        _ = try JSONDecoder().decode(
            DesiredInstallListSnapshot.self,
            from: data
        )
    }
}

@Test("Desired-install responses require an exact job acknowledgement revision")
func desiredInstallResponseRejectsMismatchedRevision() throws {
    var item = desiredInstallItem(state: "awaiting_local_approval")
    var acknowledgement = try #require(
        item["acknowledgement"] as? [String: Any]
    )
    acknowledgement["job_revision"] = 2
    item["acknowledgement"] = acknowledgement
    let data = try desiredInstallListData(item: item)

    #expect(throws: DecodingError.self) {
        _ = try JSONDecoder().decode(
            DesiredInstallListSnapshot.self,
            from: data
        )
    }
}

@Test("Desired-install client requests use canonical identities and closed bodies")
func desiredInstallRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let list = try client.desiredInstallListRequest(offset: 4, limit: 32)
    #expect(
        list.url?.absoluteString
            == "http://localhost:17321/manager/fleet/desired-installs?offset=4&limit=32"
    )
    #expect(list.httpMethod == "GET")
    #expect(
        list.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )

    let read = try client.desiredInstallReadRequest(jobID: desiredJobID)
    #expect(
        read.url?.absoluteString
            == "http://localhost:17321/manager/fleet/desired-installs/\(desiredJobID)"
    )
    #expect(read.httpMethod == "GET")

    for action in ["approve", "cancel", "refuse"] {
        let request = try client.desiredInstallMutationRequest(
            jobID: desiredJobID,
            jobRevision: 7,
            action: action
        )
        #expect(
            request.url?.absoluteString
                == "http://localhost:17321/manager/fleet/desired-installs/\(desiredJobID)/\(action)"
        )
        #expect(request.httpMethod == "POST")
        #expect(
            request.value(forHTTPHeaderField: "Content-Type")
                == "application/json"
        )
        let body = try #require(request.httpBody)
        let object = try #require(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        )
        #expect(object.count == 2)
        #expect(object["schema_version"] as? Int == 1)
        #expect(object["job_revision"] as? Int == 7)
        #expect(object["job_id"] == nil)
    }
}

@Test("Desired-install requests reject path-like IDs, case changes, and stale revisions")
func desiredInstallRequestValidation() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!
    )

    #expect(throws: DesiredInstallRequestError.invalidJobID) {
        _ = try client.desiredInstallReadRequest(jobID: "../../models")
    }
    #expect(throws: DesiredInstallRequestError.invalidJobID) {
        _ = try client.desiredInstallReadRequest(
            jobID: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".uppercased()
        )
    }
    #expect(throws: DesiredInstallRequestError.invalidJobRevision) {
        _ = try client.desiredInstallMutationRequest(
            jobID: desiredJobID,
            jobRevision: 0,
            action: "approve"
        )
    }
    #expect(throws: DesiredInstallRequestError.invalidPageLimit) {
        _ = try client.desiredInstallListRequest(offset: 0, limit: 257)
    }
}

@Test("Desired-install view state never replaces a newer exact revision with stale data")
func desiredInstallViewModelFencesStaleRevisions() throws {
    var viewModel = DesiredInstallViewModel()
    let current = try decodeDesiredInstallDetail(
        executorAvailable: true,
        approvalAvailable: true,
        item: desiredInstallItem(
            revision: 2,
            desiredState: "cancel",
            state: "cancelled",
            resultCode: "cancelled_by_hub"
        )
    )
    viewModel.apply(current)

    let stale = try decodeDesiredInstallDetail(
        executorAvailable: false,
        approvalAvailable: false,
        item: desiredInstallItem(
            revision: 1,
            state: "awaiting_local_approval",
            resultCode: "local_approval_required",
            approvalAvailable: true
        )
    )
    viewModel.apply(stale)

    #expect(viewModel.items.count == 1)
    #expect(viewModel.items[0].job.jobRevision == 2)
    #expect(viewModel.items[0].acknowledgement.state == .cancelled)
    #expect(viewModel.item(jobID: desiredJobID, jobRevision: 1) == nil)
    #expect(viewModel.item(jobID: desiredJobID, jobRevision: 2) != nil)
}

private func decodeDesiredInstallList(
    executorAvailable: Bool,
    approvalAvailable: Bool,
    item: [String: Any]
) throws -> DesiredInstallListSnapshot {
    try JSONDecoder().decode(
        DesiredInstallListSnapshot.self,
        from: desiredInstallListData(
            executorAvailable: executorAvailable,
            approvalAvailable: approvalAvailable,
            item: item
        )
    )
}

private func desiredInstallListData(
    executorAvailable: Bool = false,
    approvalAvailable: Bool = false,
    item: [String: Any]
) throws -> Data {
    try JSONSerialization.data(
        withJSONObject: [
            "schema_version": 1,
            "executor_available": executorAvailable,
            "approval_available": approvalAvailable,
            "offset": 0,
            "limit": 100,
            "total": 1,
            "items": [item],
        ]
    )
}

private func decodeDesiredInstallDetail(
    executorAvailable: Bool,
    approvalAvailable: Bool,
    item: [String: Any]
) throws -> DesiredInstallDetailSnapshot {
    let data = try JSONSerialization.data(
        withJSONObject: [
            "schema_version": 1,
            "executor_available": executorAvailable,
            "approval_available": approvalAvailable,
            "item": item,
        ]
    )
    return try JSONDecoder().decode(
        DesiredInstallDetailSnapshot.self,
        from: data
    )
}

private func desiredInstallItem(
    revision: Int = 1,
    desiredState: String = "run",
    state: String,
    installationID: String? = nil,
    bytesDownloaded: Int64 = 0,
    totalBytes: Int64? = 144_000_000_000,
    resultCode: String? = nil,
    refusalAvailable: Bool = false,
    approvalAvailable: Bool = false,
    cancellationAvailable: Bool = false,
    includeCancellationAvailability: Bool = true
) -> [String: Any] {
    var acknowledgement: [String: Any] = [
        "schema_version": 1,
        "job_id": desiredJobID,
        "job_revision": revision,
        "state": state,
        "bytes_downloaded": bytesDownloaded,
        "total_bytes": totalBytes ?? NSNull(),
        "updated_at": 1_788_027_600.25,
        "result_code": resultCode ?? NSNull(),
    ]
    if let installationID {
        acknowledgement["installation_id"] = installationID
    }
    var localActions: [String: Any] = [
        "refusal_available": refusalAvailable,
        "approval_available": approvalAvailable,
    ]
    if includeCancellationAvailability {
        localActions["cancellation_available"] = cancellationAvailable
    }
    return [
        "job": [
            "schema_version": 1,
            "job_id": desiredJobID,
            "job_revision": revision,
            "idempotency_key": "88888888-8888-4888-8888-888888888888",
            "desired_state": desiredState,
            "created_at": 1_785_528_050.0,
            "expires_at": 1_785_528_950.0,
            "valid_for_seconds": 900,
            "pairing_id": "28bfef6e-ce8d-4cd7-828e-79a3c99642eb",
            "credential_generation": 3,
            "recommendation_basis": [
                "inventory_instance_id": "4ea23b26-b02f-45bc-be1d-be47e40c1e76",
                "inventory_sequence": 42,
            ],
            "catalog_version": "2026.08.1",
            "catalog_digest": "sha256:" + String(repeating: "1", count: 64),
            "logical_model_id": "model:glm-5.3-flash",
            "recipe_id": "recipe:omlx-glm-5.3-flash-mlx-4bit",
            "artifact_id": "artifact:glm-5.3-flash-mlx-4bit",
            "engine": "omlx",
            "capabilities": [
                "chat/completions",
                "completions",
                "messages",
                "responses",
            ],
            "guaranteed_context_tokens": 131_072,
            "alias": "glm-5.3-flash",
            "storage_location_id": "22222222-2222-4222-8222-222222222222",
            "storage_binding_generation": 2,
        ],
        "acknowledgement": acknowledgement,
        "local_actions": localActions,
    ]
}
