import Foundation

public enum ModelRuntimePreparationAction: Equatable, Sendable {
    case none
    case refresh
    case installManaged(engine: InferenceEngine, version: String)
    case installDS4GLM53Preview(version: String, channel: String)
    case installAppleDeveloperTools
    case downloadOfficialOMLX(installerURL: String)
    case enableEngine
    case saveSettings
    case restartService
    case openOMLXApplication(path: String)
}

/// A model-first view of the runtime work needed before one selected model can
/// serve requests.
///
/// This plan is deliberately advisory. It never starts a model download,
/// changes the selected storage location, enables an engine, or mutates a
/// runtime. The menu app must present the action and let the user initiate it.
public struct ModelRuntimePreparation: Equatable, Sendable {
    public let engine: InferenceEngine
    public let ready: Bool
    public let title: String
    public let detail: String
    public let actionLabel: String?
    public let action: ModelRuntimePreparationAction

    public init(
        engine: InferenceEngine,
        ready: Bool,
        title: String,
        detail: String,
        actionLabel: String?,
        action: ModelRuntimePreparationAction
    ) {
        self.engine = engine
        self.ready = ready
        self.title = title
        self.detail = detail
        self.actionLabel = actionLabel
        self.action = action
    }
}

public enum ModelRuntimePreparationPlanner {
    public static func plan(
        engine: InferenceEngine,
        family: String?,
        engineEnabled: Bool,
        runtimeUpdates: RuntimeUpdateSnapshot?,
        readiness: EngineReadiness?,
        restartRequired: Bool,
        engineEnablePendingSave: Bool = false,
        appleDeveloperToolsInstalled: Bool? = nil
    ) -> ModelRuntimePreparation {
        guard let update = runtimeUpdates?.engines.first(where: {
            $0.engine == engine
        }) else {
            return preparation(
                engine: engine,
                title: "Check the \(engine.displayName) runtime",
                detail: "Verify the official runtime before relying on this model for inference. Downloading weights remains a separate action and keeps the folder selected below.",
                actionLabel: "Check Runtime",
                action: .refresh
            )
        }

        if engine == .ds4 {
            let isGLM53Preview = family == "glm-5.3-flash"
            let expectedChannel = isGLM53Preview
                ? ManagedRuntimeChannel.ds4GLM53FlashChannel
                : "official"
            if update.installedChannel != expectedChannel {
                if appleDeveloperToolsInstalled == nil {
                    return preparation(
                        engine: engine,
                        title: "Check Apple's DS4 build tools",
                        detail: "DS4 is built from an exact official commit. Verify Apple's compiler tools before preparing its runtime; no Terminal command is required.",
                        actionLabel: "Check Prerequisites",
                        action: .refresh
                    )
                }
                if appleDeveloperToolsInstalled == false {
                    return preparation(
                        engine: engine,
                        title: "Install Apple's DS4 build tools",
                        detail: "DS4 needs Apple's Command Line Tools. Unified Inference can open the fixed macOS installer dialog without using a shell or changing model storage.",
                        actionLabel: "Install Apple Tools…",
                        action: .installAppleDeveloperTools
                    )
                }
                if isGLM53Preview,
                   let channel = update.ds4GLM53FlashPreview,
                   channel.canInstall,
                   let version = nonempty(channel.availableVersion) {
                    return preparation(
                        engine: engine,
                        title: "Prepare the exact GLM 5.3 runtime",
                        detail: "This model requires DS4's official experimental glm-5.3-flash source channel. Runtime activation does not download, move, or load model weights.",
                        actionLabel: "Install Preview Runtime…",
                        action: .installDS4GLM53Preview(
                            version: version,
                            channel: channel.channel
                        )
                    )
                }
                if !isGLM53Preview,
                   update.canInstall,
                   let version = nonempty(update.availableVersion) {
                    return managedInstall(update, version: version)
                }
                let diagnostic = isGLM53Preview
                    ? update.ds4GLM53FlashPreview?.diagnostic
                    : update.diagnostic
                return unavailable(
                    engine: engine,
                    detail: diagnostic
                        ?? "The selected model does not match the active DS4 source channel. Check official runtime availability before downloading it."
                )
            }
        }

        if !update.installed {
            if engine == .omlx,
               let installerURL = approvedOMLXInstallerURL(
                   update.officialInstallerUrl
               ) {
                return preparation(
                    engine: engine,
                    title: "Install the official oMLX app",
                    detail: "This MLX model uses the externally owned oMLX app. Install it, complete its welcome flow, and start its loopback server; Unified Inference will keep using the exact model folder selected below.",
                    actionLabel: "Download Official oMLX",
                    action: .downloadOfficialOMLX(installerURL: installerURL)
                )
            }
            if update.canInstall,
               let version = nonempty(update.availableVersion) {
                return managedInstall(update, version: version)
            }
            return unavailable(
                engine: engine,
                detail: update.diagnostic
                    ?? "No verified install action is currently available for this runtime. Check again before relying on the model for inference."
            )
        }

        if !engineEnabled {
            return preparation(
                engine: engine,
                title: "Enable \(engine.displayName)",
                detail: "The runtime is installed, but this engine is disabled. Enabling it changes only engine availability; it does not move weights or alter the selected storage folder.",
                actionLabel: "Enable Engine",
                action: .enableEngine
            )
        }

        if engineEnablePendingSave {
            return preparation(
                engine: engine,
                title: "Save to enable \(engine.displayName)",
                detail: "The engine is enabled only in the current Settings draft. Save that change before restarting the background service; model weights and their selected storage folder remain unchanged.",
                actionLabel: "Save Settings",
                action: .saveSettings
            )
        }

        guard let readiness, readiness.engine == engine else {
            return preparation(
                engine: engine,
                title: "Verify \(engine.displayName) health",
                detail: "The runtime is installed and enabled. Refresh health to prove that it can accept this model before the first request triggers JIT loading.",
                actionLabel: "Refresh Health",
                action: .refresh
            )
        }

        if !readiness.enabled {
            return preparation(
                engine: engine,
                title: "Restart to enable \(engine.displayName)",
                detail: restartRequired
                    ? "The engine setting is saved but has not reached the background service. Restarting applies it without loading or relocating model weights."
                    : "The running background service has not applied the enabled engine setting. Restart it before the first inference request.",
                actionLabel: "Restart Service",
                action: .restartService
            )
        }

        if readiness.ready {
            return ModelRuntimePreparation(
                engine: engine,
                ready: true,
                title: "\(engine.displayName) is ready",
                detail: "The selected model can remain cold after download. Its first request will acquire the normal residency lease and JIT-load it.",
                actionLabel: nil,
                action: .none
            )
        }

        if engine == .omlx,
           let path = update.installedPath,
           path.hasSuffix(".app") {
            return preparation(
                engine: engine,
                title: "Start the oMLX server",
                detail: readiness.diagnostic
                    ?? "The official app is installed, but its authenticated loopback service is not ready on the configured address.",
                actionLabel: "Open oMLX",
                action: .openOMLXApplication(path: path)
            )
        }

        return unavailable(
            engine: engine,
            detail: readiness.diagnostic
                ?? "The runtime is installed and enabled, but its authoritative health check is not ready. Resolve that state before the first inference request."
        )
    }

