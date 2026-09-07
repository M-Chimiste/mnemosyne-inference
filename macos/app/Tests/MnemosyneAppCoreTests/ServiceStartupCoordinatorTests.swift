import Foundation
import MnemosyneAppCore
import Testing

@Test("Startup keeps transient connection failures quiet until connected")
@MainActor func startupRetriesConnection() async {
    var probes = 0
    var pauses = 0
    let startup = ServiceStartupCoordinator(maxAttempts: 3, probe: {
        probes += 1
        if probes < 3 { throw URLError(.cannotConnectToHost) }
    }, pause: { pauses += 1 })
    #expect(startup.state == .preparing)
    await startup.connect(registration: .enabled)
    #expect(probes == 3)
    #expect(pauses == 2)
    #expect(startup.state == .ready)
}

@Test("Startup stops after bounded retries and can recover on explicit retry")
@MainActor func startupTimeoutAndRetry() async {
    var unavailable = true
    var probes = 0
    let startup = ServiceStartupCoordinator(maxAttempts: 2, probe: {
        probes += 1
        if unavailable { throw URLError(.timedOut) }
    }, pause: {})
    await startup.connect(registration: .enabled)
    #expect(probes == 2)
    guard case .failed = startup.state else {
        Issue.record("Expected one startup recovery message")
        return
    }
    unavailable = false
    await startup.connect(registration: .enabled)
    #expect(startup.state == .ready)
}

@Test("Disabled and approval-required services do not generate connection errors")
@MainActor func startupRespectsRegistration() async {
    var probes = 0
    let startup = ServiceStartupCoordinator(probe: { probes += 1 })
    await startup.connect(registration: .notRegistered)
    #expect(startup.state == .disabled)
    await startup.connect(registration: .requiresApproval)
    #expect(startup.state == .requiresApproval)
    #expect(probes == 0)
}

@Test("Authentication and malformed responses fail immediately instead of being hidden")
@MainActor func startupFailsNonTransientErrors() async {
    for error in [ControlAPIError.unexpectedStatus(401), .rejected(403, "forbidden"),
                  .invalidResponse, .unexpectedStatus(500)] {
        var probes = 0
        let startup = ServiceStartupCoordinator(probe: {
            probes += 1
            throw error
        }, pause: { Issue.record("Should not retry a permanent failure") })
        await startup.connect(registration: .enabled)
        #expect(probes == 1)
        guard case .failed = startup.state else {
            Issue.record("Expected immediate failure")
            continue
        }
    }
}

@Test("Startup retries temporary HTTP unavailability")
@MainActor func startupRetriesUnavailableHTTP() async {
    var probes = 0
    let startup = ServiceStartupCoordinator(probe: {
        probes += 1
        if probes == 1 { throw ControlAPIError.unexpectedStatus(503) }
    }, pause: {})
    await startup.connect(registration: .enabled)
    #expect(startup.state == .ready)
    #expect(probes == 2)
}

@Test("An older probe cannot declare readiness after a new registration transition")
@MainActor func startupFencesOlderProbe() async {
    var resume: CheckedContinuation<Void, Never>?
    let startup = ServiceStartupCoordinator(probe: {
        await withCheckedContinuation { resume = $0 }
    })
    let task = Task { await startup.connect(registration: .enabled) }
    while resume == nil { await Task.yield() }
    startup.prepare()
    resume?.resume()
    await task.value
    #expect(startup.state == .preparing)
    await startup.connect(registration: .notRegistered)
    #expect(startup.state == .disabled)
}
