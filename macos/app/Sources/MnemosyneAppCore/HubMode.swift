import Foundation

public struct HubPublishedDeployment: Codable, Equatable, Identifiable, Sendable {
    public let alias: String
    public let deploymentID: String
    public let capabilities: [String]

    public var id: String { alias }

    private enum CodingKeys: String, CodingKey {
        case alias
        case deploymentID = "deployment_id"
        case capabilities
    }
}

public struct HubModeConfiguration: Codable, Equatable, Sendable {
    public static let schemaVersion = 1

    public let schemaVersion: Int
    public let publicOrigin: String
    public let localWorkerNodeID: String
    public let managedTailscaleServe: Bool
    public let includesLocalWorker: Bool
    public let publishedDeployments: [HubPublishedDeployment]

    public init(
        schemaVersion: Int = HubModeConfiguration.schemaVersion,
        publicOrigin: String,
        localWorkerNodeID: String,
        managedTailscaleServe: Bool,
        includesLocalWorker: Bool = true,
        publishedDeployments: [HubPublishedDeployment]
    ) {
        self.schemaVersion = schemaVersion
        self.publicOrigin = publicOrigin
        self.localWorkerNodeID = localWorkerNodeID
        self.managedTailscaleServe = managedTailscaleServe
        self.includesLocalWorker = includesLocalWorker
        self.publishedDeployments = publishedDeployments
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case publicOrigin = "public_origin"
        case localWorkerNodeID = "local_worker_node_id"
        case managedTailscaleServe = "managed_tailscale_serve"
        case includesLocalWorker = "includes_local_worker"
        case publishedDeployments = "published_deployments"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        publicOrigin = try values.decode(String.self, forKey: .publicOrigin)
        localWorkerNodeID = try values.decode(String.self, forKey: .localWorkerNodeID)
        managedTailscaleServe = try values.decode(
            Bool.self,
            forKey: .managedTailscaleServe
        )
        includesLocalWorker = try values.decodeIfPresent(
            Bool.self,
            forKey: .includesLocalWorker
        ) ?? true
        publishedDeployments = try values.decode(
            [HubPublishedDeployment].self,
            forKey: .publishedDeployments
        )
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(schemaVersion, forKey: .schemaVersion)
        try values.encode(publicOrigin, forKey: .publicOrigin)
        try values.encode(localWorkerNodeID, forKey: .localWorkerNodeID)
        try values.encode(managedTailscaleServe, forKey: .managedTailscaleServe)
        try values.encode(includesLocalWorker, forKey: .includesLocalWorker)
        try values.encode(publishedDeployments, forKey: .publishedDeployments)
    }
}

/// Secret material used only while preparing the local worker and Hub process.
///
/// This type intentionally has no diagnostic or string representation. Callers
/// must never place an instance in logs, errors, preferences, or UI state.
public struct HubModeSecrets: Sendable {
    public let clientKey: String
    public let adminKey: String
    public let pairingMasterKey: String
    public let localWorkerSnapshotKey: String
    public let localWorkerDispatchKey: String
}

public enum HubModeError: Error, Equatable, LocalizedError {
    case unsafeStatePath
    case invalidPrivateEnvironment
    case privateStateWriteFailed
    case invalidPublicOrigin
    case invalidWorkerIdentity
    case noEligibleModels
    case localWorkerUnavailable
    case localWorkerRejected
    case localWorkerResponseInvalid
    case tailscaleUnavailable
    case tailscaleNotConnected
    case tailscaleCommandFailed
    case tailscaleCommandTimedOut

    public var errorDescription: String? {
        switch self {
        case .unsafeStatePath:
            "Hub Mode found an unsafe or symlinked private-state path."
        case .invalidPrivateEnvironment:
            "The preserved Hub credential file is incomplete or invalid."
        case .privateStateWriteFailed:
            "Hub Mode could not update its private configuration safely."
        case .invalidPublicOrigin:
            "Enter one HTTPS origin without a path, query, or credentials."
        case .invalidWorkerIdentity:
            "The local worker did not report a valid stable node identity."
        case .noEligibleModels:
            "Install or import at least one Fleet-eligible model before including this Mac as a Hub worker."
        case .localWorkerUnavailable:
            "The local inference worker did not become ready after its credential update."
        case .localWorkerRejected:
            "The local inference worker rejected the Hub snapshot credential."
        case .localWorkerResponseInvalid:
            "The local inference worker returned an invalid Fleet snapshot."
        case .tailscaleUnavailable:
            "Tailscale CLI integration is not installed on this Mac."
        case .tailscaleNotConnected:
            "Tailscale is not connected or did not report a MagicDNS name."
        case .tailscaleCommandFailed:
            "Tailscale could not publish the local Hub through HTTPS."
        case .tailscaleCommandTimedOut:
            "Tailscale did not finish configuring HTTPS in time."
        }
    }
}

