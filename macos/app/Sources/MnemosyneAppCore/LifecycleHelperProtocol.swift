import CryptoKit
import Foundation

public enum LifecycleHelperProtocolError: Error, Equatable, Sendable {
    case malformed
    case oversized
    case invalidAuthority
    case mismatchedAuthority
    case expired
    case replayed
}

public struct LifecycleHelperChallengeV2: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let helperProtocolVersion: Int
    public let nonce: String
    public let transactionID: String
    public let transactionAuthorityDigest: String
    public let executionManifestDigest: String
    public let recoveryCloneIdentityDigest: String
    public let expectedHelperIdentifier: String
    public let expectedHelperBuildDigest: String
    public let expectedTeamIdentifier: String
    public let expectedCodeRequirementDigest: String
    public let expectedAppBuildDigest: String
    public let expectedAuthorizationProofAlgorithm: String
    public let expectedAuthorizationKeyID: String
    public let sessionID: String
    public let issuedAt: Double
    public let expiresAt: Double

    public init(
        schemaVersion: Int = 2,
        helperProtocolVersion: Int = 2,
        nonce: String,
        transactionID: String,
        transactionAuthorityDigest: String,
        executionManifestDigest: String,
        recoveryCloneIdentityDigest: String,
        expectedHelperIdentifier: String,
        expectedHelperBuildDigest: String,
        expectedTeamIdentifier: String,
        expectedCodeRequirementDigest: String,
        expectedAppBuildDigest: String,
        expectedAuthorizationProofAlgorithm: String,
        expectedAuthorizationKeyID: String,
        sessionID: String,
        issuedAt: Double,
        expiresAt: Double
    ) {
        self.schemaVersion = schemaVersion
        self.helperProtocolVersion = helperProtocolVersion
        self.nonce = nonce
        self.transactionID = transactionID
        self.transactionAuthorityDigest = transactionAuthorityDigest
        self.executionManifestDigest = executionManifestDigest
        self.recoveryCloneIdentityDigest = recoveryCloneIdentityDigest
        self.expectedHelperIdentifier = expectedHelperIdentifier
        self.expectedHelperBuildDigest = expectedHelperBuildDigest
        self.expectedTeamIdentifier = expectedTeamIdentifier
        self.expectedCodeRequirementDigest = expectedCodeRequirementDigest
        self.expectedAppBuildDigest = expectedAppBuildDigest
        self.expectedAuthorizationProofAlgorithm = expectedAuthorizationProofAlgorithm
        self.expectedAuthorizationKeyID = expectedAuthorizationKeyID
        self.sessionID = sessionID
        self.issuedAt = issuedAt
        self.expiresAt = expiresAt
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case helperProtocolVersion = "helper_protocol_version"
        case nonce
        case transactionID = "transaction_id"
        case transactionAuthorityDigest = "transaction_authority_digest"
        case executionManifestDigest = "execution_manifest_digest"
        case recoveryCloneIdentityDigest = "recovery_clone_identity_digest"
        case expectedHelperIdentifier = "expected_helper_identifier"
        case expectedHelperBuildDigest = "expected_helper_build_digest"
        case expectedTeamIdentifier = "expected_team_identifier"
        case expectedCodeRequirementDigest = "expected_code_requirement_digest"
        case expectedAppBuildDigest = "expected_app_build_digest"
        case expectedAuthorizationProofAlgorithm = "expected_authorization_proof_algorithm"
        case expectedAuthorizationKeyID = "expected_authorization_key_id"
        case sessionID = "session_id"
        case issuedAt = "issued_at"
        case expiresAt = "expires_at"
    }
}

