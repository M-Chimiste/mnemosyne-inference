import Foundation
import Testing
@testable import MnemosyneAppCore

@MainActor
@Test("Guided setup evidence requires presentation before durable completion")
func guidedSetupEvidenceRecordsFirstPresentationAndCompletion() {
    let suite = "GuidedSetupEvidenceTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer {
        defaults.removePersistentDomain(forName: suite)
    }

    GuidedSetupEvidenceStore.recordFirstPresentation(
        defaults: defaults,
        version: "0.9.0",
        build: "45",
        now: Date(timeIntervalSince1970: 100)
    )
    GuidedSetupEvidenceStore.recordFirstPresentation(
        defaults: defaults,
        version: "0.9.0",
        build: "45",
        now: Date(timeIntervalSince1970: 200)
    )

    let presented = GuidedSetupEvidenceStore.snapshot(defaults: defaults)
    #expect(!presented.completed)
    #expect(presented.firstPresentedVersion == "0.9.0")
    #expect(presented.firstPresentedBuild == "45")
    #expect(presented.firstPresentedAt == 100)
    #expect(presented.completedAt == nil)

    GuidedSetupEvidenceStore.recordFirstPresentation(
        defaults: defaults,
        version: "0.9.0",
        build: "46",
        now: Date(timeIntervalSince1970: 250)
    )

    let newerCandidate = GuidedSetupEvidenceStore.snapshot(defaults: defaults)
    #expect(newerCandidate.firstPresentedVersion == "0.9.0")
    #expect(newerCandidate.firstPresentedBuild == "46")
    #expect(newerCandidate.firstPresentedAt == 250)

    GuidedSetupEvidenceStore.recordCompletion(
        defaults: defaults,
        version: "0.9.0",
        build: "46",
        now: Date(timeIntervalSince1970: 300)
    )

    let completed = GuidedSetupEvidenceStore.snapshot(defaults: defaults)
    #expect(completed.completed)
    #expect(completed.completedVersion == "0.9.0")
    #expect(completed.completedBuild == "46")
    #expect(completed.completedAt == 300)
}
