import Foundation

/// A metadata-only projection of the existing authenticated Fleet overview.
public struct HubWorkspaceOverview: Decodable, Sendable {
    public struct Resident: Decodable, Sendable {
        public let alias: String?
        public let engine: String?
    }
    public struct Node: Decodable, Identifiable, Sendable {
        public let nodeID: String
        public let enrollmentID: String
        public let online: Bool
        public let joinedState: String
        public let activeRequests: Int
        public let residentModel: Resident?
        public var id: String { enrollmentID }
        enum CodingKeys: String, CodingKey {
            case nodeID = "node_id", enrollmentID = "enrollment_id"
            case online, joinedState = "joined_state", activeRequests = "active_requests"
            case residentModel = "resident_model"
        }
    }
    public let schemaVersion: Int
    public let nodes: [Node]
    enum CodingKeys: String, CodingKey { case schemaVersion = "schema_version", nodes }
}
