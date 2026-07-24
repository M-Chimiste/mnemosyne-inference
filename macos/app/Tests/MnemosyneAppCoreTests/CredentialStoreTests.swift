import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Credential store reports presence without returning secret values")
func credentialStatusIsSecretSafe() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    let url = temporary.appending(path: ".env")
    try Data("ADMIN_PASSWORD=local-secret\nUNMANAGED=preserved\n".utf8).write(to: url)

    let status = try CredentialStore(environmentURL: url).status()

    #expect(status.configured == [.adminPassword])
}

@Test("Credential updates preserve unmanaged lines and use private permissions")
func credentialUpdatesPreserveUnmanagedContent() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    let url = temporary.appending(path: ".env")
    try Data("# local settings\nADMIN_PASSWORD=old\nUNMANAGED=preserved\n".utf8).write(to: url)
    let store = CredentialStore(environmentURL: url)

    try store.apply(
        replacements: [.adminPassword: "new-secret", .omlxAPIKey: "omlx-secret"],
        clearing: []
    )

    let contents = try String(contentsOf: url, encoding: .utf8)
    #expect(contents.contains("# local settings"))
    #expect(contents.contains("UNMANAGED=preserved"))
    #expect(contents.contains("ADMIN_PASSWORD=new-secret"))
    #expect(contents.contains("OMLX_API_KEY=omlx-secret"))
    let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
    #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
}

@Test("Credential clearing removes the managed assignment only")
func credentialClearing() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    let url = temporary.appending(path: ".env")
    try Data("ADMIN_PASSWORD=old\nUNMANAGED=preserved\n".utf8).write(to: url)
    let store = CredentialStore(environmentURL: url)

    try store.apply(replacements: [:], clearing: [.adminPassword])

    let contents = try String(contentsOf: url, encoding: .utf8)
    #expect(!contents.contains("ADMIN_PASSWORD"))
    #expect(contents.contains("UNMANAGED=preserved"))
}
