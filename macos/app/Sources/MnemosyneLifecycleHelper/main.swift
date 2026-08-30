import Darwin
import Foundation
import LocalAuthentication
import MnemosyneAppCore
import Security

private enum HelperFailure: Error {
    case authorityInvalid
    case authorityMismatch
    case expired
    case conflict
    case rejected
    case unavailable

    var code: String {
        switch self {
        case .authorityInvalid:
            "native_lifecycle_helper_authority_invalid"
        case .authorityMismatch:
            "native_lifecycle_helper_authority_mismatch"
        case .expired:
            "native_lifecycle_helper_authority_expired"
        case .conflict:
            "native_lifecycle_helper_authority_conflict"
        case .rejected:
            "native_lifecycle_helper_authority_rejected"
        case .unavailable:
            "native_lifecycle_helper_authority_unavailable"
        }
    }
}

private struct CodeIdentity {
    let identifier: String
    let teamIdentifier: String
    let cdHash: String
    let codeRequirementDigest: String
    let mainExecutable: URL
}

private struct VerifiedBundle {
    let appURL: URL
    let identity: CodeIdentity
    let manifest: LifecycleHelperPeerManifestV2
    let helperIdentity: CodeIdentity
}

@main
private enum MnemosyneLifecycleHelper {
    static func main() async {
        do {
            let descriptor = try inheritedSessionDescriptor()
            defer { Darwin.close(descriptor) }
            try requireUnnamedConnectedUnixStream(descriptor)
            try disableSIGPIPE(descriptor)

            let verified = try verifyBundleAndPeer(socketDescriptor: descriptor)
            let payload = try readFrame(descriptor)
            try requireNoSecondFrame(descriptor)
            let now = Date().timeIntervalSince1970
            let challenge: LifecycleHelperChallengeV2
            do {
                challenge = try LifecycleHelperProtocolV2.decodeChallenge(
                    payload,
                    now: now
                )
            } catch LifecycleHelperProtocolError.expired {
                throw HelperFailure.expired
            } catch LifecycleHelperProtocolError.replayed {
                throw HelperFailure.conflict
            } catch {
                throw HelperFailure.authorityInvalid
            }
            try verify(challenge: challenge, against: verified)

            let replayGuard = LifecycleHelperReplayGuard(capacity: 1)
            do {
                try replayGuard.consume(challenge)
            } catch {
                throw HelperFailure.conflict
            }

            try await authenticateDeviceOwner()
            let authenticatedAt = Date().timeIntervalSince1970
            guard authenticatedAt < challenge.expiresAt else {
                throw HelperFailure.expired
            }
            let digest = try LifecycleHelperProtocolV2.authorizationDigest(
                for: challenge
            )
            // A public SHA-256 echo is not proof that this signed helper ran
            // LocalAuthentication. Production receipt emission therefore
            // stays closed until the service provisions a direct
            // peer-attested proof key for this exact signed helper build.
            let proof = try authorizationProof(
                challenge: challenge,
                authorizationDigest: digest,
                authenticatedAt: authenticatedAt
            )
            let receipt = LifecycleHelperReceiptV2(
                challenge: challenge,
                authorizationDigest: digest,
                authenticatedAt: authenticatedAt,
                authorizationProof: proof
            )
            let receiptPayload = try LifecycleHelperProtocolV2.encodeReceipt(receipt)
            try writeFrame(receiptPayload, to: descriptor)
        } catch let failure as HelperFailure {
            writeFixedDiagnostic(failure.code)
            Darwin.exit(1)
        } catch {
            writeFixedDiagnostic(HelperFailure.unavailable.code)
            Darwin.exit(1)
        }
    }
}

private func authorizationProof(
    challenge: LifecycleHelperChallengeV2,
    authorizationDigest: String,
    authenticatedAt: Double
) throws -> String {
    _ = try LifecycleHelperProtocolV2.authorizationProofPayload(
        authorizationDigest: authorizationDigest,
        authenticatedAt: authenticatedAt,
        algorithm: challenge.expectedAuthorizationProofAlgorithm,
        keyID: challenge.expectedAuthorizationKeyID
    )
    // Intentionally unavailable: no private proof material is embedded in the
    // app, accepted from the environment, or derived from public code-signing
    // metadata. A future direct service/helper OS-peer ceremony owns this.
    throw HelperFailure.unavailable
}

