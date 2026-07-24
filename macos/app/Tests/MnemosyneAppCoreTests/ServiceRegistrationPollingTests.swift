import Testing
@testable import MnemosyneAppCore

@MainActor
@Test("Unregister polling waits for the terminal disabled state")
func waitsForAsyncUnregisterTerminalState() async throws {
    var states: [ManagedServiceRegistrationState] = [
        .enabled,
        .enabled,
        .notRegistered,
    ]

    let result = try await ServiceRegistrationPolling.waitUntilUnregistered(
        service: "Background service",
        timeout: .seconds(1),
        pollInterval: .zero
    ) {
        states.isEmpty ? .notRegistered : states.removeFirst()
    }

    #expect(result == .notRegistered)
}

@MainActor
@Test("Approval-required is a terminal registered state")
func registeredApprovalStateIsTerminalAndVisible() async throws {
    let result = try await ServiceRegistrationPolling.waitUntilRegistered(
        service: "Background service",
        timeout: .zero,
        pollInterval: .zero
    ) {
        .requiresApproval
    }

    #expect(result == .requiresApproval)
}

@MainActor
@Test("Registration transition timeouts are actionable")
func transitionTimeoutIsActionable() async {
    var timeoutDescription: String?
    var unexpectedError: String?
    do {
        _ = try await ServiceRegistrationPolling.waitUntilUnregistered(
            service: "Background service",
            timeout: .zero,
            pollInterval: .zero
        ) {
            .enabled
        }
    } catch let error as ServiceRegistrationPollingError {
        timeoutDescription = error.localizedDescription
    } catch {
        unexpectedError = error.localizedDescription
    }

    #expect(unexpectedError == nil)
    #expect(timeoutDescription?.contains("disabled state") == true)
    #expect(timeoutDescription?.contains("Login Items") == true)
}
