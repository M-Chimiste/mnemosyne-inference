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

@MainActor
@Test("Re-registration settling refuses a service that became registered again")
func reregistrationSettlingRequiresStableDisabledState() async {
    var timeoutDescription: String?
    do {
        try await ServiceRegistrationPolling.waitUntilSafeToReregister(
            service: "Background service",
            settleDuration: .zero
        ) {
            .enabled
        }
    } catch let error as ServiceRegistrationPollingError {
        timeoutDescription = error.localizedDescription
    } catch {
        timeoutDescription = error.localizedDescription
    }

    #expect(timeoutDescription?.contains("stable disabled state") == true)
}

@MainActor
@Test("Re-registration settling accepts a service that stays disabled")
func reregistrationSettlingAcceptsDisabledState() async throws {
    try await ServiceRegistrationPolling.waitUntilSafeToReregister(
        service: "Background service",
        settleDuration: .zero
    ) {
        .notRegistered
    }
}
