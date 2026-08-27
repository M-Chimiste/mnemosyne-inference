import Foundation
import Testing
@testable import MnemosyneAppCore

private actor RestartProbeSequence {
    enum Step: Sendable {
        case unavailable
        case snapshot(ConfigurationSnapshot)
    }

    private var steps: [Step]
    private(set) var probeCount = 0

    init(_ steps: [Step]) {
        self.steps = steps
    }

    func next() throws -> ConfigurationSnapshot {
        probeCount += 1
        let step = steps.isEmpty ? .unavailable : steps.removeFirst()
        switch step {
        case .unavailable:
            throw URLError(.cannotConnectToHost)
        case let .snapshot(snapshot):
            return snapshot
        }
    }
}

@Test("Restart confirmation waits for the exact saved revision to become active")
func restartConfirmationWaitsForExactAppliedRevision() async throws {
    let expectedRevision = String(repeating: "e", count: 64)
    let staleRevision = String(repeating: "s", count: 64)
    let sequence = RestartProbeSequence([
        .unavailable,
        .snapshot(
            ConfigurationSnapshot(
                config: NativeSettings(),
                revision: staleRevision,
                appliedRevision: staleRevision
            )
        ),
        .snapshot(
            ConfigurationSnapshot(
                config: NativeSettings(),
                revision: expectedRevision,
                appliedRevision: staleRevision,
                restartRequired: true
            )
        ),
        .snapshot(
            ConfigurationSnapshot(
                config: NativeSettings(),
                revision: expectedRevision,
                appliedRevision: expectedRevision
            )
        ),
    ])

    let result = try await ServiceRestartConfirmation.waitForAppliedConfiguration(
        expectedRevision: expectedRevision,
        timeout: .seconds(1),
        pollInterval: .zero
    ) {
        try await sequence.next()
    }

    #expect(result.revision == expectedRevision)
    #expect(result.appliedRevision == expectedRevision)
    #expect(await sequence.probeCount == 4)
}

@Test("Restart confirmation timeout retains actionable revision diagnostics")
func restartConfirmationTimeoutIsActionable() async {
    let expectedRevision = String(repeating: "e", count: 64)
    let staleRevision = String(repeating: "s", count: 64)
    let diagnostic = ServiceRestartConfirmationError.timedOut(
        expectedRevision: expectedRevision,
        persistedRevision: expectedRevision,
        appliedRevision: staleRevision,
        lastServiceError: nil
    ).localizedDescription
    #expect(diagnostic.contains("save itself is durable"))
    #expect(diagnostic.contains("restart again only if"))
    #expect(diagnostic.contains("applied \(staleRevision)"))

    let sequence = RestartProbeSequence([
        .snapshot(
            ConfigurationSnapshot(
                config: NativeSettings(),
                revision: expectedRevision,
                appliedRevision: staleRevision,
                restartRequired: true
            )
        ),
    ])

    await #expect(throws: ServiceRestartConfirmationError.self) {
        _ = try await ServiceRestartConfirmation.waitForAppliedConfiguration(
            expectedRevision: expectedRevision,
            timeout: .zero,
            pollInterval: .zero
        ) {
            try await sequence.next()
        }
    }
}
