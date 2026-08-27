import Foundation

public enum ServiceRestartConfirmationError: Error, Equatable, LocalizedError, Sendable {
    case timedOut(
        expectedRevision: String,
        persistedRevision: String?,
        appliedRevision: String?,
        lastServiceError: String?
    )

    public var errorDescription: String? {
        switch self {
        case let .timedOut(
            expectedRevision,
            persistedRevision,
            appliedRevision,
            lastServiceError
        ):
            var details = [
                "expected \(expectedRevision)",
                "saved \(persistedRevision ?? "unavailable")",
                "applied \(appliedRevision ?? "unavailable")",
            ]
            if let lastServiceError, !lastServiceError.isEmpty {
                details.append("last control error: \(lastServiceError)")
            }
            return """
            The background service was re-registered, but it did not confirm the saved \
            configuration before the restart check timed out. The save itself is durable. \
            Refresh Settings; restart again only if it still reports a pending restart. \
            Details: \(details.joined(separator: "; ")).
            """
        }
    }
}

public enum ServiceRestartConfirmation {
    public typealias ConfigurationProbe =
        @Sendable () async throws -> ConfigurationSnapshot

    /// Wait until the restarted control service reports the exact persisted
    /// revision as active. A stale-but-healthy process is not sufficient:
    /// both revision fields must match the revision saved by this app and the
    /// service must no longer report a pending restart.
    public static func waitForAppliedConfiguration(
        expectedRevision: String,
        timeout: Duration = .seconds(90),
        pollInterval: Duration = .milliseconds(500),
        probe: @escaping ConfigurationProbe
    ) async throws -> ConfigurationSnapshot {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        var lastSnapshot: ConfigurationSnapshot?
        var lastServiceError: String?

        while true {
            try Task.checkCancellation()
            do {
                let snapshot = try await probe()
                lastSnapshot = snapshot
                lastServiceError = nil
                if snapshot.revision == expectedRevision,
                   snapshot.appliedRevision == snapshot.revision,
                   !snapshot.restartRequired {
                    return snapshot
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                lastServiceError = error.localizedDescription
            }

            let now = clock.now
            guard now < deadline else {
                throw ServiceRestartConfirmationError.timedOut(
                    expectedRevision: expectedRevision,
                    persistedRevision: lastSnapshot?.revision,
                    appliedRevision: lastSnapshot?.appliedRevision,
                    lastServiceError: lastServiceError
                )
            }
            let nextAttempt = min(
                now.advanced(by: pollInterval),
                deadline
            )
            try await clock.sleep(until: nextAttempt)
        }
    }
}