private func inheritedSessionDescriptor() throws -> Int32 {
    let arguments = CommandLine.arguments
    guard arguments.count == 3,
          arguments[1] == "--session-fd",
          let descriptor = Int32(arguments[2]),
          descriptor >= 3,
          descriptor <= 1024,
          fcntl(descriptor, F_GETFD) >= 0
    else {
        throw HelperFailure.unavailable
    }
    return descriptor
}

private func requireUnnamedConnectedUnixStream(_ descriptor: Int32) throws {
    var socketType: Int32 = 0
    var socketTypeLength = socklen_t(MemoryLayout.size(ofValue: socketType))
    guard getsockopt(
        descriptor,
        SOL_SOCKET,
        SO_TYPE,
        &socketType,
        &socketTypeLength
    ) == 0,
        socketType == SOCK_STREAM
    else {
        throw HelperFailure.unavailable
    }
    try requireUnnamedUnixAddress(descriptor, peer: false)
    try requireUnnamedUnixAddress(descriptor, peer: true)
}

private func requireUnnamedUnixAddress(_ descriptor: Int32, peer: Bool) throws {
    var address = sockaddr_un()
    var length = socklen_t(MemoryLayout<sockaddr_un>.size)
    let result = withUnsafeMutablePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
            peer
                ? getpeername(descriptor, socketAddress, &length)
                : getsockname(descriptor, socketAddress, &length)
        }
    }
    guard result == 0, address.sun_family == sa_family_t(AF_UNIX) else {
        throw HelperFailure.unavailable
    }
    let headerBytes = MemoryLayout<UInt8>.size + MemoryLayout<sa_family_t>.size
    guard Int(length) <= headerBytes || withUnsafeBytes(of: &address, { raw in
        raw.dropFirst(headerBytes).prefix(max(0, Int(length) - headerBytes))
            .allSatisfy { $0 == 0 }
    }) else {
        throw HelperFailure.unavailable
    }
}

private func disableSIGPIPE(_ descriptor: Int32) throws {
    var enabled: Int32 = 1
    guard setsockopt(
        descriptor,
        SOL_SOCKET,
        SO_NOSIGPIPE,
        &enabled,
        socklen_t(MemoryLayout.size(ofValue: enabled))
    ) == 0 else {
        throw HelperFailure.unavailable
    }
}

private func readFrame(_ descriptor: Int32) throws -> Data {
    let header = try readExactly(4, from: descriptor)
    let length = header.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
    guard length > 0, length <= LifecycleHelperProtocolV2.maximumJSONBytes else {
        throw HelperFailure.authorityInvalid
    }
    return try readExactly(Int(length), from: descriptor)
}

private func readExactly(_ count: Int, from descriptor: Int32) throws -> Data {
    var result = Data(count: count)
    var offset = 0
    while offset < count {
        let amount = result.withUnsafeMutableBytes { buffer -> Int in
            guard let base = buffer.baseAddress else { return -1 }
            return Darwin.read(descriptor, base.advanced(by: offset), count - offset)
        }
        if amount > 0 {
            offset += amount
        } else if amount < 0, errno == EINTR {
            continue
        } else {
            throw HelperFailure.unavailable
        }
    }
    return result
}

private func requireNoSecondFrame(_ descriptor: Int32) throws {
    var byte: UInt8 = 0
    let result = recv(descriptor, &byte, 1, MSG_PEEK | MSG_DONTWAIT)
    if result > 0 {
        throw HelperFailure.conflict
    }
    if result < 0, errno != EAGAIN, errno != EWOULDBLOCK {
        throw HelperFailure.unavailable
    }
}

