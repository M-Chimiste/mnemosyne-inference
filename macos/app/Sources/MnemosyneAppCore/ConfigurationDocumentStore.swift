import Foundation

public struct ConfigurationDocuments: Equatable, Sendable {
    public var configYAML: String
    public var environment: String

    public init(configYAML: String, environment: String) {
        self.configYAML = configYAML
        self.environment = environment
    }
}

public struct ConfigurationDocumentStore: Sendable {
    public let configURL: URL
    public let environmentURL: URL

    public init(configURL: URL, environmentURL: URL) {
        self.configURL = configURL
        self.environmentURL = environmentURL
    }

    public func load() throws -> ConfigurationDocuments {
        ConfigurationDocuments(
            configYAML: try read(configURL),
            environment: try read(environmentURL)
        )
    }

    public func save(_ documents: ConfigurationDocuments) throws {
        try write(documents.configYAML, to: configURL)
        try write(documents.environment, to: environmentURL)
    }

    private func read(_ url: URL) throws -> String {
        guard FileManager.default.fileExists(atPath: url.path) else { return "" }
        return try String(contentsOf: url, encoding: .utf8)
    }

    private func write(_ contents: String, to url: URL) throws {
        let fileManager = FileManager.default
        let directory = url.deletingLastPathComponent()
        if !fileManager.fileExists(atPath: directory.path) {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        }
        try Data(contents.utf8).write(to: url, options: .atomic)
        try fileManager.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: url.path
        )
    }
}
