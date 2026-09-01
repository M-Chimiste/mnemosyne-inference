import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Native Hub pairing decodes pending claims and disabled enrollments")
func nativeHubPairingDecoding() throws {
    let claims = try JSONDecoder().decode(
        HubPairingClaimList.self,
        from: Data(
            #"""
            {
              "schema_version": 1,
              "claims": [{
                "schema_version": 1,
                "claim_id": "11111111-1111-4111-8111-111111111111",
                "invitation_id": "22222222-2222-4222-8222-222222222222",
                "pairing_id": "33333333-3333-4333-8333-333333333333",
                "display_name": "Metis",
                "reporting_node_id": "metis",
                "service_version": "0.9.0",
                "platform": "macos",
                "protocol_version": 1,
                "state": "claimed",
                "claimed_at": 1788210000.0,
                "expires_at": 1788210300.0
              }]
            }
            """#.utf8
        )
    )
    #expect(claims.schemaVersion == 1)
    #expect(claims.claims.count == 1)
    #expect(claims.claims[0].id == "11111111-1111-4111-8111-111111111111")
    #expect(claims.claims[0].displayName == "Metis")

    let enrollments = try JSONDecoder().decode(
        HubPairingEnrollmentList.self,
        from: Data(
            #"""
            {
              "schema_version": 1,
              "enrollments": [{
                "schema_version": 1,
                "pairing_id": "33333333-3333-4333-8333-333333333333",
                "reporting_node_id": "metis",
                "display_name": "Metis",
                "platform": "macos",
                "service_version": "0.9.0",
                "protocol_version": 1,
                "service_class": "primary",
                "state": "disabled",
                "hub_enabled": false,
                "credential_generation": 1,
                "created_at": 1788210000.0,
                "updated_at": 1788210010.0,
                "revoked_at": null,
                "failure_code": null
              }]
            }
            """#.utf8
        )
    )
    #expect(enrollments.enrollments.count == 1)
    #expect(enrollments.enrollments[0].state == "disabled")
    #expect(!enrollments.enrollments[0].hubEnabled)
}

@Test("Native Hub pairing sends the PIN only to the loopback admin endpoint")
func nativeHubPairingApprovalRequest() throws {
    let client = try HubPairingAdminClient(adminKey: "private-admin-key")
    let claimID = "11111111-1111-4111-8111-111111111111"
    let requestID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    let request = try client.approvePresenceRequest(
        claimID: claimID,
        pin: "042381",
        requestID: requestID
    )

    #expect(
        request.url?.absoluteString
            == "http://127.0.0.1:17400/fleet/api/v1/pairing/claims/\(claimID)/approve-presence"
    )
    #expect(request.httpMethod == "POST")
    #expect(
        request.value(forHTTPHeaderField: "Authorization")
            == "Bearer private-admin-key"
    )
    #expect(
        request.value(forHTTPHeaderField: "Content-Type")
            == "application/json"
    )
    let body = try #require(request.httpBody)
    let payload = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(payload.count == 5)
    #expect(payload["schema_version"] as? Int == 1)
    #expect(payload["request_id"] as? String == requestID)
    #expect(payload["presence_pin"] as? String == "042381")
    #expect(payload["service_class"] as? String == "primary")
    #expect(payload["hub_enabled"] as? Bool == false)
}

@Test("Native Hub pairing refresh and enable requests stay bounded to loopback")
func nativeHubPairingListAndEnableRequests() throws {
    let client = try HubPairingAdminClient(adminKey: "private-admin-key")
    let claims = client.pendingClaimsRequest()
    #expect(
        claims.url?.absoluteString
            == "http://127.0.0.1:17400/fleet/api/v1/pairing/claims?limit=1000"
    )
    #expect(claims.httpMethod == "GET")

    let pairingID = "33333333-3333-4333-8333-333333333333"
    let requestID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    let enable = try client.setEnrollmentEnabledRequest(
        pairingID: pairingID,
        enabled: true,
        requestID: requestID
    )
    #expect(
        enable.url?.absoluteString
            == "http://127.0.0.1:17400/fleet/api/v1/pairing/enrollments/\(pairingID)/enabled"
    )
    #expect(enable.httpMethod == "PUT")
    let body = try #require(enable.httpBody)
    let payload = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(payload.count == 3)
    #expect(payload["enabled"] as? Bool == true)
}

@Test("Native Hub pairing rejects non-PIN input and non-loopback origins")
func nativeHubPairingRejectsUnsafeInput() throws {
    let client = try HubPairingAdminClient(adminKey: "private-admin-key")
    #expect(throws: HubPairingAdminError.invalidPIN) {
        try client.approvePresenceRequest(
            claimID: "11111111-1111-4111-8111-111111111111",
            pin: "12 456"
        )
    }
    #expect(throws: HubPairingAdminError.invalidConfiguration) {
        try HubPairingAdminClient(
            adminKey: "private-admin-key",
            baseURL: URL(string: "https://nyx.example.ts.net")!
        )
    }
}