public struct HubConfigurationStore: Sendable {
    public static let clientKeyName = "MNEMOSYNE_FLEET_CLIENT_KEY"
    public static let adminKeyName = "MNEMOSYNE_FLEET_ADMIN_KEY"
    public static let pairingMasterKeyName =
        "MNEMOSYNE_FLEET_PAIRING_MASTER_KEY"
    public static let localSnapshotKeyName = "MNEMOSYNE_NYX_FLEET_KEY"
    public static let localDispatchKeyName = "MNEMOSYNE_NYX_INFERENCE_KEY"

    public let rootURL: URL
    public let configurationURL: URL
    public let environmentURL: URL
    public let metadataURL: URL

    public init(rootURL: URL) {
        self.rootURL = rootURL.standardizedFileURL
        configurationURL = self.rootURL.appending(path: "config.toml")
        environmentURL = self.rootURL.appending(path: ".env")
        metadataURL = self.rootURL.appending(path: "hub-mode.json")
    }

    public init(nativeEnvironmentURL: URL) {
        self.init(
            rootURL: nativeEnvironmentURL.deletingLastPathComponent()
                .appending(path: "hub", directoryHint: .isDirectory)
        )
    }

    public func loadConfiguration() throws -> HubModeConfiguration? {
        guard FileManager.default.fileExists(atPath: metadataURL.path) else {
            return nil
        }
        try requireRegularFile(metadataURL)
        let data = try Data(contentsOf: metadataURL, options: [.mappedIfSafe])
        let value = try JSONDecoder().decode(HubModeConfiguration.self, from: data)
        guard value.schemaVersion == HubModeConfiguration.schemaVersion else {
            throw HubModeError.privateStateWriteFailed
        }
        return value
    }

    public func prepareSecrets(
        nativeCredentialStore: CredentialStore,
        provisionLocalWorker: Bool = true
    ) throws -> HubModeSecrets {
        try prepareRoot()
        let secrets: HubModeSecrets
        if FileManager.default.fileExists(atPath: environmentURL.path) {
            secrets = try readSecrets()
        } else {
            secrets = HubModeSecrets(
                clientKey: randomHex(bytes: 32),
                adminKey: randomHex(bytes: 32),
                pairingMasterKey: randomBase64URL(bytes: 32),
                localWorkerSnapshotKey: randomHex(bytes: 32),
                localWorkerDispatchKey: randomHex(bytes: 32)
            )
            try writePrivate(
                environmentText(for: secrets).data(using: .utf8)!,
                to: environmentURL
            )
        }

        if provisionLocalWorker {
            try nativeCredentialStore.apply(
                replacements: [
                    .fleetAPIKey: secrets.localWorkerSnapshotKey,
                    .fleetInferenceAPIKey: secrets.localWorkerDispatchKey,
                ],
                clearing: []
            )
        }
        return secrets
    }

    public func saveConfiguration(
        publicOrigin: String,
        localWorkerNodeID: String,
        managedTailscaleServe: Bool,
        includesLocalWorker: Bool = true,
        deployments: [HubPublishedDeployment]
    ) throws -> HubModeConfiguration {
        try prepareRoot()
        let origin = try Self.normalizedHTTPSOrigin(publicOrigin)
        var nodeID = localWorkerNodeID.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        if includesLocalWorker {
            guard
                !nodeID.isEmpty,
                nodeID.utf8.count <= 128,
                !nodeID.unicodeScalars.contains(where: { $0.value < 0x20 })
            else {
                throw HubModeError.invalidWorkerIdentity
            }
        } else {
            nodeID = ""
        }

        let models = try normalizedDeployments(
            deployments,
            requireNonempty: includesLocalWorker
        )
        let value = HubModeConfiguration(
            publicOrigin: origin,
            localWorkerNodeID: nodeID,
            managedTailscaleServe: managedTailscaleServe,
            includesLocalWorker: includesLocalWorker,
            publishedDeployments: models
        )
        let config = renderTOML(value)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try writePrivate(Data(config.utf8), to: configurationURL)
        try writePrivate(encoder.encode(value), to: metadataURL)
        return value
    }

