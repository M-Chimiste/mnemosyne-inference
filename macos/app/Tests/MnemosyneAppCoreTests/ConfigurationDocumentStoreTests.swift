import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Configuration documents round-trip with private permissions")
func configurationDocumentsRoundTrip() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }

    let store = ConfigurationDocumentStore(
        configURL: temporary.appending(path: "config.yaml"),
        environmentURL: temporary.appending(path: ".env")
    )
    let documents = ConfigurationDocuments(
        configYAML: "models: []\n",
        environment: "ADMIN_PASSWORD=local-secret\n"
    )

    try store.save(documents)

    #expect(try store.load() == documents)
    let configAttributes = try FileManager.default.attributesOfItem(
        atPath: store.configURL.path
    )
    let environmentAttributes = try FileManager.default.attributesOfItem(
        atPath: store.environmentURL.path
    )
    #expect((configAttributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
    #expect((environmentAttributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
}

@Test("Missing configuration documents load as empty drafts")
func missingConfigurationDocuments() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    let store = ConfigurationDocumentStore(
        configURL: temporary.appending(path: "config.yaml"),
        environmentURL: temporary.appending(path: ".env")
    )

    #expect(
        try store.load()
            == ConfigurationDocuments(configYAML: "", environment: "")
    )
}
