import Foundation

/// The menu app's view of the native service connection settings.
///
/// Mnemosyne Core owns the full YAML schema. The menu only needs three values
/// from the `server` mapping, so keeping this parser deliberately narrow avoids
/// adding a second YAML dependency and prevents the UI from becoming another
/// configuration authority.
public struct ControlConnectionConfiguration: Equatable, Sendable {
    public static let defaultBaseURL = URL(string: "http://127.0.0.1:17321")!
    public static let defaultPasswordEnvironmentKey = "ADMIN_PASSWORD"

    public let baseURL: URL
    public let passwordEnvironmentKey: String
    public let adminPassword: String?
    public let configURL: URL
    public let environmentURL: URL

    public init(
        baseURL: URL,
        passwordEnvironmentKey: String,
        adminPassword: String?,
        configURL: URL,
        environmentURL: URL
    ) {
        self.baseURL = baseURL
        self.passwordEnvironmentKey = passwordEnvironmentKey
        self.adminPassword = adminPassword
        self.configURL = configURL
        self.environmentURL = environmentURL
    }

    /// Resolve the Finder-safe default paths and any development overrides.
    public static func load() -> ControlConnectionConfiguration {
        let applicationSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0]
        return load(
            processEnvironment: ProcessInfo.processInfo.environment,
            applicationSupportDirectory: applicationSupport
        )
    }

    /// Injectable entry point used by tests and command-line development.
    public static func load(
        processEnvironment: [String: String],
        applicationSupportDirectory: URL
    ) -> ControlConnectionConfiguration {
        let supportRoot = applicationSupportDirectory
            .appending(path: "Mnemosyne", directoryHint: .isDirectory)
        let configURL = fileURL(
            processEnvironment["MNEMOSYNE_MACOS_CONFIG_PATH"],
            defaultingTo: supportRoot.appending(path: "config.yaml")
        )
        let environmentURL = fileURL(
            processEnvironment["MNEMOSYNE_MACOS_ENV_PATH"],
            defaultingTo: supportRoot.appending(path: ".env")
        )

        let configContents = try? String(contentsOf: configURL, encoding: .utf8)
        let server = parseServerSettings(configContents ?? "")
        let configuredBaseURL = controlBaseURL(
            bind: server["control_bind"],
            port: server["control_port"]
        )
        let overrideBaseURL = processEnvironment["MNEMOSYNE_CONTROL_URL"]
            .flatMap(validControlOrigin)
        let passwordKey = nonemptyScalar(server["control_password_env"])
            ?? defaultPasswordEnvironmentKey

        let fileEnvironment = (try? String(contentsOf: environmentURL, encoding: .utf8))
            .map(parseEnvironment) ?? [:]
        // Python's load_env uses os.environ.setdefault, so a launch-time value
        // wins over the private file and the first duplicate in that file wins.
        let password = normalizedPassword(
            processEnvironment[passwordKey] ?? fileEnvironment[passwordKey]
        )

        return ControlConnectionConfiguration(
            baseURL: overrideBaseURL ?? configuredBaseURL,
            passwordEnvironmentKey: passwordKey,
            adminPassword: password,
            configURL: configURL,
            environmentURL: environmentURL
        )
    }

    private static func fileURL(_ override: String?, defaultingTo fallback: URL) -> URL {
        guard let override = nonemptyScalar(override) else { return fallback }
        let expanded = (override as NSString).expandingTildeInPath
        return URL(fileURLWithPath: expanded).standardizedFileURL
    }

    private static func controlBaseURL(bind: String?, port: String?) -> URL {
        let parsedPort = port.flatMap(Int.init).flatMap {
            (1_024 ... 65_535).contains($0) ? $0 : nil
        } ?? defaultBaseURL.port!
        var host = nonemptyScalar(bind) ?? defaultBaseURL.host!

        // A bind wildcard is not a connectable address. Keep traffic on this
        // Mac by translating it to the corresponding loopback family.
        switch host.lowercased() {
        case "0.0.0.0", "*":
            host = "127.0.0.1"
        case "::", "[::]":
            host = "::1"
        default:
            break
        }

        var components = URLComponents()
        components.scheme = "http"
        if host.contains(":") {
            let unbracketed = host.trimmingCharacters(
                in: CharacterSet(charactersIn: "[]")
            )
            components.percentEncodedHost = "[\(unbracketed)]"
        } else {
            components.host = host
        }
        components.port = parsedPort
        guard let url = components.url, url.host != nil else {
            return URL(string: "http://127.0.0.1:\(parsedPort)")!
        }
        return url
    }

    /// Accept only an origin. This keeps endpoint appending predictable and
    /// prevents credentials embedded in a development URL from being reused.
    private static func validControlOrigin(_ raw: String) -> URL? {
        guard let value = nonemptyScalar(raw),
              let components = URLComponents(string: value),
              ["http", "https"].contains(components.scheme?.lowercased() ?? ""),
              components.host != nil,
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil,
              components.path.isEmpty || components.path == "/"
        else {
            return nil
        }
        var origin = components
        origin.path = ""
        return origin.url
    }

    /// Parse direct scalar children of the block-style `server:` mapping.
    private static func parseServerSettings(_ contents: String) -> [String: String] {
        let relevantKeys = Set(["control_bind", "control_port", "control_password_env"])
        var result: [String: String] = [:]
        var serverIndent: Int?
        var childIndent: Int?

        for rawLine in contents.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
            let uncommented = stripComment(String(rawLine))
            let trimmed = uncommented.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty else { continue }
            let indent = uncommented.prefix { $0 == " " }.count

            if serverIndent == nil {
                if trimmed == "server:" {
                    serverIndent = indent
                }
                continue
            }
            guard let rootIndent = serverIndent else { continue }
            if indent <= rootIndent {
                break
            }
            if childIndent == nil {
                childIndent = indent
            }
            guard indent == childIndent, let separator = trimmed.firstIndex(of: ":") else {
                continue
            }
            let key = trimmed[..<separator].trimmingCharacters(in: .whitespaces)
            guard relevantKeys.contains(key) else { continue }
            let scalar = trimmed[trimmed.index(after: separator)...]
            if let value = nonemptyScalar(String(scalar)) {
                result[key] = unquote(value)
            }
        }
        return result
    }

    /// Mirror the service's small dotenv semantics: trim each line, ignore
    /// comments, split on the first `=`, trim quotes, and keep the first value.
    private static func parseEnvironment(_ contents: String) -> [String: String] {
        var result: [String: String] = [:]
        for rawLine in contents.split(whereSeparator: \.isNewline) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, !line.hasPrefix("#"), let separator = line.firstIndex(of: "=") else {
                continue
            }
            let key = line[..<separator].trimmingCharacters(in: .whitespaces)
            guard !key.isEmpty, result[key] == nil else { continue }
            let rawValue = line[line.index(after: separator)...]
                .trimmingCharacters(in: .whitespaces)
            result[key] = rawValue.trimmingCharacters(
                in: CharacterSet(charactersIn: "\"'")
            )
        }
        return result
    }

    private static func stripComment(_ line: String) -> String {
        var quote: Character?
        var escaped = false
        for index in line.indices {
            let character = line[index]
            if escaped {
                escaped = false
                continue
            }
            if character == "\\", quote == "\"" {
                escaped = true
                continue
            }
            if character == "\"" || character == "'" {
                if quote == nil {
                    quote = character
                } else if quote == character {
                    quote = nil
                }
                continue
            }
            if character == "#", quote == nil {
                return String(line[..<index])
            }
        }
        return line
    }

    private static func unquote(_ value: String) -> String {
        guard value.count >= 2, let first = value.first, let last = value.last,
              (first == "\"" && last == "\"") || (first == "'" && last == "'")
        else {
            return value
        }
        return String(value.dropFirst().dropLast())
    }

    private static func nonemptyScalar(_ value: String?) -> String? {
        guard let normalized = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !normalized.isEmpty
        else {
            return nil
        }
        return normalized
    }

    private static func normalizedPassword(_ value: String?) -> String? {
        nonemptyScalar(value)
    }
}
