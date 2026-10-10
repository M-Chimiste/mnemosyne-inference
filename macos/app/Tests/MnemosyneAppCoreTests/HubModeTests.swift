import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Hub Mode creates separate persistent credentials and enrolls only the local worker slots")
func hubModeCreatesSeparatedCredentials() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(
        at: temporary,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    let nativeEnvironment = temporary.appending(path: "native.env")
    try Data("TOKEN_LEDGER_DSN=preserved\n".utf8).write(to: nativeEnvironment)
    let nativeStore = CredentialStore(environmentURL: nativeEnvironment)
    let hubStore = HubConfigurationStore(
        rootURL: temporary.appending(path: "hub", directoryHint: .isDirectory)
    )

    let first = try hubStore.prepareSecrets(nativeCredentialStore: nativeStore)
    let second = try hubStore.prepareSecrets(nativeCredentialStore: nativeStore)

    #expect(first.clientKey == second.clientKey)
    #expect(first.adminKey == second.adminKey)
    #expect(first.pairingMasterKey == second.pairingMasterKey)
    #expect(first.localWorkerSnapshotKey == second.localWorkerSnapshotKey)
    #expect(first.localWorkerDispatchKey == second.localWorkerDispatchKey)
    #expect(Set([
        first.clientKey,
        first.adminKey,
        first.pairingMasterKey,
        first.localWorkerSnapshotKey,
        first.localWorkerDispatchKey,
    ]).count == 5)
    #expect(first.clientKey?.count == 64)
    #expect(first.pairingMasterKey.count == 43)

    let nativeText = try String(contentsOf: nativeEnvironment, encoding: .utf8)
    #expect(nativeText.contains("TOKEN_LEDGER_DSN=preserved"))
    #expect(nativeText.contains("FLEET_API_KEY=\(first.localWorkerSnapshotKey)"))
    #expect(nativeText.contains("FLEET_INFERENCE_API_KEY=\(first.localWorkerDispatchKey)"))
    #expect(!nativeText.contains(first.clientKey!))
    #expect(!nativeText.contains(first.adminKey))
    #expect(!nativeText.contains(first.pairingMasterKey))

    let attributes = try FileManager.default.attributesOfItem(
        atPath: hubStore.environmentURL.path
    )
    #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
}

@Test("Hub configuration enrolls an optional local worker while the Fleet catalog owns routes")
func hubModeRendersClosedFleetConfiguration() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let nativeEnvironment = temporary.appending(path: "native.env")
    let nativeStore = CredentialStore(environmentURL: nativeEnvironment)
    let hubStore = HubConfigurationStore(
        rootURL: temporary.appending(path: "hub", directoryHint: .isDirectory)
    )
    let secrets = try hubStore.prepareSecrets(
        nativeCredentialStore: nativeStore,
        requireClientKey: false
    )
    let deployment = HubPublishedDeployment(
        alias: "glm-5.3-flash",
        deploymentID: "sha256:" + String(repeating: "a", count: 64),
        capabilities: ["responses", "chat/completions", "responses"]
    )

    let saved = try hubStore.saveConfiguration(
        publicOrigin: " HTTPS://nyx.example.ts.net/ ",
        localWorkerNodeID: "nyx-worker",
        managedTailscaleServe: true,
        deployments: [deployment]
    )

    #expect(saved.publicOrigin == "https://nyx.example.ts.net")
    #expect(saved.localWorkerNodeID == "nyx-worker")
    #expect(saved.includesLocalWorker)
    #expect(saved.requestTimeoutSeconds == 300)
    #expect(saved.publishedDeployments.first?.capabilities == [
        "chat/completions", "responses",
    ])
    #expect(try hubStore.loadConfiguration() == saved)

    let config = try String(contentsOf: hubStore.configurationURL, encoding: .utf8)
    #expect(config.contains("host = \"127.0.0.1\""))
    #expect(config.contains("port = 17400"))
    #expect(config.contains(
        "inference_auth_mode = \"tailscale_serve_or_bearer\""
    ))
    #expect(config.contains("url = \"http://127.0.0.1:1240\""))
    #expect(config.contains("service_class = \"overflow\""))
    #expect(config.contains("enabled = true"))
    #expect(!config.contains("[[models]]"))
    #expect(!config.contains("deployment_id = \"\(deployment.deploymentID)\""))
    #expect(config.contains("api_key_env = \"MNEMOSYNE_FLEET_CLIENT_KEY\""))
    #expect(secrets.clientKey == nil)
    #expect(try hubStore.clientKey() == nil)
    #expect(!config.contains(secrets.adminKey))
    #expect(!config.contains(secrets.pairingMasterKey))
    #expect(!config.contains(secrets.localWorkerSnapshotKey))
    #expect(!config.contains(secrets.localWorkerDispatchKey))
}