private func writeFrame(_ payload: Data, to descriptor: Int32) throws {
    let frame: Data
    do {
        frame = try LifecycleHelperProtocolV2.encodeFrame(payload: payload)
    } catch {
        throw HelperFailure.unavailable
    }
    var offset = 0
    while offset < frame.count {
        let amount = frame.withUnsafeBytes { buffer -> Int in
            guard let base = buffer.baseAddress else { return -1 }
            return Darwin.write(descriptor, base.advanced(by: offset), frame.count - offset)
        }
        if amount > 0 {
            offset += amount
        } else if amount < 0, errno == EINTR {
            continue
        } else {
            throw HelperFailure.unavailable
        }
    }
}

private func verifyBundleAndPeer(socketDescriptor: Int32) throws -> VerifiedBundle {
    let helperURL = try currentExecutableURL()
    let macOSDirectory = helperURL.deletingLastPathComponent()
    let helperContents = macOSDirectory.deletingLastPathComponent()
    let helperWrapper = helperContents.deletingLastPathComponent()
    let helpersDirectory = helperWrapper.deletingLastPathComponent()
    let outerContents = helpersDirectory.deletingLastPathComponent()
    let appURL = outerContents.deletingLastPathComponent()
    guard helperURL.lastPathComponent == "mnemosyne-lifecycle-helper",
          macOSDirectory.lastPathComponent == "MacOS",
          helperContents.lastPathComponent == "Contents",
          helperWrapper.lastPathComponent == "MnemosyneLifecycleAuthorization.app",
          helpersDirectory.lastPathComponent == "Helpers",
          outerContents.lastPathComponent == "Contents",
          appURL.pathExtension == "app",
          !helperURL.path.contains("/../")
    else {
        throw HelperFailure.unavailable
    }

    let appCode = try staticCode(at: appURL)
    let sealFlags = SecCSFlags(
        rawValue: UInt32(
            kSecCSCheckAllArchitectures
                | kSecCSCheckNestedCode
                | kSecCSStrictValidate
        )
    )
    guard SecStaticCodeCheckValidity(appCode, sealFlags, nil) == errSecSuccess else {
        throw HelperFailure.authorityInvalid
    }

    var selfCode: SecCode?
    guard SecCodeCopySelf([], &selfCode) == errSecSuccess,
          let selfCode
    else {
        throw HelperFailure.unavailable
    }
    guard SecCodeCheckValidity(selfCode, [], nil) == errSecSuccess else {
        throw HelperFailure.authorityInvalid
    }
    let helperIdentity = try codeIdentity(staticCode(for: selfCode))
    let appIdentity = try codeIdentity(appCode)
    guard helperIdentity.identifier == LifecycleHelperProtocolV2.helperIdentifier,
          helperIdentity.mainExecutable.resolvingSymlinksInPath().standardizedFileURL
              == helperURL.resolvingSymlinksInPath().standardizedFileURL
    else {
        throw HelperFailure.authorityMismatch
    }

    let manifestURL = appURL.appending(
        path: LifecycleHelperProtocolV2.peerManifestRelativePath
    )
    let manifestData: Data
    do {
        manifestData = try Data(
            contentsOf: manifestURL,
            options: [.mappedIfSafe, .uncached]
        )
    } catch {
        throw HelperFailure.authorityInvalid
    }
    let manifest: LifecycleHelperPeerManifestV2
    do {
        manifest = try LifecycleHelperProtocolV2.decodePeerManifest(manifestData)
    } catch {
        throw HelperFailure.authorityInvalid
    }

    guard helperIdentity.teamIdentifier != "not set",
          !helperIdentity.teamIdentifier.isEmpty,
          appIdentity.teamIdentifier == helperIdentity.teamIdentifier,
          manifest.expectedTeamIdentifier == helperIdentity.teamIdentifier,
          manifest.helperRelativePath == LifecycleHelperProtocolV2.helperRelativePath,
          manifest.helperIdentifier == helperIdentity.identifier,
          manifest.helperTeamIdentifier == helperIdentity.teamIdentifier,
          manifest.helperCDHash == helperIdentity.cdHash,
          manifest.helperCodeRequirementDigest
              == helperIdentity.codeRequirementDigest
    else {
        // Development ad-hoc builds are staged and verified, but can never
        // produce a production owner-authentication receipt.
        throw HelperFailure.unavailable
    }

    guard let bundle = Bundle(url: appURL),
          let bundleIdentifier = bundle.bundleIdentifier,
          let shortVersion = bundle.object(
              forInfoDictionaryKey: "CFBundleShortVersionString"
          ) as? String,
          let buildNumber = bundle.object(
              forInfoDictionaryKey: "CFBundleVersion"
          ) as? String,
          bundleIdentifier == manifest.appBundleIdentifier,
          shortVersion == manifest.appShortVersion,
          buildNumber == manifest.appBuildNumber
    else {
        throw HelperFailure.authorityMismatch
    }
    let appBuildDigest = try LifecycleHelperProtocolV2.appBuildDigest(
        bundleIdentifier: bundleIdentifier,
        shortVersion: shortVersion,
        buildNumber: buildNumber,
        teamIdentifier: helperIdentity.teamIdentifier
    )
    let helperBuildDigest = try LifecycleHelperProtocolV2.helperBuildDigest(
        identifier: helperIdentity.identifier,
        teamIdentifier: helperIdentity.teamIdentifier,
        cdHash: helperIdentity.cdHash,
        appBuildDigest: appBuildDigest
    )
    guard appBuildDigest == manifest.appBuildDigest,
          helperBuildDigest == manifest.helperBuildDigest
    else {
        throw HelperFailure.authorityMismatch
    }

    let peerCode = try codeForPeer(of: socketDescriptor)
    guard SecCodeCheckValidity(peerCode, [], nil) == errSecSuccess else {
        throw HelperFailure.authorityInvalid
    }
    let peerIdentity = try codeIdentity(staticCode(for: peerCode))
    let expectedPeerURL = appURL.appending(
        path: manifest.servicePythonRelativePath
    )
        .resolvingSymlinksInPath().standardizedFileURL
    guard manifest.servicePythonAuthoritative == false,
          peerIdentity.identifier == manifest.servicePythonIdentifier,
          peerIdentity.teamIdentifier == manifest.servicePythonTeamIdentifier,
          peerIdentity.teamIdentifier == helperIdentity.teamIdentifier,
          peerIdentity.cdHash == manifest.servicePythonCDHash,
          peerIdentity.codeRequirementDigest
              == manifest.servicePythonCodeRequirementDigest,
          peerIdentity.mainExecutable.resolvingSymlinksInPath().standardizedFileURL
              == expectedPeerURL
    else {
        throw HelperFailure.authorityMismatch
    }
    return VerifiedBundle(
        appURL: appURL,
        identity: appIdentity,
        manifest: manifest,
        helperIdentity: helperIdentity
    )
}

