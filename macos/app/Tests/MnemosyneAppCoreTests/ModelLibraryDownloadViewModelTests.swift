import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Model Library ViewModel preserves a non-default Download-to choice across refreshes")
func downloadDestinationSurvivesCatalogRefresh() throws {
    var viewModel = ModelLibraryDownloadViewModel()
    viewModel.initialize(defaultStorageKey: "internal")
    viewModel.applyStorageStatuses([
        "internal": storageStatus(name: "internal", available: true),
        "athena-nested": storageStatus(name: "athena-nested", available: true),
    ])
    viewModel.selectStorage("athena-nested")

    viewModel.applySearchResults([try libraryModel(displayName: "First")])
    viewModel.applyDetails(try libraryDetails(summary: "First details"))
    viewModel.applySearchResults([try libraryModel(displayName: "Refreshed")])
    viewModel.applyDetails(try libraryDetails(summary: "Refreshed details"))

    #expect(viewModel.selectedStorageKey == "athena-nested")
    #expect(viewModel.selectedStorageStatus?.name == "athena-nested")
    #expect(viewModel.selectedStorageIsAvailable)
    #expect(viewModel.searchResults.first?.displayName == "Refreshed")
    #expect(viewModel.details?.summary == "Refreshed details")
}

@Test("Model Library ViewModel disables downloads when the exact destination is unavailable")
func unavailableDownloadDestinationClosesInstall() {
    var viewModel = ModelLibraryDownloadViewModel()
    viewModel.initialize(defaultStorageKey: "internal")
    viewModel.applyStorageStatuses([
        "internal": storageStatus(name: "internal", available: true),
        "athena-nested": storageStatus(
            name: "athena-nested",
            available: false
        ),
    ])
    viewModel.selectStorage("athena-nested")

    #expect(viewModel.selectedStorageKey == "athena-nested")
    #expect(!viewModel.selectedStorageIsAvailable)
}

@Test("Model Library ViewModel never substitutes the default for a missing selected key")
func downloadDestinationDoesNotFallbackSilently() {
    var viewModel = ModelLibraryDownloadViewModel()
    viewModel.initialize(defaultStorageKey: "internal")
    viewModel.selectStorage("external-volume-models")

    // A later service/configuration refresh still advertises a healthy
    // default, but the user's exact selected key has disappeared.
    viewModel.applyStorageStatuses([
        "internal": storageStatus(name: "internal", available: true),
    ])
    viewModel.initialize(defaultStorageKey: "internal")

    #expect(viewModel.selectedStorageKey == "external-volume-models")
    #expect(viewModel.selectedStorageStatus == nil)
    #expect(!viewModel.selectedStorageIsAvailable)
}

private func storageStatus(
    name: String,
    available: Bool
) -> StorageStatus {
    StorageStatus(
        name: name,
        path: name == "internal"
            ? "/Users/example/Library/Application Support/Mnemosyne/models"
            : "/Volumes/Athena/nested/models",
        exists: available,
        isDirectory: available,
        writable: available,
        mountPath: name == "internal" ? "/" : "/Volumes/Athena",
        volumeUuid: name == "internal" ? "root-volume" : "athena-volume",
        expectedVolumeUuid: name == "internal"
            ? "root-volume" : "athena-volume",
        volumeMatches: available,
        totalBytes: 2_000_000_000,
        freeBytes: available ? 1_000_000_000 : nil,
        diagnostic: available ? nil : "volume_unavailable"
    )
}

private func libraryModel(displayName: String) throws -> LibraryModel {
    let object: [String: Any] = [
        "repo_id": "org/model",
        "engine": "llama.cpp",
        "display_name": displayName,
        "model_kind": "language",
        "compatibility": "supported",
        "compatibility_reason": "Published GGUF",
        "installable": true,
    ]
    return try JSONDecoder.nativeSettingsDecoder().decode(
        LibraryModel.self,
        from: JSONSerialization.data(withJSONObject: object)
    )
}

private func libraryDetails(summary: String) throws -> LibraryModelDetails {
    let object: [String: Any] = [
        "repo_id": "org/model",
        "summary": summary,
        "tags": ["gguf"],
    ]
    return try JSONDecoder.nativeSettingsDecoder().decode(
        LibraryModelDetails.self,
        from: JSONSerialization.data(withJSONObject: object)
    )
}
