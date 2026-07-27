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
}

public struct InstallRuntimeUpdateRequest: Codable, Equatable, Sendable {
    public let version: String?

    public init(version: String?) {
        self.version = version
    }
}
