import Darwin
import Foundation
import Testing
@testable import MnemosyneAppCore

@_silgen_name("flock")
private func credentialTestFlock(
    _ descriptor: Int32,
    _ operation: Int32
) -> Int32

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

@Test("The fleet gateway credentials are managed independently")
func fleetCredentialManagedIndependently() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(
        at: temporary,
        withIntermediateDirectories: true
    )
    let url = temporary.appending(path: ".env")
    let store = CredentialStore(environmentURL: url)

    try store.apply(
        replacements: [
            .fleetAPIKey: "fleet-secret",
            .fleetInferenceAPIKey: "dispatch-secret",
        ],
        clearing: []
    )

    #expect(
        try store.status().configured
            == [.fleetAPIKey, .fleetInferenceAPIKey]
    )
    #expect(
        try String(contentsOf: url, encoding: .utf8)
            .contains("FLEET_API_KEY=fleet-secret")
    )
    #expect(
        try String(contentsOf: url, encoding: .utf8)
            .contains("FLEET_INFERENCE_API_KEY=dispatch-secret")
    )
}

@Test("Paired Fleet credentials cannot be replaced or cleared generically")
func pairedFleetCredentialsAreEnrollmentOwned() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(
        at: temporary,
        withIntermediateDirectories: true
    )
    let url = temporary.appending(path: ".env")
    let original = """
    FLEET_API_KEY=snapshot-secret
    FLEET_INFERENCE_API_KEY=dispatch-secret
    FLEET_MANAGEMENT_API_KEY=management-secret
    UNMANAGED=preserved
    """ + "\n"
    try Data(original.utf8).write(to: url)
    let store = CredentialStore(environmentURL: url)

    #expect(throws: CredentialStoreError.pairingManagedFleetCredential) {
        try store.apply(
            replacements: [.fleetAPIKey: "replacement"],
            clearing: []
        )
    }
    #expect(throws: CredentialStoreError.pairingManagedFleetCredential) {
        try store.apply(
            replacements: [:],
            clearing: [.fleetInferenceAPIKey]
        )
    }
    #expect(try String(contentsOf: url, encoding: .utf8) == original)

    // Unrelated write-only settings remain editable while paired.
    try store.apply(
        replacements: [.huggingFaceToken: "hf-secret"],
        clearing: []
    )
    let updated = try String(contentsOf: url, encoding: .utf8)
    #expect(updated.contains("FLEET_API_KEY=snapshot-secret"))
    #expect(updated.contains("FLEET_INFERENCE_API_KEY=dispatch-secret"))
    #expect(updated.contains("FLEET_MANAGEMENT_API_KEY=management-secret"))
    #expect(updated.contains("HF_TOKEN=hf-secret"))
    #expect(updated.contains("UNMANAGED=preserved"))
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

@Test("Credential updates lock before reading and preserve the latest unrelated lines")
func credentialUpdateHonorsSiblingLock() async throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(
        at: temporary,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    let environment = temporary.appending(path: ".env")
    let lock = temporary.appending(path: ".env.lock")
    let started = temporary.appending(path: "started")
    let finished = temporary.appending(path: "finished")
    try Data("UNMANAGED=initial\n".utf8).write(to: environment)
    try Data().write(to: lock)
    try FileManager.default.setAttributes(
        [.posixPermissions: 0o600],
        ofItemAtPath: lock.path
    )

    let lockDescriptor = lock.path.withCString {
        Darwin.open($0, O_RDWR | O_CLOEXEC | O_NOFOLLOW)
    }
    #expect(lockDescriptor >= 0)
    guard lockDescriptor >= 0 else { return }
    defer { Darwin.close(lockDescriptor) }
    #expect(credentialTestFlock(lockDescriptor, LOCK_EX) == 0)

    let store = CredentialStore(environmentURL: environment)
    let applying = Task.detached {
        try Data().write(to: started)
        try store.apply(
            replacements: [.adminPassword: "new-secret"],
            clearing: []
        )
        try Data().write(to: finished)
    }
    for _ in 0..<100 where !FileManager.default.fileExists(atPath: started.path) {
        try await Task.sleep(for: .milliseconds(5))
    }
    #expect(FileManager.default.fileExists(atPath: started.path))
    try await Task.sleep(for: .milliseconds(100))
    #expect(!FileManager.default.fileExists(atPath: finished.path))

    try Data("UNMANAGED=latest\n".utf8).write(to: environment)
    #expect(credentialTestFlock(lockDescriptor, LOCK_UN) == 0)
    try await applying.value

    let contents = try String(contentsOf: environment, encoding: .utf8)
    #expect(contents.contains("UNMANAGED=latest"))
    #expect(contents.contains("ADMIN_PASSWORD=new-secret"))
}

@Test("A symlinked private credential lock is rejected without touching its target")
func credentialStoreRejectsSymlinkedLock() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    let environment = temporary.appending(path: ".env")
    let lock = temporary.appending(path: ".env.lock")
    let victim = temporary.appending(path: "victim.lock")
    try Data("UNMANAGED=preserved\n".utf8).write(to: environment)
    try Data("VICTIM=unchanged\n".utf8).write(to: victim)
    try FileManager.default.createSymbolicLink(at: lock, withDestinationURL: victim)

    #expect(throws: CredentialStoreError.self) {
        try CredentialStore(environmentURL: environment).apply(
            replacements: [.adminPassword: "new-secret"],
            clearing: []
        )
    }
    #expect(
        try String(contentsOf: environment, encoding: .utf8)
            == "UNMANAGED=preserved\n"
    )
    #expect(
        try String(contentsOf: victim, encoding: .utf8)
            == "VICTIM=unchanged\n"
    )
}

@Test("A symlinked private credential file is rejected without touching its target")
func credentialStoreRejectsSymlinkedEnvironment() throws {
    let temporary = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    defer { try? FileManager.default.removeItem(at: temporary) }
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    let environment = temporary.appending(path: ".env")
    let victim = temporary.appending(path: "victim.env")
    try Data("VICTIM=unchanged\n".utf8).write(to: victim)
    try FileManager.default.createSymbolicLink(
        at: environment,
        withDestinationURL: victim
    )

    #expect(throws: CredentialStoreError.self) {
        try CredentialStore(environmentURL: environment).apply(
            replacements: [.adminPassword: "new-secret"],
            clearing: []
        )
    }
    #expect(
        try String(contentsOf: victim, encoding: .utf8)
            == "VICTIM=unchanged\n"
    )
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

@Test("Only enrollment-owned Fleet credentials are classified for pairing")
func pairingManagedCredentialClassification() {
    #expect(ManagedCredential.fleetAPIKey.isFleetPairingCredential)
    #expect(ManagedCredential.fleetInferenceAPIKey.isFleetPairingCredential)
    #expect(!ManagedCredential.inferenceAPIKey.isFleetPairingCredential)
    #expect(!ManagedCredential.adminPassword.isFleetPairingCredential)
    #expect(!ManagedCredential.tokenSidecarPostgresDSN.isFleetPairingCredential)
}
