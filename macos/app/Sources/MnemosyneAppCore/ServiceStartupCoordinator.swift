import Combine
import Foundation

/// One startup gate for the menu and Settings. Only read-only connection probes
/// are retried; registration, inference, and user mutations are never replayed.
@MainActor
public final class ServiceStartupCoordinator: ObservableObject {
    public enum State: Equatable {
        case preparing
        case connecting
        case ready
        case disabled
        case requiresApproval
        case failed(String)

        public var isWaiting: Bool {
            self == .preparing || self == .connecting
        }

        public var message: String {
            switch self {
            case .preparing: "Preparing background services…"
            case .connecting: "Starting background service…"
            case .ready: "Connected"
            case .disabled: "The background service is disabled."
            case .requiresApproval: "Approve the background service in System Settings → General → Login Items."
            case let .failed(message): message
            }
        }
    }

    @Published public private(set) var state: State = .preparing
    private var generation = 0
    private let probe: @MainActor () async throws -> Void
    private let pause: @MainActor () async throws -> Void
    private let maxAttempts: Int

    public init(
        maxAttempts: Int = 30,
        probe: @escaping @MainActor () async throws -> Void,
        pause: @escaping @MainActor () async throws -> Void = {
            try await Task.sleep(for: .seconds(1))
        }
    ) {
        self.maxAttempts = max(1, maxAttempts)
        self.probe = probe
        self.pause = pause
    }

    public func prepare() {
        generation += 1
        state = .preparing
    }

    public func connect(registration: ManagedServiceRegistrationState) async {
        generation += 1
        let attemptGeneration = generation
        switch registration {
        case .notRegistered:
            state = .disabled
            return
        case .requiresApproval:
            state = .requiresApproval
            return
        case .notFound, .unknown:
            state = .failed("The background service registration is unavailable. Retry or open Login Items to check it.")
            return
        case .enabled:
            break
        }
        state = .connecting
        let deadline = ContinuousClock.now.advanced(by: .seconds(45))
        for attempt in 0 ..< maxAttempts {
            guard !Task.isCancelled, generation == attemptGeneration else { return }
            do {
                try await probe()
                guard !Task.isCancelled, generation == attemptGeneration else { return }
                state = .ready
                return
            } catch {
                guard !Task.isCancelled, generation == attemptGeneration else { return }
                guard Self.isTransient(error) else {
                    state = .failed(Self.failureMessage(error))
                    return
                }
            }
            if attempt + 1 == maxAttempts || ContinuousClock.now >= deadline { break }
            do {
                try await pause()
            } catch {
                return
            }
        }
        guard !Task.isCancelled, generation == attemptGeneration else { return }
        state = .failed("The background service is taking longer than expected to start. Retry the connection or open logs for details.")
    }

    private static func isTransient(_ error: any Error) -> Bool {
        if let error = error as? URLError {
            return [.cannotConnectToHost, .networkConnectionLost, .timedOut,
                    .notConnectedToInternet, .cannotFindHost].contains(error.code)
        }
        if let error = error as? ControlAPIError {
            switch error {
            case .unexpectedStatus(503), .rejected(503, _): return true
            default: return false
            }
        }
        return false
    }

    private static func failureMessage(_ error: any Error) -> String {
        if let error = error as? ControlAPIError {
            switch error {
            case .unexpectedStatus(401), .unexpectedStatus(403),
                 .rejected(401, _), .rejected(403, _):
                return "The background service rejected the app’s credentials. Check the control connection credentials before retrying."
            default: break
            }
        }
        return "Could not connect to the background service: \(error.localizedDescription)"
    }
}
