import Foundation

/// The Model Library's transient download destination and refreshed content.
///
/// The selected storage key is an explicit user choice. Search results,
/// details, and storage health refresh independently and must never replace it
/// with the configured default. If the chosen key disappears or becomes
/// unavailable, installation closes until the user deliberately chooses a
/// different destination.
public struct ModelLibraryDownloadViewModel: Equatable, Sendable {
    public private(set) var selectedStorageKey: String
    public private(set) var storageStatuses: [String: StorageStatus]
    public private(set) var searchResults: [LibraryModel]
    public private(set) var details: LibraryModelDetails?
    private var initializedFromConfiguration: Bool

    public init() {
        selectedStorageKey = "internal"
        storageStatuses = [:]
        searchResults = []
        details = nil
        initializedFromConfiguration = false
    }

    /// Apply the configured default only to a new Settings view model.
    /// Reconfiguration and background catalog refreshes preserve the exact
    /// user's current Download-to choice, including an unavailable one.
    public mutating func initialize(defaultStorageKey: String) {
        guard !initializedFromConfiguration else { return }
        selectedStorageKey = defaultStorageKey
        initializedFromConfiguration = true
    }

    public mutating func selectStorage(_ key: String) {
        selectedStorageKey = key
        initializedFromConfiguration = true
    }

    public mutating func applyStorageStatuses(
        _ statuses: [String: StorageStatus]
    ) {
        storageStatuses = statuses
    }

    public mutating func setStorageStatus(
        _ status: StorageStatus,
        for key: String
    ) {
        storageStatuses[key] = status
    }

    public mutating func removeStorageStatus(for key: String) {
        storageStatuses.removeValue(forKey: key)
    }

    public mutating func applySearchResults(_ results: [LibraryModel]) {
        searchResults = results
    }

    public mutating func applyDetails(_ refreshedDetails: LibraryModelDetails?) {
        details = refreshedDetails
    }

    public var selectedStorageStatus: StorageStatus? {
        storageStatuses[selectedStorageKey]
    }

    public var selectedStorageIsAvailable: Bool {
        selectedStorageStatus?.isAvailable == true
    }
}
