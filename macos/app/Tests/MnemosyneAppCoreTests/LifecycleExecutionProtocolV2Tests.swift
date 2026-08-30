import Foundation
import Testing
@testable import MnemosyneAppCore

private let txID = "00000001-0000-4000-8000-000000000001"
private let grantID = "00000002-0000-4000-8000-000000000002"
private let sessionID = "00000003-0000-4000-8000-000000000003"
private let nonce = "00000004-0000-4000-8000-000000000004"
private let leaseID = "00000005-0000-4000-8000-000000000005"
private let effectID = "00000006-0000-4000-8000-000000000006"

private func digest(_ digit: Character) -> String {
    "sha256:" + String(repeating: String(digit), count: 64)
}

private func registration() -> LifecycleRunnerRegistrationV2 {
    LifecycleRunnerRegistrationV2(
        transactionID: txID,
        grantID: grantID,
        grantDigest: digest("1"),
        runnerSessionID: sessionID,
        sequence: 1,
        nonce: nonce,
        runnerBuildDigest: digest("2"),
        runnerIdentityDigest: digest("3"),
        teamIdentifier: "TEAM123456",
        codeRequirementDigest: digest("4"),
        requestedLeaseSeconds: 60
    )
}

@Test("Lifecycle execution v2 registration is canonical, bounded, and path-free")
func lifecycleExecutionRegistrationRoundTrip() throws {
    let message = LifecycleExecutionMessageV2.register(registration())
    let frame = try LifecycleExecutionProtocolV2.encodeFrame(message)
    #expect(frame.count <= LifecycleExecutionProtocolV2.maximumFrameBytes)
    #expect(try LifecycleExecutionProtocolV2.decodeFrame(frame) == message)
    let text = String(decoding: frame.dropFirst(4), as: UTF8.self)
    for forbidden in ["\"path\"", "\"pid\"", "\"port\"", "\"label\"", "\"argv\"", "\"credential\""] {
        #expect(!text.contains(forbidden))
    }
}

@Test("Lifecycle execution start, observe, apply, finalize, and refusal are closed")
func lifecycleExecutionCommandsRoundTrip() throws {
    let start = LifecycleExecutionStartV2(
        protocolVersion: 2,
        messageType: .start,
        transactionID: txID,
        grantID: grantID,
        grantDigest: digest("1"),
        runnerSessionID: sessionID,
        leaseID: leaseID,
        leaseEpoch: 1,
        sequence: 2,
        nonce: nonce,
        direction: .forward,
        executionManifestDigest: digest("5"),
        recoveryCloneIdentityDigest: digest("6"),
        authorizationDigest: digest("7"),
        authorizationSessionID: "00000007-0000-4000-8000-000000000007"
    )
    let observe = LifecycleExecutionObserveV2(
        protocolVersion: 2,
        messageType: .observe,
        transactionID: txID,
        grantID: grantID,
        grantDigest: digest("1"),
        runnerSessionID: sessionID,
        leaseID: leaseID,
        leaseEpoch: 1,
        sequence: 3,
        nonce: nonce,
        effectID: effectID,
        effectKind: .resolveExclusiveWeight,
        targetDigest: digest("8"),
        attempt: 1,
        priorReceiptDigest: nil
    )
    let apply = LifecycleExecutionApplyV2(
        protocolVersion: 2,
        messageType: .apply,
        transactionID: txID,
        grantID: grantID,
        grantDigest: digest("1"),
        runnerSessionID: sessionID,
        leaseID: leaseID,
        leaseEpoch: 1,
        sequence: 4,
        nonce: nonce,
        effectID: effectID,
        effectKind: .resolveExclusiveWeight,
        targetDigest: digest("8"),
        attempt: 1,
        observationReceiptDigest: digest("9")
    )
    let finalize = LifecycleExecutionFinalizeV2(
        protocolVersion: 2,
        messageType: .finalize,
        transactionID: txID,
        grantID: grantID,
        grantDigest: digest("1"),
        runnerSessionID: sessionID,
        leaseID: leaseID,
        leaseEpoch: 1,
        sequence: 5,
        nonce: nonce,
        direction: .forward,
        finalReceiptDigest: digest("a")
    )
    let refused = LifecycleExecutionRefusedV2(
        transactionID: txID,
        grantID: grantID,
        runnerSessionID: sessionID,
        sequence: 6,
        nonce: nonce,
        requestNonce: "00000008-0000-4000-8000-000000000008",
        errorCode: "runner_adapter_unavailable"
    )
    let messages: [LifecycleExecutionMessageV2] = [
        .start(start), .observe(observe), .apply(apply),
        .finalize(finalize), .refused(refused),
    ]
    for message in messages {
        let frame = try LifecycleExecutionProtocolV2.encodeFrame(message)
        #expect(try LifecycleExecutionProtocolV2.decodeFrame(frame) == message)
    }
}

