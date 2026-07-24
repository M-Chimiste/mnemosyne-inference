import Foundation

public enum LMStudioInventoryAvailability: Equatable, Sendable {
    case available
    case disabled
    case saveRequired
    case restartRequired

    public var canOpen: Bool { self == .available }

    public var guidance: String? {
        switch self {
        case .available:
            nil
        case .disabled:
            "Turn on the LM Studio migration bridge, then save and restart the background service."
        case .saveRequired:
            "Save the LM Studio engine changes before opening its model inventory."
        case .restartRequired:
            "Restart the background service to apply the saved LM Studio settings before opening its model inventory."
        }
    }

    public static func evaluate(
        draft: LMStudioSettings,
        saved: LMStudioSettings,
        savedRevision: String,
        appliedRevision: String,
        restartRequired: Bool
    ) -> LMStudioInventoryAvailability {
        guard draft.enabled else { return .disabled }
        guard saved.enabled, draft == saved else { return .saveRequired }
        guard !restartRequired,
              !savedRevision.isEmpty,
              savedRevision == appliedRevision
        else {
            return .restartRequired
        }
        return .available
    }
}

public struct LMStudioInventorySnapshot: Codable, Equatable, Sendable {
    public let models: [LMStudioDiscoveredModel]
}

public struct LMStudioDiscoveredModel: Codable, Equatable, Identifiable, Sendable {
    public let key: String
    public let displayName: String
    public let type: String
    public let publisher: String?
    public let architecture: String?
    public let quantizationName: String?
    public let bitsPerWeight: Double?
    public let sizeBytes: Int64?
    public let paramsString: String?
    public let maxContextLength: Int?
    public let format: String?
    public let vision: Bool?
    public let trainedForToolUse: Bool?
    public let loaded: Bool

    public var id: String { key }
    public var isImportable: Bool { type == "llm" || type == "embedding" }
}
