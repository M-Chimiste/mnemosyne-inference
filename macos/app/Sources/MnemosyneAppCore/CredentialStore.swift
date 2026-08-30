import Darwin
import Foundation

public enum ManagedCredential: String, CaseIterable, Identifiable, Sendable {
    case omlxAPIKey = "OMLX_API_KEY"
    case omlxAdminSession = "OMLX_ADMIN_SESSION"
    case huggingFaceToken = "HF_TOKEN"
    case inferenceAPIKey = "INFERENCE_API_KEY"
    case fleetAPIKey = "FLEET_API_KEY"
    case fleetInferenceAPIKey = "FLEET_INFERENCE_API_KEY"
    case adminPassword = "ADMIN_PASSWORD"
    case tokenSidecarPostgresDSN = "TOKEN_SIDECAR_POSTGRES_DSN"

    public var id: String { rawValue }

    /// Credentials whose lifecycle belongs to the Hub pairing transaction.
    ///
    /// Legacy static enrollments still edit these values through Settings;
    /// callers must also consult the local pairing status before hiding or
    /// rejecting their editor.
    public var isFleetPairingCredential: Bool {
        switch self {
        case .fleetAPIKey, .fleetInferenceAPIKey:
            true
        default:
            false
        }
    }

    public var displayName: String {
        switch self {
        case .omlxAPIKey: "oMLX API key"
        case .omlxAdminSession: "oMLX admin session"
        case .huggingFaceToken: "Hugging Face token"
        case .inferenceAPIKey: "Inference API key"
        case .fleetAPIKey: "Fleet gateway API key"
        case .fleetInferenceAPIKey: "Fleet dispatch API key"
        case .adminPassword: "Control service password"
        case .tokenSidecarPostgresDSN: "Postgres usage ledger URL"
        }
    }

    public var help: String {
        switch self {
        case .omlxAPIKey: "Optional credential sent only to the local oMLX API."
        case .omlxAdminSession: "Session used to unload models through the oMLX admin API."
        case .huggingFaceToken:
            "Optional token for authenticated Hugging Face downloads and required for gated or private models."
        case .inferenceAPIKey:
            "Optional. When configured, /v1/* requests require this bearer key. Leave it unset to allow unauthenticated inference, including from the local network."
        case .fleetAPIKey:
            "Dedicated bearer credential used by the Hub to read this Mac's fleet snapshot."
        case .fleetInferenceAPIKey:
            "Dedicated bearer credential accepted only for Hub-routed inference requests."
        case .adminPassword: "Required only when the control API is exposed beyond this Mac."
        case .tokenSidecarPostgresDSN: "Connection URL used by Unified Inference to deliver token usage to the central ledger."
        }
    }
}

public enum CredentialDraftPreview {
    public static func render(
        _ value: String,
        for credential: ManagedCredential
    ) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        if credential == .tokenSidecarPostgresDSN {
            return postgresDSN(trimmed) ?? compactSecret(trimmed)
        }
        return compactSecret(trimmed)
    }

    private static func postgresDSN(_ value: String) -> String? {
        guard
            let components = URLComponents(string: value),
            let scheme = components.scheme,
            let host = components.host,
            !scheme.isEmpty,
            !host.isEmpty
        else {
            return nil
        }

        var preview = "\(scheme)://"
        if let user = components.user, !user.isEmpty {
            preview += user
            if let password = components.password {
                preview += ":\(truncatedPassword(password))"
            }
            preview += "@"
        }
        if host.contains(":") {
            preview += "[\(host)]"
        } else {
            preview += host
        }
        if let port = components.port {
            preview += ":\(port)"
        }
        preview += components.path
        if components.query != nil {
            preview += "?…"
        }
        if components.fragment != nil {
            preview += "#…"
        }
        if let password = components.password {
            let noun = password.count == 1 ? "character" : "characters"
            preview += " · password \(password.count) \(noun)"
        }
        return preview
    }

    private static func truncatedPassword(_ value: String) -> String {
        guard value.count > 8 else {
            return String(repeating: "•", count: max(1, value.count))
        }
        return middleTruncated(value)
    }

    private static func compactSecret(_ value: String) -> String {
        let noun = value.count == 1 ? "character" : "characters"
        guard value.count > 8 else {
            return "\(String(repeating: "•", count: max(1, value.count))) · \(value.count) \(noun)"
        }
        return "\(middleTruncated(value)) · \(value.count) \(noun)"
    }

    private static func middleTruncated(_ value: String) -> String {
        let hiddenCharacterCount = 4
        let prefixCount = min(12, max(2, (value.count - hiddenCharacterCount) / 2))
        let suffixCount = min(
            7,
            max(2, value.count - prefixCount - hiddenCharacterCount)
        )
        return "\(value.prefix(prefixCount)) •••• \(value.suffix(suffixCount))"
    }
}