private func verify(
    challenge: LifecycleHelperChallengeV2,
    against verified: VerifiedBundle
) throws {
    let manifest = verified.manifest
    guard challenge.expectedHelperIdentifier == manifest.helperIdentifier,
          challenge.expectedHelperBuildDigest == manifest.helperBuildDigest,
          challenge.expectedTeamIdentifier == manifest.expectedTeamIdentifier,
          challenge.expectedCodeRequirementDigest
              == manifest.helperCodeRequirementDigest,
          challenge.expectedAppBuildDigest == manifest.appBuildDigest
    else {
        throw HelperFailure.authorityMismatch
    }
}

private func currentExecutableURL() throws -> URL {
    var size: UInt32 = 0
    _ = _NSGetExecutablePath(nil, &size)
    guard size > 0, size <= 16 * 1024 else {
        throw HelperFailure.unavailable
    }
    var buffer = [CChar](repeating: 0, count: Int(size))
    guard _NSGetExecutablePath(&buffer, &size) == 0 else {
        throw HelperFailure.unavailable
    }
    return URL(fileURLWithFileSystemRepresentation: buffer, isDirectory: false, relativeTo: nil)
        .standardizedFileURL
}

private func staticCode(at url: URL) throws -> SecStaticCode {
    var code: SecStaticCode?
    guard SecStaticCodeCreateWithPath(url as CFURL, [], &code) == errSecSuccess,
          let code
    else {
        throw HelperFailure.authorityInvalid
    }
    return code
}

