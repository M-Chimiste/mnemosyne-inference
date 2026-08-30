import Foundation
import Testing
@testable import MnemosyneAppCore

private let cleanupStorage = StorageLocationSettings(
    name: "athena-models",
    path: "/Volumes/Athena/models"
)
private let cleanupDestination =
    "/Volumes/Athena/models/llama.cpp/owner/model-GGUF"

@Test("Alias reuse selects only the exact profile-backed installation")
func cleanupResolverDoesNotSelectLatestAlias() {
    let exactID = "11111111-1111-4111-8111-111111111111"
    let exact = cleanupInstall(id: exactID)
    let newerAliasReuse = cleanupInstall(
        id: "22222222-2222-4222-8222-222222222222",
        destination: "/Volumes/Athena/models/llama.cpp/owner/other-GGUF",
        status: "failed",
        filename: "other.gguf"
    )

    let decision = ModelCleanupResolver.resolve(
        profile: cleanupProfile(),
        storageLocations: [cleanupStorage],
        installs: [newerAliasReuse, exact]
    )

    #expect(decision == .managed(installationID: exactID))
}

@Test("Multiple exact install rows refuse cleanup instead of guessing")
func cleanupResolverRefusesAmbiguousIdentity() {
    let decision = ModelCleanupResolver.resolve(
        profile: cleanupProfile(),
        storageLocations: [cleanupStorage],
        installs: [
            cleanupInstall(id: "11111111-1111-4111-8111-111111111111"),
            cleanupInstall(id: "22222222-2222-4222-8222-222222222222"),
        ]
    )

    #expect(decision == .refused(.ambiguousManagedRecords))
    #expect(!decision.permitsFileCleanup)
    #expect(decision.confirmationMessage.contains("multiple managed install records"))
    #expect(decision.confirmationMessage.contains("Keep Files"))
}

@Test("A same-alias identity mismatch refuses the imported cleanup path")
func cleanupResolverRefusesAliasOnlyMatch() {
    let mismatched = cleanupInstall(
        id: "22222222-2222-4222-8222-222222222222",
        destination: "/Volumes/Athena/models/llama.cpp/owner/other-GGUF",
        filename: "other.gguf"
    )

    let decision = ModelCleanupResolver.resolve(
        profile: cleanupProfile(),
        storageLocations: [cleanupStorage],
        installs: [mismatched]
    )

    #expect(decision == .refused(.managedIdentityMismatch))
    #expect(decision.installationID == nil)
    #expect(!decision.permitsFileCleanup)
}

