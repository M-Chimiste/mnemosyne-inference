import Foundation

public enum StartupAtLoginDefaultAction: Equatable, Sendable {
    case enableBoth
    case preserveExistingChoice
    case none
}

/// One-time policy for the app and service login registrations.
///
/// A genuinely fresh setup requests both registrations so Unified Inference
/// behaves like an ordinary always-available local inference app. Upgrades do
/// not reinterpret a missing registration as permission to restore it, and a
/// later explicit disable remains durable because the one-time marker stays
/// set.
public enum StartupAtLoginDefaults {
    public static let appliedKey = "didApplyStartupAtLoginDefaultsV1"

    public static func pendingAction(
        defaults: UserDefaults = .standard,
        guidedSetupCompleted: Bool
    ) -> StartupAtLoginDefaultAction {
        guard defaults.object(forKey: appliedKey) == nil else { return .none }
        return guidedSetupCompleted ? .preserveExistingChoice : .enableBoth
    }

    public static func markApplied(defaults: UserDefaults = .standard) {
        defaults.set(true, forKey: appliedKey)
    }
}