@Test("Existing HTTPS Hub configuration retains bearer-only inference")
func existingHTTPSHubRetainsBearerAuthentication() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let hubStore = HubConfigurationStore(
        rootURL: temporary.appending(path: "hub", directoryHint: .isDirectory)
    )
    _ = try hubStore.prepareSecrets(
        nativeCredentialStore: CredentialStore(
            environmentURL: temporary.appending(path: "native.env")
        ),
        provisionLocalWorker: false
    )
    _ = try hubStore.saveConfiguration(
        publicOrigin: "https://hub.example.internal",
        localWorkerNodeID: "",
        managedTailscaleServe: false,
        includesLocalWorker: false,
        deployments: []
    )
    let config = try String(
        contentsOf: hubStore.configurationURL,
        encoding: .utf8
    )
    #expect(config.contains("inference_auth_mode = \"bearer\""))
}

@Test("Preserved managed Hub configuration upgrades without replacing secrets")
func preservedManagedHubConfigurationUpgradesInPlace() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let nativeEnvironment = temporary.appending(path: "native.env")
    let hubStore = HubConfigurationStore(
        rootURL: temporary.appending(path: "hub", directoryHint: .isDirectory)
    )
    _ = try hubStore.prepareSecrets(
        nativeCredentialStore: CredentialStore(environmentURL: nativeEnvironment),
        provisionLocalWorker: false,
        requireClientKey: false
    )
    _ = try hubStore.saveConfiguration(
        publicOrigin: "https://nyx.example.ts.net",
        localWorkerNodeID: "",
        managedTailscaleServe: true,
        includesLocalWorker: false,
        deployments: []
    )
    let secretsBefore = try Data(contentsOf: hubStore.environmentURL)
    #expect(!String(decoding: secretsBefore, as: UTF8.self).contains(
        HubConfigurationStore.clientKeyName
    ))
    var legacy = try String(
        contentsOf: hubStore.configurationURL,
        encoding: .utf8
    )
    legacy = legacy.replacingOccurrences(
        of: "inference_auth_mode = \"tailscale_serve_or_bearer\"\n",
        with: ""
    )
    try Data(legacy.utf8).write(
        to: hubStore.configurationURL,
        options: [.atomic]
    )

    #expect(try hubStore.refreshManagedConfiguration())
    #expect(!(try hubStore.refreshManagedConfiguration()))
    let upgraded = try String(
        contentsOf: hubStore.configurationURL,
        encoding: .utf8
    )
    #expect(upgraded.contains(
        "inference_auth_mode = \"tailscale_serve_or_bearer\""
    ))
    #expect(try Data(contentsOf: hubStore.environmentURL) == secretsBefore)
}

@Test("Managed Tailscale Hub can generate and remove an optional client key")
func managedHubOptionalClientKeyLifecycle() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let hubStore = HubConfigurationStore(
        rootURL: temporary.appending(path: "hub", directoryHint: .isDirectory)
    )
    let nativeStore = CredentialStore(
        environmentURL: temporary.appending(path: "native.env")
    )
    let original = try hubStore.prepareSecrets(
        nativeCredentialStore: nativeStore,
        provisionLocalWorker: false,
        requireClientKey: false
    )
    #expect(original.clientKey == nil)

    let generated = try hubStore.generateClientKey()
    #expect(generated.count == 64)
    #expect(try hubStore.clientKey() == generated)
    let withKey = try Data(contentsOf: hubStore.environmentURL)
    #expect(String(decoding: withKey, as: UTF8.self).contains(
        "\(HubConfigurationStore.clientKeyName)=\(generated)"
    ))

    try hubStore.removeClientKey()
    #expect(try hubStore.clientKey() == nil)
    let withoutKey = try String(
        contentsOf: hubStore.environmentURL,
        encoding: .utf8
    )
    #expect(!withoutKey.contains(HubConfigurationStore.clientKeyName))
    #expect(withoutKey.contains(
        "\(HubConfigurationStore.adminKeyName)=\(original.adminKey)"
    ))
    #expect(withoutKey.contains(
        "\(HubConfigurationStore.pairingMasterKeyName)=\(original.pairingMasterKey)"
    ))
}

