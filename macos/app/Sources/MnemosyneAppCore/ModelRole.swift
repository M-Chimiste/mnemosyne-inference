import Foundation

/// A nontechnical model purpose. The persisted service configuration still
/// stores endpoint capabilities, but the menu app only exposes combinations
/// that the selected native engine can route safely.
public enum ModelRole: String, Codable, CaseIterable, Identifiable, Sendable {
    case generation
    case embeddings
    case rerank
    case image

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .generation: "Generation"
        case .embeddings: "Embeddings"
        case .rerank: "Rerank"
        case .image: "Image"
        }
    }

    public var systemImage: String {
        switch self {
        case .generation: "text.bubble"
        case .embeddings: "point.3.connected.trianglepath.dotted"
        case .rerank: "list.number"
        case .image: "photo"
        }
    }

    public var explanation: String {
        switch self {
        case .generation:
            "Chat, text completions, Responses, and Anthropic Messages"
        case .embeddings:
            "Vector embeddings only"
        case .rerank:
            "Document reranking only"
        case .image:
            "Image generation only"
        }
    }

    public var capabilities: [String] {
        switch self {
        case .generation:
            ["chat/completions", "completions", "responses", "messages"]
        case .embeddings:
            ["embeddings"]
        case .rerank:
            ["rerank"]
        case .image:
            ["images/generations"]
        }
    }
}

public extension ModelProfileSettings {
    /// Resolve only endpoint combinations that can be represented by the
    /// ordinary UI. A legacy custom combination remains untouched and returns
    /// nil until the user deliberately chooses one typed role.
    var configuredRole: ModelRole? {
        if engine == .mflux || kind == .image {
            return .image
        }
        guard let capabilities else {
            switch engine {
            case .llamaCpp, .ds4:
                return .generation
            case .lmstudio, .omlx:
                return nil
            case .mflux:
                return .image
            }
        }

        let configured = Set(capabilities)
        let generation = Set(ModelRole.generation.capabilities)
        if configured == generation {
            return .generation
        }
        if configured == Set(ModelRole.embeddings.capabilities) {
            return .embeddings
        }
        if configured == Set(ModelRole.rerank.capabilities) {
            return .rerank
        }
        if configured == Set(ModelRole.image.capabilities) {
            return .image
        }
        return nil
    }

    /// Limit choices to valid engine-level combinations. A multimodal
    /// projector is part of a generation profile and cannot be combined with
    /// llama.cpp's embeddings or reranking modes.
    var availableRoles: [ModelRole] {
        switch engine {
        case .lmstudio:
            [.generation, .embeddings]
        case .llamaCpp:
            if let projectorPath = load.projectorPath,
               !projectorPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                [.generation]
            } else {
                [.generation, .embeddings, .rerank]
            }
        case .omlx:
            [.generation, .embeddings, .rerank]
        case .ds4:
            [.generation]
        case .mflux:
            [.image]
        }
    }

    mutating func applyRole(_ role: ModelRole) {
        guard availableRoles.contains(role) else { return }
        capabilities = role.capabilities

        if role == .image {
            kind = .image
            image = image ?? .init()
            return
        }

        kind = .language
        image = nil
        guard engine == .llamaCpp else { return }
        switch role {
        case .generation:
            load.pooling = nil
        case .embeddings:
            if load.pooling == "rank" {
                load.pooling = nil
            }
        case .rerank:
            load.pooling = "rank"
        case .image:
            break
        }
    }
}
