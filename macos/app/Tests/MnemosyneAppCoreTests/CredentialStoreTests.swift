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

@Test("Postgres ledger connection is managed as a write-only credential")
func postgresLedgerCredentialLifecycle() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    let url = temporary.appending(path: ".env")
    let original = "postgresql://writer:old-secret@ledger.local:5432/usage"
    try Data("TOKEN_SIDECAR_POSTGRES_DSN=\(original)\n".utf8).write(to: url)
    let store = CredentialStore(environmentURL: url)

    let status = try store.status()

    #expect(status.configured == [.tokenSidecarPostgresDSN])

    let replacement = "postgresql://writer:new-secret@ledger.local:5432/usage?sslmode=require"
    try store.apply(
        replacements: [.tokenSidecarPostgresDSN: replacement],
        clearing: []
    )

    var contents = try String(contentsOf: url, encoding: .utf8)
    #expect(contents.contains("TOKEN_SIDECAR_POSTGRES_DSN=\(replacement)"))
    #expect(!contents.contains(original))
    let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
    #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)

    try store.apply(replacements: [:], clearing: [.tokenSidecarPostgresDSN])

    contents = try String(contentsOf: url, encoding: .utf8)
    #expect(!contents.contains("TOKEN_SIDECAR_POSTGRES_DSN"))
    #expect(try store.status().configured.isEmpty)
}

@Test("Postgres draft previews preserve connection details and truncate the password")
func postgresCredentialDraftPreview() {
    let value = """
    postgresql://writer:super-secret@nyx:5432/token_sidecar?sslmode=require
    """

    let preview = CredentialDraftPreview.render(
        value,
        for: .tokenSidecarPostgresDSN
    )

    #expect(
        preview
            == "postgresql://writer:supe •••• cret@nyx:5432/token_sidecar?… · password 12 characters"
    )
    #expect(!preview.contains("super-secret"))
    #expect(!preview.contains("sslmode"))
}

@Test("Opaque credential previews expose only short prefix and suffix checks")
func opaqueCredentialDraftPreview() {
    let preview = CredentialDraftPreview.render(
        "hf_abcdefghijklmnopqrstuvwxyz",
        for: .huggingFaceToken
    )

    #expect(preview == "hf_abcdefghi •••• tuvwxyz · 29 characters")
    #expect(!preview.contains("bcdefghijklmnopqrstuv"))
}