    public func adminKey() throws -> String {
        try readSecrets().adminKey
    }

    public func clientKey() throws -> String {
        try readSecrets().clientKey
    }

    public static func normalizedHTTPSOrigin(_ raw: String) throws -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            trimmed.utf8.count <= 2_048,
            var components = URLComponents(string: trimmed),
            components.scheme?.lowercased() == "https",
            let host = components.host,
            !host.isEmpty,
            components.user == nil,
            components.password == nil,
            components.query == nil,
            components.fragment == nil,
            components.path.isEmpty || components.path == "/"
        else {
            throw HubModeError.invalidPublicOrigin
        }
        components.scheme = "https"
        components.path = ""
        guard let normalized = components.url?.absoluteString else {
            throw HubModeError.invalidPublicOrigin
        }
        return normalized.hasSuffix("/")
            ? String(normalized.dropLast()) : normalized
    }

    private func normalizedDeployments(
        _ deployments: [HubPublishedDeployment],
        requireNonempty: Bool
    ) throws -> [HubPublishedDeployment] {
        let supported = Set([
            "chat/completions", "completions", "responses", "messages",
            "embeddings", "rerank", "images/generations",
        ])
        var aliases = Set<String>()
        let values = deployments.compactMap { deployment -> HubPublishedDeployment? in
            let alias = deployment.alias.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            guard
                !alias.isEmpty,
                alias.utf8.count <= 256,
                aliases.insert(alias).inserted,
                deployment.deploymentID.range(
                    of: #"^sha256:[0-9a-f]{64}$"#,
                    options: .regularExpression
                ) != nil
            else { return nil }
            let capabilities = Array(Set(deployment.capabilities)).sorted()
            guard !capabilities.isEmpty,
                  Set(capabilities).isSubset(of: supported)
            else { return nil }
            return HubPublishedDeployment(
                alias: alias,
                deploymentID: deployment.deploymentID,
                capabilities: capabilities
            )
        }.sorted { $0.alias < $1.alias }
        if requireNonempty && values.isEmpty {
            throw HubModeError.noEligibleModels
        }
        return values
    }

    private func renderTOML(_ value: HubModeConfiguration) -> String {
        let state = rootURL.appending(path: "state", directoryHint: .isDirectory)
        var lines = [
            "# Managed by Unified Inference Hub Mode. Secrets remain in .env.",
            "[server]",
            "host = \"127.0.0.1\"",
            "port = 17400",
            "api_key_env = \"\(Self.clientKeyName)\"",
            "admin_api_key_env = \"\(Self.adminKeyName)\"",
            "database_path = \(tomlString(state.appending(path: "fleet.db").path))",
            "request_timeout_seconds = 300",
            "max_body_bytes = 16777216",
            "route_history_limit = 10000",
            "poll_interval_seconds = 2",
            "snapshot_ttl_seconds = 10",
            "",
            "[batch]",
            "enabled = true",
            "max_active_jobs = 32",
            "max_requests_per_job = 256",
            "max_concurrency = 4",
            "max_result_bytes_per_item = 16777216",
            "max_retained_result_bytes = 268435456",
            "retention_seconds = 3600",
            "",
            "[pairing]",
            "enabled = true",
            "public_origin = \(tomlString(value.publicOrigin))",
            "master_key_env = \"\(Self.pairingMasterKeyName)\"",
            "metadata_database_path = \(tomlString(state.appending(path: "private/pairing-metadata.db").path))",
            "secret_database_path = \(tomlString(state.appending(path: "private/pairing-secrets.db").path))",
            "inventory_database_path = \(tomlString(state.appending(path: "mac-inventory.db").path))",
            "inventory_ttl_seconds = 60",
            "activation_timeout_seconds = 15",
            "https_cidr_allowlist = [\"100.64.0.0/10\", \"fd7a:115c:a1e0::/48\"]",
            "tailscale_cidr_allowlist = [\"100.64.0.0/10\", \"fd7a:115c:a1e0::/48\"]",
            "trusted_lan_http_cidr_allowlist = []",
            "allowed_node_ports = [1240, 443]",
            "dns_resolution_timeout_seconds = 5",
            "",
            "[catalog]",
            "enabled = false",
            "",
            "[placement]",
            "remote_installs_enabled = false",
            "recommendation_valid_seconds = 60",
        ]
        if value.includesLocalWorker {
            lines.append(contentsOf: [
                "",
                "[[nodes]]",
                "node_id = \(tomlString(value.localWorkerNodeID))",
                "url = \"http://127.0.0.1:1240\"",
                "fleet_token_env = \"\(Self.localSnapshotKeyName)\"",
                "inference_token_env = \"\(Self.localDispatchKeyName)\"",
                "service_class = \"overflow\"",
                "routing_weight = 1",
            ])
        }
        lines.append(contentsOf: ["", "[ledger]", ""])
        return lines.joined(separator: "\n")
    }

    private func environmentText(for secrets: HubModeSecrets) -> String {
        [
            "# Managed by Unified Inference Hub Mode. Do not share this file.",
            "\(Self.clientKeyName)=\(secrets.clientKey)",
            "\(Self.adminKeyName)=\(secrets.adminKey)",
            "\(Self.pairingMasterKeyName)=\(secrets.pairingMasterKey)",
            "\(Self.localSnapshotKeyName)=\(secrets.localWorkerSnapshotKey)",
            "\(Self.localDispatchKeyName)=\(secrets.localWorkerDispatchKey)",
            "",
        ].joined(separator: "\n")
    }

    private func readSecrets() throws -> HubModeSecrets {
        try requireRegularFile(environmentURL)
        guard
            let raw = try? String(contentsOf: environmentURL, encoding: .utf8),
            raw.utf8.count <= 64 * 1_024
        else { throw HubModeError.invalidPrivateEnvironment }
        var values: [String: String] = [:]
        for line in raw.split(separator: "\n", omittingEmptySubsequences: false) {
            let text = String(line).trimmingCharacters(in: .whitespaces)
            if text.isEmpty || text.hasPrefix("#") { continue }
            let parts = text.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else {
                throw HubModeError.invalidPrivateEnvironment
            }
            let key = String(parts[0])
            let value = String(parts[1])
            guard values[key] == nil, !value.isEmpty,
                  !value.contains(where: { $0.isNewline })
            else { throw HubModeError.invalidPrivateEnvironment }
            values[key] = value
        }
        let names = [
            Self.clientKeyName, Self.adminKeyName, Self.pairingMasterKeyName,
            Self.localSnapshotKeyName, Self.localDispatchKeyName,
        ]
        guard names.allSatisfy({ values[$0]?.count ?? 0 >= 43 }) else {
            throw HubModeError.invalidPrivateEnvironment
        }
        let distinct = Set(names.compactMap { values[$0] })
        guard distinct.count == names.count else {
            throw HubModeError.invalidPrivateEnvironment
        }
        return HubModeSecrets(
            clientKey: values[Self.clientKeyName]!,
            adminKey: values[Self.adminKeyName]!,
            pairingMasterKey: values[Self.pairingMasterKeyName]!,
            localWorkerSnapshotKey: values[Self.localSnapshotKeyName]!,
            localWorkerDispatchKey: values[Self.localDispatchKeyName]!
        )
    }

    private func prepareRoot() throws {
        let manager = FileManager.default
        if manager.fileExists(atPath: rootURL.path) {
            try requireDirectory(rootURL)
        } else {
            do {
                try manager.createDirectory(
                    at: rootURL,
                    withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                throw HubModeError.privateStateWriteFailed
            }
        }
        try manager.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: rootURL.path
        )
        for directory in [
            rootURL.appending(path: "state", directoryHint: .isDirectory),
            rootURL.appending(path: "state/private", directoryHint: .isDirectory),
        ] {
            if manager.fileExists(atPath: directory.path) {
                try requireDirectory(directory)
            } else {
                try manager.createDirectory(
                    at: directory,
                    withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            }
            try manager.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: directory.path
            )
        }
    }

    private func writePrivate(_ data: Data, to destination: URL) throws {
        if FileManager.default.fileExists(atPath: destination.path) {
            try requireRegularFile(destination)
        }
        do {
            try data.write(to: destination, options: [.atomic])
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: destination.path
            )
        } catch let error as HubModeError {
            throw error
        } catch {
            throw HubModeError.privateStateWriteFailed
        }
    }

    private func requireDirectory(_ url: URL) throws {
        let values = try url.resourceValues(forKeys: [
            .isDirectoryKey, .isSymbolicLinkKey,
        ])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            throw HubModeError.unsafeStatePath
        }
    }

    private func requireRegularFile(_ url: URL) throws {
        let values = try url.resourceValues(forKeys: [
            .isRegularFileKey, .isSymbolicLinkKey,
        ])
        guard values.isRegularFile == true, values.isSymbolicLink != true else {
            throw HubModeError.unsafeStatePath
        }
    }

    private func randomHex(bytes: Int) -> String {
        var generator = SystemRandomNumberGenerator()
        return (0 ..< bytes).map { _ in
            String(format: "%02x", UInt8.random(in: 0 ... 255, using: &generator))
        }.joined()
    }

    private func randomBase64URL(bytes: Int) -> String {
        var generator = SystemRandomNumberGenerator()
        let data = Data((0 ..< bytes).map { _ in
            UInt8.random(in: 0 ... 255, using: &generator)
        })
        return data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private func tomlString(_ value: String) -> String {
        let escaped = value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\n", with: "\\n")
            .replacingOccurrences(of: "\r", with: "\\r")
            .replacingOccurrences(of: "\t", with: "\\t")
        return "\"\(escaped)\""
    }
}