public struct LifecycleHelperReceiptV2: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let helperProtocolVersion: Int
    public let nonce: String
    public let transactionID: String
    public let transactionAuthorityDigest: String
    public let executionManifestDigest: String
    public let recoveryCloneIdentityDigest: String
    public let expectedHelperIdentifier: String
    public let expectedHelperBuildDigest: String
    public let expectedTeamIdentifier: String
    public let expectedCodeRequirementDigest: String
    public let expectedAppBuildDigest: String
    public let expectedAuthorizationProofAlgorithm: String
    public let expectedAuthorizationKeyID: String
    public let sessionID: String
    public let issuedAt: Double
    public let expiresAt: Double
    public let authorizationDigest: String
    public let authenticatedAt: Double
    public let authorizationProof: String

    public init(
        challenge: LifecycleHelperChallengeV2,
        authorizationDigest: String,
        authenticatedAt: Double,
        authorizationProof: String
    ) {
        schemaVersion = challenge.schemaVersion
        helperProtocolVersion = challenge.helperProtocolVersion
        nonce = challenge.nonce
        transactionID = challenge.transactionID
        transactionAuthorityDigest = challenge.transactionAuthorityDigest
        executionManifestDigest = challenge.executionManifestDigest
        recoveryCloneIdentityDigest = challenge.recoveryCloneIdentityDigest
        expectedHelperIdentifier = challenge.expectedHelperIdentifier
        expectedHelperBuildDigest = challenge.expectedHelperBuildDigest
        expectedTeamIdentifier = challenge.expectedTeamIdentifier
        expectedCodeRequirementDigest = challenge.expectedCodeRequirementDigest
        expectedAppBuildDigest = challenge.expectedAppBuildDigest
        expectedAuthorizationProofAlgorithm = challenge.expectedAuthorizationProofAlgorithm
        expectedAuthorizationKeyID = challenge.expectedAuthorizationKeyID
        sessionID = challenge.sessionID
        issuedAt = challenge.issuedAt
        expiresAt = challenge.expiresAt
        self.authorizationDigest = authorizationDigest
        self.authenticatedAt = authenticatedAt
        self.authorizationProof = authorizationProof
    }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case helperProtocolVersion = "helper_protocol_version"
        case nonce
        case transactionID = "transaction_id"
        case transactionAuthorityDigest = "transaction_authority_digest"
        case executionManifestDigest = "execution_manifest_digest"
        case recoveryCloneIdentityDigest = "recovery_clone_identity_digest"
        case expectedHelperIdentifier = "expected_helper_identifier"
        case expectedHelperBuildDigest = "expected_helper_build_digest"
        case expectedTeamIdentifier = "expected_team_identifier"
        case expectedCodeRequirementDigest = "expected_code_requirement_digest"
        case expectedAppBuildDigest = "expected_app_build_digest"
        case expectedAuthorizationProofAlgorithm = "expected_authorization_proof_algorithm"
        case expectedAuthorizationKeyID = "expected_authorization_key_id"
        case sessionID = "session_id"
        case issuedAt = "issued_at"
        case expiresAt = "expires_at"
        case authorizationDigest = "authorization_digest"
        case authenticatedAt = "authenticated_at"
        case authorizationProof = "authorization_proof"
    }
}

public struct LifecycleHelperPeerManifestV2: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let helperProtocolVersion: Int
    public let appBundleIdentifier: String
    public let appShortVersion: String
    public let appBuildNumber: String
    public let appBuildDigest: String
    public let expectedTeamIdentifier: String
    public let helperRelativePath: String
    public let helperIdentifier: String
    public let helperTeamIdentifier: String
    public let helperCDHash: String
    public let helperCodeRequirementDigest: String
    public let helperBuildDigest: String
    public let runnerRelativePath: String
    public let runnerIdentifier: String
    public let runnerTeamIdentifier: String
    public let runnerCDHash: String
    public let runnerCodeRequirementDigest: String
    public let runnerBuildDigest: String
    public let servicePythonRelativePath: String
    public let servicePythonIdentifier: String
    public let servicePythonTeamIdentifier: String
    public let servicePythonCDHash: String
    public let servicePythonCodeRequirementDigest: String
    public let servicePythonAuthoritative: Bool

    enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case helperProtocolVersion = "helper_protocol_version"
        case appBundleIdentifier = "app_bundle_identifier"
        case appShortVersion = "app_short_version"
        case appBuildNumber = "app_build_number"
        case appBuildDigest = "app_build_digest"
        case expectedTeamIdentifier = "expected_team_identifier"
        case helperRelativePath = "helper_relative_path"
        case helperIdentifier = "helper_identifier"
        case helperTeamIdentifier = "helper_team_identifier"
        case helperCDHash = "helper_cdhash"
        case helperCodeRequirementDigest = "helper_code_requirement_digest"
        case helperBuildDigest = "helper_build_digest"
        case runnerRelativePath = "runner_relative_path"
        case runnerIdentifier = "runner_identifier"
        case runnerTeamIdentifier = "runner_team_identifier"
        case runnerCDHash = "runner_cdhash"
        case runnerCodeRequirementDigest = "runner_code_requirement_digest"
        case runnerBuildDigest = "runner_build_digest"
        case servicePythonRelativePath = "service_python_relative_path"
        case servicePythonIdentifier = "service_python_identifier"
        case servicePythonTeamIdentifier = "service_python_team_identifier"
        case servicePythonCDHash = "service_python_cdhash"
        case servicePythonCodeRequirementDigest = "service_python_code_requirement_digest"
        case servicePythonAuthoritative = "service_python_authoritative"
    }
}

