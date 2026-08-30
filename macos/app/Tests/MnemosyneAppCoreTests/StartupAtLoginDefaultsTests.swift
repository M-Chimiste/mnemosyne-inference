import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Fresh setup enables both login registrations exactly once")
func freshSetupStartupDefaultsAreOneShot() {
    let suite = "StartupAtLoginDefaultsTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }

    #expect(
        StartupAtLoginDefaults.pendingAction(
            defaults: defaults,
            guidedSetupCompleted: false
        ) == .enableBoth
    )
    StartupAtLoginDefaults.markApplied(defaults: defaults)
    #expect(
        StartupAtLoginDefaults.pendingAction(
            defaults: defaults,
            guidedSetupCompleted: false
        ) == .none
    )
}

@Test("Existing setup preserves its explicit login choices")
func existingSetupStartupDefaultsDoNotRestoreRegistrations() {
    let suite = "StartupAtLoginDefaultsTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }

    #expect(
        StartupAtLoginDefaults.pendingAction(
            defaults: defaults,
            guidedSetupCompleted: true
        ) == .preserveExistingChoice
    )
    StartupAtLoginDefaults.markApplied(defaults: defaults)
    #expect(
        StartupAtLoginDefaults.pendingAction(
            defaults: defaults,
            guidedSetupCompleted: true
        ) == .none
    )
}