public struct HubLocalSnapshotClient: Sendable {
    public init() {}

    public func waitForEligibleSnapshot(
        snapshotKey: String,
        timeoutSeconds: Double = 30
    ) async throws -> (nodeID: String, deployments: [HubPublishedDeployment]) {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        var lastError: Error = HubModeError.localWorkerUnavailable
        while Date() < deadline {
            do {
                return try await snapshot(snapshotKey: snapshotKey)
            } catch {
                if let hubError = error as? HubModeError,
                   hubError == .noEligibleModels
                {
                    throw hubError
                }
                lastError = error
                try? await Task.sleep(for: .milliseconds(500))
            }
        }
        if let hubError = lastError as? HubModeError {
            throw hubError
        }
        throw HubModeError.localWorkerUnavailable
    }

    private func snapshot(
        snapshotKey: String
    ) async throws -> (nodeID: String, deployments: [HubPublishedDeployment]) {
        var request = URLRequest(
            url: URL(string: "http://127.0.0.1:1240/fleet/v1/snapshot")!
        )
        request.timeoutInterval = 3
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.setValue("Bearer \(snapshotKey)", forHTTPHeaderField: "Authorization")
        request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        let (data, response) = try await URLSession(configuration: configuration)
            .data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw HubModeError.localWorkerResponseInvalid
        }
        guard http.statusCode == 200 else {
            throw http.statusCode == 401 || http.statusCode == 403
                ? HubModeError.localWorkerRejected
                : HubModeError.localWorkerUnavailable
        }
        guard data.count <= 8 * 1_024 * 1_024 else {
            throw HubModeError.localWorkerResponseInvalid
        }
        let decoded: LocalSnapshot
        do {
            decoded = try JSONDecoder().decode(LocalSnapshot.self, from: data)
        } catch {
            throw HubModeError.localWorkerResponseInvalid
        }
        let nodeID = decoded.node.nodeID
        guard !nodeID.isEmpty, nodeID.utf8.count <= 128 else {
            throw HubModeError.invalidWorkerIdentity
        }
        let deployments: [HubPublishedDeployment] = decoded.deployments.compactMap {
            value -> HubPublishedDeployment? in
            guard value.fleetEligible,
                  value.identityConfidence == "authoritative"
            else { return nil }
            return HubPublishedDeployment(
                alias: value.alias,
                deploymentID: value.deploymentID,
                capabilities: value.identity.capabilities
            )
        }
        guard !deployments.isEmpty else { throw HubModeError.noEligibleModels }
        return (nodeID, deployments)
    }
}

