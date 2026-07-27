import Foundation

public struct GuidedSetupEvidence: Equatable, Sendable {
    public let completed: Bool
    public let firstPresentedVersion: String?
    public let firstPresentedBuild: String?
    public let firstPresentedAt: TimeInterval?
    public let completedVersion: String?
    public let completedBuild: String?
    public let completedAt: TimeInterval?
}

@MainActor
public enum GuidedSetupEvidenceStore {
    public static let completionKey = "didCompleteNativeSetupV1"
    public static let firstPresentedVersionKey =
        "nativeSetupFirstPresentedVersionV1"
    public static let firstPresentedBuildKey =
        "nativeSetupFirstPresentedBuildV1"
    public static let firstPresentedAtKey = "nativeSetupFirstPresentedAtV1"
    public static let completedVersionKey = "nativeSetupCompletedVersionV1"
    public static let completedBuildKey = "nativeSetupCompletedBuildV1"
    public static let completedAtKey = "nativeSetupCompletedAtV1"

    public static func recordFirstPresentation(
        defaults: UserDefaults = .standard,
        version: String,
        build: String,
        now: Date = Date()
    ) {
        if defaults.string(forKey: firstPresentedVersionKey) == version,
           defaults.string(forKey: firstPresentedBuildKey) == build,
           defaults.object(forKey: firstPresentedAtKey) != nil {
            return
        }
        defaults.set(version, forKey: firstPresentedVersionKey)
        defaults.set(build, forKey: firstPresentedBuildKey)
        defaults.set(now.timeIntervalSince1970, forKey: firstPresentedAtKey)
    }

    public static func recordCompletion(
        defaults: UserDefaults = .standard,
        version: String,
        build: String,
        now: Date = Date()
    ) {
        defaults.set(true, forKey: completionKey)
        defaults.set(version, forKey: completedVersionKey)
        defaults.set(build, forKey: completedBuildKey)
        defaults.set(now.timeIntervalSince1970, forKey: completedAtKey)
    }

    public static func snapshot(
        defaults: UserDefaults = .standard
    ) -> GuidedSetupEvidence {
        GuidedSetupEvidence(
            completed: defaults.bool(forKey: completionKey),
            firstPresentedVersion: defaults.string(
                forKey: firstPresentedVersionKey
            ),
            firstPresentedBuild: defaults.string(
                forKey: firstPresentedBuildKey
            ),
            firstPresentedAt: number(
                defaults.object(forKey: firstPresentedAtKey)
            ),
            completedVersion: defaults.string(forKey: completedVersionKey),
            completedBuild: defaults.string(forKey: completedBuildKey),
            completedAt: number(defaults.object(forKey: completedAtKey))
        )
    }

    private static func number(_ value: Any?) -> TimeInterval? {
        (value as? NSNumber)?.doubleValue
    }
}