public struct CredentialStatus: Equatable, Sendable {
    public let configured: Set<ManagedCredential>

    public init(configured: Set<ManagedCredential>) {
        self.configured = configured
    }
}

private let privateEnvironmentLockSuffix = ".lock"

// Darwin imports both the `struct flock` type and the BSD `flock(2)` symbol
// under the same Swift name. Give the stable C symbol an unambiguous local
// name so this lock interoperates with Python's `fcntl.flock`.
@_silgen_name("flock")
private func mnemosyneFlock(_ descriptor: Int32, _ operation: Int32) -> Int32

private struct POSIXFileIdentity: Equatable {
    let device: dev_t
    let inode: ino_t

    init(_ metadata: stat) {
        device = metadata.st_dev
        inode = metadata.st_ino
    }
}

private struct LockedEnvironment {
    let directoryDescriptor: Int32
    let lockDescriptor: Int32
    let lockIdentity: POSIXFileIdentity
}

public struct CredentialStore: Sendable {
    public let environmentURL: URL

    public init(environmentURL: URL) {
        self.environmentURL = environmentURL
    }

    public func status() throws -> CredentialStatus {
        let contents = try withLockedEnvironment(exclusive: false) { locked in
            let (text, identity) = try read(locked)
            try validateLockedPaths(locked)
            guard try currentEnvironmentIdentity(locked) == identity else {
                throw CredentialStoreError.invalidPrivateEnvironment
            }
            return text
        }
        let configured: Set<ManagedCredential> = Set(
            contents.components(separatedBy: .newlines).compactMap { line in
            guard let (key, value) = assignment(in: line), !value.isEmpty else { return nil }
            return ManagedCredential(rawValue: key)
            }
        )
        return CredentialStatus(configured: configured)
    }

    public func apply(
        replacements: [ManagedCredential: String],
        clearing: Set<ManagedCredential>
    ) throws {
        for value in replacements.values where value.contains("\n") || value.contains("\r") {
            throw CredentialStoreError.multilineValue
        }

        try withLockedEnvironment(exclusive: true) { locked in
            let (original, originalIdentity) = try read(locked)
            let pairingOwnsFleetCredentials = original
                .components(separatedBy: .newlines)
                .contains { line in
                    guard let (key, value) = assignment(in: line) else {
                        return false
                    }
                    return key == "FLEET_MANAGEMENT_API_KEY" && !value.isEmpty
                }
            let fleetCredentials: Set<ManagedCredential> = [
                .fleetAPIKey,
                .fleetInferenceAPIKey,
            ]
            if pairingOwnsFleetCredentials,
               !fleetCredentials.isDisjoint(with: Set(replacements.keys).union(clearing))
            {
                throw CredentialStoreError.pairingManagedFleetCredential
            }
            let rendered = render(
                original,
                replacements: replacements,
                clearing: clearing
            )
            try write(
                rendered,
                replacing: originalIdentity,
                in: locked
            )
        }
    }

    private var lockName: String {
        environmentURL.lastPathComponent + privateEnvironmentLockSuffix
    }

    private func withLockedEnvironment<T>(
        exclusive: Bool,
        _ body: (LockedEnvironment) throws -> T
    ) throws -> T {
        guard
            environmentURL.isFileURL,
            !environmentURL.lastPathComponent.isEmpty,
            environmentURL.lastPathComponent != ".",
            environmentURL.lastPathComponent != ".."
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }

        let fileManager = FileManager.default
        let directory = environmentURL.deletingLastPathComponent()
        if try pathMetadata(directory) == nil {
            do {
                try fileManager.createDirectory(
                    at: directory,
                    withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                guard try pathMetadata(directory) != nil else {
                    throw CredentialStoreError.privateEnvironmentWriteFailed
                }
            }
        }
        guard let pathDirectoryMetadata = try pathMetadata(directory) else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        guard
            isDirectory(pathDirectoryMetadata.st_mode),
            pathDirectoryMetadata.st_uid == geteuid(),
            pathDirectoryMetadata.st_mode & 0o022 == 0
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }

        let directoryDescriptor = directory.path.withCString {
            Darwin.open(
                $0,
                O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW
            )
        }
        guard directoryDescriptor >= 0 else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        defer { Darwin.close(directoryDescriptor) }

