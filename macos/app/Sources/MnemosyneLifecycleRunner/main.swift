import Darwin
import Foundation
import MnemosyneAppCore
import Security

private enum RunnerFailure: Error {
    case unavailable
}

private struct CodeIdentity {
    let identifier: String
    let teamIdentifier: String
    let cdHash: String
    let codeRequirementDigest: String
    let mainExecutable: URL
}

@main
private enum MnemosyneLifecycleRunner {
    static func main() {
        var sessionDescriptor: Int32?
        do {
            let descriptor = try inheritedSessionDescriptor()
            sessionDescriptor = descriptor
            try requireUnnamedConnectedUnixStream(descriptor)
            try disableSIGPIPE(descriptor)

            let requestFrame = try readFrame(descriptor)
            try requireNoSecondFrame(descriptor)
            let refusalFrame = try LifecycleRunnerInertAdapterV2.refusalFrame(
                for: requestFrame,
                refusalNonce: UUID().uuidString.lowercased()
            )

            do {
                try verifyBundleAndPeer(socketDescriptor: descriptor)
            } catch {
                // This milestone grants no authority. A parseable request gets
                // the same inert refusal even when signed-peer attestation is
                // absent, including from an ad-hoc development bundle.
                returnUnavailable(refusalFrame, to: descriptor)
            }
            returnUnavailable(refusalFrame, to: descriptor)
        } catch {
            if let descriptor = sessionDescriptor {
                Darwin.close(descriptor)
            }
            writeFixedDiagnostic(LifecycleRunnerInertAdapterV2.refusalCode)
            Darwin.exit(78)
        }
    }
}

private func returnUnavailable(_ frame: Data, to descriptor: Int32) -> Never {
    do {
        try writeExactly(frame, to: descriptor)
    } catch {
        writeFixedDiagnostic(LifecycleRunnerInertAdapterV2.refusalCode)
    }
    Darwin.close(descriptor)
    Darwin.exit(78)
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
        throw RunnerFailure.unavailable
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
        throw RunnerFailure.unavailable
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
        throw RunnerFailure.unavailable
    }
    let headerBytes = MemoryLayout<UInt8>.size + MemoryLayout<sa_family_t>.size
    guard Int(length) <= headerBytes || withUnsafeBytes(of: &address, { raw in
        raw.dropFirst(headerBytes).prefix(max(0, Int(length) - headerBytes))
            .allSatisfy { $0 == 0 }
    }) else {
        throw RunnerFailure.unavailable
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
        throw RunnerFailure.unavailable
    }
}

private func readFrame(_ descriptor: Int32) throws -> Data {
    let header = try readExactly(4, from: descriptor)
    let length = header.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
    guard length > 0,
          length <= LifecycleExecutionProtocolV2.maximumJSONBytes
    else {
        throw RunnerFailure.unavailable
    }
    var frame = header
    frame.append(try readExactly(Int(length), from: descriptor))
    return frame
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
            throw RunnerFailure.unavailable
        }
    }
    return result
}

private func requireNoSecondFrame(_ descriptor: Int32) throws {
    var byte: UInt8 = 0
    let result = recv(descriptor, &byte, 1, MSG_PEEK | MSG_DONTWAIT)
    if result > 0 {
        throw RunnerFailure.unavailable
    }
    if result < 0, errno != EAGAIN, errno != EWOULDBLOCK {
        throw RunnerFailure.unavailable
    }
}

private func writeExactly(_ frame: Data, to descriptor: Int32) throws {
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
            throw RunnerFailure.unavailable
        }
    }
}

