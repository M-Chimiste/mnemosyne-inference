import Foundation

public struct ProductBuildIdentity: Equatable, Sendable {
    public let version: String?
    public let build: String?

    public init(version: String?, build: String?) {
        self.version = Self.nonempty(version)
        self.build = Self.nonempty(build)
    }

    public static var current: ProductBuildIdentity {
        ProductBuildIdentity(
            version: Bundle.main.object(
                forInfoDictionaryKey: "CFBundleShortVersionString"
            ) as? String,
            build: Bundle.main.object(
                forInfoDictionaryKey: "CFBundleVersion"
            ) as? String
        )
    }

    public var compactLabel: String {
        switch (version, build) {
        case let (.some(version), .some(build)):
            "\(version) (\(build))"
        case let (.some(version), .none):
            version
        case let (.none, .some(build)):
            "build \(build)"
        case (.none, .none):
            "development"
        }
    }

    public var accessibilityLabel: String {
        switch (version, build) {
        case let (.some(version), .some(build)):
            "Version \(version), build \(build)"
        case let (.some(version), .none):
            "Version \(version)"
        case let (.none, .some(build)):
            "Build \(build)"
        case (.none, .none):
            "Development build"
        }
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
