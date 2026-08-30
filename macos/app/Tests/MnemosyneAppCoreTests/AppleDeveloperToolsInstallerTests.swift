import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("The DS4 prerequisite uses only Apple's fixed GUI installer command")
func appleDeveloperToolsCommandIsBounded() {
    #expect(AppleDeveloperToolsInstaller.executableURL.path == "/usr/bin/xcode-select")
    #expect(
        AppleDeveloperToolsInstaller.compilerProbeExecutableURL.path
            == "/usr/bin/xcrun"
    )
    #expect(AppleDeveloperToolsInstaller.statusArguments == ["--print-path"])
    #expect(
        AppleDeveloperToolsInstaller.compilerProbeArguments == ["--find", "clang"]
    )
    #expect(AppleDeveloperToolsInstaller.installArguments == ["--install"])
}

@Test("The developer-tools runner rejects shells and arbitrary arguments")
func appleDeveloperToolsRunnerRejectsArbitraryCommands() async {
    await #expect(throws: AppleDeveloperToolsInstallerError.self) {
        _ = try await AppleDeveloperToolsInstaller.run(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["--install"]
        )
    }
    await #expect(throws: AppleDeveloperToolsInstallerError.self) {
        _ = try await AppleDeveloperToolsInstaller.run(
            executableURL: AppleDeveloperToolsInstaller.executableURL,
            arguments: ["--switch", "/tmp/toolchain"]
        )
    }
}

@Test("The developer-tools child is killed and reaped at its deadline")
func appleDeveloperToolsRunnerTimesOut() async {
    let clock = ContinuousClock()
    let started = clock.now

    await #expect(
        throws: AppleDeveloperToolsInstallerError.timedOut
    ) {
        _ = try await AppleDeveloperToolsInstaller.runLaunchedProcess(
            executableURL: URL(fileURLWithPath: "/bin/sleep"),
            arguments: ["5"],
            timeout: .milliseconds(50)
        )
    }

    #expect(started.duration(to: clock.now) < .seconds(1))
}

@Test("Cancelling the developer-tools task kills and reaps its exact child")
func appleDeveloperToolsRunnerHonorsCancellation() async throws {
    let clock = ContinuousClock()
    let task = Task {
        try await AppleDeveloperToolsInstaller.runLaunchedProcess(
            executableURL: URL(fileURLWithPath: "/bin/sleep"),
            arguments: ["5"],
            timeout: .seconds(10)
        )
    }
    try await Task.sleep(for: .milliseconds(50))
    let cancelledAt = clock.now
    task.cancel()

    await #expect(throws: CancellationError.self) {
        _ = try await task.value
    }
    #expect(cancelledAt.duration(to: clock.now) < .seconds(1))
}
