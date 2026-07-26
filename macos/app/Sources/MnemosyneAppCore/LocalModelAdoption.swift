import Foundation

public struct LocalModelSource: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let displayName: String
    public let path: String
    public let source: String
}

public struct LocalModelSourcesSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let sources: [LocalModelSource]
}

public struct LocalProjectorCandidate: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let path: String
    public let filename: String
    public let sizeBytes: Int64
}

public struct LocalModelCandidate: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let sourceKey: String
    public let engine: InferenceEngine
    public let displayName: String
    public let modelPath: String
    public let allPaths: [String]
    public let shardCount: Int
    public let quantization: String?
    public let sizeBytes: Int64
    public let compatibility: String
    public let compatibilityReason: String
    public let capabilities: [String]
    public let architecture: String?
    public let contextLength: Int?
    public let parameterCount: Int64?
    public let summary: String?
    public let modelCardMarkdown: String?
    public let recommendedProjectorId: String?
    public let projectorOptions: [LocalProjectorCandidate]
    public let existingAlias: String?
    public let alreadyImported: Bool

    public var isImportable: Bool {
        compatibility != "unavailable" && !alreadyImported
    }
}

public struct LocalModelScanSnapshot: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let root: String
    public let mountPath: String?
    public let volumeUuid: String?
    public let scopeId: String?
    public let models: [LocalModelCandidate]
}

public struct LocalModelImportSelection: Codable, Equatable, Sendable {
    public let candidateId: String
    public let alias: String?
    public let projectorId: String?
    public let includeProjector: Bool

    public init(
        candidateId: String,
        alias: String? = nil,
        projectorId: String? = nil,
        includeProjector: Bool = true
    ) {
        self.candidateId = candidateId
        self.alias = alias
        self.projectorId = projectorId
        self.includeProjector = includeProjector
    }
}

public struct LocalModelImportRequest: Codable, Equatable, Sendable {
    public let path: String
    public let scopeId: String?
    public let selections: [LocalModelImportSelection]

    public init(
        path: String,
        scopeId: String? = nil,
        selections: [LocalModelImportSelection]
    ) {
        self.path = path
        self.scopeId = scopeId
        self.selections = selections
    }
}

public struct AdoptedLocalModel: Codable, Equatable, Identifiable, Sendable {
    public let candidateId: String
    public let alias: String
    public let engine: InferenceEngine
    public let modelPath: String
    public let projectorPath: String?
    public let migrated: Bool

    public var id: String { candidateId }
}

public struct LocalModelImportResult: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let imported: [AdoptedLocalModel]
    public let restartRequired: Bool
    public let revision: String
    public let config: NativeSettings
}
