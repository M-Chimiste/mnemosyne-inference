import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("A selected GGUF model offers the verified managed llama.cpp runtime")
func modelFirstLlamaRuntimeInstall() {
    let update = runtimeUpdate(
        engine: .llamaCpp,
        installed: false,
        availableVersion: "b7000",
        updateAvailable: true,
        canInstall: true
    )

    let plan = ModelRuntimePreparationPlanner.plan(
        engine: .llamaCpp,
        family: nil,
        engineEnabled: true,
        runtimeUpdates: snapshot(update),
        readiness: nil,
        restartRequired: false
    )

    #expect(!plan.ready)
    #expect(
        plan.action == .installManaged(engine: .llamaCpp, version: "b7000")
    )
}

@Test("A selected MLX model links only to the exact official oMLX DMG shape")
func modelFirstOMLXInstallerIsClosed() {
    let official = runtimeUpdate(
        engine: .omlx,
        installed: false,
        officialInstallerURL:
            "https://github.com/jundot/omlx/releases/download/v0.6.0/oMLX-0.6.0-macos26.dmg",
        updateAvailable: true
    )
    let officialPlan = ModelRuntimePreparationPlanner.plan(
        engine: .omlx,
        family: nil,
        engineEnabled: false,
        runtimeUpdates: snapshot(official),
        readiness: nil,
        restartRequired: false
    )
    #expect(
        officialPlan.action == .downloadOfficialOMLX(
            installerURL:
                "https://github.com/jundot/omlx/releases/download/v0.6.0/oMLX-0.6.0-macos26.dmg"
        )
    )

    let untrusted = runtimeUpdate(
        engine: .omlx,
        installed: false,
        officialInstallerURL: "https://downloads.example.test/oMLX.dmg",
        updateAvailable: true
    )
    let untrustedPlan = ModelRuntimePreparationPlanner.plan(
        engine: .omlx,
        family: nil,
        engineEnabled: false,
        runtimeUpdates: snapshot(untrusted),
        readiness: nil,
        restartRequired: false
    )
    #expect(untrustedPlan.action == .refresh)
    #expect(!untrustedPlan.ready)
}

@Test("GLM 5.3 never treats the ordinary DS4 channel as compatible")
func modelFirstGLM53RequiresExactDS4Channel() {
    let preview = ManagedRuntimeChannel(
        channel: ManagedRuntimeChannel.ds4GLM53FlashChannel,
        sourceBranch: ManagedRuntimeChannel.ds4GLM53FlashChannel,
        releaseTier: "experimental",
        availableVersion: "cccccccccccc",
        availableRevision: String(repeating: "c", count: 40),
        releaseNotesUrl: "https://github.com/antirez/ds4/commit/cccc",
        updateAvailable: true,
        canInstall: true,
        diagnostic: nil
    )
    let update = runtimeUpdate(
        engine: .ds4,
        installed: true,
        installedChannel: "official",
        updateAvailable: false,
        managedChannels: [preview]
    )

    let plan = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "glm-5.3-flash",
        engineEnabled: true,
        runtimeUpdates: snapshot(update),
        readiness: engineReadiness(engine: .ds4, ready: true),
        restartRequired: false,
        appleDeveloperToolsInstalled: true
    )

    #expect(
        plan.action == .installDS4GLM53Preview(
            version: "cccccccccccc",
            channel: ManagedRuntimeChannel.ds4GLM53FlashChannel
        )
    )
    #expect(!plan.ready)
}

@Test("A normal DS4 model switches away from an incompatible preview channel")
func modelFirstOrdinaryDS4RequiresOfficialChannel() {
    let update = runtimeUpdate(
        engine: .ds4,
        installed: true,
        installedChannel: ManagedRuntimeChannel.ds4GLM53FlashChannel,
        availableVersion: "dddddddddddd",
        updateAvailable: true,
        canInstall: true
    )

    let plan = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "deepseek-v4",
        engineEnabled: true,
        runtimeUpdates: snapshot(update),
        readiness: engineReadiness(engine: .ds4, ready: true),
        restartRequired: false,
        appleDeveloperToolsInstalled: true
    )

    #expect(
        plan.action == .installManaged(engine: .ds4, version: "dddddddddddd")
    )
}

@Test("DS4 requests Apple's GUI tools without treating the request as readiness")
func modelFirstDS4DeveloperToolsRemainEvidenceBased() {
    let update = runtimeUpdate(
        engine: .ds4,
        installed: false,
        availableVersion: "dddddddddddd",
        updateAvailable: true,
        canInstall: true
    )

    let unknown = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "deepseek-v4",
        engineEnabled: false,
        runtimeUpdates: snapshot(update),
        readiness: nil,
        restartRequired: false,
        appleDeveloperToolsInstalled: nil
    )
    #expect(unknown.action == .refresh)
    #expect(!unknown.ready)

    let missing = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "deepseek-v4",
        engineEnabled: false,
        runtimeUpdates: snapshot(update),
        readiness: nil,
        restartRequired: false,
        appleDeveloperToolsInstalled: false
    )
    #expect(missing.action == .installAppleDeveloperTools)
    #expect(!missing.ready)

    let installed = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "deepseek-v4",
        engineEnabled: false,
        runtimeUpdates: snapshot(update),
        readiness: nil,
        restartRequired: false,
        appleDeveloperToolsInstalled: true
    )
    #expect(
        installed.action
            == .installManaged(engine: .ds4, version: "dddddddddddd")
    )
}