private func verifyBundleAndPeer(socketDescriptor: Int32) throws {
    let runnerURL = try currentExecutableURL()
    let macOSDirectory = runnerURL.deletingLastPathComponent()
    let contents = macOSDirectory.deletingLastPathComponent()
    let appURL = contents.deletingLastPathComponent()
    guard runnerURL.lastPathComponent == "mnemosyne-lifecycle-runner",
          macOSDirectory.lastPathComponent == "MacOS",
          contents.lastPathComponent == "Contents",
          appURL.pathExtension == "app",
          !runnerURL.path.contains("/../")
    else {
        throw RunnerFailure.unavailable
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
        throw RunnerFailure.unavailable
    }

    var selfCode: SecCode?
    guard SecCodeCopySelf([], &selfCode) == errSecSuccess,
          let selfCode,
          SecCodeCheckValidity(selfCode, [], nil) == errSecSuccess
    else {
        throw RunnerFailure.unavailable
    }
    let runnerIdentity = try codeIdentity(staticCode(for: selfCode))
    let appIdentity = try codeIdentity(appCode)

    let manifestURL = appURL.appending(
        path: LifecycleHelperProtocolV2.peerManifestRelativePath
    )
    let manifest: LifecycleHelperPeerManifestV2
    do {
        manifest = try LifecycleHelperProtocolV2.decodePeerManifest(
            Data(contentsOf: manifestURL, options: [.mappedIfSafe, .uncached])
        )
    } catch {
        throw RunnerFailure.unavailable
    }

    guard manifest.expectedTeamIdentifier != "not set",
          !manifest.expectedTeamIdentifier.isEmpty,
          runnerIdentity.identifier == LifecycleExecutionProtocolV2.runnerIdentifier,
          runnerIdentity.identifier == manifest.runnerIdentifier,
          runnerIdentity.teamIdentifier == manifest.runnerTeamIdentifier,
          runnerIdentity.teamIdentifier == manifest.expectedTeamIdentifier,
          runnerIdentity.cdHash == manifest.runnerCDHash,
          runnerIdentity.codeRequirementDigest
              == manifest.runnerCodeRequirementDigest,
          runnerIdentity.mainExecutable.resolvingSymlinksInPath().standardizedFileURL
              == runnerURL.resolvingSymlinksInPath().standardizedFileURL,
          appIdentity.identifier == manifest.appBundleIdentifier,
          appIdentity.teamIdentifier == manifest.expectedTeamIdentifier
    else {
        throw RunnerFailure.unavailable
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
        throw RunnerFailure.unavailable
    }
    let appBuildDigest = try LifecycleHelperProtocolV2.appBuildDigest(
        bundleIdentifier: bundleIdentifier,
        shortVersion: shortVersion,
        buildNumber: buildNumber,
        teamIdentifier: runnerIdentity.teamIdentifier
    )
    let runnerBuildDigest = try LifecycleHelperProtocolV2.runnerBuildDigest(
        identifier: runnerIdentity.identifier,
        teamIdentifier: runnerIdentity.teamIdentifier,
        cdHash: runnerIdentity.cdHash,
        appBuildDigest: appBuildDigest
    )
    guard appBuildDigest == manifest.appBuildDigest,
          runnerBuildDigest == manifest.runnerBuildDigest
    else {
        throw RunnerFailure.unavailable
    }

    let peerCode = try codeForPeer(of: socketDescriptor)
    guard SecCodeCheckValidity(peerCode, [], nil) == errSecSuccess else {
        throw RunnerFailure.unavailable
    }
    let peerIdentity = try codeIdentity(staticCode(for: peerCode))
    let expectedPeerURL = appURL.appending(
        path: manifest.servicePythonRelativePath
    ).resolvingSymlinksInPath().standardizedFileURL
    guard manifest.servicePythonAuthoritative == false,
          peerIdentity.identifier == manifest.servicePythonIdentifier,
          peerIdentity.teamIdentifier == manifest.servicePythonTeamIdentifier,
          peerIdentity.teamIdentifier == runnerIdentity.teamIdentifier,
          peerIdentity.cdHash == manifest.servicePythonCDHash,
          peerIdentity.codeRequirementDigest
              == manifest.servicePythonCodeRequirementDigest,
          peerIdentity.mainExecutable.resolvingSymlinksInPath().standardizedFileURL
              == expectedPeerURL
    else {
        throw RunnerFailure.unavailable
    }
}

private func currentExecutableURL() throws -> URL {
    var size: UInt32 = 0
    _ = _NSGetExecutablePath(nil, &size)
    guard size > 0, size <= 16 * 1024 else {
        throw RunnerFailure.unavailable
    }
    var buffer = [CChar](repeating: 0, count: Int(size))
    guard _NSGetExecutablePath(&buffer, &size) == 0 else {
        throw RunnerFailure.unavailable
    }
    return URL(
        fileURLWithFileSystemRepresentation: buffer,
        isDirectory: false,
        relativeTo: nil
    ).standardizedFileURL
}

private func staticCode(at url: URL) throws -> SecStaticCode {
    var code: SecStaticCode?
    guard SecStaticCodeCreateWithPath(url as CFURL, [], &code) == errSecSuccess,
          let code
    else {
        throw RunnerFailure.unavailable
    }
    return code
}

private func staticCode(for code: SecCode) throws -> SecStaticCode {
    var result: SecStaticCode?
    guard SecCodeCopyStaticCode(code, [], &result) == errSecSuccess,
          let result
    else {
        throw RunnerFailure.unavailable
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
        throw RunnerFailure.unavailable
    }
    let tokenData = withUnsafeBytes(of: &auditToken) { Data($0) }
    let attributes = [kSecGuestAttributeAudit as String: tokenData] as CFDictionary
    var code: SecCode?
    guard SecCodeCopyGuestWithAttributes(nil, attributes, [], &code) == errSecSuccess,
          let code
    else {
        throw RunnerFailure.unavailable
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
        throw RunnerFailure.unavailable
    }
    let requirement = unsafeBitCast(
        requirementValue as CFTypeRef,
        to: SecRequirement.self
    )
    var requirementText: CFString?
    guard SecRequirementCopyString(requirement, [], &requirementText) == errSecSuccess,
          let requirementString = requirementText as String?
    else {
        throw RunnerFailure.unavailable
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

private func writeFixedDiagnostic(_ value: String) {
    let data = Data((value + "\n").utf8)
    data.withUnsafeBytes { bytes in
        guard let base = bytes.baseAddress else { return }
        _ = Darwin.write(STDERR_FILENO, base, bytes.count)
    }
}
