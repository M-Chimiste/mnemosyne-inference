import Foundation

public struct ModelSummary: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let engine: String?
    public let upstreamModel: String?
    public let capabilities: [String]?

    public init(
        id: String,
        engine: String?,
        upstreamModel: String?,
        capabilities: [String]?
    ) {
        self.id = id
        self.engine = engine
        self.upstreamModel = upstreamModel
        self.capabilities = capabilities
    }

    enum CodingKeys: String, CodingKey {
        case id
        case engine
        case upstreamModel = "upstream_model"
        case capabilities
    }
}

public struct ModelCatalogSnapshot: Codable, Equatable, Sendable {
    public let models: [ModelSummary]
    public let residentAlias: String?

    public init(models: [ModelSummary], residentAlias: String?) {
        self.models = models
        self.residentAlias = residentAlias
    }

    enum CodingKeys: String, CodingKey {
        case models
        case residentAlias = "resident_alias"
    }
}

struct LoadModelRequest: Codable, Equatable, Sendable {
    let model: String
}
