import Foundation

public struct StorageStatus: Codable, Equatable, Identifiable, Sendable {
    public let name: String?
    public let path: String
    public let exists: Bool
    public let isDirectory: Bool
    public let writable: Bool
    public let mountPath: String?
    public let volumeUuid: String?
    public let scopeId: String?
    public let expectedVolumeUuid: String?
    public let volumeMatches: Bool
    public let totalBytes: Int64?
    public let freeBytes: Int64?
    public let diagnostic: String?

    public var id: String { name ?? path }
    public var isAvailable: Bool { exists && isDirectory && writable && volumeMatches }

    public init(
        name: String?,
        path: String,
        exists: Bool,
        isDirectory: Bool,
        writable: Bool,
        mountPath: String?,
        volumeUuid: String?,
        scopeId: String? = nil,
        expectedVolumeUuid: String?,
        volumeMatches: Bool,
        totalBytes: Int64?,
        freeBytes: Int64?,
        diagnostic: String?
    ) {
        self.name = name
        self.path = path
        self.exists = exists
        self.isDirectory = isDirectory
        self.writable = writable
        self.mountPath = mountPath
        self.volumeUuid = volumeUuid
        self.scopeId = scopeId
        self.expectedVolumeUuid = expectedVolumeUuid
        self.volumeMatches = volumeMatches
        self.totalBytes = totalBytes
        self.freeBytes = freeBytes
        self.diagnostic = diagnostic
    }
}

public struct StorageSnapshot: Codable, Equatable, Sendable {
    public let `default`: String
    public let locations: [StorageStatus]
}

public struct LibraryModel: Codable, Equatable, Identifiable, Sendable {
    public let repoId: String
    public let engine: InferenceEngine
    public let displayName: String
    public let modelKind: ModelKindSetting
    public let compatibility: String
    public let compatibilityReason: String
    public let downloads: Int?
    public let likes: Int?
    public let sizeBytes: Int64?
    public let quantization: String?
    public let filename: String?
    public let projectorFilename: String?
    public let projectorOptions: [String]?
    public let downloadFiles: [String]?
    public let resolvedRevision: String?
    public let requiresFileSelection: Bool?
    public let family: String?
    public let releaseTier: String?
    public let recommendedMemoryGb: Int?
    public let installable: Bool?
    public let suggestedRole: ModelRole?
    public let defaultQuantize: Int?
    public let defaultWidth: Int?
    public let defaultHeight: Int?
    public let defaultNumInferenceSteps: Int?
    public let defaultGuidanceScale: Double?
    public let architecture: String?
    public let contextLength: Int?
    public let parameterCount: Int64?

    public var id: String { "\(engine.rawValue):\(repoId):\(filename ?? "snapshot")" }
    public var isInstallable: Bool { installable != false }
    public var needsFileSelection: Bool { requiresFileSelection == true }
    public var availableProjectors: [String] { projectorOptions ?? [] }
}

public struct LibraryModelDetails: Codable, Equatable, Sendable {
    public let repoId: String
    public let resolvedRevision: String?
    public let architecture: String?
    public let contextLength: Int?
    public let parameterCount: Int64?
    public let summary: String?
    public let modelCardMarkdown: String?
    public let license: String?
    public let pipelineTag: String?
    public let tags: [String]
    public let lastModified: String?
}

public struct LibraryModelsSnapshot: Codable, Equatable, Sendable {
    public let models: [LibraryModel]
}

public struct ModelInstall: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let repoId: String
    public let engine: InferenceEngine
    public let storage: String
    public let alias: String
    public let destination: String
    public let status: String
    public let revision: String?
    public let filename: String?
    public let projectorFilename: String?
    public let contextLength: Int?
    public let downloadFiles: [String]?
    public let capabilities: [String]?
    public let family: String?
    public let bytesDownloaded: Int64
    public let totalBytes: Int64?
    public let downloadSpeedBps: Double?
    public let error: String?
    public let pid: Int?
    public let createdAt: Double
    public let updatedAt: Double

    public var isActive: Bool {
        status == "preparing"
            || status == "queued"
            || status == "downloading"
            || status == "registering"
    }

    public var canRetry: Bool {
        ["preparing", "failed", "cancelled", "partial", "downloaded"].contains(status)
    }

    public var canDismiss: Bool {
        !isActive
    }

    public var progressFraction: Double? {
        guard let totalBytes, totalBytes > 0 else { return nil }
        return min(1, max(0, Double(bytesDownloaded) / Double(totalBytes)))
    }
}

public struct ModelInstallObservation: Equatable, Sendable {
    public let newlyInstalledAliases: [String]
    public let hasActiveInstalls: Bool

    public init(
        newlyInstalledAliases: [String],
        hasActiveInstalls: Bool
    ) {
        self.newlyInstalledAliases = newlyInstalledAliases
        self.hasActiveInstalls = hasActiveInstalls
    }
}

public struct ModelInstallMonitorState: Sendable {
    private var installedIDs: Set<String>

    public init(installs: [ModelInstall]) {
        installedIDs = Set(
            installs.lazy.filter { $0.status == "installed" }.map(\.id)
        )
    }

    public mutating func observe(
        _ installs: [ModelInstall]
    ) -> ModelInstallObservation {
        let currentInstalledIDs = Set(
            installs.lazy.filter { $0.status == "installed" }.map(\.id)
        )
        let newlyInstalledAliases: [String] = installs.compactMap { install -> String? in
            guard
                install.status == "installed",
                !installedIDs.contains(install.id)
            else {
                return nil
            }
            return install.alias
        }
        installedIDs = currentInstalledIDs
        return ModelInstallObservation(
            newlyInstalledAliases: newlyInstalledAliases,
            hasActiveInstalls: installs.contains(where: \.isActive)
        )
    }
}

public struct ModelInstallsSnapshot: Codable, Equatable, Sendable {
    public let installs: [ModelInstall]
}

public struct StartModelInstallRequest: Codable, Equatable, Sendable {
    public let repoId: String
    public let engine: InferenceEngine
    public let storage: String
    public let alias: String?
    public let revision: String?
    public let filename: String?
    public let projectorFilename: String?
    public let includeProjector: Bool
    public let capabilities: [String]

    public init(
        model: LibraryModel,
        storage: String,
        alias: String? = nil,
        revision: String? = nil,
        projectorFilename: String? = nil,
        includeProjector: Bool = true,
        role: ModelRole
    ) {
        repoId = model.repoId
        engine = model.engine
        self.storage = storage
        self.alias = alias
        self.revision = revision ?? model.resolvedRevision
        filename = model.filename
        self.projectorFilename = projectorFilename
        self.includeProjector = includeProjector
        capabilities = role.capabilities(for: model.engine)
    }
}