        var openedDirectoryMetadata = stat()
        guard
            fstat(directoryDescriptor, &openedDirectoryMetadata) == 0,
            isDirectory(openedDirectoryMetadata.st_mode),
            openedDirectoryMetadata.st_uid == geteuid(),
            POSIXFileIdentity(openedDirectoryMetadata)
                == POSIXFileIdentity(pathDirectoryMetadata),
            fchmod(directoryDescriptor, 0o700) == 0
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }

        let lockDescriptor = lockName.withCString {
            Darwin.openat(
                directoryDescriptor,
                $0,
                O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW,
                mode_t(0o600)
            )
        }
        guard lockDescriptor >= 0 else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        defer { Darwin.close(lockDescriptor) }

        var lockMetadata = stat()
        guard
            fstat(lockDescriptor, &lockMetadata) == 0,
            validateOwnedRegular(
                lockMetadata,
                allowReadableByOthers: true
            ),
            fchmod(lockDescriptor, 0o600) == 0,
            mnemosyneFlock(lockDescriptor, exclusive ? LOCK_EX : LOCK_SH) == 0
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        defer { _ = mnemosyneFlock(lockDescriptor, LOCK_UN) }

        let locked = LockedEnvironment(
            directoryDescriptor: directoryDescriptor,
            lockDescriptor: lockDescriptor,
            lockIdentity: POSIXFileIdentity(lockMetadata)
        )
        try validateLockedPaths(locked)
        return try body(locked)
    }

    private func read(
        _ locked: LockedEnvironment
    ) throws -> (String, POSIXFileIdentity?) {
        let descriptor = environmentURL.lastPathComponent.withCString {
            Darwin.openat(
                locked.directoryDescriptor,
                $0,
                O_RDONLY | O_CLOEXEC | O_NOFOLLOW
            )
        }
        if descriptor < 0, errno == ENOENT {
            return ("", nil)
        }
        guard descriptor >= 0 else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        defer { Darwin.close(descriptor) }

        var metadata = stat()
        guard
            fstat(descriptor, &metadata) == 0,
            validateOwnedRegular(metadata, allowReadableByOthers: true)
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }

        var contents = Data()
        var buffer = [UInt8](repeating: 0, count: 16 * 1024)
        while true {
            let count = buffer.withUnsafeMutableBytes { bytes in
                Darwin.read(descriptor, bytes.baseAddress, bytes.count)
            }
            if count == 0 { break }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else {
                throw CredentialStoreError.invalidPrivateEnvironment
            }
            contents.append(contentsOf: buffer.prefix(Int(count)))
        }
        guard let text = String(data: contents, encoding: .utf8) else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        return (text, POSIXFileIdentity(metadata))
    }

    private func render(
        _ original: String,
        replacements: [ManagedCredential: String],
        clearing: Set<ManagedCredential>
    ) -> String {
        var output: [String] = []
        var handled: Set<ManagedCredential> = []

        for line in original.components(separatedBy: .newlines) {
            guard
                let (key, _) = assignment(in: line),
                let credential = ManagedCredential(rawValue: key)
            else {
                output.append(line)
                continue
            }
            guard handled.insert(credential).inserted else { continue }
            if clearing.contains(credential) { continue }
            if let replacement = replacements[credential], !replacement.isEmpty {
                output.append("\(credential.rawValue)=\(replacement)")
            } else {
                output.append(line)
            }
        }

        for credential in ManagedCredential.allCases where !handled.contains(credential) {
            guard
                !clearing.contains(credential),
                let replacement = replacements[credential],
                !replacement.isEmpty
            else { continue }
            output.append("\(credential.rawValue)=\(replacement)")
        }

        while output.last == "" { output.removeLast() }
        return output.isEmpty ? "" : output.joined(separator: "\n") + "\n"
    }

    private func write(
        _ contents: String,
        replacing originalIdentity: POSIXFileIdentity?,
        in locked: LockedEnvironment
    ) throws {
        let temporaryName = ".\(environmentURL.lastPathComponent).\(UUID().uuidString).tmp"
        var temporaryDescriptor = temporaryName.withCString {
            Darwin.openat(
                locked.directoryDescriptor,
                $0,
                O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
                mode_t(0o600)
            )
        }
        guard temporaryDescriptor >= 0 else {
            throw CredentialStoreError.privateEnvironmentWriteFailed
        }
        defer {
            if temporaryDescriptor >= 0 {
                Darwin.close(temporaryDescriptor)
            }
            temporaryName.withCString {
                _ = Darwin.unlinkat(locked.directoryDescriptor, $0, 0)
            }
        }

        let payload = Data(contents.utf8)
        try payload.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(
                    temporaryDescriptor,
                    bytes.baseAddress?.advanced(by: offset),
                    bytes.count - offset
                )
                if count < 0, errno == EINTR { continue }
                guard count > 0 else {
                    throw CredentialStoreError.privateEnvironmentWriteFailed
                }
                offset += count
            }
        }
        guard
            fchmod(temporaryDescriptor, 0o600) == 0,
            fsync(temporaryDescriptor) == 0
        else {
            throw CredentialStoreError.privateEnvironmentWriteFailed
        }
        guard Darwin.close(temporaryDescriptor) == 0 else {
            temporaryDescriptor = -1
            throw CredentialStoreError.privateEnvironmentWriteFailed
        }
        temporaryDescriptor = -1

        try validateLockedPaths(locked)
        guard try currentEnvironmentIdentity(locked) == originalIdentity else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        let renamed = temporaryName.withCString { temporaryPath in
            environmentURL.lastPathComponent.withCString { environmentPath in
                Darwin.renameat(
                    locked.directoryDescriptor,
                    temporaryPath,
                    locked.directoryDescriptor,
                    environmentPath
                )
            }
        }
        guard renamed == 0, fsync(locked.directoryDescriptor) == 0 else {
            throw CredentialStoreError.privateEnvironmentWriteFailed
        }
    }

    private func validateLockedPaths(_ locked: LockedEnvironment) throws {
        let directory = environmentURL.deletingLastPathComponent()
        guard let currentDirectoryMetadata = try pathMetadata(directory) else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        var openedDirectoryMetadata = stat()
        guard
            fstat(locked.directoryDescriptor, &openedDirectoryMetadata) == 0,
            isDirectory(currentDirectoryMetadata.st_mode),
            currentDirectoryMetadata.st_uid == geteuid(),
            POSIXFileIdentity(currentDirectoryMetadata)
                == POSIXFileIdentity(openedDirectoryMetadata)
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }

        var openedLockMetadata = stat()
        var currentLockMetadata = stat()
        let lockStatus = lockName.withCString {
            Darwin.fstatat(
                locked.directoryDescriptor,
                $0,
                &currentLockMetadata,
                AT_SYMLINK_NOFOLLOW
            )
        }
        guard
            fstat(locked.lockDescriptor, &openedLockMetadata) == 0,
            lockStatus == 0,
            validateOwnedRegular(
                openedLockMetadata,
                allowReadableByOthers: false
            ),
            validateOwnedRegular(
                currentLockMetadata,
                allowReadableByOthers: false
            ),
            POSIXFileIdentity(openedLockMetadata) == locked.lockIdentity,
            POSIXFileIdentity(currentLockMetadata) == locked.lockIdentity
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
    }

    private func currentEnvironmentIdentity(
        _ locked: LockedEnvironment
    ) throws -> POSIXFileIdentity? {
        var metadata = stat()
        let status = environmentURL.lastPathComponent.withCString {
            Darwin.fstatat(
                locked.directoryDescriptor,
                $0,
                &metadata,
                AT_SYMLINK_NOFOLLOW
            )
        }
        if status != 0, errno == ENOENT { return nil }
        guard
            status == 0,
            validateOwnedRegular(metadata, allowReadableByOthers: true)
        else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        return POSIXFileIdentity(metadata)
    }

    private func pathMetadata(_ url: URL) throws -> stat? {
        var metadata = stat()
        let status = url.path.withCString { Darwin.lstat($0, &metadata) }
        if status != 0, errno == ENOENT { return nil }
        guard status == 0 else {
            throw CredentialStoreError.invalidPrivateEnvironment
        }
        return metadata
    }

    private func validateOwnedRegular(
        _ metadata: stat,
        allowReadableByOthers: Bool
    ) -> Bool {
        guard
            isRegular(metadata.st_mode),
            metadata.st_uid == geteuid(),
            metadata.st_nlink == 1,
            metadata.st_mode & 0o022 == 0
        else {
            return false
        }
        if !allowReadableByOthers, metadata.st_mode & 0o077 != 0 {
            return false
        }
        return true
    }

    private func isRegular(_ mode: mode_t) -> Bool {
        mode & S_IFMT == S_IFREG
    }

    private func isDirectory(_ mode: mode_t) -> Bool {
        mode & S_IFMT == S_IFDIR
    }

    private func assignment(in line: String) -> (String, String)? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, !trimmed.hasPrefix("#"), let separator = trimmed.firstIndex(of: "=") else {
            return nil
        }
        let key = String(trimmed[..<separator]).trimmingCharacters(in: .whitespaces)
        let value = String(trimmed[trimmed.index(after: separator)...])
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        return (key, value)
    }
}

public enum CredentialStoreError: Error, LocalizedError {
    case multilineValue
    case pairingManagedFleetCredential
    case invalidPrivateEnvironment
    case privateEnvironmentWriteFailed

    public var errorDescription: String? {
        switch self {
        case .multilineValue:
            "Credentials cannot contain line breaks."
        case .pairingManagedFleetCredential:
            "Paired Hub credentials are managed by enrollment. Revoke or forget the pairing before changing them."
        case .invalidPrivateEnvironment:
            "The private credential file or its lock is unsafe."
        case .privateEnvironmentWriteFailed:
            "The private credential file could not be updated."
        }
    }
}
