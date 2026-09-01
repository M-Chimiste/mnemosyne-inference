import Foundation
import Testing
@testable import MnemosyneAppCore

private let invitationID = "11111111-1111-4111-8111-111111111111"
private let pairingSecret = "pairing-secret-value-that-is-never-persisted"
private let hubOrigin = "https://hub.example"
private let macLocator = "http://studio-mac.local:1240"

@Test("Short-code pairing requests carry only the Hub and discovered Mac address")
func presencePairingRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "control-password"
    )
    let requestID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    let payload = FleetPairingPresenceRequest(
        requestID: requestID,
        hubOrigin: hubOrigin,
        locator: macLocator
    )
    let request = try client.fleetPairingPresenceRequest(payload)

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/fleet/pairing/request"
    )
    #expect(request.httpMethod == "POST")
    let body = try #require(request.httpBody)
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(object.count == 5)
    #expect(object["schema_version"] as? Int == 1)
    #expect(object["request_id"] as? String == requestID)
    #expect(object["hub_origin"] as? String == hubOrigin)
    #expect(object["locator"] as? String == macLocator)
    #expect(object["transport"] as? String == "tailscale")
    #expect(!String(reflecting: payload).contains(macLocator))
}

@Test("Pairing mutations encode only the version-one ceremony fields")
func pairingMutationRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "control-password"
    )
    let payload = FleetPairingControlRequest(
        invitationID: invitationID,
        pairingSecret: pairingSecret,
        hubOrigin: hubOrigin,
        locator: macLocator
    )

    for (request, suffix) in [
        (try client.fleetPairingBeginRequest(payload), "begin"),
        (try client.fleetPairingResumeRequest(payload), "resume"),
    ] {
        #expect(
            request.url?.absoluteString
                == "http://localhost:17321/manager/fleet/pairing/\(suffix)"
        )
        #expect(request.httpMethod == "POST")
        #expect(
            request.value(forHTTPHeaderField: "Authorization")
                == "Basic YWRtaW46Y29udHJvbC1wYXNzd29yZA=="
        )
        #expect(
            request.value(forHTTPHeaderField: "Content-Type")
                == "application/json"
        )
        let body = try #require(request.httpBody)
        let object = try #require(
            JSONSerialization.jsonObject(with: body) as? [String: Any]
        )
        #expect(object.count == 5)
        #expect(object["schema_version"] as? Int == 1)
        #expect(object["invitation_id"] as? String == invitationID)
        #expect(object["pairing_secret"] as? String == pairingSecret)
        #expect(object["hub_origin"] as? String == hubOrigin)
        #expect(object["locator"] as? String == macLocator)
    }
}

@Test("Secret-bearing pairing values redact descriptions and reflection")
func pairingRequestRedaction() {
    let payload = FleetPairingControlRequest(
        invitationID: invitationID,
        pairingSecret: pairingSecret,
        hubOrigin: hubOrigin,
        locator: macLocator
    )
    let submission = FleetPairingCeremonySubmission(
        operation: .begin,
        request: payload
    )

    for rendered in [
        String(describing: payload),
        String(reflecting: payload),
        String(describing: submission),
        String(reflecting: submission),
    ] {
        #expect(!rendered.contains(pairingSecret))
        #expect(rendered.contains("<redacted>"))
    }


    let untrustedFailure = FleetPairingAPIError(
        statusCode: 400,
        code: pairingSecret,
        retryable: false
    )
    #expect(untrustedFailure.code == "pairing_invalid_response")
    #expect(!untrustedFailure.localizedDescription.contains(pairingSecret))
}

