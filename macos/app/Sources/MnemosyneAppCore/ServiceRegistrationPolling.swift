import Foundation

public enum ManagedServiceRegistrationState: String, Equatable, Sendable {
    case notRegistered
    case enabled
    case requiresApproval
    case notFound
    case unknown
}

public enum ServiceRegistrationPollingError: Error, Equatable, LocalizedError, Sendable {
    case serviceNotFound(service: String)
    case timedOut(
        service: String,
        expected: String,
        lastState: ManagedServiceRegistrationState
    )

    public var errorDescription: String? {
        switch self {
        case let .serviceNotFound(service):
            "\(service) was not found in this application bundle. Reinstall Unified Inference and try again."
        case let .timedOut(service, expected, lastState):
            "\(service) did not reach \(expected) before the ServiceManagement check timed out (last state: \(lastState.rawValue)). Try again or review Login Items in System Settings."
        }
    }
}

public enum ServiceRegistrationPolling {
    public typealias StateProbe = @MainActor () -> ManagedServiceRegistrationState
    // Background Task Management can continue enforcing the previous helper's
    // launch requirement well after SMAppService reports `.notRegistered`.
    // On macOS 26 this has been observed for more than ten seconds during an
    // in-place Developer ID update; registering sooner makes launchd kill the
    // new helper with a Launch Constraint Violation. Restarts are rare and a
    // bounded delay is preferable to a crash/throttle/retry loop.
    public static let reregistrationSettleDuration: Duration = .seconds(20)

    @MainActor
    public static func waitUntilUnregistered(
        service: String,
        timeout: Duration = .seconds(10),
        pollInterval: Duration = .milliseconds(100),
        state: @escaping StateProbe
    ) async throws -> ManagedServiceRegistrationState {
        try await wait(
            service: service,
            expected: "the disabled state",
            timeout: timeout,
            pollInterval: pollInterval,
            state: state
        ) { $0 == .notRegistered }
    }

    @MainActor
    public static func waitUntilRegistered(
        service: String,
        timeout: Duration = .seconds(10),
        pollInterval: Duration = .milliseconds(100),
        state: @escaping StateProbe
    ) async throws -> ManagedServiceRegistrationState {
        try await wait(
            service: service,
            expected: "an enabled or approval-required state",
            timeout: timeout,
            pollInterval: pollInterval,
            state: state
        ) { $0 == .enabled || $0 == .requiresApproval }
    }

    /// `SMAppService.unregister(completionHandler:)` can report completion
    /// before Background Task Management has finished invalidating the old
    /// launch requirement. Give macOS one full run-loop turn plus a bounded
    /// settling interval before registering changed code again.
    @MainActor
    public static func waitUntilSafeToReregister(
        service: String,
        settleDuration: Duration = reregistrationSettleDuration,
        state: @escaping StateProbe
    ) async throws {
        await Task.yield()
        let clock = ContinuousClock()
        try await clock.sleep(for: settleDuration)

        let lastState = state()
        if lastState == .notFound {
            throw ServiceRegistrationPollingError.serviceNotFound(service: service)
        }
        guard lastState == .notRegistered else {
            throw ServiceRegistrationPollingError.timedOut(
                service: service,
                expected: "a stable disabled state safe for re-registration",
                lastState: lastState
            )
        }
    }

    @MainActor
    private static func wait(
        service: String,
        expected: String,
        timeout: Duration,
        pollInterval: Duration,
        state: @escaping StateProbe,
        accepts: (ManagedServiceRegistrationState) -> Bool
    ) async throws -> ManagedServiceRegistrationState {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        var lastState = state()

        while true {
            try Task.checkCancellation()
            if accepts(lastState) {
                return lastState
            }
            if lastState == .notFound {
                throw ServiceRegistrationPollingError.serviceNotFound(service: service)
            }

            let now = clock.now
            guard now < deadline else {
                throw ServiceRegistrationPollingError.timedOut(
                    service: service,
                    expected: expected,
                    lastState: lastState
                )
            }
            try await clock.sleep(
                until: min(now.advanced(by: pollInterval), deadline)
            )
            lastState = state()
        }
    }
}