@Test("Only an installed row with a canonical exact ID can be selected")
func cleanupResolverRequiresReadyCanonicalInstall() {
    let incomplete = ModelCleanupResolver.resolve(
        profile: cleanupProfile(),
        storageLocations: [cleanupStorage],
        installs: [
            cleanupInstall(
                id: "11111111-1111-4111-8111-111111111111",
                status: "downloaded"
            ),
        ]
    )
    let uppercaseID = ModelCleanupResolver.resolve(
        profile: cleanupProfile(),
        storageLocations: [cleanupStorage],
        installs: [
            cleanupInstall(id: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ]
    )

    #expect(incomplete == .refused(.managedInstallNotReady))
    #expect(uppercaseID == .refused(.invalidInstallationIdentity))
}

@Test("Exact managed cleanup removes only its installation row")
func cleanupDecisionRetainsAliasReuseRows() {
    let exactID = "11111111-1111-4111-8111-111111111111"
    let reusedID = "22222222-2222-4222-8222-222222222222"
    let installs = [
        cleanupInstall(id: exactID),
        cleanupInstall(
            id: reusedID,
            destination: "/Volumes/Athena/models/llama.cpp/owner/other-GGUF",
            status: "failed",
            filename: "other.gguf"
        ),
    ]

    let remaining = ModelCleanupDecision
        .managed(installationID: exactID)
        .retainingUnrelatedInstalls(from: installs)

    #expect(remaining.map(\.id) == [reusedID])
    #expect(remaining[0].alias == installs[0].alias)
}

@Test("An imported profile with no ledger row preserves the nil-ID path")
func importedCleanupHasNoInstallationID() {
    let installs = [
        cleanupInstall(
            id: "22222222-2222-4222-8222-222222222222",
            alias: "unrelated",
            destination: "/Volumes/Athena/models/llama.cpp/owner/unrelated-GGUF",
            filename: "unrelated.gguf"
        ),
    ]
    let decision = ModelCleanupResolver.resolve(
        profile: cleanupProfile(),
        storageLocations: [cleanupStorage],
        installs: installs
    )

    #expect(decision == .imported)
    #expect(decision.permitsFileCleanup)
    #expect(decision.installationID == nil)
    #expect(decision.retainingUnrelatedInstalls(from: installs) == installs)
}

@Test("Projector and registered-storage identity are part of cleanup proof")
func cleanupResolverChecksProjectorAndStorageRoot() {
    let install = cleanupInstall(
        id: "11111111-1111-4111-8111-111111111111",
        projectorFilename: "vision/mmproj.gguf"
    )
    let matching = cleanupProfile(
        projectorPath: cleanupDestination + "/vision/mmproj.gguf"
    )
    let wrongProjector = cleanupProfile(
        projectorPath: cleanupDestination + "/vision/other.gguf"
    )

    #expect(
        ModelCleanupResolver.resolve(
            profile: matching,
            storageLocations: [cleanupStorage],
            installs: [install]
        ) == .managed(installationID: install.id)
    )
    #expect(
        ModelCleanupResolver.resolve(
            profile: wrongProjector,
            storageLocations: [cleanupStorage],
            installs: [install]
        ) == .refused(.managedIdentityMismatch)
    )
    #expect(
        ModelCleanupResolver.resolve(
            profile: matching,
            storageLocations: [
                StorageLocationSettings(
                    name: cleanupStorage.name,
                    path: "/Volumes/Other/models"
                ),
            ],
            installs: [install]
        ) == .refused(.managedIdentityMismatch)
    )
}

@Test("Trash success copy applies to every cleanup source")
func cleanupCopyDoesNotLabelTrashAsImportedOnly() {
    let success = ModelCleanupDecision.successMessage(
        alias: "shared-alias",
        filesDisposition: "trashed"
    )

    #expect(success.contains("Trash"))
    #expect(success.contains("removed its profile"))
    #expect(!success.localizedCaseInsensitiveContains("imported"))
    #expect(ModelCleanupDecision.imported.confirmationMessage.contains("Trash"))
    #expect(
        ModelCleanupDecision
            .managed(installationID: "11111111-1111-4111-8111-111111111111")
            .confirmationMessage.contains("Trash")
    )
}

private func cleanupProfile(
    projectorPath: String? = nil
) -> ModelProfileSettings {
    ModelProfileSettings(
        alias: "shared-alias",
        engine: .llamaCpp,
        model: cleanupDestination + "/model-Q4_K_M.gguf",
        storage: cleanupStorage.name,
        load: ModelLoadSettings(projectorPath: projectorPath)
    )
}

private func cleanupInstall(
    id: String,
    alias: String = "shared-alias",
    destination: String = cleanupDestination,
    status: String = "installed",
    filename: String = "model-Q4_K_M.gguf",
    projectorFilename: String? = nil
) -> ModelInstall {
    ModelInstall(
        id: id,
        repoId: "owner/model-GGUF",
        engine: .llamaCpp,
        storage: cleanupStorage.name,
        alias: alias,
        destination: destination,
        status: status,
        revision: "abc123",
        filename: filename,
        projectorFilename: projectorFilename,
        contextLength: nil,
        downloadFiles: nil,
        capabilities: nil,
        family: nil,
        bytesDownloaded: 1,
        totalBytes: 1,
        downloadSpeedBps: nil,
        error: nil,
        pid: nil,
        createdAt: 1,
        updatedAt: 1
    )
}