@Test("A live ceremony hides the secret, resumes, and clears on completion")
func pairingCeremonyStateFlow() throws {
    let settingsBefore = NativeSettings()
    var state = FleetPairingCeremonyState()
    enterInvitation(into: &state)

    #expect(state.canSubmit)
    #expect(state.showsInvitationEntry)
    #expect(state.hasSecretInMemory)

    let claim = try state.prepareSubmission()
    #expect(claim.operation == .begin)
    #expect(state.stage == .submitting)
    #expect(!state.showsInvitationEntry)
    #expect(state.pairingSecretForSecureEntry.isEmpty)
    #expect(state.invitationID.isEmpty)
    #expect(state.hubOrigin.isEmpty)
    #expect(state.locator.isEmpty)
    #expect(state.hasSecretInMemory)
    #expect(!String(reflecting: state).contains(pairingSecret))

    state.apply(try pendingOperationResponse())
    #expect(state.stage == .awaitingApproval)
    #expect(state.canResumeWithoutReentry)
    #expect(state.nextActionText.contains("Resume Pairing"))

    let resume = try state.prepareSubmission()
    #expect(resume.operation == .resume)
    #expect(resume.request == claim.request)

    state.apply(try completeOperationResponse())
    #expect(state.stage == .paired)
    #expect(state.workflowPhase == "complete")
    #expect(!state.hasSecretInMemory)
    #expect(!state.showsInvitationEntry)
    #expect(NativeSettings() == settingsBefore)
}

@Test("A short-code ceremony waits automatically and clears hidden material")
func presencePairingCeremonyStateFlow() throws {
    let pin = "314159"
    let response = FleetPairingPresenceResponse(
        presencePIN: pin,
        expiresAt: 1_788_031_200,
        invitationID: invitationID,
        pairingSecret: pairingSecret,
        hubOrigin: hubOrigin,
        locator: macLocator
    )
    var state = FleetPairingCeremonyState()

    let submission = try state.preparePresenceSubmission(response)
    #expect(submission.operation == .begin)
    #expect(state.isPresenceCeremony)
    #expect(state.hasSecretInMemory)
    #expect(!String(reflecting: response).contains(pairingSecret))
    #expect(!String(reflecting: response).contains(macLocator))
    #expect(!String(reflecting: response).contains(pin))

    state.apply(try pendingOperationResponse())
    #expect(state.stage == .awaitingApproval)
    #expect(state.nextActionText.contains("finish automatically"))

    state.apply(try completeOperationResponse())
    #expect(state.stage == .paired)
    #expect(!state.hasSecretInMemory)
    #expect(state.nextActionText.contains("finishing enablement"))
}

@Test("Closing Settings clears invitation material and restart requires re-entry")
func pairingCeremonyViewTeardown() throws {
    var state = FleetPairingCeremonyState()
    enterInvitation(into: &state)
    _ = try state.prepareSubmission()
    state.apply(try pendingOperationResponse())
    #expect(state.hasSecretInMemory)

    state.viewDidDisappear()
    #expect(!state.hasSecretInMemory)
    #expect(state.pairingSecretForSecureEntry.isEmpty)
    #expect(state.invitationID.isEmpty)
    #expect(state.hubOrigin.isEmpty)
    #expect(state.locator.isEmpty)

    state.synchronize(with: try pendingPairingSnapshot())
    #expect(state.stage == .awaitingApproval)
    #expect(state.showsInvitationEntry)
    #expect(!state.canResumeWithoutReentry)
    #expect(state.nextActionText.contains("Re-enter"))

    enterInvitation(into: &state)
    let resume = try state.prepareSubmission()
    #expect(resume.operation == .resume)
    state.cancel()
    #expect(!state.hasSecretInMemory)
    #expect(state.stage == .collecting)
}