public enum LifecycleHelperProtocolV2 {
    public static let helperIdentifier = "com.mnemosyne.inference.lifecycle-helper"
    public static let helperRelativePath = "Contents/Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper"
    public static let runnerIdentifier = LifecycleExecutionProtocolV2.runnerIdentifier
    public static let runnerRelativePath = "Contents/MacOS/mnemosyne-lifecycle-runner"
    public static let peerManifestRelativePath = "Contents/Resources/lifecycle-helper-peer-v2.json"
    public static let maximumJSONBytes = 16 * 1024
    public static let maximumFrameBytes = maximumJSONBytes + 4
    public static let maximumAuthorizationLifetime: Double = 120

    public static func decodeChallenge(
        _ data: Data,
        now: Double
    ) throws -> LifecycleHelperChallengeV2 {
        try requireExactKeys(
            data,
            keys: Set(LifecycleHelperChallengeV2.CodingKeys.allCases.map(\.rawValue))
        )
        let challenge: LifecycleHelperChallengeV2
        do {
            challenge = try JSONDecoder().decode(LifecycleHelperChallengeV2.self, from: data)
        } catch {
            throw LifecycleHelperProtocolError.malformed
        }
        try validate(challenge, now: now)
        return challenge
    }

    public static func validate(
        _ challenge: LifecycleHelperChallengeV2,
        now: Double
    ) throws {
        guard challenge.schemaVersion == 2,
              challenge.helperProtocolVersion == 2,
              challenge.expectedHelperIdentifier == helperIdentifier,
              canonicalUUID(challenge.nonce),
              canonicalUUID(challenge.transactionID),
              canonicalUUID(challenge.sessionID),
              validDigest(challenge.transactionAuthorityDigest),
              validDigest(challenge.executionManifestDigest),
              validDigest(challenge.recoveryCloneIdentityDigest),
              validDigest(challenge.expectedHelperBuildDigest),
              validTeamIdentifier(challenge.expectedTeamIdentifier),
              validDigest(challenge.expectedCodeRequirementDigest),
              validDigest(challenge.expectedAppBuildDigest),
              validProofAlgorithm(challenge.expectedAuthorizationProofAlgorithm),
              validDigest(challenge.expectedAuthorizationKeyID),
              now.isFinite,
              challenge.issuedAt.isFinite,
              challenge.expiresAt.isFinite,
              challenge.issuedAt > 0,
              challenge.expiresAt > challenge.issuedAt,
              challenge.expiresAt - challenge.issuedAt <= maximumAuthorizationLifetime
        else {
            throw LifecycleHelperProtocolError.invalidAuthority
        }
        guard challenge.issuedAt <= now else {
            throw LifecycleHelperProtocolError.invalidAuthority
        }
        guard challenge.expiresAt > now else {
            throw LifecycleHelperProtocolError.expired
        }
    }

    public static func authorizationDigest(
        for challenge: LifecycleHelperChallengeV2
    ) throws -> String {
        sha256(try canonicalData(challenge))
    }

    public static func authorizationProofPayload(
        authorizationDigest: String,
        authenticatedAt: Double,
        algorithm: String,
        keyID: String
    ) throws -> Data {
        guard validDigest(authorizationDigest),
              authenticatedAt.isFinite,
              authenticatedAt > 0,
              validProofAlgorithm(algorithm),
              validDigest(keyID)
        else {
            throw LifecycleHelperProtocolError.invalidAuthority
        }
        let bits = String(format: "%016llx", authenticatedAt.bitPattern)
        return Data([
            "mnemosyne-lifecycle-helper-proof-v1",
            authorizationDigest,
            bits,
            algorithm,
            keyID,
        ].joined(separator: "\0").utf8)
    }

    public static func encodeReceipt(
        _ receipt: LifecycleHelperReceiptV2
    ) throws -> Data {
        let data = try canonicalData(receipt)
        guard data.count <= maximumJSONBytes else {
            throw LifecycleHelperProtocolError.oversized
        }
        return data
    }