private struct LocalSnapshot: Decodable {
    struct Node: Decodable {
        let nodeID: String
        private enum CodingKeys: String, CodingKey { case nodeID = "node_id" }
    }

    struct Deployment: Decodable {
        struct Identity: Decodable { let capabilities: [String] }
        let alias: String
        let deploymentID: String
        let identity: Identity
        let identityConfidence: String
        let fleetEligible: Bool

        private enum CodingKeys: String, CodingKey {
            case alias, identity
            case deploymentID = "deployment_id"
            case identityConfidence = "identity_confidence"
            case fleetEligible = "fleet_eligible"
        }
    }

    let node: Node
    let deployments: [Deployment]
}

public struct HubTailscaleManager: Sendable {
    public struct Discovery: Equatable, Sendable {
        public let executableURL: URL
        public let publicOrigin: String
        public let dnsName: String

        public func inferenceOrigin(port: Int) throws -> String {
            guard (1 ... 65_535).contains(port) else {
                throw HubModeError.invalidPublicOrigin
            }
            return "http://\(dnsName):\(port)"
        }
    }

    public init() {}

    public func discover() async throws -> Discovery {
        let executable = try executableURL()
        let output = try await run(executable, arguments: ["status", "--json"])
        guard
            let object = try? JSONSerialization.jsonObject(with: output) as? [String: Any],
            let own = object["Self"] as? [String: Any],
            let rawDNS = own["DNSName"] as? String
        else { throw HubModeError.tailscaleNotConnected }
        let dns = rawDNS.hasSuffix(".") ? String(rawDNS.dropLast()) : rawDNS
        guard !dns.isEmpty else { throw HubModeError.tailscaleNotConnected }
        let origin = try HubConfigurationStore.normalizedHTTPSOrigin(
            "https://\(dns)"
        )
        return Discovery(
            executableURL: executable,
            publicOrigin: origin,
            dnsName: dns
        )
    }