@Test("Installed runtimes still require an explicit engine enable and restart")
func modelFirstEngineEnableAndRestartAreExplicit() {
    let update = runtimeUpdate(
        engine: .ds4,
        installed: true,
        installedChannel: "official",
        updateAvailable: false
    )
    let disabled = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "deepseek-v4",
        engineEnabled: false,
        runtimeUpdates: snapshot(update),
        readiness: engineReadiness(engine: .ds4, enabled: false, ready: false),
        restartRequired: false,
        appleDeveloperToolsInstalled: true
    )
    #expect(disabled.action == .enableEngine)

    let stagedButUnsaved = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "deepseek-v4",
        engineEnabled: true,
        runtimeUpdates: snapshot(update),
        readiness: engineReadiness(engine: .ds4, enabled: false, ready: false),
        restartRequired: false,
        engineEnablePendingSave: true,
        appleDeveloperToolsInstalled: true
    )
    #expect(stagedButUnsaved.action == .saveSettings)

    let pendingRestart = ModelRuntimePreparationPlanner.plan(
        engine: .ds4,
        family: "deepseek-v4",
        engineEnabled: true,
        runtimeUpdates: snapshot(update),
        readiness: engineReadiness(engine: .ds4, enabled: false, ready: false),
        restartRequired: true,
        appleDeveloperToolsInstalled: true
    )
    #expect(pendingRestart.action == .restartService)
}

@Test("A ready runtime describes cold JIT loading without taking an action")
func modelFirstReadyRuntimePreservesJITLoading() {
    let update = runtimeUpdate(
        engine: .llamaCpp,
        installed: true,
        updateAvailable: false
    )
    let plan = ModelRuntimePreparationPlanner.plan(
        engine: .llamaCpp,
        family: nil,
        engineEnabled: true,
        runtimeUpdates: snapshot(update),
        readiness: engineReadiness(engine: .llamaCpp, ready: true),
        restartRequired: false
    )

    #expect(plan.ready)
    #expect(plan.action == .none)
    #expect(plan.detail.contains("JIT"))
}

@Test("An installed official oMLX app can be opened when its server is stopped")
func modelFirstOMLXOpenApplication() {
    let update = runtimeUpdate(
        engine: .omlx,
        installed: true,
        installedPath: "/Applications/oMLX.app",
        updateAvailable: false
    )
    let plan = ModelRuntimePreparationPlanner.plan(
        engine: .omlx,
        family: nil,
        engineEnabled: true,
        runtimeUpdates: snapshot(update),
        readiness: engineReadiness(
            engine: .omlx,
            enabled: true,
            ready: false,
            diagnostic: "loopback service is stopped"
        ),
        restartRequired: false
    )

    #expect(
        plan.action == .openOMLXApplication(path: "/Applications/oMLX.app")
    )
}

private func snapshot(_ update: EngineRuntimeUpdate) -> RuntimeUpdateSnapshot {
    RuntimeUpdateSnapshot(
        channel: "official",
        manifestUrl: nil,
        checkedAt: 1,
        coreProtocol: 1,
        engines: [update]
    )
}

private func runtimeUpdate(
    engine: InferenceEngine,
    installed: Bool,
    installedPath: String? = nil,
    installedChannel: String? = nil,
    officialInstallerURL: String? = nil,
    availableVersion: String? = nil,
    updateAvailable: Bool,
    canInstall: Bool = false,
    managedChannels: [ManagedRuntimeChannel]? = nil
) -> EngineRuntimeUpdate {
    EngineRuntimeUpdate(
        engine: engine,
        releaseTier: engine == .llamaCpp || engine == .omlx
            ? "stable" : "preview",
        displayName: engine.displayName,
        ownership: engine == .omlx ? "external" : "managed_or_external",
        installed: installed,
        installedVersion: installed ? "installed" : nil,
        installedRevision: nil,
        installedPath: installedPath,
        installedChannel: installedChannel,
        installationKind: nil,
        upgradeStrategy: nil,
        latestUpstreamVersion: availableVersion,
        latestUpstreamRevision: nil,
        latestUpstreamUrl: nil,
        officialInstallerUrl: officialInstallerURL,
        availableVersion: availableVersion,
        availableRevision: nil,
        releaseNotesUrl: nil,
        updateAvailable: updateAvailable,
        canInstall: canInstall,
        canRollback: false,
        managementNote: "Official source.",
        diagnostic: nil,
        managedChannels: managedChannels
    )
}

private func engineReadiness(
    engine: InferenceEngine,
    enabled: Bool = true,
    ready: Bool,
    diagnostic: String? = nil
) -> EngineReadiness {
    EngineReadiness(
        engine: engine,
        releaseTier: engine == .llamaCpp || engine == .omlx
            ? "stable" : "preview",
        enabled: enabled,
        installed: true,
        installedVersion: "installed",
        installedPath: nil,
        serviceState: ready ? "ready" : "stopped",
        authoritative: true,
        residentModels: [],
        ready: ready,
        diagnostic: diagnostic
    )
}