@Test("Lifecycle execution v2 rejects unknown, duplicate, and path-bearing keys")
func lifecycleExecutionStrictKeys() throws {
    let frame = try LifecycleExecutionProtocolV2.encodeFrame(.register(registration()))
    let payload = Data(frame.dropFirst(4))
    var object = try #require(
        JSONSerialization.jsonObject(with: payload) as? [String: Any]
    )
    object["path"] = "/tmp/not-authority"
    let unknownPayload = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    #expect(throws: LifecycleExecutionProtocolError.self) {
        try LifecycleExecutionProtocolV2.decodeFrame(framed(unknownPayload))
    }

    let raw = String(decoding: payload, as: UTF8.self)
    let duplicate = try #require(
        raw.replacingOccurrences(
            of: "\"grant_id\":",
            with: "\"grant_id\":\"\(grantID)\",\"grant_id\":",
            maxReplacements: 1
        ).data(using: .utf8)
    )
    #expect(throws: LifecycleExecutionProtocolError.self) {
        try LifecycleExecutionProtocolV2.decodeFrame(framed(duplicate))
    }
}

@Test("Lifecycle execution registration rejects ad-hoc and tampered identities")
func lifecycleExecutionIdentityTampering() {
    let adHoc = LifecycleRunnerRegistrationV2(
        transactionID: txID,
        grantID: grantID,
        grantDigest: digest("1"),
        runnerSessionID: sessionID,
        sequence: 1,
        nonce: nonce,
        runnerBuildDigest: digest("2"),
        runnerIdentityDigest: digest("3"),
        teamIdentifier: "ADHOC00000",
        codeRequirementDigest: digest("4"),
        requestedLeaseSeconds: 60
    )
    #expect(throws: LifecycleExecutionProtocolError.self) {
        try LifecycleExecutionProtocolV2.encodeFrame(.register(adHoc))
    }
}

@Test("Inert lifecycle runner accepts only registration and returns one fixed refusal")
func lifecycleRunnerInertRefusal() throws {
    let request = try LifecycleExecutionProtocolV2.encodeFrame(
        .register(registration())
    )
    let refusalNonce = "00000008-0000-4000-8000-000000000008"
    let response = try LifecycleRunnerInertAdapterV2.refusalFrame(
        for: request,
        refusalNonce: refusalNonce
    )
    guard case let .refused(refusal) = try LifecycleExecutionProtocolV2.decodeFrame(response) else {
        Issue.record("inert runner returned a non-refusal message")
        return
    }
    #expect(refusal.errorCode == "runner_adapter_unavailable")
    #expect(refusal.transactionID == txID)
    #expect(refusal.grantID == grantID)
    #expect(refusal.runnerSessionID == sessionID)
    #expect(refusal.sequence == 1)
    #expect(refusal.nonce == refusalNonce)
    #expect(refusal.requestNonce == nonce)

    let text = String(decoding: response.dropFirst(4), as: UTF8.self)
    for forbidden in [
        "\"path\"", "\"pid\"", "\"port\"", "\"label\"", "\"argv\"",
        "\"credential\"", "\"lease_id\"", "\"effect_id\"",
    ] {
        #expect(!text.contains(forbidden))
    }
}

@Test("Inert lifecycle runner refuses every command-shaped input")
func lifecycleRunnerInertRejectsCommands() throws {
    let refused = LifecycleExecutionRefusedV2(
        transactionID: txID,
        grantID: grantID,
        runnerSessionID: sessionID,
        sequence: 1,
        nonce: nonce,
        requestNonce: "00000008-0000-4000-8000-000000000008",
        errorCode: "runner_adapter_unavailable"
    )
    #expect(throws: LifecycleExecutionProtocolError.invalidAuthority) {
        try LifecycleRunnerInertAdapterV2.refusal(
            for: .refused(refused),
            refusalNonce: "00000009-0000-4000-8000-000000000009"
        )
    }
}

private func framed(_ payload: Data) -> Data {
    var length = UInt32(payload.count).bigEndian
    var frame = Data(bytes: &length, count: 4)
    frame.append(payload)
    return frame
}

private extension String {
    func replacingOccurrences(
        of target: String,
        with replacement: String,
        maxReplacements: Int
    ) -> String {
        var result = self
        for _ in 0 ..< maxReplacements {
            guard let range = result.range(of: target) else { break }
            result.replaceSubrange(range, with: replacement)
        }
        return result
    }
}