    public func enableServe(using discovery: Discovery) async throws {
        _ = try await run(
            discovery.executableURL,
            arguments: [
                "serve", "--bg", "--yes", "--https=443",
                "http://127.0.0.1:17400",
            ],
            timeoutSeconds: 30
        )
    }

    public func disableServeIfAvailable() async {
        guard let executable = try? executableURL() else { return }
        _ = try? await run(
            executable,
            arguments: ["serve", "--https=443", "off"],
            timeoutSeconds: 15
        )
    }

    private func executableURL() throws -> URL {
        let candidates = [
            "/usr/local/bin/tailscale",
            "/opt/homebrew/bin/tailscale",
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        ]
        guard let path = candidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) else { throw HubModeError.tailscaleUnavailable }
        return URL(fileURLWithPath: path)
    }

    private func run(
        _ executable: URL,
        arguments: [String],
        timeoutSeconds: Double = 15
    ) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                let output = Pipe()
                process.executableURL = executable
                process.arguments = arguments
                process.standardOutput = output
                process.standardError = output
                var environment = ProcessInfo.processInfo.environment
                environment["TAILSCALE_BE_CLI"] = "1"
                process.environment = environment
                do {
                    try process.run()
                } catch {
                    continuation.resume(
                        throwing: HubModeError.tailscaleCommandFailed
                    )
                    return
                }
                let finished = DispatchSemaphore(value: 0)
                process.terminationHandler = { _ in finished.signal() }
                if finished.wait(timeout: .now() + timeoutSeconds) == .timedOut {
                    process.terminate()
                    _ = finished.wait(timeout: .now() + 2)
                    continuation.resume(
                        throwing: HubModeError.tailscaleCommandTimedOut
                    )
                    return
                }
                let data = output.fileHandleForReading.readDataToEndOfFile()
                guard process.terminationStatus == 0, data.count <= 1_024 * 1_024
                else {
                    continuation.resume(
                        throwing: HubModeError.tailscaleCommandFailed
                    )
                    return
                }
                continuation.resume(returning: data)
            }
        }
    }
}