    public static func decodeReceipt(_ data: Data) throws -> LifecycleHelperReceiptV2 {
        try requireExactKeys(
            data,
            keys: Set(LifecycleHelperReceiptV2.CodingKeys.allCases.map(\.rawValue))
        )
        do {
            let receipt = try JSONDecoder().decode(LifecycleHelperReceiptV2.self, from: data)
            guard validDigest(receipt.authorizationDigest),
                  validProofAlgorithm(receipt.expectedAuthorizationProofAlgorithm),
                  validDigest(receipt.expectedAuthorizationKeyID),
                  validProof(receipt.authorizationProof),
                  receipt.authenticatedAt.isFinite,
                  receipt.authenticatedAt >= receipt.issuedAt,
                  receipt.authenticatedAt < receipt.expiresAt
            else {
                throw LifecycleHelperProtocolError.invalidAuthority
            }
            return receipt
        } catch {
            if let error = error as? LifecycleHelperProtocolError {
                throw error
            }
            throw LifecycleHelperProtocolError.malformed
        }
    }

    public static func encodeFrame(payload: Data) throws -> Data {
        guard !payload.isEmpty, payload.count <= maximumJSONBytes else {
            throw LifecycleHelperProtocolError.oversized
        }
        var length = UInt32(payload.count).bigEndian
        var frame = Data(bytes: &length, count: MemoryLayout<UInt32>.size)
        frame.append(payload)
        return frame
    }

