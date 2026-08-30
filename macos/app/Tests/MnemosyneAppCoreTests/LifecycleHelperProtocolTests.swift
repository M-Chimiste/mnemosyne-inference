import Foundation
import Testing

@testable import MnemosyneAppCore

private let helperNow = 1_788_000_000.0

private func helperChallenge(
    nonce: String = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    issuedAt: Double = helperNow - 1,
    expiresAt: Double = helperNow + 30
) -> LifecycleHelperChallengeV2 {
    LifecycleHelperChallengeV2(
        nonce: nonce,
        transactionID: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        transactionAuthorityDigest: "sha256:" + String(repeating: "1", count: 64),
        executionManifestDigest: "sha256:" + String(repeating: "2", count: 64),
        recoveryCloneIdentityDigest: "sha256:" + String(repeating: "3", count: 64),
        expectedHelperIdentifier: LifecycleHelperProtocolV2.helperIdentifier,
        expectedHelperBuildDigest: "sha256:" + String(repeating: "4", count: 64),
        expectedTeamIdentifier: "ABCDE12345",
        expectedCodeRequirementDigest: "sha256:" + String(repeating: "5", count: 64),
        expectedAppBuildDigest: "sha256:" + String(repeating: "6", count: 64),
        expectedAuthorizationProofAlgorithm: "test-hmac-sha256-v1",
        expectedAuthorizationKeyID: "sha256:" + String(repeating: "7", count: 64),
        sessionID: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        issuedAt: issuedAt,
        expiresAt: expiresAt
    )
}

private func encodedChallenge(_ challenge: LifecycleHelperChallengeV2) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return try encoder.encode(challenge)
}

@Test("Lifecycle helper v2 challenge is closed, canonical, framed, and receipt-bound")
func lifecycleHelperChallengeRoundTrip() throws {
    let challenge = helperChallenge()
    let payload = try encodedChallenge(challenge)
    let decoded = try LifecycleHelperProtocolV2.decodeChallenge(payload, now: helperNow)
    #expect(decoded == challenge)

    let digest = try LifecycleHelperProtocolV2.authorizationDigest(for: decoded)
    let receipt = LifecycleHelperReceiptV2(
        challenge: decoded,
        authorizationDigest: digest,
        authenticatedAt: helperNow,
        authorizationProof: String(repeating: "a", count: 64)
    )
    let receiptData = try LifecycleHelperProtocolV2.encodeReceipt(receipt)
    #expect(try LifecycleHelperProtocolV2.decodeReceipt(receiptData) == receipt)

    let frame = try LifecycleHelperProtocolV2.encodeFrame(payload: receiptData)
    #expect(try LifecycleHelperProtocolV2.decodeFrame(frame) == receiptData)
    #expect(digest == LifecycleHelperProtocolV2.sha256(payload))

    let proofPayload = try LifecycleHelperProtocolV2.authorizationProofPayload(
        authorizationDigest: digest,
        authenticatedAt: helperNow,
        algorithm: decoded.expectedAuthorizationProofAlgorithm,
        keyID: decoded.expectedAuthorizationKeyID
    )
    #expect(proofPayload.starts(with: Data("mnemosyne-lifecycle-helper-proof-v1\0".utf8)))
}

@Test("Lifecycle helper rejects unknown and duplicate challenge fields")
func lifecycleHelperRejectsOpenOrDuplicateJSON() throws {
    let payload = try encodedChallenge(helperChallenge())
    var object = try #require(
        JSONSerialization.jsonObject(with: payload) as? [String: Any]
    )
    object["path"] = "/Applications/other.app"
    let open = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    #expect(throws: LifecycleHelperProtocolError.malformed) {
        try LifecycleHelperProtocolV2.decodeChallenge(open, now: helperNow)
    }

    let text = try #require(String(data: payload, encoding: .utf8))
    let duplicate = Data(
        text.replacingOccurrences(
            of: "{",
            with: "{\"nonce\":\"dddddddd-dddd-4ddd-8ddd-dddddddddddd\",",
            options: [],
            range: text.startIndex ..< text.index(after: text.startIndex)
        ).utf8
    )
    #expect(throws: LifecycleHelperProtocolError.malformed) {
        try LifecycleHelperProtocolV2.decodeChallenge(duplicate, now: helperNow)
    }
}