@Test("Hub-only configuration does not provision or enroll the local worker")
func hubOnlyModeRendersWithoutLocalInference() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(
        at: temporary,
        withIntermediateDirectories: true
    )
    let nativeEnvironment = temporary.appending(path: "native.env")
    try Data("TOKEN_LEDGER_DSN=preserved\n".utf8).write(to: nativeEnvironment)
    let hubStore = HubConfigurationStore(
        rootURL: temporary.appending(path: "hub", directoryHint: .isDirectory)
    )

    _ = try hubStore.prepareSecrets(
        nativeCredentialStore: CredentialStore(
            environmentURL: nativeEnvironment
        ),
        provisionLocalWorker: false
    )
    let saved = try hubStore.saveConfiguration(
        publicOrigin: "https://nyx.example.ts.net",
        localWorkerNodeID: "nyx",
        managedTailscaleServe: true,
        includesLocalWorker: false,
        deployments: []
    )

    #expect(!saved.includesLocalWorker)
    #expect(saved.localWorkerNodeID.isEmpty)
    #expect(saved.publishedDeployments.isEmpty)
    #expect(try hubStore.loadConfiguration() == saved)
    let nativeText = try String(
        contentsOf: nativeEnvironment,
        encoding: .utf8
    )
    #expect(nativeText == "TOKEN_LEDGER_DSN=preserved\n")
    let config = try String(
        contentsOf: hubStore.configurationURL,
        encoding: .utf8
    )
    #expect(config.contains("[pairing]"))
    #expect(config.contains("enabled = true"))
    #expect(!config.contains("[[nodes]]"))
    #expect(!config.contains("[[models]]"))
}

@Test("Older Hub metadata continues to include its local worker")
func legacyHubMetadataDefaultsToLocalWorker() throws {
    let data = Data(
        """
        {
          "schema_version": 1,
          "public_origin": "https://nyx.example.ts.net",
          "local_worker_node_id": "nyx",
          "managed_tailscale_serve": true,
          "published_deployments": []
        }
        """.utf8
    )
    let decoded = try JSONDecoder().decode(
        HubModeConfiguration.self,
        from: data
    )
    #expect(decoded.includesLocalWorker)
    #expect(decoded.requestTimeoutSeconds == 300)
}

@Test("Hub request timeout survives refresh and reconfiguration without changing private state")
func hubRequestTimeoutPersists() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let nativeEnvironment = temporary.appending(path: "native.env")
    let hubStore = HubConfigurationStore(rootURL: temporary.appending(path: "hub"))
    _ = try hubStore.prepareSecrets(
        nativeCredentialStore: CredentialStore(environmentURL: nativeEnvironment)
    )
    let deployment = HubPublishedDeployment(
        alias: "model",
        deploymentID: "sha256:" + String(repeating: "c", count: 64),
        capabilities: ["chat/completions"]
    )
    let original = try hubStore.saveConfiguration(
        publicOrigin: "https://nyx.example.ts.net",
        localWorkerNodeID: "nyx-worker",
        managedTailscaleServe: true,
        deployments: [deployment]
    )
    let secretsBefore = try Data(contentsOf: hubStore.environmentURL)
    let nativeBefore = try Data(contentsOf: nativeEnvironment)
    let stateFile = hubStore.rootURL.appending(path: "state/preserved.db")
    try Data("preserved pairing and route state".utf8).write(to: stateFile)

    let updated = try hubStore.updateRequestTimeout(900)
    #expect(updated.requestTimeoutSeconds == 900)
    #expect(updated.publicOrigin == original.publicOrigin)
    #expect(updated.localWorkerNodeID == original.localWorkerNodeID)
    #expect(updated.managedTailscaleServe == original.managedTailscaleServe)
    #expect(updated.includesLocalWorker == original.includesLocalWorker)
    #expect(updated.publishedDeployments == original.publishedDeployments)

    // App-launch refresh must use the persisted timeout, even after regenerating TOML.
    try Data("[server]\nrequest_timeout_seconds = 300\n".utf8).write(
        to: hubStore.configurationURL
    )
    let reopened = HubConfigurationStore(rootURL: hubStore.rootURL)
    #expect(try reopened.refreshManagedConfiguration())
    #expect(!(try reopened.refreshManagedConfiguration()))
    #expect(try reopened.loadConfiguration() == updated)
    #expect(try String(contentsOf: reopened.configurationURL, encoding: .utf8)
        .contains("request_timeout_seconds = 900.0"))

    let reconfigured = try reopened.saveConfiguration(
        publicOrigin: original.publicOrigin,
        localWorkerNodeID: "",
        managedTailscaleServe: false,
        includesLocalWorker: false,
        deployments: []
    )
    #expect(reconfigured.requestTimeoutSeconds == 900)
    #expect(try Data(contentsOf: hubStore.environmentURL) == secretsBefore)
    #expect(try Data(contentsOf: nativeEnvironment) == nativeBefore)
    #expect(try String(contentsOf: stateFile, encoding: .utf8)
        == "preserved pairing and route state")
}

