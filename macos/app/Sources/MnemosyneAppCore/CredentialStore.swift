import Foundation

public enum ManagedCredential: String, CaseIterable, Identifiable, Sendable {
    case omlxAPIKey = "OMLX_API_KEY"
    case omlxAdminSession = "OMLX_ADMIN_SESSION"
    case huggingFaceToken = "HF_TOKEN"
    case inferenceAPIKey = "INFERENCE_API_KEY"
    case adminPassword = "ADMIN_PASSWORD"
    case tokenSidecarPostgresDSN = "TOKEN_SIDECAR_POSTGRES_DSN"

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .omlxAPIKey: "oMLX API key"
        case .omlxAdminSession: "oMLX admin session"
        case .huggingFaceToken: "Hugging Face token"
        case .inferenceAPIKey: "Inference API key"
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
        case .inferenceAPIKey: "Required only when the inference API is exposed beyond this Mac."
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

public struct CredentialStore: Sendable {
    public let environmentURL: URL

    public init(environmentURL: URL) {
        self.environmentURL = environmentURL
    }

    public func status() throws -> CredentialStatus {
        let contents = try read()
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

        let original = try read()
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
        let rendered = output.isEmpty ? "" : output.joined(separator: "\n") + "\n"
        try write(rendered)
    }

    private func read() throws -> String {
        guard FileManager.default.fileExists(atPath: environmentURL.path) else { return "" }
        return try String(contentsOf: environmentURL, encoding: .utf8)
    }

    private func write(_ contents: String) throws {
        let fileManager = FileManager.default
        let directory = environmentURL.deletingLastPathComponent()
        if !fileManager.fileExists(atPath: directory.path) {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        }
        try Data(contents.utf8).write(to: environmentURL, options: .atomic)
        try fileManager.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: environmentURL.path
        )
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

    public var errorDescription: String? {
        "Credentials cannot contain line breaks."
    }
}