@Test("Lifecycle helper refuses malformed authority, unsafe timing, and oversized frames")
func lifecycleHelperRejectsInvalidAuthorityAndFrames() throws {
    let expired = try encodedChallenge(
        helperChallenge(issuedAt: helperNow - 40, expiresAt: helperNow - 1)
    )
    #expect(throws: LifecycleHelperProtocolError.expired) {
        try LifecycleHelperProtocolV2.decodeChallenge(expired, now: helperNow)
    }

    let tooLong = try encodedChallenge(
        helperChallenge(issuedAt: helperNow - 1, expiresAt: helperNow + 121)
    )
    #expect(throws: LifecycleHelperProtocolError.invalidAuthority) {
        try LifecycleHelperProtocolV2.decodeChallenge(tooLong, now: helperNow)
    }

    var oversized = Data([0, 1, 0, 0])
    oversized.append(Data(repeating: 0, count: 16))
    #expect(throws: LifecycleHelperProtocolError.oversized) {
        try LifecycleHelperProtocolV2.decodeFrame(oversized)
    }
}

@Test("Lifecycle helper replay guard consumes each session nonce once")
func lifecycleHelperReplayGuardRejectsSecondUse() throws {
    let guardrail = LifecycleHelperReplayGuard()
    let challenge = helperChallenge()
    try guardrail.consume(challenge)
    #expect(throws: LifecycleHelperProtocolError.replayed) {
        try guardrail.consume(challenge)
    }
}

@Test("Lifecycle peer manifest is closed and path-bounded")
func lifecyclePeerManifestValidation() throws {
    let appDigest = try LifecycleHelperProtocolV2.appBuildDigest(
        bundleIdentifier: "com.mnemosyne.inference.menu",
        shortVersion: "0.9.0",
        buildNumber: "1",
        teamIdentifier: "ABCDE12345"
    )
    let helperDigest = try LifecycleHelperProtocolV2.helperBuildDigest(
        identifier: LifecycleHelperProtocolV2.helperIdentifier,
        teamIdentifier: "ABCDE12345",
        cdHash: String(repeating: "a", count: 40),
        appBuildDigest: appDigest
    )
    let runnerDigest = try LifecycleHelperProtocolV2.runnerBuildDigest(
        identifier: LifecycleHelperProtocolV2.runnerIdentifier,
        teamIdentifier: "ABCDE12345",
        cdHash: String(repeating: "c", count: 40),
        appBuildDigest: appDigest
    )
    let object: [String: Any] = [
        "schema_version": 2,
        "helper_protocol_version": 2,
        "app_bundle_identifier": "com.mnemosyne.inference.menu",
        "app_short_version": "0.9.0",
        "app_build_number": "1",
        "app_build_digest": appDigest,
        "expected_team_identifier": "ABCDE12345",
        "helper_relative_path": LifecycleHelperProtocolV2.helperRelativePath,
        "helper_identifier": LifecycleHelperProtocolV2.helperIdentifier,
        "helper_team_identifier": "ABCDE12345",
        "helper_cdhash": String(repeating: "a", count: 40),
        "helper_code_requirement_digest": "sha256:" + String(repeating: "b", count: 64),
        "helper_build_digest": helperDigest,
        "runner_relative_path": LifecycleHelperProtocolV2.runnerRelativePath,
        "runner_identifier": LifecycleHelperProtocolV2.runnerIdentifier,
        "runner_team_identifier": "ABCDE12345",
        "runner_cdhash": String(repeating: "c", count: 40),
        "runner_code_requirement_digest": "sha256:" + String(repeating: "d", count: 64),
        "runner_build_digest": runnerDigest,
        "service_python_relative_path": "Contents/Resources/Python/cpython-3.12/bin/python3",
        "service_python_identifier": "org.python.python",
        "service_python_team_identifier": "ABCDE12345",
        "service_python_cdhash": String(repeating: "e", count: 40),
        "service_python_code_requirement_digest": "sha256:" + String(repeating: "f", count: 64),
        "service_python_authoritative": false,
    ]
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    let manifest = try LifecycleHelperProtocolV2.decodePeerManifest(data)
    #expect(manifest.appBuildDigest == appDigest)
    #expect(manifest.helperBuildDigest == helperDigest)
    #expect(manifest.runnerBuildDigest == runnerDigest)
    #expect(manifest.servicePythonAuthoritative == false)

    var escaping = object
    escaping["service_python_relative_path"] = "Contents/Resources/Python/../other/python3"
    let escapingData = try JSONSerialization.data(
        withJSONObject: escaping,
        options: [.sortedKeys]
    )
    #expect(throws: LifecycleHelperProtocolError.invalidAuthority) {
        try LifecycleHelperProtocolV2.decodePeerManifest(escapingData)
    }

    var authoritativePython = object
    authoritativePython["service_python_authoritative"] = true
    let authoritativeData = try JSONSerialization.data(
        withJSONObject: authoritativePython,
        options: [.sortedKeys]
    )
    #expect(throws: LifecycleHelperProtocolError.invalidAuthority) {
        try LifecycleHelperProtocolV2.decodePeerManifest(authoritativeData)
    }
}