@Test("Invalid Hub timeouts leave the saved configuration untouched", arguments: [
    0.0, -1.0, 86_401.0, Double.infinity, -Double.infinity, Double.nan,
])
func hubRequestTimeoutRejectsInvalidValues(_ seconds: Double) throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let store = HubConfigurationStore(rootURL: temporary.appending(path: "hub"))
    _ = try store.saveConfiguration(
        publicOrigin: "https://nyx.example.ts.net",
        localWorkerNodeID: "",
        managedTailscaleServe: true,
        includesLocalWorker: false,
        deployments: []
    )
    let metadataBefore = try Data(contentsOf: store.metadataURL)
    let tomlBefore = try Data(contentsOf: store.configurationURL)
    #expect(throws: HubModeError.invalidRequestTimeout) {
        try store.updateRequestTimeout(seconds)
    }
    #expect(throws: HubModeError.invalidRequestTimeout) {
        try store.saveConfiguration(
            publicOrigin: "https://nyx.example.ts.net",
            localWorkerNodeID: "",
            managedTailscaleServe: true,
            includesLocalWorker: false,
            requestTimeoutSeconds: seconds,
            deployments: []
        )
    }
    #expect(try Data(contentsOf: store.metadataURL) == metadataBefore)
    #expect(try Data(contentsOf: store.configurationURL) == tomlBefore)
}

@Test("Hub timeout changes require an existing Hub and accept the Fleet timeout bounds")
func hubRequestTimeoutRequiresConfiguredHub() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let store = HubConfigurationStore(rootURL: temporary)
    #expect(throws: HubModeError.hubNotConfigured) {
        try store.updateRequestTimeout(900)
    }
    #expect(!FileManager.default.fileExists(atPath: temporary.path))
    for seconds in [0.5, 86_400.0] {
        let saved = try store.saveConfiguration(
            publicOrigin: "https://nyx.example.ts.net",
            localWorkerNodeID: "",
            managedTailscaleServe: true,
            includesLocalWorker: false,
            requestTimeoutSeconds: seconds,
            deployments: []
        )
        #expect(try store.loadConfiguration() == saved)
    }
}

@Test("Invalid persisted Hub timeout cannot be regenerated into Fleet configuration")
func hubRequestTimeoutRejectsInvalidMetadata() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let store = HubConfigurationStore(rootURL: temporary)
    _ = try store.saveConfiguration(
        publicOrigin: "https://nyx.example.ts.net",
        localWorkerNodeID: "",
        managedTailscaleServe: true,
        includesLocalWorker: false,
        deployments: []
    )
    var metadata = try #require(JSONSerialization.jsonObject(
        with: Data(contentsOf: store.metadataURL)
    ) as? [String: Any])
    metadata["request_timeout_seconds"] = 0
    try JSONSerialization.data(withJSONObject: metadata).write(to: store.metadataURL)
    let tomlBefore = try Data(contentsOf: store.configurationURL)
    #expect(throws: DecodingError.self) { try store.refreshManagedConfiguration() }
    #expect(try Data(contentsOf: store.configurationURL) == tomlBefore)
}

@Test("Hub Mode refuses unsafe origins and empty deployment authority")
func hubModeRejectsUnsafeConfiguration() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let hubStore = HubConfigurationStore(
        rootURL: temporary.appending(path: "hub", directoryHint: .isDirectory)
    )
    let deployment = HubPublishedDeployment(
        alias: "model",
        deploymentID: "sha256:" + String(repeating: "b", count: 64),
        capabilities: ["responses"]
    )

    #expect(throws: HubModeError.invalidPublicOrigin) {
        try hubStore.saveConfiguration(
            publicOrigin: "http://nyx.example.ts.net",
            localWorkerNodeID: "nyx-worker",
            managedTailscaleServe: false,
            deployments: [deployment]
        )
    }
    #expect(throws: HubModeError.noEligibleModels) {
        try hubStore.saveConfiguration(
            publicOrigin: "https://nyx.example.ts.net",
            localWorkerNodeID: "nyx-worker",
            managedTailscaleServe: false,
            deployments: []
        )
    }
}

@Test("Hub Mode refuses a symlinked private-state root")
func hubModeRejectsSymlinkedStateRoot() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    let victim = temporary.appending(path: "victim", directoryHint: .isDirectory)
    let link = temporary.appending(path: "hub", directoryHint: .isDirectory)
    try FileManager.default.createDirectory(
        at: victim,
        withIntermediateDirectories: true
    )
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: victim)

    #expect(throws: HubModeError.unsafeStatePath) {
        try HubConfigurationStore(rootURL: link).prepareSecrets(
            nativeCredentialStore: CredentialStore(
                environmentURL: temporary.appending(path: "native.env")
            )
        )
    }
}
