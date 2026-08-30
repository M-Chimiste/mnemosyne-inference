import Foundation

public struct RuntimeUpdateSnapshot: Codable, Equatable, Sendable {
    public let channel: String
    public let manifestUrl: String?
    public let checkedAt: Double?
    public let coreProtocol: Int
    public let engines: [EngineRuntimeUpdate]

    public init(
        channel: String,
        manifestUrl: String?,
        checkedAt: Double?,
        coreProtocol: Int,
        engines: [EngineRuntimeUpdate]
    ) {
        self.channel = channel
        self.manifestUrl = manifestUrl
        self.checkedAt = checkedAt
        self.coreProtocol = coreProtocol
        self.engines = engines
    }
}

public struct EngineRuntimeUpdate: Codable, Equatable, Identifiable, Sendable {
    public let engine: InferenceEngine
    public let releaseTier: String?
    public let displayName: String
    public let ownership: String
    public let installed: Bool
    public let installedVersion: String?
    public let installedRevision: String?
    public let installedPath: String?
    public let installedChannel: String?
    public let installationKind: String?
    public let upgradeStrategy: String?
    public let latestUpstreamVersion: String?
    public let latestUpstreamRevision: String?
    public let latestUpstreamUrl: String?
    public let officialInstallerUrl: String?
    public let availableVersion: String?
    public let availableRevision: String?
    public let releaseNotesUrl: String?
    public let updateAvailable: Bool
    public let canInstall: Bool
    public let canRollback: Bool
    public let managementNote: String
    public let diagnostic: String?
    public let managedChannels: [ManagedRuntimeChannel]?

    public var id: InferenceEngine { engine }

    public var releaseTierLabel: String? {
        releaseTier?.uppercased()
    }

    public var installedLabel: String {
        installedVersion ?? (installed ? "Detected" : "Not installed")
    }

    public var availableLabel: String? {
        availableVersion ?? latestUpstreamVersion
    }

    public var installationKindLabel: String? {
        switch installationKind {
        case "official_app", "official_app_cli": "Official app"
        case "homebrew_stable": "Homebrew stable"
        case "homebrew_head": "Homebrew HEAD"
        case "running_external": "External server"
        case "external_cli": "External CLI"
        default: nil
        }
    }

    public var ds4GLM53FlashPreview: ManagedRuntimeChannel? {
        guard engine == .ds4 else { return nil }
        return managedChannels?.first {
            $0.channel == ManagedRuntimeChannel.ds4GLM53FlashChannel
                && $0.sourceBranch == ManagedRuntimeChannel.ds4GLM53FlashChannel
                && $0.releaseTier == "experimental"
        }
    }
}

public struct ManagedRuntimeChannel: Codable, Equatable, Identifiable, Sendable {
    public static let ds4GLM53FlashChannel = "glm-5.3-flash"

    public let channel: String
    public let sourceBranch: String
    public let releaseTier: String
    public let availableVersion: String?
    public let availableRevision: String?
    public let releaseNotesUrl: String?
    public let updateAvailable: Bool
    public let canInstall: Bool
    public let diagnostic: String?

    public var id: String { channel }

    public var releaseTierLabel: String {
        releaseTier.uppercased()
    }
}

public struct InstallRuntimeUpdateRequest: Codable, Equatable, Sendable {
    public let version: String?
    public let channel: String?

    public init(version: String?, channel: String? = nil) {
        self.version = version
        self.channel = channel
    }
}

public enum GLM53PreviewPresentation {
    public static let q2MinimumMemoryGB = 128
    public static let q4MinimumMemoryGB = 256

    public static func queryTargetsPreview(_ query: String) -> Bool {
        let compact = query.lowercased().filter { $0.isLetter || $0.isNumber }
        return compact.contains("glm53")
    }

    public static func modelTargetsPreview(_ model: LibraryModel) -> Bool {
        if model.family == "glm-5.3-flash" { return true }
        return [model.repoId, model.displayName]
            .map { value in
                value.lowercased().filter { $0.isLetter || $0.isNumber }
            }
            .contains { $0.contains("glm53") }
    }

    public static func visibleModels(
        query: String,
        models: [LibraryModel]
    ) -> [LibraryModel] {
        guard queryTargetsPreview(query) else { return models }

        // Until the exact source-bound DS4 preview is active, do not present
        // generic Hub matches as though another Mac runtime had verified this
        // architecture. Installing weights remains a separate user action.
        return models.filter { model in
            !modelTargetsPreview(model)
                || (
                    model.engine == .ds4
                        && model.family == "glm-5.3-flash"
                        && model.releaseTier == "experimental"
                )
        }
    }

    public static func shouldOfferRuntimeInstall(
        query: String,
        models: [LibraryModel],
        ds4Update: EngineRuntimeUpdate?
    ) -> Bool {
        guard
            queryTargetsPreview(query),
            !models.contains(where: {
                $0.engine == .ds4
                    && $0.family == "glm-5.3-flash"
                    && $0.releaseTier == "experimental"
            }),
            ds4Update?.ds4GLM53FlashPreview?.canInstall == true
        else { return false }
        return true
    }
}

public struct OMLXCacheHealth: Codable, Equatable, Sendable {
    public let available: Bool
    public let totalRequests: Int
    public let totalCachedTokens: Int
    public let cacheEfficiency: Double?
    public let ssdFileCount: Int
    public let ssdSizeBytes: Int
    public let ssdLimitBytes: Int
    public let hotSizeBytes: Int
    public let hotLimitBytes: Int
    public let resetRecommended: Bool
    public let diagnostic: String?
}

public struct OMLXCacheResetResult: Codable, Equatable, Sendable {
    public let status: String
    public let deletedFiles: Int
    public let cache: OMLXCacheHealth?
}
