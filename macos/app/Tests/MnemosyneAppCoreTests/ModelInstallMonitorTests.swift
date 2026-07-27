import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Install monitoring reports profile registration transitions")
func installMonitorDetectsInstalledTransition() {
    let queued = modelInstall(id: "one", alias: "model-one", status: "queued")
    var monitor = ModelInstallMonitorState(installs: [queued])

    let downloading = monitor.observe([
        modelInstall(id: "one", alias: "model-one", status: "downloading"),
    ])
    #expect(downloading.hasActiveInstalls)
    #expect(downloading.newlyInstalledAliases.isEmpty)

    let installed = monitor.observe([
        modelInstall(id: "one", alias: "model-one", status: "installed"),
    ])
    #expect(!installed.hasActiveInstalls)
    #expect(installed.newlyInstalledAliases == ["model-one"])

    let unchanged = monitor.observe([
        modelInstall(id: "one", alias: "model-one", status: "installed"),
    ])
    #expect(!unchanged.hasActiveInstalls)
    #expect(unchanged.newlyInstalledAliases.isEmpty)
}

@Test("Install monitoring treats failed downloads as terminal without registration")
func installMonitorDetectsFailedTerminalState() {
    let queued = modelInstall(id: "one", alias: "model-one", status: "queued")
    var monitor = ModelInstallMonitorState(installs: [queued])

    let observation = monitor.observe([
        modelInstall(id: "one", alias: "model-one", status: "failed"),
    ])

    #expect(!observation.hasActiveInstalls)
    #expect(observation.newlyInstalledAliases.isEmpty)
}

private func modelInstall(
    id: String,
    alias: String,
    status: String
) -> ModelInstall {
    ModelInstall(
        id: id,
        repoId: "org/repo",
        engine: .llamaCpp,
        storage: "internal",
        alias: alias,
        destination: "/models/\(alias)",
        status: status,
        revision: nil,
        filename: "\(alias).gguf",
        projectorFilename: nil,
        contextLength: nil,
        downloadFiles: nil,
        capabilities: nil,
        family: nil,
        bytesDownloaded: 0,
        totalBytes: nil,
        downloadSpeedBps: nil,
        error: nil,
        pid: nil,
        createdAt: 0,
        updatedAt: 0
    )
}