@Test("Static Fleet enrollment remains an explicit migration boundary")
func staticPairingMigrationBoundary() throws {
    let payload = #"""
    {
      "schema_version": 1,
      "available": true,
      "state": "unpaired",
      "credentials_configured": false,
      "legacy_credentials_present": true,
      "last_error_code": null,
      "workflow": {
        "available": true,
        "phase": null,
        "last_error_code": null
      }
    }
    """#.data(using: .utf8)!
    let snapshot = try JSONDecoder().decode(
        FleetPairingSnapshot.self,
        from: payload
    )
    var state = FleetPairingCeremonyState()
    enterInvitation(into: &state)

    state.synchronize(with: snapshot)

    #expect(snapshot.permitsParticipationControl)
    #expect(!snapshot.ownsFleetCredentials)
    #expect(state.stage == .blocked)
    #expect(!state.showsInvitationEntry)
    #expect(!state.hasSecretInMemory)
    #expect(state.nextActionText.contains("static Fleet enrollment"))
}

@Test("A locally revoked Mac can be paired again with a new invitation")
func revokedPairingAllowsFreshInvitation() throws {
    let payload = #"""
    {
      "schema_version": 1,
      "available": true,
      "state": "revoked",
      "credentials_configured": false,
      "legacy_credentials_present": false,
      "last_error_code": null,
      "self_revoke": null,
      "workflow": {
        "available": true,
        "phase": "complete",
        "last_error_code": null
      }
    }
    """#.data(using: .utf8)!
    let snapshot = try JSONDecoder().decode(
        FleetPairingSnapshot.self,
        from: payload
    )
    var state = FleetPairingCeremonyState()

    state.synchronize(with: snapshot)

    #expect(state.stage == .collecting)
    #expect(state.showsInvitationEntry)
    #expect(state.statusText == "Enrollment removed")
    #expect(state.nextActionText.contains("new Hub invitation"))
}

@Test("A revoked pairing with pending local cleanup requires exact removal retry")
func revokedPairingWithPendingCleanupDoesNotAllowFreshInvitation() throws {
    let payload = #"""
    {
      "schema_version": 1,
      "available": true,
      "state": "revoked",
      "credentials_configured": true,
      "legacy_credentials_present": false,
      "last_error_code": null,
      "self_revoke": {
        "schema_version": 1,
        "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "phase": "hub_committed"
      },
      "workflow": {
        "available": true,
        "phase": "complete",
        "last_error_code": null
      }
    }
    """#.data(using: .utf8)!
    let snapshot = try JSONDecoder().decode(
        FleetPairingSnapshot.self,
        from: payload
    )
    var state = FleetPairingCeremonyState()

    state.synchronize(with: snapshot)

    #expect(state.stage == .blocked)
    #expect(!state.showsInvitationEntry)
    #expect(state.statusText == "Hub removal needs confirmation")
    #expect(state.nextActionText.contains("Retry Removal"))
    #expect(state.nextActionText.contains("Pooled routing remains denied"))
}

@Test("Pairing failures expose fixed guidance without reflecting request input")
func pairingCeremonyFailureRedaction() throws {
    var state = FleetPairingCeremonyState()
    enterInvitation(into: &state)
    _ = try state.prepareSubmission()

    state.recordFailure(.invitationRejected)

    #expect(state.stage == .failed)
    #expect(state.statusText == "Invitation rejected")
    #expect(!state.statusText.contains(pairingSecret))
    #expect(!state.nextActionText.contains(pairingSecret))
    #expect(!String(reflecting: state).contains(pairingSecret))
    // A retry in the same open view can reuse memory without revealing it.
    #expect(state.canResumeWithoutReentry)
}

@Test("Only a conclusively rejected unclaimed attempt offers discard recovery")
func rejectedUnclaimedAttemptDiscardEligibility() throws {
    let payload = #"""
    {
      "schema_version": 1,
      "available": true,
      "state": "pending",
      "pairing_id": null,
      "credentials_configured": false,
      "legacy_credentials_present": false,
      "last_error_code": null,
      "workflow": {
        "schema_version": 1,
        "available": true,
        "phase": "claiming",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "invitation_id": "11111111-1111-4111-8111-111111111111",
        "claim_request_id": "33333333-3333-4333-8333-333333333333",
        "provision_request_id": "44444444-4444-4444-8444-444444444444",
        "activation_request_id": "55555555-5555-4555-8555-555555555555",
        "claim_id": null,
        "pairing_id": null,
        "reporting_node_id": "studio-mac",
        "credential_generation": null,
        "expires_at": null,
        "last_error_code": "pairing_claim_rejected",
        "updated_at": 1788027600.0
      }
    }
    """#.data(using: .utf8)!
    let rejected = try JSONDecoder().decode(
        FleetPairingSnapshot.self,
        from: payload
    )
    #expect(rejected.canDiscardRejectedAttempt)

    let awaitingApproval = try pendingPairingSnapshot()
    #expect(!awaitingApproval.canDiscardRejectedAttempt)
    #expect(!awaitingApproval.canDiscardTerminalAttempt)

    let terminalPayload = #"""
    {
      "schema_version": 1,
      "available": true,
      "state": "pending",
      "pairing_id": null,
      "credentials_configured": false,
      "legacy_credentials_present": false,
      "last_error_code": null,
      "workflow": {
        "schema_version": 1,
        "available": true,
        "phase": "awaiting_approval",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "invitation_id": "11111111-1111-4111-8111-111111111111",
        "claim_request_id": "33333333-3333-4333-8333-333333333333",
        "provision_request_id": "44444444-4444-4444-8444-444444444444",
        "activation_request_id": "55555555-5555-4555-8555-555555555555",
        "claim_id": "66666666-6666-4666-8666-666666666666",
        "pairing_id": "77777777-7777-4777-8777-777777777777",
        "reporting_node_id": "studio-mac",
        "credential_generation": null,
        "expires_at": 1788031200.0,
        "last_error_code": "pairing_remote_attempt_terminal",
        "updated_at": 1788027600.0
      }
    }
    """#.data(using: .utf8)!
    let terminal = try JSONDecoder().decode(
        FleetPairingSnapshot.self,
        from: terminalPayload
    )
    #expect(terminal.canDiscardTerminalAttempt)

    var state = FleetPairingCeremonyState()
    state.synchronize(with: terminal)
    #expect(state.stage == .failed)
    #expect(state.statusText == "Pairing attempt expired")
}

private func enterInvitation(into state: inout FleetPairingCeremonyState) {
    state.setInvitationID(invitationID)
    state.setPairingSecret(pairingSecret)
    state.setHubOrigin("\(hubOrigin)/")
    state.setLocator("\(macLocator)/")
}

private func pendingOperationResponse() throws
    -> FleetPairingOperationResponse
{
    let payload = #"""
    {
      "schema_version": 1,
      "accepted": true,
      "next_action": "resume_after_approval",
      "pairing": {
        "schema_version": 1,
        "available": true,
        "state": "pending",
        "credentials_configured": false,
        "legacy_credentials_present": false,
        "last_error_code": null,
        "workflow": {
          "schema_version": 1,
          "available": true,
          "phase": "awaiting_approval",
          "attempt_id": "22222222-2222-4222-8222-222222222222",
          "invitation_id": "11111111-1111-4111-8111-111111111111",
          "claim_request_id": "33333333-3333-4333-8333-333333333333",
          "provision_request_id": "44444444-4444-4444-8444-444444444444",
          "activation_request_id": "55555555-5555-4555-8555-555555555555",
          "claim_id": "66666666-6666-4666-8666-666666666666",
          "pairing_id": "77777777-7777-4777-8777-777777777777",
          "reporting_node_id": "studio-mac",
          "credential_generation": null,
          "expires_at": 1788031200.0,
          "last_error_code": "pairing_approval_pending",
          "updated_at": 1788027600.0
        }
      }
    }
    """#.data(using: .utf8)!
    return try JSONDecoder().decode(
        FleetPairingOperationResponse.self,
        from: payload
    )
}

private func completeOperationResponse() throws
    -> FleetPairingOperationResponse
{
    let payload = #"""
    {
      "schema_version": 1,
      "accepted": true,
      "next_action": null,
      "workflow": {
        "schema_version": 1,
        "available": true,
        "phase": "complete",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "invitation_id": "11111111-1111-4111-8111-111111111111",
        "claim_request_id": "33333333-3333-4333-8333-333333333333",
        "provision_request_id": "44444444-4444-4444-8444-444444444444",
        "activation_request_id": "55555555-5555-4555-8555-555555555555",
        "claim_id": "66666666-6666-4666-8666-666666666666",
        "pairing_id": "77777777-7777-4777-8777-777777777777",
        "reporting_node_id": "studio-mac",
        "credential_generation": 1,
        "expires_at": 1788031200.0,
        "last_error_code": null,
        "updated_at": 1788027601.0
      }
    }
    """#.data(using: .utf8)!
    return try JSONDecoder().decode(
        FleetPairingOperationResponse.self,
        from: payload
    )
}

private func pendingPairingSnapshot() throws -> FleetPairingSnapshot {
    let response = try pendingOperationResponse()
    return try #require(response.pairing)
}