    public static func decodeFrame(_ frame: Data) throws -> Data {
        guard frame.count >= 4 else {
            throw LifecycleHelperProtocolError.malformed
        }
        let length = frame.prefix(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard length > 0, length <= maximumJSONBytes,
              frame.count == Int(length) + 4
        else {
            throw LifecycleHelperProtocolError.oversized
        }
        return frame.dropFirst(4)
    }

    public static func decodePeerManifest(_ data: Data) throws -> LifecycleHelperPeerManifestV2 {
        try requireExactKeys(
            data,
            keys: Set(LifecycleHelperPeerManifestV2.CodingKeys.allCases.map(\.rawValue))
        )
        let manifest: LifecycleHelperPeerManifestV2
        do {
            manifest = try JSONDecoder().decode(LifecycleHelperPeerManifestV2.self, from: data)
        } catch {
            throw LifecycleHelperProtocolError.malformed
        }
        guard manifest.schemaVersion == 2,
              manifest.helperProtocolVersion == 2,
              manifest.appBundleIdentifier == "com.mnemosyne.inference.menu",
              validVersion(manifest.appShortVersion),
              validBuildNumber(manifest.appBuildNumber),
              validDigest(manifest.appBuildDigest),
              validManifestTeamIdentifier(manifest.expectedTeamIdentifier),
              manifest.helperRelativePath == helperRelativePath,
              manifest.helperIdentifier == helperIdentifier,
              manifest.helperTeamIdentifier == manifest.expectedTeamIdentifier,
              validCDHash(manifest.helperCDHash),
              validDigest(manifest.helperCodeRequirementDigest),
              validDigest(manifest.helperBuildDigest),
              manifest.runnerRelativePath == runnerRelativePath,
              manifest.runnerIdentifier == runnerIdentifier,
              manifest.runnerTeamIdentifier == manifest.expectedTeamIdentifier,
              validCDHash(manifest.runnerCDHash),
              validDigest(manifest.runnerCodeRequirementDigest),
              validDigest(manifest.runnerBuildDigest),
              validServicePythonRelativePath(manifest.servicePythonRelativePath),
              validCodeIdentifier(manifest.servicePythonIdentifier),
              manifest.servicePythonTeamIdentifier == manifest.expectedTeamIdentifier,
              validCDHash(manifest.servicePythonCDHash),
              validDigest(manifest.servicePythonCodeRequirementDigest),
              manifest.servicePythonAuthoritative == false
        else {
            throw LifecycleHelperProtocolError.invalidAuthority
        }
        return manifest
    }

    public static func appBuildDigest(
        bundleIdentifier: String,
        shortVersion: String,
        buildNumber: String,
        teamIdentifier: String
    ) throws -> String {
        try digestObject([
            "app_build_number": buildNumber,
            "app_bundle_identifier": bundleIdentifier,
            "app_short_version": shortVersion,
            "team_identifier": teamIdentifier,
        ])
    }

    public static func helperBuildDigest(
        identifier: String,
        teamIdentifier: String,
        cdHash: String,
        appBuildDigest: String
    ) throws -> String {
        try digestObject([
            "app_build_digest": appBuildDigest,
            "cdhash": cdHash,
            "identifier": identifier,
            "team_identifier": teamIdentifier,
        ])
    }

    public static func runnerBuildDigest(
        identifier: String,
        teamIdentifier: String,
        cdHash: String,
        appBuildDigest: String
    ) throws -> String {
        try digestObject([
            "app_build_digest": appBuildDigest,
            "cdhash": cdHash,
            "identifier": identifier,
            "team_identifier": teamIdentifier,
        ])
    }

    public static func sha256(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    public static func validDigest(_ value: String) -> Bool {
        value.count == 71
            && value.hasPrefix("sha256:")
            && value.dropFirst(7).allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }

    private static func validProofAlgorithm(_ value: String) -> Bool {
        guard 1 ... 64 ~= value.utf8.count,
              let first = value.unicodeScalars.first,
              first.isASCII,
              CharacterSet.lowercaseLetters.contains(first)
        else { return false }
        return value.unicodeScalars.allSatisfy {
            $0.isASCII && (
                CharacterSet.lowercaseLetters.contains($0)
                    || CharacterSet.decimalDigits.contains($0)
                    || $0 == "-"
            )
        }
    }

    private static func validProof(_ value: String) -> Bool {
        32 ... 4096 ~= value.utf8.count
            && value.unicodeScalars.allSatisfy {
                $0.isASCII && (
                    CharacterSet.alphanumerics.contains($0)
                        || $0 == "_" || $0 == "-"
                )
            }
    }

    private static func canonicalData<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        do {
            return try encoder.encode(value)
        } catch {
            throw LifecycleHelperProtocolError.malformed
        }
    }

    private static func digestObject(_ object: [String: String]) throws -> String {
        guard JSONSerialization.isValidJSONObject(object) else {
            throw LifecycleHelperProtocolError.invalidAuthority
        }
        let data = try JSONSerialization.data(
            withJSONObject: object,
            options: [.sortedKeys, .withoutEscapingSlashes]
        )
        return sha256(data)
    }

    private static func canonicalUUID(_ value: String) -> Bool {
        guard let parsed = UUID(uuidString: value) else { return false }
        return parsed.uuidString.lowercased() == value
    }

    private static func validTeamIdentifier(_ value: String) -> Bool {
        1 ... 64 ~= value.utf8.count
            && value.unicodeScalars.allSatisfy {
                $0.isASCII && (CharacterSet.alphanumerics.contains($0) || "._-".contains(Character($0)))
            }
    }

    // codesign reports this fixed sentinel for ad-hoc development builds. It is
    // accepted only in the sealed peer manifest so the helper can reach its
    // explicit production no-Team refusal; a challenge may never claim it.
    private static func validManifestTeamIdentifier(_ value: String) -> Bool {
        value == "not set" || validTeamIdentifier(value)
    }

    private static func validVersion(_ value: String) -> Bool {
        let parts = value.split(separator: ".", omittingEmptySubsequences: false)
        return parts.count == 3 && parts.allSatisfy {
            !$0.isEmpty && $0.allSatisfy(\.isNumber)
        }
    }

    private static func validBuildNumber(_ value: String) -> Bool {
        !value.isEmpty && value.first != "0" && value.allSatisfy(\.isNumber)
    }

    private static func validCDHash(_ value: String) -> Bool {
        [40, 64].contains(value.count)
            && value.allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }

    private static func validCodeIdentifier(_ value: String) -> Bool {
        1 ... 255 ~= value.utf8.count
            && value.unicodeScalars.allSatisfy {
                $0.isASCII && (CharacterSet.alphanumerics.contains($0) || "._-".contains(Character($0)))
            }
    }

    private static func validServicePythonRelativePath(_ value: String) -> Bool {
        value.hasPrefix("Contents/Resources/Python/")
            && !value.hasPrefix("/")
            && !value.contains("\\")
            && !value.split(separator: "/", omittingEmptySubsequences: false)
                .contains(where: { $0.isEmpty || $0 == "." || $0 == ".." })
    }

    private static func requireExactKeys(_ data: Data, keys: Set<String>) throws {
        guard !data.isEmpty, data.count <= maximumJSONBytes else {
            throw LifecycleHelperProtocolError.oversized
        }
        let parsed: Any
        do {
            parsed = try JSONSerialization.jsonObject(with: data)
        } catch {
            throw LifecycleHelperProtocolError.malformed
        }
        guard let object = parsed as? [String: Any], Set(object.keys) == keys else {
            throw LifecycleHelperProtocolError.malformed
        }
        let observedKeys = try topLevelObjectKeys(data)
        guard observedKeys.count == keys.count, Set(observedKeys) == keys else {
            throw LifecycleHelperProtocolError.malformed
        }
    }

    private static func topLevelObjectKeys(_ data: Data) throws -> [String] {
        let bytes = Array(data)
        var cursor = 0
        func whitespace() {
            while cursor < bytes.count, [9, 10, 13, 32].contains(bytes[cursor]) {
                cursor += 1
            }
        }
        func stringEnd() throws -> Int {
            guard cursor < bytes.count, bytes[cursor] == 34 else {
                throw LifecycleHelperProtocolError.malformed
            }
            let start = cursor
            cursor += 1
            var escaped = false
            while cursor < bytes.count {
                let byte = bytes[cursor]
                cursor += 1
                if escaped {
                    escaped = false
                } else if byte == 92 {
                    escaped = true
                } else if byte == 34 {
                    return start
                } else if byte < 32 {
                    throw LifecycleHelperProtocolError.malformed
                }
            }
            throw LifecycleHelperProtocolError.malformed
        }
        func skipValue() throws {
            whitespace()
            guard cursor < bytes.count else {
                throw LifecycleHelperProtocolError.malformed
            }
            if bytes[cursor] == 34 {
                _ = try stringEnd()
                return
            }
            var depth = 0
            var inString = false
            var escaped = false
            while cursor < bytes.count {
                let byte = bytes[cursor]
                if inString {
                    cursor += 1
                    if escaped {
                        escaped = false
                    } else if byte == 92 {
                        escaped = true
                    } else if byte == 34 {
                        inString = false
                    }
                    continue
                }
                if byte == 34 {
                    inString = true
                    cursor += 1
                } else if byte == 123 || byte == 91 {
                    depth += 1
                    cursor += 1
                } else if byte == 125 || byte == 93 {
                    if depth == 0 { return }
                    depth -= 1
                    cursor += 1
                } else if depth == 0, byte == 44 {
                    return
                } else {
                    cursor += 1
                }
            }
        }

        whitespace()
        guard cursor < bytes.count, bytes[cursor] == 123 else {
            throw LifecycleHelperProtocolError.malformed
        }
        cursor += 1
        var keys: [String] = []
        while true {
            whitespace()
            if cursor < bytes.count, bytes[cursor] == 125 {
                cursor += 1
                break
            }
            let keyStart = try stringEnd()
            let keyEnd = cursor
            let keyData = Data(bytes[keyStart ..< keyEnd])
            guard let key = try? JSONDecoder().decode(String.self, from: keyData) else {
                throw LifecycleHelperProtocolError.malformed
            }
            keys.append(key)
            whitespace()
            guard cursor < bytes.count, bytes[cursor] == 58 else {
                throw LifecycleHelperProtocolError.malformed
            }
            cursor += 1
            try skipValue()
            whitespace()
            guard cursor < bytes.count else {
                throw LifecycleHelperProtocolError.malformed
            }
            if bytes[cursor] == 44 {
                cursor += 1
                continue
            }
            if bytes[cursor] == 125 {
                cursor += 1
                break
            }
            throw LifecycleHelperProtocolError.malformed
        }
        whitespace()
        guard cursor == bytes.count else {
            throw LifecycleHelperProtocolError.malformed
        }
        return keys
    }
}

public final class LifecycleHelperReplayGuard: @unchecked Sendable {
    private let lock = NSLock()
    private var consumed: Set<String> = []
    private let capacity: Int

    public init(capacity: Int = 128) {
        self.capacity = max(1, min(capacity, 1024))
    }

    public func consume(_ challenge: LifecycleHelperChallengeV2) throws {
        let key = "\(challenge.sessionID):\(challenge.nonce)"
        lock.lock()
        defer { lock.unlock() }
        guard consumed.count < capacity, consumed.insert(key).inserted else {
            throw LifecycleHelperProtocolError.replayed
        }
    }
}