    private static func managedInstall(
        _ update: EngineRuntimeUpdate,
        version: String
    ) -> ModelRuntimePreparation {
        preparation(
            engine: update.engine,
            title: "Install the official \(update.displayName) runtime",
            detail: "Unified Inference can download, verify, stage, and activate this runtime. This does not download, move, or load model weights.",
            actionLabel: "Install Runtime",
            action: .installManaged(engine: update.engine, version: version)
        )
    }

    private static func unavailable(
        engine: InferenceEngine,
        detail: String
    ) -> ModelRuntimePreparation {
        preparation(
            engine: engine,
            title: "\(engine.displayName) needs attention",
            detail: detail,
            actionLabel: "Check Again",
            action: .refresh
        )
    }

    private static func preparation(
        engine: InferenceEngine,
        title: String,
        detail: String,
        actionLabel: String?,
        action: ModelRuntimePreparationAction
    ) -> ModelRuntimePreparation {
        ModelRuntimePreparation(
            engine: engine,
            ready: false,
            title: title,
            detail: detail,
            actionLabel: actionLabel,
            action: action
        )
    }

    private static func nonempty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }

    /// Accept only the exact official release-asset shape produced by the
    /// native service. This keeps a compromised/stale payload from turning a
    /// model card into a general-purpose external URL launcher.
    private static func approvedOMLXInstallerURL(_ value: String?) -> String? {
        guard
            let value,
            let url = URL(string: value),
            url.scheme == "https",
            url.host?.lowercased() == "github.com",
            url.path.hasPrefix("/jundot/omlx/releases/download/"),
            url.path.lowercased().hasSuffix(".dmg"),
            url.user == nil,
            url.password == nil,
            url.port == nil,
            url.query == nil,
            url.fragment == nil
        else { return nil }
        return value
    }
}