private func staticCode(for code: SecCode) throws -> SecStaticCode {
    var result: SecStaticCode?
    guard SecCodeCopyStaticCode(code, [], &result) == errSecSuccess,
          let result
    else {
        throw HelperFailure.authorityInvalid
    }
    return result
}

private func codeForPeer(of descriptor: Int32) throws -> SecCode {
    var auditToken = audit_token_t()
    var tokenLength = socklen_t(MemoryLayout.size(ofValue: auditToken))
    guard getsockopt(
        descriptor,
        SOL_LOCAL,
        LOCAL_PEERTOKEN,
        &auditToken,
        &tokenLength
    ) == 0,
        tokenLength == MemoryLayout.size(ofValue: auditToken)
    else {
        throw HelperFailure.unavailable
    }
    let tokenData = withUnsafeBytes(of: &auditToken) { Data($0) }
    let attributes = [kSecGuestAttributeAudit as String: tokenData] as CFDictionary
    var code: SecCode?
    guard SecCodeCopyGuestWithAttributes(nil, attributes, [], &code) == errSecSuccess,
          let code
    else {
        throw HelperFailure.authorityInvalid
    }
    return code
}

private func codeIdentity(_ code: SecStaticCode) throws -> CodeIdentity {
    let flags = SecCSFlags(
        rawValue: UInt32(kSecCSSigningInformation | kSecCSRequirementInformation)
    )
    var information: CFDictionary?
    guard SecCodeCopySigningInformation(code, flags, &information) == errSecSuccess,
          let dictionary = information as? [String: Any],
          let identifier = dictionary[kSecCodeInfoIdentifier as String] as? String,
          let team = dictionary[kSecCodeInfoTeamIdentifier as String] as? String,
          let unique = dictionary[kSecCodeInfoUnique as String] as? Data,
          let requirementValue = dictionary[
              kSecCodeInfoDesignatedRequirement as String
          ],
          CFGetTypeID(requirementValue as CFTypeRef)
              == SecRequirementGetTypeID(),
          let mainExecutable = dictionary[
              kSecCodeInfoMainExecutable as String
          ] as? URL
    else {
        throw HelperFailure.authorityInvalid
    }
    let requirement = unsafeBitCast(
        requirementValue as CFTypeRef,
        to: SecRequirement.self
    )
    var requirementText: CFString?
    guard SecRequirementCopyString(requirement, [], &requirementText) == errSecSuccess,
          let requirementString = requirementText as String?
    else {
        throw HelperFailure.authorityInvalid
    }
    return CodeIdentity(
        identifier: identifier,
        teamIdentifier: team,
        cdHash: unique.map { String(format: "%02x", $0) }.joined(),
        codeRequirementDigest: LifecycleHelperProtocolV2.sha256(
            Data(requirementString.utf8)
        ),
        mainExecutable: mainExecutable
    )
}

private func authenticateDeviceOwner() async throws {
    let context = LAContext()
    context.localizedFallbackTitle = "Use Password"
    var error: NSError?
    guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
        throw HelperFailure.unavailable
    }
    do {
        let accepted = try await context.evaluatePolicy(
            .deviceOwnerAuthentication,
            localizedReason: "Authorize this Unified Inference lifecycle transaction."
        )
        guard accepted else {
            throw HelperFailure.rejected
        }
    } catch {
        throw HelperFailure.rejected
    }
}

private func writeFixedDiagnostic(_ value: String) {
    let data = Data((value + "\n").utf8)
    data.withUnsafeBytes { bytes in
        guard let base = bytes.baseAddress else { return }
        _ = Darwin.write(STDERR_FILENO, base, bytes.count)
    }
}
