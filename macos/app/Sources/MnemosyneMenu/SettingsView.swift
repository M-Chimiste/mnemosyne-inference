import Foundation
import MnemosyneAppCore
import SwiftUI

private enum ResidencyPreset: String, CaseIterable, Identifiable {
    case performance = "Performance"
    case balanced = "Balanced"
    case memorySaver = "Memory Saver"
    case custom = "Custom"

    var id: String { rawValue }
}

struct SettingsView: View {
    @ObservedObject var viewModel: SettingsViewModel
    @ObservedObject var registration: LaunchAgentRegistration
    let markSetupCompleted: () -> Void
    let restartService: () -> Void
    @State private var previewedCredentialDrafts: Set<ManagedCredential> = []
    @State private var selfTestAlias = ""
    @State private var alternativeSourceIndex: Int?
    private let productBuildIdentity = ProductBuildIdentity.current

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            VStack(spacing: 0) {
                header
                Divider()
                page
                Divider()
                footer
            }
        }
        .frame(minWidth: 900, minHeight: 650)
        .task {
            if !viewModel.isLoaded { await viewModel.load() }
        }
        .alert("Discard unsaved changes?", isPresented: $viewModel.confirmDiscard) {
            Button("Cancel", role: .cancel) {}
            Button("Discard Changes", role: .destructive) {
                viewModel.discardChanges()
            }
        } message: {
            Text("This restores every setting to the last saved value.")
        }
        .alert("Remove this model?", isPresented: $viewModel.confirmRemoveModel) {
            Button("Cancel", role: .cancel) {}
            Button("Keep Files", role: .destructive) {
                viewModel.removeSelectedModel()
            }
            Button("Delete Files", role: .destructive) {
                Task { await viewModel.deleteSelectedModelFiles() }
            }
        } message: {
            Text(
                "Keep Files removes only the profile after you save. Delete Files immediately removes the profile and its app-managed download. Finder imports and manually configured paths are never eligible for file deletion."
            )
        }
        .alert(
            "Install oMLX with Homebrew?",
            isPresented: $viewModel.confirmHomebrewOMLXInstall
        ) {
            Button("Cancel", role: .cancel) {}
            Button("Install Stable oMLX") {
                Task { await viewModel.installOMLXWithHomebrew() }
            }
        } message: {
            Text(
                "Unified Inference will run these exact commands after approval:\n\nbrew tap jundot/omlx https://github.com/jundot/omlx\nbrew install omlx\n\nHomebrew will own the installation. This never uses --HEAD or replaces an existing oMLX installation. The stable formula omits optional custom kernels; use the recommended official app when you need them."
            )
        }
        .alert(
            "Update oMLX with Homebrew?",
            isPresented: $viewModel.confirmHomebrewOMLXUpgrade
        ) {
            Button("Cancel", role: .cancel) {
                viewModel.pendingOMLXUpgrade = nil
            }
            Button("Drain and Update") {
                guard let update = viewModel.pendingOMLXUpgrade else { return }
                viewModel.pendingOMLXUpgrade = nil
                Task { await viewModel.installRuntimeUpdate(update) }
            }
        } message: {
            Text(
                "Unified Inference will stop new oMLX requests, drain active work, unload the resident model, and delegate these fixed operations to Homebrew:\n\nomlx stop\nbrew update\nbrew upgrade omlx\nomlx start\n\nAdmission reopens only after the updated oMLX control plane is healthy and empty."
            )
        }
        .alert(
            "Reset oMLX SSD cache?",
            isPresented: $viewModel.confirmOMLXCacheReset
        ) {
            Button("Cancel", role: .cancel) {}
            Button("Drain and Reset", role: .destructive) {
                Task { await viewModel.resetOMLXCache() }
            }
        } message: {
            Text(
                "Unified Inference will drain active requests and unload the resident model, then ask oMLX to delete its reusable SSD KV-cache blocks. Model weights and configuration are not changed. The next matching prompt will need a fresh prefill."
            )
        }
        .sheet(isPresented: $viewModel.showLocalModelImporter) {
            ExistingModelImporterView(viewModel: viewModel)
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("SETTINGS")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12)
                .padding(.top, 14)
                .padding(.bottom, 4)
            ForEach(SettingsViewModel.Section.allCases) { section in
                Button {
                    viewModel.selectedSection = section
                } label: {
                    Label(section.rawValue, systemImage: section.symbol)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .background(
                            RoundedRectangle(cornerRadius: 6)
                                .fill(
                                    viewModel.selectedSection == section
                                        ? Color.accentColor.opacity(0.18) : Color.clear
                                )
                        )
                }
                .buttonStyle(.plain)
            }
            Spacer()
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("Unified Inference")
                    .font(.caption)
                    .lineLimit(1)
                Spacer(minLength: 2)
                Text(productBuildIdentity.compactLabel)
                    .font(.caption2)
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                    .accessibilityLabel(productBuildIdentity.accessibilityLabel)
            }
            .foregroundStyle(.secondary)
            .help(productBuildIdentity.accessibilityLabel)
            .padding(12)
        }
        .frame(width: 180)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.55))
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(viewModel.selectedSection.rawValue)
                    .font(.title2.weight(.semibold))
                Text(sectionDescription)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if viewModel.isWorking {
                ProgressView().controlSize(.small)
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
    }

    @ViewBuilder
    private var page: some View {
        if !viewModel.isLoaded {
            VStack(spacing: 16) {
                Image(systemName: "gearshape.2")
                    .font(.system(size: 36))
                    .foregroundStyle(.secondary)
                Text(viewModel.statusMessage)
                    .foregroundStyle(viewModel.statusColor)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 520)
                if registration.agentStatus == .enabled {
                    HStack {
                        Button("Try Again") { Task { await viewModel.load() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(viewModel.isWorking)
                        Button("Restart Service") { restartService() }
                            .disabled(viewModel.isWorking)
                        Button("Open Logs") { openApplicationSupportLogs() }
                    }
                } else {
                    Text(
                        "Unified Inference needs its background service before Settings can inspect engines, storage, or models."
                    )
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 520)
                    if registration.agentStatus == .requiresApproval {
                        Button("Approve in Login Items") {
                            registration.openLoginItemsSettings()
                        }
                        .buttonStyle(.borderedProminent)
                    } else {
                        Button("Enable Background Service") {
                            Task { await enableServiceAndLoad() }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(registration.isChangingRegistration)
                    }
                }
                if let error = registration.lastError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                        .frame(maxWidth: 560)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(40)
        } else {
            switch viewModel.selectedSection {
            case .setup: setupPage
            case .general: generalPage
            case .engines: enginesPage
            case .updates: runtimeUpdatesPage
            case .storage: storagePage
            case .library: libraryPage
            case .models: modelsPage
            case .usage: usagePage
            case .credentials: credentialsPage
            }
        }
    }

    private var setupPage: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Get this Mac ready for inference")
                            .font(.title3.weight(.semibold))
                        Text(
                            "Complete the checks below, then run one real request through the public API. Advanced settings remain available in the sidebar."
                        )
                        .foregroundStyle(.secondary)
                        if let version = viewModel.readinessSnapshot?.productVersion {
                            Text("Core \(version)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    if viewModel.isRefreshingReadiness {
                        ProgressView().controlSize(.small)
                    }
                    Button("Refresh Health") {
                        Task { await viewModel.refreshReadiness() }
                    }
                    .disabled(viewModel.isRefreshingReadiness)
                }

                setupCard(
                    title: "1. Background service",
                    symbol: "bolt.horizontal.circle",
                    ready: registration.agentStatus == .enabled
                        && viewModel.readinessSnapshot?.core.ready == true
                ) {
                    LabeledContent(
                        "Registration",
                        value: registration.label(for: registration.agentStatus)
                    )
                    if let core = viewModel.readinessSnapshot?.core {
                        LabeledContent("Core state", value: core.state.capitalized)
                        if let diagnostic = core.diagnostic ?? core.startupError {
                            Label(diagnostic, systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                                .textSelection(.enabled)
                        }
                    }
                    HStack {
                        if registration.agentStatus != .enabled {
                            Button("Enable Service") {
                                Task { await enableServiceAndLoad() }
                            }
                            .buttonStyle(.borderedProminent)
                        }
                        Button("Restart Service") { restartService() }
                            .disabled(
                                registration.agentStatus != .enabled
                                    || viewModel.isWorking
                            )
                        Button("Reconcile Engines") {
                            Task { await viewModel.reconcileService() }
                        }
                        .disabled(
                            registration.agentStatus != .enabled
                                || viewModel.isReconciling
                        )
                    }
                }

                setupCard(
                    title: "2. Stable inference engines",
                    symbol: "cpu",
                    ready: stableEngineReady
                ) {
                    if let engines = viewModel.readinessSnapshot?.engines {
                        ForEach(engines) { engine in
                            engineHealthRow(engine)
                        }
                    } else {
                        Text("Refresh health to inspect installed runtimes.")
                            .foregroundStyle(.secondary)
                    }
                    HStack {
                        Button("Install or Update Runtimes") {
                            viewModel.selectedSection = .updates
                        }
                        Button("Configure Engines") {
                            viewModel.selectedSection = .engines
                        }
                    }
                    Text(
                        "llama.cpp and oMLX are the supported V1 engines. DS4 and MFLUX remain Preview until their remaining hardware acceptance gates close."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                setupCard(
                    title: "3. Model storage",
                    symbol: "externaldrive",
                    ready: storageReady
                ) {
                    if let storage = viewModel.readinessSnapshot?.storage {
                        ForEach(storage) { location in
                            HStack {
                                Label(
                                    location.name,
                                    systemImage: location.available && location.writable
                                        ? "checkmark.circle.fill"
                                        : "exclamationmark.triangle.fill"
                                )
                                .foregroundStyle(
                                    location.available && location.writable
                                        ? Color.green : Color.orange
                                )
                                Text(location.path)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                Spacer()
                                if let free = location.freeBytes {
                                    Text(
                                        ByteCountFormatter.string(
                                            fromByteCount: free,
                                            countStyle: .file
                                        ) + " free"
                                    )
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                }
                            }
                            if let diagnostic = location.diagnostic {
                                Text(diagnostic)
                                    .font(.caption)
                                    .foregroundStyle(.orange)
                            }
                        }
                    }
                    Button("Choose Model Storage") {
                        viewModel.selectedSection = .storage
                    }
                }

                setupCard(
                    title: "4. Models",
                    symbol: "shippingbox",
                    ready: (viewModel.readinessSnapshot?.models.callable ?? 0) > 0
                ) {
                    if let models = viewModel.readinessSnapshot?.models {
                        LabeledContent(
                            "Configured",
                            value: models.configured.formatted()
                        )
                        LabeledContent("Callable", value: models.callable.formatted())
                    }
                    HStack {
                        Button("Browse Model Library") {
                            viewModel.selectedSection = .library
                        }
                        Button("Add Existing Models…") {
                            Task { await viewModel.chooseExistingModelsFolder() }
                        }
                        .disabled(
                            viewModel.hasUnsavedChanges
                                || viewModel.isScanningLocalModels
                                || viewModel.isImportingLocalModels
                        )
                    }
                }

                setupCard(
                    title: "5. End-to-end verification",
                    symbol: "checkmark.seal",
                    ready: verifiedSelfTest
                ) {
                    if selfTestModels.isEmpty {
                        Text("Add a callable model before running verification.")
                            .foregroundStyle(.secondary)
                    } else {
                        Picker("Model", selection: $selfTestAlias) {
                            ForEach(selfTestModels, id: \.alias) { model in
                                Text("\(model.alias) · \(model.engine.displayName)")
                                    .tag(model.alias)
                            }
                        }
                        .onAppear(perform: selectDefaultSelfTestModel)
                        Button("Run Model Self-Test") {
                            Task {
                                let passed = await viewModel.runSelfTest(
                                    model: effectiveSelfTestAlias
                                )
                                if passed {
                                    markSetupCompleted()
                                }
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(
                            effectiveSelfTestAlias.isEmpty
                                || viewModel.isRunningSelfTest
                        )
                    }
                    if viewModel.isRunningSelfTest {
                        HStack {
                            ProgressView().controlSize(.small)
                            Text("Loading the model and waiting for a real response…")
                                .foregroundStyle(.secondary)
                        }
                    }
                    if let result = viewModel.lastSelfTest {
                        Label(
                            result.vision
                                ? "Vision request passed"
                                : "Inference request passed",
                            systemImage: "checkmark.circle.fill"
                        )
                        .foregroundStyle(.green)
                        LabeledContent(
                            "Response time",
                            value: Duration.milliseconds(result.responseMs).formatted(
                                .units(allowed: [.seconds, .milliseconds], width: .abbreviated)
                            )
                        )
                        if let usage = result.usage {
                            LabeledContent(
                                "Tokens",
                                value: "\(usage.promptTokens) prompt + \(usage.completionTokens) completion = \(usage.totalTokens)"
                            )
                            Label(
                                result.usageRecorded == true
                                    ? "Usage recorded durably"
                                    : "Usage was not recorded",
                                systemImage: result.usageRecorded == true
                                    ? "checkmark.circle.fill"
                                    : "exclamationmark.triangle.fill"
                            )
                            .foregroundStyle(
                                result.usageRecorded == true ? Color.green : Color.orange
                            )
                        }
                        if let preview = result.responsePreview {
                            Text(preview)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                }

                setupCard(
                    title: "Diagnostics",
                    symbol: "stethoscope",
                    ready: viewModel.readinessSnapshot?.core.ready == true
                ) {
                    if let usage = viewModel.readinessSnapshot?.usage {
                        LabeledContent("Usage identity", value: usage.nodeId)
                        LabeledContent(
                            "Postgres delivery",
                            value: usage.writerReady
                                ? "Ready · \(usage.outboxDepth) queued"
                                : "Not configured · local usage remains durable"
                        )
                        if let error = usage.lastError {
                            Text(error)
                                .font(.caption)
                                .foregroundStyle(.orange)
                                .textSelection(.enabled)
                        }
                    }
                    HStack {
                        Button("Copy Redacted Diagnostics") {
                            copyReadinessDiagnostics()
                        }
                        Button("Open Logs") {
                            openApplicationSupportLogs()
                        }
                    }
                }
            }
            .padding(22)
        }
    }

    private func setupCard<Content: View>(
        title: String,
        symbol: String,
        ready: Bool,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 11) {
            HStack {
                Label(title, systemImage: symbol)
                    .font(.headline)
                Spacer()
                Label(
                    ready ? "Ready" : "Needs attention",
                    systemImage: ready
                        ? "checkmark.circle.fill"
                        : "exclamationmark.circle.fill"
                )
                .font(.caption.weight(.semibold))
                .foregroundStyle(ready ? Color.green : Color.orange)
            }
            Divider()
            content()
        }
        .padding(15)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.secondary.opacity(0.16))
        )
    }

    private func engineHealthRow(_ engine: EngineReadiness) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Label(
                engine.engine.displayName,
                systemImage: engine.ready
                    ? "checkmark.circle.fill"
                    : engine.enabled ? "exclamationmark.triangle.fill" : "circle"
            )
            .foregroundStyle(
                engine.ready ? Color.green : engine.enabled ? Color.orange : Color.secondary
            )
            Text(engine.releaseTier.uppercased())
                .font(.caption2.weight(.bold))
                .foregroundStyle(engine.isStable ? Color.blue : Color.orange)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(
                    (engine.isStable ? Color.blue : Color.orange).opacity(0.12),
                    in: Capsule()
                )
            Spacer()
            Text(engineHealthLabel(engine))
                .foregroundStyle(.secondary)
            if let version = engine.installedVersion {
                Text(version)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func engineHealthLabel(_ engine: EngineReadiness) -> String {
        if !engine.enabled { return "Disabled" }
        if !engine.installed { return "Runtime not installed" }
        if engine.ready { return "Ready" }
        return engine.serviceState.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private var stableEngineReady: Bool {
        viewModel.readinessSnapshot?.engines.contains {
            $0.isStable && $0.enabled && $0.ready
        } == true
    }

    private var storageReady: Bool {
        viewModel.readinessSnapshot?.storage.contains {
            $0.available && $0.writable && $0.volumeMatches
        } == true
    }

    private var verifiedSelfTest: Bool {
        viewModel.lastSelfTest?.completesGuidedSetup == true
    }

    private var selfTestModels: [ModelProfileSettings] {
        viewModel.settings.models.filter {
            $0.enabled && $0.kind != .image && engineEnabled($0.engine)
        }
    }

    private func engineEnabled(_ engine: InferenceEngine) -> Bool {
        switch engine {
        case .llamaCpp:
            viewModel.settings.engines.llamaCpp.enabled
        case .omlx:
            viewModel.settings.engines.omlx.enabled
        case .ds4:
            viewModel.settings.engines.ds4.enabled
        case .mflux:
            viewModel.settings.engines.mflux.enabled
        case .mlxcel:
            viewModel.settings.engines.mlxcel.enabled
        case .mistralRs:
            viewModel.settings.engines.mistralRs.enabled
        }
    }

    private var effectiveSelfTestAlias: String {
        if selfTestModels.contains(where: { $0.alias == selfTestAlias }) {
            return selfTestAlias
        }
        return selfTestModels.first?.alias ?? ""
    }

    private func selectDefaultSelfTestModel() {
        if selfTestAlias.isEmpty {
            selfTestAlias = selfTestModels.first?.alias ?? ""
        }
    }

    private func enableServiceAndLoad() async {
        await registration.enableAgent()
        guard registration.agentStatus == .enabled else { return }
        for _ in 0 ..< 30 {
            await viewModel.load()
            if viewModel.isLoaded {
                await viewModel.refreshReadiness()
                return
            }
            try? await Task.sleep(for: .seconds(1))
        }
    }

    private func copyReadinessDiagnostics() {
        guard let snapshot = viewModel.readinessSnapshot else { return }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(snapshot),
              let value = String(data: data, encoding: .utf8)
        else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(value, forType: .string)
    }

    private func openApplicationSupportLogs() {
        let root = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0]
            .appending(path: "Mnemosyne", directoryHint: .isDirectory)
            .appending(path: "logs", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(
            at: root,
            withIntermediateDirectories: true
        )
        NSWorkspace.shared.open(root)
    }

    private var generalPage: some View {
        Form {
            Section("API addresses") {
                LabeledContent("Inference API") {
                    HStack {
                        Text(viewModel.settings.server.inferenceBind).foregroundStyle(.secondary)
                        Text(":")
                        TextField(
                            "Port",
                            value: $viewModel.settings.server.inferencePort,
                            format: .number.grouping(.never)
                        )
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 76)
                    }
                }
                LabeledContent("Control API") {
                    HStack {
                        Text(viewModel.settings.server.controlBind).foregroundStyle(.secondary)
                        Text(":")
                        TextField(
                            "Port",
                            value: $viewModel.settings.server.controlPort,
                            format: .number.grouping(.never)
                        )
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 76)
                    }
                }
                Text("Both services stay private to this Mac. Changing a port requires a restart.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Model residency") {
                Picker("Preset", selection: residencyPreset) {
                    ForEach(ResidencyPreset.allCases) { preset in
                        Text(preset.rawValue).tag(preset)
                    }
                }
                .pickerStyle(.segmented)
                Toggle("Unload the current model when it has been idle", isOn: idleUnloadEnabled)
                if viewModel.settings.server.idleUnloadSeconds != nil {
                    LabeledContent("Idle time") {
                        integerField(idleUnloadSeconds, unit: "seconds")
                    }
                }
                LabeledContent("Model startup timeout") {
                    secondsField($viewModel.settings.server.startupTimeoutSeconds)
                }
                LabeledContent("Queued request timeout") {
                    secondsField($viewModel.settings.server.swapQueueTimeoutSeconds)
                }
                optionalIntegerField(
                    "Concurrency ceiling",
                    value: $viewModel.settings.server.maxConcurrency
                )
                LabeledContent("Maximum queued requests") {
                    integerField(
                        $viewModel.settings.server.maxQueueDepth,
                        unit: "requests"
                    )
                }
                LabeledContent("Shutdown grace period") {
                    secondsField($viewModel.settings.server.shutdownGraceSeconds)
                }
                Text(
                    viewModel.settings.server.idleUnloadSeconds == nil
                        ? "Performance mode keeps one verified model warm, avoiding repeated weight loads and cache scans."
                        : "Memory-saver mode releases the resident model after the configured idle interval."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                Text("Leave the concurrency ceiling blank to use the engine's authoritative scheduler limit. A ceiling can reduce memory pressure; it cannot exceed what the engine reports.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Image generation") {
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.server.imageRequestTimeoutSeconds)
                }
                LabeledContent("Maximum image pixels") {
                    integerField(
                        $viewModel.settings.server.imageMaxPixels,
                        unit: "pixels",
                        fieldWidth: 120
                    )
                }
            }

            Section("Local storage") {
                TextField("State database", text: $viewModel.settings.paths.stateDatabase)
                TextField("Log folder", text: $viewModel.settings.paths.logDirectory)
            }
        }
        .formStyle(.grouped)
    }

    private var enginesPage: some View {
        Form {
            Section {
                Toggle(
                    "Enable manager-owned llama.cpp",
                    isOn: $viewModel.settings.engines.llamaCpp.enabled
                )
                LabeledContent("Local port") {
                    TextField(
                        "Port",
                        value: $viewModel.settings.engines.llamaCpp.port,
                        format: .number.grouping(.never)
                    )
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 80)
                }
                TextField(
                    "llama-server executable",
                    text: $viewModel.settings.engines.llamaCpp.binary
                )
                TextField(
                    "Working folder",
                    text: $viewModel.settings.engines.llamaCpp.workingDirectory
                )
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.engines.llamaCpp.requestTimeoutSeconds)
                }
                LabeledContent("Shutdown grace period") {
                    secondsField($viewModel.settings.engines.llamaCpp.shutdownGraceSeconds)
                }
                Text("Unified Inference starts and stops the official llama-server build itself. Install and update that runtime from Runtime Updates.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                engineHeader("llama.cpp", detail: "Native GGUF inference")
            }

            Section {
                Toggle("Enable oMLX", isOn: $viewModel.settings.engines.omlx.enabled)
                TextField("Local API address", text: $viewModel.settings.engines.omlx.baseUrl)
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.engines.omlx.requestTimeoutSeconds)
                }
            } header: {
                engineHeader("oMLX", detail: "Apple Silicon language models")
            }

            Section {
                Toggle("Enable DS4", isOn: $viewModel.settings.engines.ds4.enabled)
                LabeledContent("Local port") {
                    TextField(
                        "Port",
                        value: $viewModel.settings.engines.ds4.port,
                        format: .number.grouping(.never)
                    )
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 80)
                }
                TextField("DS4 executable", text: $viewModel.settings.engines.ds4.binary)
                TextField("Working folder", text: $viewModel.settings.engines.ds4.workingDirectory)
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.engines.ds4.requestTimeoutSeconds)
                }
            } header: {
                engineHeader("DS4", detail: "DeepSeek V4 and GLM 5.2 GGUF models")
            }

            Section {
                Toggle("Enable MFLUX", isOn: $viewModel.settings.engines.mflux.enabled)
                LabeledContent("Local port") {
                    TextField(
                        "Port",
                        value: $viewModel.settings.engines.mflux.port,
                        format: .number.grouping(.never)
                    )
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 80)
                }
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.engines.mflux.requestTimeoutSeconds)
                }
                Text("MFLUX runs in an isolated worker so image-model memory is released cleanly.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                engineHeader("MFLUX", detail: "Qwen Image and Krea 2")
            }

            Section {
                Toggle(
                    "Enable mlxcel (Preview)",
                    isOn: $viewModel.settings.engines.mlxcel.enabled
                )
                LabeledContent("Local port") {
                    TextField(
                        "Port",
                        value: $viewModel.settings.engines.mlxcel.port,
                        format: .number.grouping(.never)
                    )
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 80)
                }
                TextField(
                    "mlxcel-server executable",
                    text: $viewModel.settings.engines.mlxcel.binary
                )
                TextField(
                    "Working folder",
                    text: $viewModel.settings.engines.mlxcel.workingDirectory
                )
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.engines.mlxcel.requestTimeoutSeconds)
                }
                Text("Install and upgrade the official Homebrew formula with `brew tap lablup/tap` and `brew install mlxcel`. Unified Inference owns only the model server process.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                engineHeader("mlxcel", detail: "Native MLX text and vision serving")
            }

            Section {
                Toggle(
                    "Enable mistral.rs (Preview)",
                    isOn: $viewModel.settings.engines.mistralRs.enabled
                )
                LabeledContent("Local port") {
                    TextField(
                        "Port",
                        value: $viewModel.settings.engines.mistralRs.port,
                        format: .number.grouping(.never)
                    )
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 80)
                }
                TextField(
                    "mistralrs executable",
                    text: $viewModel.settings.engines.mistralRs.binary
                )
                TextField(
                    "Working folder",
                    text: $viewModel.settings.engines.mistralRs.workingDirectory
                )
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.engines.mistralRs.requestTimeoutSeconds)
                }
                Text("The official mistral.rs installer owns the binary and its `mistralrs update` path. Unified Inference launches it offline against a pinned local snapshot.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                engineHeader("mistral.rs", detail: "Safetensors language and multimodal models")
            }

        }
        .formStyle(.grouped)
    }

    private var runtimeUpdatesPage: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Engine runtimes update independently from the menu app.")
                            .font(.headline)
                        Text("llama.cpp comes from official GitHub releases, MFLUX from official PyPI releases, and DS4 from the official antirez repository. For oMLX, the recommended official app includes precompiled custom kernels and owns its own updates.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if viewModel.isCheckingRuntimeUpdates
                        || viewModel.isInstallingOMLXWithHomebrew {
                        ProgressView().controlSize(.small)
                    }
                    Button("Check Now") {
                        Task { await viewModel.refreshRuntimeUpdates(force: true) }
                    }
                    .disabled(
                        viewModel.isCheckingRuntimeUpdates
                            || viewModel.updatingRuntimeEngine != nil
                            || viewModel.isInstallingOMLXWithHomebrew
                    )
                }

                if let snapshot = viewModel.runtimeUpdateSnapshot {
                    HStack(spacing: 8) {
                        Label(snapshot.channel.capitalized, systemImage: "checkmark.seal")
                        Text("Core protocol \(snapshot.coreProtocol)")
                        if let checkedAt = snapshot.checkedAt {
                            Text("Checked \(Date(timeIntervalSince1970: checkedAt).formatted(date: .abbreviated, time: .shortened))")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)

                    ForEach(snapshot.engines) { update in
                        runtimeUpdateCard(update)
                    }
                } else if viewModel.isCheckingRuntimeUpdates {
                    VStack(spacing: 12) {
                        ProgressView()
                        Text("Checking installed and upstream engine versions…")
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, minHeight: 220)
                } else {
                    ContentUnavailableView(
                        "No Update Information",
                        systemImage: "arrow.triangle.2.circlepath",
                            description: Text("Choose Check Now to inspect llama.cpp, oMLX, MFLUX, DS4, mlxcel, and mistral.rs.")
                    )
                    .frame(maxWidth: .infinity, minHeight: 260)
                }

                Text("Managed runtime downloads are staged and validated while inference remains available; activation drains through the residency coordinator and retains the previous runtime for rollback. Externally owned engines show their detected binary and official install or update path without Unified Inference replacing vendor files.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(22)
        }
    }

    private func runtimeUpdateCard(_ update: EngineRuntimeUpdate) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(update.displayName)
                    .font(.title3.weight(.semibold))
                if let tier = update.releaseTierLabel {
                    Text(tier)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(
                            update.releaseTier == "stable" ? Color.green : Color.orange
                        )
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(
                            (
                                update.releaseTier == "stable"
                                    ? Color.green : Color.orange
                            ).opacity(0.12),
                            in: Capsule()
                        )
                }
                if update.updateAvailable {
                    Text(
                        update.engine == .omlx && !update.installed
                            ? "OFFICIAL INSTALLER"
                            : (update.canInstall ? "OFFICIAL UPDATE" : "UPSTREAM UPDATE")
                    )
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(
                            update.canInstall || (update.engine == .omlx && !update.installed)
                                ? Color.green : Color.blue
                        )
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(
                            (
                                update.canInstall
                                    || (update.engine == .omlx && !update.installed)
                                    ? Color.green : Color.blue
                            ).opacity(0.12),
                            in: Capsule()
                        )
                } else if update.diagnostic != nil {
                    Text("CHECK FAILED")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.orange)
                } else if update.installed {
                    Text("CURRENT")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if viewModel.updatingRuntimeEngine == update.engine
                    || (update.engine == .omlx
                        && viewModel.isInstallingOMLXWithHomebrew) {
                    ProgressView().controlSize(.small)
                }
            }

            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 6) {
                GridRow {
                    Text("Installed").foregroundStyle(.secondary)
                    Text(update.installedLabel)
                        .font(.system(.body, design: .monospaced))
                }
                if let available = update.availableLabel {
                    GridRow {
                        Text("Official")
                            .foregroundStyle(.secondary)
                        Text(available).font(.system(.body, design: .monospaced))
                    }
                }
                if let path = update.installedPath {
                    GridRow {
                        Text("Location").foregroundStyle(.secondary)
                        Text(path)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .textSelection(.enabled)
                    }
                }
                if let kind = update.installationKindLabel {
                    GridRow {
                        Text("Installation").foregroundStyle(.secondary)
                        Text(kind)
                    }
                }
            }

            Text(update.managementNote)
                .font(.callout)
                .foregroundStyle(.secondary)

            if update.engine == .omlx, !update.installed {
                Text("Download the official app, drag it to Applications, and start its server on 127.0.0.1:17322. Then choose Check Again. The official app avoids the fragile Homebrew HEAD custom-kernel build.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if update.engine == .omlx,
               update.upgradeStrategy == "migrate_to_stable" {
                Text("This is an unreproducible Homebrew HEAD build. Migrate once to the official stable app for precompiled kernels and one-click updates; Unified Inference will not rebuild an arbitrary moving commit.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if update.engine == .omlx,
               let cache = viewModel.omlxCacheHealth {
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Label("SSD prompt cache", systemImage: "externaldrive")
                        Spacer()
                        Text(ByteCountFormatter.string(fromByteCount: Int64(cache.ssdSizeBytes), countStyle: .file))
                            .font(.system(.caption, design: .monospaced))
                    }
                    Text("\(cache.ssdFileCount) files · \(cache.totalCachedTokens.formatted()) cached tokens across \(cache.totalRequests.formatted()) requests")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if let diagnostic = cache.diagnostic {
                        Label(diagnostic, systemImage: "gauge.with.dots.needle.67percent")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
            }

            if let diagnostic = update.diagnostic {
                Label(diagnostic, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack {
                if (update.engine == .mlxcel || update.engine == .mistralRs),
                   let target = update.officialInstallerUrl,
                   let url = URL(string: target) {
                    Link(
                        update.installed ? "Update Instructions" : "Install Instructions",
                        destination: url
                    )
                    .buttonStyle(.borderedProminent)
                }
                if update.engine == .omlx,
                   !update.installed,
                   let target = update.officialInstallerUrl,
                   let url = URL(string: target) {
                    Link(
                        "Download oMLX \(update.latestUpstreamVersion ?? "")",
                        destination: url
                    )
                    .buttonStyle(.borderedProminent)
                }
                if update.engine == .omlx, !update.installed {
                    Button("Install with Homebrew…") {
                        viewModel.confirmHomebrewOMLXInstall = true
                    }
                    .disabled(
                        viewModel.isInstallingOMLXWithHomebrew
                            || viewModel.isCheckingRuntimeUpdates
                    )
                } else if update.canInstall {
                    Button(
                        update.engine == .omlx
                            ? "Update to \(update.availableLabel ?? "Stable")"
                            : "Install \(update.availableVersion ?? "Update")"
                    ) {
                        if update.engine == .omlx {
                            viewModel.pendingOMLXUpgrade = update
                            viewModel.confirmHomebrewOMLXUpgrade = true
                        } else {
                            Task { await viewModel.installRuntimeUpdate(update) }
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(viewModel.updatingRuntimeEngine != nil)
                } else if update.updateAvailable, update.engine != .omlx {
                    Label("Official update cannot be installed", systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if update.canRollback {
                    Button("Roll Back") {
                        Task { await viewModel.rollbackRuntimeUpdate(update) }
                    }
                    .disabled(viewModel.updatingRuntimeEngine != nil)
                }
                if update.engine == .omlx,
                   update.installedPath?.hasSuffix(".app") == true {
                    Button("Open oMLX") {
                        viewModel.openOMLXApplication(update)
                    }
                }
                if update.engine == .omlx,
                   update.upgradeStrategy == "migrate_to_stable",
                   let target = update.officialInstallerUrl,
                   let url = URL(string: target) {
                    Link("Download Stable oMLX", destination: url)
                        .buttonStyle(.borderedProminent)
                }
                if update.engine == .omlx {
                    Button("Check Again") {
                        Task { await viewModel.refreshRuntimeUpdates(force: true) }
                    }
                    .disabled(
                        viewModel.isCheckingRuntimeUpdates
                            || viewModel.updatingRuntimeEngine != nil
                    )
                    if viewModel.omlxCacheHealth != nil {
                        Button("Reset SSD Cache…") {
                            viewModel.confirmOMLXCacheReset = true
                        }
                        .disabled(
                            viewModel.isResettingOMLXCache
                                || viewModel.updatingRuntimeEngine != nil
                        )
                    }
                }
                Spacer()
                if let target = update.releaseNotesUrl ?? update.latestUpstreamUrl,
                   let url = URL(string: target) {
                    Link(
                        update.engine == .omlx
                            ? (update.installed ? "Open Official Update" : "Release Notes")
                            : "Release Notes",
                        destination: url
                    )
                }
            }
        }
        .padding(16)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(.separator.opacity(0.45), lineWidth: 1)
        }
    }

    private var storagePage: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Model library folders")
                            .font(.headline)
                        Text("Choose any folder in Finder. The selected folder can be nested inside an external volume.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        Task { await viewModel.chooseStorageFolder() }
                    } label: {
                        Label("Add Folder…", systemImage: "folder.badge.plus")
                    }
                }

                ForEach(viewModel.settings.storage.locations) { location in
                    let status = viewModel.storageStatus(for: location.name)
                    VStack(alignment: .leading, spacing: 11) {
                        HStack(alignment: .firstTextBaseline) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(storageDisplayName(location.name))
                                    .font(.headline)
                                Text(location.path)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            }
                            Spacer()
                            if viewModel.settings.storage.default == location.name {
                                Text("DEFAULT")
                                    .font(.caption2.weight(.bold))
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 3)
                                    .background(.blue.opacity(0.16), in: Capsule())
                            }
                        }

                        HStack(spacing: 16) {
                            Label(
                                status?.isAvailable == true ? "Available" : "Unavailable",
                                systemImage: status?.isAvailable == true
                                    ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                            )
                            .foregroundStyle(status?.isAvailable == true ? Color.green : Color.orange)
                            if let mount = status?.mountPath {
                                Label(mount, systemImage: "externaldrive")
                            }
                            if let free = status?.freeBytes {
                                Text("\(byteCount(free)) free")
                            }
                            Spacer()
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)

                        if let diagnostic = status?.diagnostic {
                            Text(diagnostic)
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }

                        HStack {
                            Button("Use as Default") {
                                viewModel.settings.storage.default = location.name
                                viewModel.selectedLibraryStorage = location.name
                            }
                            .disabled(viewModel.settings.storage.default == location.name)
                            Button("Choose Different Folder…") {
                                Task { await viewModel.chooseStorageFolder(replacing: location.name) }
                            }
                            Spacer()
                            Button("Remove", role: .destructive) {
                                viewModel.removeStorageLocation(location.name)
                            }
                            .disabled(
                                viewModel.settings.storage.locations.count == 1
                                    || viewModel.settings.models.contains { $0.storage == location.name }
                            )
                        }
                    }
                    .padding(16)
                    .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
                    .overlay {
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(.separator.opacity(0.45), lineWidth: 1)
                    }
                }

                Text("Unified Inference records both this exact path and the containing volume identity. If an external SSD is missing, downloads fail closed instead of creating a lookalike folder on the internal disk.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(22)
        }
    }

    private var libraryPage: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 10) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    TextField("Search Hugging Face", text: $viewModel.libraryQuery)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { Task { await viewModel.refreshModelLibrary() } }
                    Button("Search") { Task { await viewModel.refreshModelLibrary() } }
                        .disabled(viewModel.isSearchingLibrary)
                    if viewModel.isSearchingLibrary {
                        ProgressView().controlSize(.small)
                    }
                }
                Text("One catalog searches every supported model format. Engine badges show where each result can run; Preview engines remain limited to their verified upstream catalogs.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(14)
            Divider()

            HSplitView {
                List(selection: Binding(
                    get: { viewModel.selectedLibraryModelID },
                    set: { viewModel.selectLibraryModel(id: $0) }
                )) {
                    ForEach(viewModel.libraryModels) { model in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(model.displayName).fontWeight(.medium)
                                Spacer()
                                engineBadge(model.engine)
                            }
                            Text(model.repoId)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                            HStack(spacing: 9) {
                                compatibilityBadge(model.compatibility)
                                if let quantization = model.quantization {
                                    Text(quantization)
                                }
                                if let size = model.sizeBytes {
                                    Text(byteCount(size))
                                }
                                if let memory = model.recommendedMemoryGb {
                                    Text("\(memory)+ GB memory")
                                }
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 4)
                        .tag(model.id)
                    }
                }
                .frame(minWidth: 330)

                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if let searchResult = viewModel.selectedLibrarySearchResult {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(alignment: .firstTextBaseline) {
                                Text(searchResult.displayName)
                                    .font(.title3.weight(.semibold))
                                    .fixedSize(horizontal: false, vertical: true)
                                Spacer(minLength: 10)
                                engineBadge(searchResult.engine)
                                compatibilityBadge(searchResult.compatibility)
                            }
                            Text(searchResult.repoId)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                            Text(searchResult.compatibilityReason)
                                .font(.callout)
                                .fixedSize(horizontal: false, vertical: true)
                            if searchResult.engine == .ds4 {
                                Label(
                                    "DS4 support is limited to exact GGUF layouts declared by the installed upstream runtime.",
                                    systemImage: "checkmark.shield"
                                )
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            }
                            if viewModel.isLoadingLibraryDetails {
                                HStack(spacing: 7) {
                                    ProgressView().controlSize(.small)
                                    Text("Reading model card and metadata…")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            } else if let details = viewModel.libraryDetails {
                                if let summary = details.summary {
                                    Text(ModelTextMarkup.attributedString(from: summary))
                                        .font(.callout)
                                        .foregroundStyle(.secondary)
                                        .textSelection(.enabled)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                LazyVGrid(
                                    columns: [
                                        GridItem(
                                            .adaptive(minimum: 145),
                                            spacing: 8,
                                            alignment: .leading
                                        )
                                    ],
                                    alignment: .leading,
                                    spacing: 7
                                ) {
                                    if let context = details.contextLength {
                                        modelBadge(
                                            "\(context.formatted()) token context",
                                            color: .blue
                                        )
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                    if let architecture = details.architecture {
                                        modelBadge(architecture, color: .purple)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                    if let parameters = details.parameterCount {
                                        modelBadge(
                                            parameterCount(parameters),
                                            color: .secondary
                                        )
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                    if let license = details.license {
                                        modelBadge(license, color: .secondary)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }
                                if let markdown = details.modelCardMarkdown {
                                    DisclosureGroup {
                                        if let url = URL(
                                            string: "https://huggingface.co/\(details.repoId)"
                                        ) {
                                            Link(
                                                "Open full card on Hugging Face",
                                                destination: url
                                            )
                                            .font(.caption)
                                        }
                                        ScrollView {
                                            ModelCardView(markdown: markdown)
                                                .padding(12)
                                        }
                                        .frame(height: 340)
                                        .background(
                                            Color(nsColor: .textBackgroundColor).opacity(0.7),
                                            in: RoundedRectangle(cornerRadius: 8)
                                        )
                                        .overlay {
                                            RoundedRectangle(cornerRadius: 8)
                                                .stroke(.separator.opacity(0.45), lineWidth: 1)
                                        }
                                    } label: {
                                        Label("Model card", systemImage: "doc.richtext")
                                    }
                                }
                            }
                        }

                        Divider()
                        if searchResult.needsFileSelection {
                            if viewModel.isLoadingLibraryFiles {
                                HStack(spacing: 8) {
                                    ProgressView().controlSize(.small)
                                    Text("Reading available GGUF quants…")
                                        .foregroundStyle(.secondary)
                                }
                            } else if viewModel.libraryFileOptions.isEmpty {
                                Label(
                                    "No installable GGUF files were found in this repository.",
                                    systemImage: "exclamationmark.triangle"
                                )
                                .font(.caption)
                                .foregroundStyle(.orange)
                            } else {
                                Picker(
                                    "GGUF quant",
                                    selection: Binding(
                                        get: { viewModel.selectedLibraryFileID ?? "" },
                                        set: {
                                            viewModel.selectLibraryFile(
                                                id: $0.isEmpty ? nil : $0
                                            )
                                        }
                                    )
                                ) {
                                    Text("Choose a quant…").tag("")
                                    ForEach(viewModel.libraryFileOptions) { file in
                                        Text(
                                            [
                                                file.quantization,
                                                file.sizeBytes.map(byteCount),
                                                file.displayName,
                                            ]
                                            .compactMap { $0 }
                                            .joined(separator: " — ")
                                        )
                                        .tag(file.id)
                                    }
                                }

                                if let model = viewModel.selectedLibraryModel,
                                   !model.availableProjectors.isEmpty {
                                    Picker(
                                        "Vision projector",
                                        selection: Binding(
                                            get: { viewModel.selectedLibraryProjector },
                                            set: { viewModel.selectLibraryProjector($0) }
                                        )
                                    ) {
                                        Text("Text only (opt out)").tag("")
                                        ForEach(model.availableProjectors, id: \.self) {
                                            Text($0).tag($0)
                                        }
                                    }
                                    Text("The highest-fidelity nearby vision projector is selected automatically. Choose Text only to opt out, or select a different projector.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Divider()
                        }

                        Picker("Model role", selection: $viewModel.selectedLibraryRole) {
                            ForEach(viewModel.availableLibraryRoles) { role in
                                Label(role.displayName, systemImage: role.systemImage)
                                    .tag(role)
                            }
                        }
                        if viewModel.availableLibraryRoles.count > 1 {
                            Text(viewModel.selectedLibraryRole.explanation)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else if let role = viewModel.availableLibraryRoles.first {
                            LabeledContent("API purpose", value: role.explanation)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }

                        Picker("Download to", selection: $viewModel.selectedLibraryStorage) {
                            ForEach(viewModel.settings.storage.locations) { location in
                                Text(storageDisplayName(location.name)).tag(location.name)
                            }
                        }
                        if let status = viewModel.storageStatus(for: viewModel.selectedLibraryStorage) {
                            Label(
                                status.isAvailable
                                    ? "\(byteCount(status.freeBytes ?? 0)) free at \(status.path)"
                                    : status.diagnostic ?? "Folder unavailable",
                                systemImage: status.isAvailable
                                    ? "externaldrive.fill.badge.checkmark" : "externaldrive.badge.exclamationmark"
                            )
                            .font(.caption)
                            .foregroundStyle(status.isAvailable ? Color.secondary : Color.orange)
                        }
                        Button {
                            Task { await viewModel.installSelectedLibraryModel() }
                        } label: {
                            Label("Download and Add Model", systemImage: "arrow.down.circle.fill")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(
                            viewModel.isWorking
                                || viewModel.hasUnsavedChanges
                                || viewModel.requiresRestart
                                || viewModel.selectedLibraryModel?.isInstallable != true
                                || !viewModel.availableLibraryRoles.contains(
                                    viewModel.selectedLibraryRole
                                )
                                || viewModel.storageStatus(for: viewModel.selectedLibraryStorage)?.isAvailable != true
                        )
                        if !searchResult.needsFileSelection, !searchResult.isInstallable {
                            Label(
                                searchResult.compatibilityReason,
                                systemImage: "exclamationmark.triangle"
                            )
                            .font(.caption)
                            .foregroundStyle(.orange)
                        }
                        if viewModel.requiresRestart {
                            Label(
                                "Restart the background service before downloading to this folder.",
                                systemImage: "arrow.clockwise.circle"
                            )
                            .font(.caption)
                            .foregroundStyle(.orange)
                        }
                        Text("Downloading never loads the model. The normal residency coordinator will load it only when an API request selects its new alias.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        } else {
                            ContentUnavailableView(
                                "Choose a Model",
                                systemImage: "shippingbox",
                                description: Text("Search Hugging Face, then choose a result with the engine support you want.")
                            )
                        }
                    }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .frame(minWidth: 350, maxWidth: .infinity, alignment: .topLeading)
            }

            if !viewModel.modelInstalls.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text("Recent downloads")
                            .font(.caption.weight(.semibold))
                        Spacer()
                        if viewModel.modelInstalls.contains(where: { $0.status == "installed" }) {
                            Button("Clear Completed") {
                                Task { await viewModel.clearCompletedInstalls() }
                            }
                            .font(.caption)
                            .buttonStyle(.borderless)
                        }
                    }
                    ForEach(viewModel.modelInstalls.prefix(3)) { install in
                        downloadInstallRow(install)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }
        }
    }

    private var modelsPage: some View {
        HStack(spacing: 0) {
            VStack(spacing: 8) {
                Button {
                    Task { await viewModel.chooseExistingModelsFolder() }
                } label: {
                    Label("Add Existing Models…", systemImage: "folder.badge.plus")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .padding(.horizontal, 10)
                .padding(.top, 10)
                .disabled(
                    viewModel.hasUnsavedChanges
                        || viewModel.isScanningLocalModels
                        || viewModel.isImportingLocalModels
                )
                .help(
                    viewModel.hasUnsavedChanges
                        ? "Save or discard current settings changes first."
                        : "Choose a folder containing existing GGUF or MLX models."
                )

                if !viewModel.localModelSources.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Suggested model folders")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        ForEach(viewModel.localModelSources) { source in
                            Button {
                                Task {
                                    await viewModel.chooseExistingModelsFolder(
                                        source: source
                                    )
                                }
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Label(source.displayName, systemImage: "folder")
                                    Text(source.path)
                                        .font(.system(.caption2, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                        .truncationMode(.middle)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .buttonStyle(.bordered)
                            .disabled(
                                viewModel.hasUnsavedChanges
                                    || viewModel.isScanningLocalModels
                                    || viewModel.isImportingLocalModels
                            )
                            .help(
                                "This path comes from LM Studio's settings or documented default. Finder will confirm access before Unified Inference scans it; LM Studio does not need to be enabled or running."
                            )
                        }
                    }
                    .padding(.horizontal, 10)
                }

                List(selection: $viewModel.selectedModelIndex) {
                    ForEach(viewModel.settings.models.indices, id: \.self) { index in
                        HStack {
                            Image(systemName: viewModel.settings.models[index].engine == .mflux ? "photo" : "text.bubble")
                            VStack(alignment: .leading, spacing: 2) {
                                Text(viewModel.settings.models[index].alias)
                                Text(viewModel.settings.models[index].engine.displayName)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .tag(index)
                    }
                }
                HStack {
                    Button { viewModel.selectedSection = .library } label: {
                        Image(systemName: "arrow.down.circle")
                            .frame(width: 30, height: 18)
                    }
                    .buttonStyle(.bordered)
                    .frame(width: 52, height: 30)
                    .help("Download a compatible model from Hugging Face.")
                    Button {
                        viewModel.confirmRemoveModel = true
                    } label: {
                        Image(systemName: "minus")
                            .frame(width: 30, height: 18)
                    }
                    .buttonStyle(.bordered)
                    .frame(width: 52, height: 30)
                    .disabled(viewModel.selectedModelIndex == nil)
                    .help("Remove the profile, with an optional managed-file deletion.")
                    Spacer()
                }
                .padding(.horizontal, 10)
                .padding(.bottom, 8)
            }
            .frame(width: 230)
            Divider()
            modelEditor
        }
        .task {
            await viewModel.refreshModelsConfiguration()
        }
    }

    @ViewBuilder
    private var modelEditor: some View {
        if let index = viewModel.selectedModelIndex,
           viewModel.settings.models.indices.contains(index) {
            Form {
                Section("Identity") {
                    Toggle("Enabled", isOn: $viewModel.settings.models[index].enabled)
                    TextField("Alias", text: $viewModel.settings.models[index].alias)
                    LabeledContent("Inference engine") {
                        Text(viewModel.settings.models[index].engine.displayName)
                            .foregroundStyle(.secondary)
                    }
                    LabeledContent("Model source") {
                        Text(viewModel.settings.models[index].model)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.trailing)
                            .textSelection(.enabled)
                    }
                    if let storage = viewModel.settings.models[index].storage {
                        LabeledContent("Storage location") {
                            Text(storage)
                                .foregroundStyle(.secondary)
                        }
                    }
                    if let servedName = viewModel.settings.models[index].servedModelName {
                        LabeledContent("Engine model name") {
                            Text(servedName)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                    if let projector = viewModel.settings.models[index].load.projectorPath {
                        LabeledContent("Vision projector") {
                            Text(projector)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.trailing)
                                .textSelection(.enabled)
                        }
                    }
                }

                engineSelectionOptions(index)
                modelRoleOptions(index)
                if viewModel.settings.models[index].engine == .mflux {
                    imageModelOptions(index)
                } else {
                    languageModelOptions(index)
                }
            }
            .formStyle(.grouped)
        } else {
            VStack(spacing: 10) {
                Image(systemName: "shippingbox")
                    .font(.system(size: 34))
                    .foregroundStyle(.secondary)
                Text("No models configured")
                    .font(.headline)
                Text("Download a compatible model or choose a folder containing models already on this Mac.")
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 430)
                HStack {
                    Button("Browse Model Library") {
                        viewModel.selectedSection = .library
                    }
                    Button("Add Existing Models…") {
                        Task { await viewModel.chooseExistingModelsFolder() }
                    }
                    .disabled(
                        viewModel.hasUnsavedChanges
                            || viewModel.isScanningLocalModels
                            || viewModel.isImportingLocalModels
                    )
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder
    private func engineSelectionOptions(_ index: Int) -> some View {
        let profile = viewModel.settings.models[index]
        let alternatives = viewModel.settings.models[index].alternatives
        let pinnableEngines = [profile.engine] + alternatives
            .filter(\.enabled)
            .map(\.engine)
        let sources = viewModel.compatibleAlternativeSources(for: index)
        if !alternatives.isEmpty || !sources.isEmpty {
            Section("Engine selection") {
                if !sources.isEmpty {
                    Picker("Attach installed profile", selection: $alternativeSourceIndex) {
                        Text("Choose a compatible model…").tag(nil as Int?)
                        ForEach(sources, id: \.self) { sourceIndex in
                            let source = viewModel.settings.models[sourceIndex]
                            Text("\(source.alias) — \(source.engine.displayName)")
                                .tag(sourceIndex as Int?)
                        }
                    }
                    Button("Use as engine alternative") {
                        guard let sourceIndex = alternativeSourceIndex else { return }
                        viewModel.attachAlternative(sourceIndex: sourceIndex, to: index)
                        alternativeSourceIndex = nil
                    }
                    .disabled(alternativeSourceIndex == nil)
                    Text("Download or import the same logical model as a separate profile, then attach it here. Its exact engine, path, role, and load settings are preserved; no weights are copied.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !alternatives.isEmpty {
                Picker(
                    "Policy",
                    selection: engineSelectionModeBinding(for: index)
                ) {
                    Text("Fixed fallback engine").tag("fixed")
                    Text("Best fresh benchmark").tag("benchmark")
                    Text("Pinned engine").tag("pinned")
                }
                if profile.selection.mode == "pinned" {
                    Picker(
                        "Pinned engine",
                        selection: pinnedEngineBinding(for: index)
                    ) {
                        ForEach(pinnableEngines) { engine in
                            Text(
                                engine == profile.engine
                                    ? "\(engine.displayName) — original fallback"
                                    : engine.displayName
                            )
                            .tag(engine)
                        }
                    }
                    Text(
                        "Pinned selection bypasses benchmark recommendations. The original fallback is still used if the pinned engine cannot load before inference starts."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                } else if profile.selection.mode == "benchmark" {
                    Picker(
                        "Optimize for",
                        selection: $viewModel.settings.models[index].selection.objective
                    ) {
                        Text("Balanced").tag("balanced")
                        Text("First-token latency").tag("latency")
                        Text("Output throughput").tag("throughput")
                    }
                    Toggle(
                        "Allow Preview engines to win",
                        isOn: $viewModel.settings.models[index].selection.allowPreview
                    )
                    LabeledContent("Minimum samples") {
                        integerField(
                            $viewModel.settings.models[index].selection.minimumSamples,
                            unit: "runs"
                        )
                    }
                    LabeledContent("Evidence lifetime") {
                        integerField(
                            $viewModel.settings.models[index].selection.maxBenchmarkAgeHours,
                            unit: "hours"
                        )
                    }
                    LabeledContent("Minimum improvement") {
                        doubleField(
                            $viewModel.settings.models[index].selection.minimumImprovementPercent,
                            unit: "%"
                        )
                    }
                }
                ForEach(alternatives) { alternative in
                    LabeledContent(alternative.engine.displayName) {
                        HStack {
                            Text(alternative.model)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Button("Detach") {
                                viewModel.detachAlternative(
                                    id: alternative.id,
                                    from: index
                                )
                            }
                            .buttonStyle(.borderless)
                        }
                    }
                }
                if let decision = viewModel.benchmarkSnapshot?.decisions.first(
                    where: { $0.alias == viewModel.settings.models[index].alias }
                ) {
                    LabeledContent("Current decision") {
                        Text(decision.selectedEngine.displayName)
                            .foregroundStyle(.secondary)
                    }
                    Text(decision.reason)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                HStack {
                    Button("Benchmark compatible engines") {
                        let alias = viewModel.settings.models[index].alias
                        Task { await viewModel.runEngineBenchmark(alias: alias) }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(
                        viewModel.hasUnsavedChanges
                            || viewModel.benchmarkingAlias != nil
                            || !viewModel.canBenchmarkModel(at: index)
                    )
                    if viewModel.benchmarkingAlias
                        == viewModel.settings.models[index].alias {
                        ProgressView()
                            .controlSize(.small)
                        Text("This can take several minutes.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Text("The original engine remains the fallback. Benchmark mode uses only fresh results for this exact model, load configuration, runtime version, and Mac.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func engineSelectionModeBinding(for index: Int) -> Binding<String> {
        Binding(
            get: { viewModel.settings.models[index].selection.mode },
            set: { mode in
                viewModel.settings.models[index].selection.mode = mode
                if mode == "pinned",
                   viewModel.settings.models[index].selection.pinnedEngine == nil {
                    viewModel.settings.models[index].selection.pinnedEngine =
                        viewModel.settings.models[index].engine
                }
            }
        )
    }

    private func pinnedEngineBinding(for index: Int) -> Binding<InferenceEngine> {
        Binding(
            get: {
                viewModel.settings.models[index].selection.pinnedEngine
                    ?? viewModel.settings.models[index].engine
            },
            set: {
                viewModel.settings.models[index].selection.pinnedEngine = $0
            }
        )
    }

    @ViewBuilder
    private func languageModelOptions(_ index: Int) -> some View {
        let engine = viewModel.settings.models[index].engine
        contextWindowOptions(index)
        if engine != .omlx {
            Section("Loading") {
                if engine == .llamaCpp {
                    optionalIntegerField(
                        "Evaluation batch size",
                        value: $viewModel.settings.models[index].load.evalBatchSize
                    )
                    optionalBooleanPicker(
                        "Flash attention",
                        value: $viewModel.settings.models[index].load.flashAttention
                    )
                    optionalBooleanPicker(
                        "Keep KV cache on GPU",
                        value: $viewModel.settings.models[index].load.offloadKvCacheToGpu
                    )
                }
                if engine == .llamaCpp {
                    optionalIntegerField(
                        "GPU layers",
                        value: $viewModel.settings.models[index].load.gpuLayers
                    )
                    optionalIntegerField(
                        "Physical batch size",
                        value: $viewModel.settings.models[index].load.ubatchSize
                    )
                    optionalIntegerField(
                        "CPU threads",
                        value: $viewModel.settings.models[index].load.threads
                    )
                    optionalIntegerField(
                        "Parallel request slots",
                        value: $viewModel.settings.models[index].load.parallel
                    )
                    if viewModel.settings.models[index].configuredRole == .embeddings {
                        Picker(
                            "Embedding pooling",
                            selection: $viewModel.settings.models[index].load.pooling
                        ) {
                            Text("Engine default").tag(nil as String?)
                            Text("None").tag("none" as String?)
                            Text("Mean").tag("mean" as String?)
                            Text("CLS").tag("cls" as String?)
                            Text("Last").tag("last" as String?)
                        }
                        Text("Pooling changes how this embeddings model produces one vector per input.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else if viewModel.settings.models[index].configuredRole == .rerank {
                        LabeledContent("Rerank pooling") {
                            Text(
                                viewModel.settings.models[index].load.pooling == "rank"
                                    ? "Rank" : "Engine default"
                            )
                            .foregroundStyle(.secondary)
                        }
                    }
                }
                if engine == .ds4 {
                    optionalIntegerField(
                        "Resident request sessions",
                        value: $viewModel.settings.models[index].load.parallel
                    )
                    Text("Two or more sessions enable DS4 request concurrency and Flash decode batching, but allocate a complete KV state per session. Leave unset for the safest single-session memory use; GLM currently gains fairness rather than native batch speed.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField(
                        "KV cache folder (optional)",
                        text: optionalStringBinding(
                            $viewModel.settings.models[index].load.kvDiskDirectory
                        )
                    )
                    optionalIntegerField(
                        "KV cache disk limit (MB)",
                        value: $viewModel.settings.models[index].load.kvDiskSpaceMb
                    )
                }
                if engine == .mlxcel {
                    optionalIntegerField(
                        "Parallel request slots",
                        value: $viewModel.settings.models[index].load.parallel
                    )
                    Text("mlxcel defaults to four continuously batched slots. Set an explicit value when memory pressure or benchmark results favor a different scheduler width.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if engine == .mistralRs {
                    Text("mistral.rs starts with a conservative single manager admission slot on Metal. Advanced runtime arguments remain configuration-only until upstream exposes an authoritative scheduler-capacity contract.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            Section("Loading") {
                Text("oMLX manages model-specific loading options in its own application.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func contextWindowOptions(_ index: Int) -> some View {
        let profile = viewModel.settings.models[index]
        let observed = viewModel.contextSnapshot?.models
            .first(where: { $0.alias == profile.alias })?.candidates
            .first(where: { $0.engine == profile.engine })
        Section("Context window") {
            if profile.engine == .mistralRs {
                LabeledContent("Policy") {
                    Text("Engine automatic")
                        .foregroundStyle(.secondary)
                }
                Text("mistral.rs remains on its reviewed engine default until its Metal runtime exposes an authoritative context-setting contract.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Picker(
                    "Policy",
                    selection: contextModeBinding(for: index, observed: observed)
                ) {
                    Text("Automatic — verified when profiled").tag("automatic")
                    if profile.context.nativeTokens != nil {
                        Text("Model native maximum").tag("native")
                    }
                    Text("Explicit limit").tag("fixed")
                }
                if profile.context.mode == "fixed" {
                    optionalIntegerField(
                        "Explicit token limit",
                        value: $viewModel.settings.models[index].context.fixedTokens
                    )
                }
                LabeledContent("Detected native limit") {
                    Text(
                        profile.context.nativeTokens?.formatted()
                            ?? observed?.nativeTokens?.formatted()
                            ?? "Unknown"
                    )
                    .foregroundStyle(.secondary)
                }
                LabeledContent("Guaranteed API limit") {
                    Text(observed?.guaranteedTokens?.formatted() ?? "Not yet observed")
                        .foregroundStyle(.secondary)
                }
                if let verified = observed?.verifiedTokens {
                    LabeledContent("Verified on this Mac") {
                        Text("\(verified.formatted()) tokens")
                            .foregroundStyle(.green)
                    }
                } else if let effective = observed?.effectiveTokens {
                    LabeledContent("Current engine setting") {
                        Text("\(effective.formatted()) tokens")
                            .foregroundStyle(.secondary)
                    }
                }
                if !profile.alternatives.isEmpty,
                   let candidates = viewModel.contextSnapshot?.models
                    .first(where: { $0.alias == profile.alias })?.candidates {
                    ForEach(candidates.filter { $0.engine != profile.engine }) { candidate in
                        LabeledContent("\(candidate.engine.displayName) guarantee") {
                            Text(candidate.guaranteedTokens?.formatted() ?? "Unknown")
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            HStack {
                Button("Profile usable context") {
                    Task { await viewModel.runContextProfile(alias: profile.alias) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    viewModel.hasUnsavedChanges
                        || viewModel.profilingContextAlias != nil
                        || profile.configuredRole != .generation
                )
                if viewModel.profilingContextAlias == profile.alias {
                    ProgressView()
                        .controlSize(.small)
                    Text("Large prefills can take several minutes.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Text("Automatic mode uses fresh long-prefill evidence for this exact model, engine runtime, and Mac. Without valid evidence it keeps the configured safe fallback. Native mode requests the model's advertised maximum without proving it fits; an explicit limit is always honored.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func contextModeBinding(
        for index: Int,
        observed: ContextWindowCandidate?
    ) -> Binding<String> {
        Binding(
            get: { viewModel.settings.models[index].context.mode },
            set: { mode in
                viewModel.settings.models[index].context.mode = mode
                if mode == "fixed" {
                    viewModel.settings.models[index].context.fixedTokens =
                        viewModel.settings.models[index].context.fixedTokens
                        ?? observed?.guaranteedTokens
                        ?? viewModel.settings.models[index].context.nativeTokens
                        ?? viewModel.settings.models[index].load.contextLength
                        ?? 32_768
                } else {
                    viewModel.settings.models[index].context.fixedTokens = nil
                }
            }
        )
    }

    private func imageModelOptions(_ index: Int) -> some View {
        Section("Image defaults") {
            LabeledContent("Model family") {
                Text(imageBinding(for: index).wrappedValue.family.displayName)
                    .foregroundStyle(.secondary)
            }
            Picker("Quantization", selection: imageBinding(for: index).quantize) {
                Text("None").tag(nil as Int?)
                ForEach([3, 4, 5, 6, 8], id: \.self) { bits in
                    Text("\(bits)-bit").tag(bits as Int?)
                }
            }
            LabeledContent("Width") {
                integerField(imageBinding(for: index).width, unit: "pixels")
            }
            LabeledContent("Height") {
                integerField(imageBinding(for: index).height, unit: "pixels")
            }
            LabeledContent("Generation steps") {
                integerField(imageBinding(for: index).numInferenceSteps, unit: "steps")
            }
            LabeledContent("Guidance scale") {
                TextField(
                    "Scale",
                    value: imageBinding(for: index).guidanceScale,
                    format: .number
                )
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: 100)
            }
        }
    }

    @ViewBuilder
    private func modelRoleOptions(_ index: Int) -> some View {
        let profile = viewModel.settings.models[index]
        let roles = profile.availableRoles
        let configuredRole = profile.configuredRole
        let onlyRole = roles.count == 1 ? roles.first : nil
        Section("Model role") {
            if let onlyRole {
                if configuredRole == onlyRole {
                    LabeledContent("Role") {
                        Label(onlyRole.displayName, systemImage: onlyRole.systemImage)
                    }
                } else {
                    LabeledContent("Required role") {
                        Label(onlyRole.displayName, systemImage: onlyRole.systemImage)
                    }
                    Button("Repair as \(onlyRole.displayName)") {
                        viewModel.selectRole(onlyRole, for: index)
                    }
                    .buttonStyle(.bordered)
                    Label(
                        "This profile's saved endpoints do not match the only role supported by \(profile.engine.displayName). Repairing replaces them with the canonical \(onlyRole.displayName) routes.",
                        systemImage: "wrench.and.screwdriver"
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
            } else {
                Picker("Role", selection: modelRoleBinding(for: index)) {
                    if configuredRole == nil {
                        Text("Choose a role…").tag(nil as ModelRole?)
                    }
                    ForEach(roles) { role in
                        Text(role.displayName).tag(role as ModelRole?)
                    }
                }
            }
            if let configuredRole,
               onlyRole == nil || configuredRole == onlyRole {
                Text(configuredRole.explanation)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if onlyRole == nil {
                Label(
                    "This legacy profile uses a custom endpoint combination. Choose the single job this model was built for.",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.caption)
                .foregroundStyle(.orange)
            }
            if roles.count > 1 {
                Text("Changing the role changes API routing; it does not convert the model itself.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var usagePage: some View {
        Form {
            Section("Token reporting") {
                Toggle(
                    "Report token usage to the central Postgres ledger",
                    isOn: $viewModel.settings.tokenSidecar.enabled
                )
                Text("Unified Inference is the token sidecar for every language engine.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                LabeledContent("Computer identifier") {
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(viewModel.tokenReportingNodeID)
                            .font(.system(.body, design: .monospaced))
                        Text(viewModel.tokenReportingIdentityDescription)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .foregroundStyle(
                    viewModel.settings.tokenSidecar.enabled ? .primary : .secondary
                )
                LabeledContent("Upload interval") {
                    integerField(
                        $viewModel.settings.tokenSidecar.flushIntervalSeconds,
                        unit: "seconds"
                    )
                }
                .disabled(!viewModel.settings.tokenSidecar.enabled)
                LabeledContent("Rows per batch") {
                    integerField($viewModel.settings.tokenSidecar.batchSize, unit: "rows")
                }
                .disabled(!viewModel.settings.tokenSidecar.enabled)
                LabeledContent("Maximum queued rows") {
                    integerField(
                        $viewModel.settings.tokenSidecar.maxOutboxRows,
                        unit: "rows",
                        fieldWidth: 120
                    )
                }
                .disabled(!viewModel.settings.tokenSidecar.enabled)
            }
            Section("Postgres connection") {
                credentialEditor(
                    .tokenSidecarPostgresDSN,
                    valueLabel: "Connection URL",
                    placeholder: "postgresql://user:password@host:5432/database"
                )
                Text("The host, port, database, username, and password are stored privately on this Mac. Existing values are never displayed. Leave this blank to keep the current connection, or use Clear to remove it; save and restart the service to apply a replacement.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Privacy") {
                Text("Language requests record token counts and model metadata. Image requests do not create token-usage records.")
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private var credentialsPage: some View {
        Form {
            Section {
                Text("Saved credentials are never displayed. Enter a value in the bordered field to add or replace a credential; leave it blank to keep the saved value.")
                    .foregroundStyle(.secondary)
            }
            ForEach(
                ManagedCredential.allCases.filter {
                    $0 != .tokenSidecarPostgresDSN
                }
            ) { credential in
                Section {
                    credentialEditor(credential)
                } header: {
                    Text(credential.displayName)
                }
            }
        }
        .formStyle(.grouped)
    }

    @ViewBuilder
    private func credentialEditor(
        _ credential: ManagedCredential,
        valueLabel: String? = nil,
        placeholder: String? = nil
    ) -> some View {
        if viewModel.credentialsToClear.contains(credential) {
            HStack {
                Label("Will be removed when you save", systemImage: "trash")
                    .foregroundStyle(.orange)
                Spacer()
                Button("Undo") { viewModel.undoCredentialClear(credential) }
            }
        } else {
            LabeledContent(
                valueLabel
                    ?? (viewModel.configuredCredentials.contains(credential)
                        ? "Replacement value"
                        : "New value")
            ) {
                HStack(spacing: 8) {
                    SecureField(
                        placeholder
                            ?? (viewModel.configuredCredentials.contains(credential)
                                ? "Enter a replacement"
                                : "Enter a credential"),
                        text: viewModel.credentialBinding(credential)
                    )
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 280)
                    .privacySensitive()
                    .accessibilityLabel("\(credential.displayName) value")

                    Button {
                        if previewedCredentialDrafts.contains(credential) {
                            previewedCredentialDrafts.remove(credential)
                        } else {
                            previewedCredentialDrafts.insert(credential)
                        }
                    } label: {
                        Image(
                            systemName: previewedCredentialDrafts.contains(credential)
                                ? "eye.slash" : "eye"
                        )
                    }
                    .buttonStyle(.borderless)
                    .help(
                        previewedCredentialDrafts.contains(credential)
                            ? "Hide pasted-value preview"
                            : "Preview the pasted value with its secret truncated"
                    )
                    .accessibilityLabel(
                        previewedCredentialDrafts.contains(credential)
                            ? "Hide \(credential.displayName) preview"
                            : "Show truncated \(credential.displayName) preview"
                    )
                    .disabled(viewModel.credentialDrafts[credential, default: ""].isEmpty)
                }
                .frame(minWidth: 320)
            }
            if previewedCredentialDrafts.contains(credential) {
                let draft = viewModel.credentialDrafts[credential, default: ""]
                if !draft.isEmpty {
                    LabeledContent("Pasted value preview") {
                        Text(CredentialDraftPreview.render(draft, for: credential))
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                            .privacySensitive()
                    }
                }
            }
            HStack {
                Label(
                    viewModel.configuredCredentials.contains(credential)
                        ? "Configured" : "Not configured",
                    systemImage: viewModel.configuredCredentials.contains(credential)
                        ? "checkmark.circle.fill" : "circle"
                )
                .font(.caption)
                .foregroundStyle(
                    viewModel.configuredCredentials.contains(credential)
                        ? Color.green : Color.secondary
                )
                Spacer()
                if viewModel.configuredCredentials.contains(credential) {
                    Button("Clear", role: .destructive) {
                        viewModel.clearCredential(credential)
                    }
                }
            }
        }
        Text(credential.help)
            .font(.caption)
            .foregroundStyle(.secondary)
    }

    private var footer: some View {
        HStack(spacing: 12) {
            if !viewModel.statusMessage.isEmpty {
                Text(viewModel.statusMessage)
                    .font(.caption)
                    .foregroundStyle(viewModel.statusColor)
                    .lineLimit(2)
            }
            Spacer()
            Button("Discard") {
                viewModel.confirmDiscard = true
            }
            .disabled(!viewModel.hasUnsavedChanges || viewModel.isWorking)
            if viewModel.requiresRestart {
                Button("Restart Service") { restartService() }
                    .disabled(viewModel.isWorking)
            }
            Button("Save Settings") {
                Task { await viewModel.save() }
            }
            .keyboardShortcut("s", modifiers: .command)
            .buttonStyle(.borderedProminent)
            .disabled(
                !viewModel.hasUnsavedChanges
                    || viewModel.isWorking
                    || !viewModel.configurationSchemaIsSupported
            )
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    private var sectionDescription: String {
        switch viewModel.selectedSection {
        case .setup: "First-run guidance, system health, recovery, and verification"
        case .general: "Ports, timeouts, model residency, and local storage"
        case .engines: "Choose and connect the inference engines available on this Mac"
        case .updates: "Check and install updates from each engine's official source"
        case .storage: "Choose exact internal or external folders for downloaded models"
        case .library: "Find compatible Hugging Face models and download them without loading"
        case .models: "Create friendly aliases and tune how each model loads"
        case .usage: "Configure local token accounting and central reporting"
        case .credentials: "Replace or remove private API keys without revealing saved values"
        }
    }

    private var idleUnloadEnabled: Binding<Bool> {
        Binding(
            get: { viewModel.settings.server.idleUnloadSeconds != nil },
            set: { viewModel.settings.server.idleUnloadSeconds = $0 ? 900 : nil }
        )
    }

    private var residencyPreset: Binding<ResidencyPreset> {
        Binding(
            get: {
                switch viewModel.settings.server.idleUnloadSeconds {
                case nil: .performance
                case 900: .balanced
                case 300: .memorySaver
                default: .custom
                }
            },
            set: { preset in
                switch preset {
                case .performance:
                    viewModel.settings.server.idleUnloadSeconds = nil
                    viewModel.settings.server.maxConcurrency = nil
                case .balanced:
                    viewModel.settings.server.idleUnloadSeconds = 900
                    viewModel.settings.server.maxConcurrency = nil
                case .memorySaver:
                    viewModel.settings.server.idleUnloadSeconds = 300
                    viewModel.settings.server.maxConcurrency = 1
                case .custom:
                    break
                }
            }
        )
    }

    private var idleUnloadSeconds: Binding<Int> {
        Binding(
            get: { viewModel.settings.server.idleUnloadSeconds ?? 900 },
            set: { viewModel.settings.server.idleUnloadSeconds = max(1, $0) }
        )
    }

    private func engineHeader(_ title: String, detail: String) -> some View {
        HStack {
            Text(title)
            Text("— \(detail)").foregroundStyle(.secondary)
        }
    }

    private func downloadInstallRow(_ install: ModelInstall) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Image(
                    systemName: install.isActive
                        ? "arrow.down.circle"
                        : install.status == "installed"
                            ? "checkmark.circle.fill"
                            : "exclamationmark.circle"
                )
                .foregroundStyle(
                    install.status == "installed" ? Color.green : Color.secondary
                )
                Text(install.alias)
                    .fontWeight(.medium)
                    .lineLimit(1)
                Text(install.status.capitalized)
                    .foregroundStyle(.secondary)
                Spacer()
                if install.isActive {
                    Button("Cancel") {
                        Task { await viewModel.cancelInstall(install) }
                    }
                    .frame(width: 62)
                } else {
                    if install.canRetry {
                        Button("Retry") {
                            Task { await viewModel.retryInstall(install) }
                        }
                        .frame(width: 62)
                    }
                    if install.canDismiss {
                        Button {
                            Task { await viewModel.dismissInstall(install) }
                        } label: {
                            Image(systemName: "trash")
                                .frame(width: 14)
                        }
                        .frame(width: 34)
                        .help("Remove this entry from download history.")
                    }
                }
            }

            if install.isActive {
                if let totalBytes = install.totalBytes, totalBytes > 0 {
                    ProgressView(
                        value: Double(min(install.bytesDownloaded, totalBytes)),
                        total: Double(totalBytes)
                    )
                    .progressViewStyle(.linear)
                    HStack {
                        Text(
                            "\(byteCount(install.bytesDownloaded)) of \(byteCount(totalBytes))"
                        )
                        if let fraction = install.progressFraction {
                            Text(
                                fraction.formatted(
                                    .percent.precision(.fractionLength(0))
                                )
                            )
                        }
                        Spacer()
                        downloadSpeedLabel(install)
                    }
                    .foregroundStyle(.secondary)
                } else {
                    ProgressView()
                        .controlSize(.small)
                    HStack {
                        Text(byteCount(install.bytesDownloaded))
                        Spacer()
                        downloadSpeedLabel(install)
                    }
                    .foregroundStyle(.secondary)
                }
            } else {
                Text(byteCount(install.bytesDownloaded))
                    .foregroundStyle(.secondary)
                if let error = install.error, !error.isEmpty {
                    Text(error)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                }
            }
        }
        .font(.caption)
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func downloadSpeedLabel(_ install: ModelInstall) -> some View {
        if install.status == "downloading" {
            if let speed = install.downloadSpeedBps, speed > 0 {
                Text("\(byteCount(Int64(speed)))/s")
                    .monospacedDigit()
            } else {
                Text("Calculating speed…")
            }
        } else if install.status == "registering" {
            Text("Registering profile…")
        } else if install.status == "queued" {
            Text("Waiting…")
        }
    }

    private func compatibilityBadge(_ value: String) -> some View {
        let color: Color = switch value {
        case "verified": .green
        case "unavailable": .orange
        default: .blue
        }
        return Text(value.uppercased())
            .font(.caption2.weight(.bold))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12), in: Capsule())
    }

    private func engineBadge(_ engine: InferenceEngine) -> some View {
        let color: Color = switch engine {
        case .llamaCpp: .blue
        case .omlx: .purple
        case .ds4: .orange
        case .mflux: .pink
        case .mlxcel: .indigo
        case .mistralRs: .teal
        }
        return Label(engine.displayName, systemImage: "checkmark.circle.fill")
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(color.opacity(0.12), in: Capsule())
            .fixedSize()
    }

    private func storageDisplayName(_ name: String) -> String {
        name.split(separator: "-").map { $0.capitalized }.joined(separator: " ")
    }

    private func byteCount(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    private func parameterCount(_ value: Int64) -> String {
        if value >= 1_000_000_000 {
            let count = (Double(value) / 1_000_000_000).formatted(
                .number.precision(.fractionLength(1))
            )
            return "\(count)B parameters"
        }
        if value >= 1_000_000 {
            let count = (Double(value) / 1_000_000).formatted(
                .number.precision(.fractionLength(1))
            )
            return "\(count)M parameters"
        }
        return "\(value.formatted()) parameters"
    }

    private func modelBadge(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12), in: Capsule())
    }

    private func secondsField(_ value: Binding<Double>) -> some View {
        HStack(spacing: 8) {
            TextField("Seconds", value: value, format: .number)
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: 104)
            Text("seconds")
                .foregroundStyle(.secondary)
                .frame(width: 58, alignment: .leading)
        }
        .frame(minWidth: 174, alignment: .trailing)
    }

    private func integerField(
        _ value: Binding<Int>,
        unit: String,
        fieldWidth: CGFloat = 104
    ) -> some View {
        HStack(spacing: 8) {
            TextField("Value", value: value, format: .number)
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: fieldWidth)
            Text(unit)
                .foregroundStyle(.secondary)
                .frame(width: 58, alignment: .leading)
        }
        .frame(minWidth: fieldWidth + 70, alignment: .trailing)
    }

    private func doubleField(
        _ value: Binding<Double>,
        unit: String,
        fieldWidth: CGFloat = 104
    ) -> some View {
        HStack(spacing: 8) {
            TextField("Value", value: value, format: .number)
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: fieldWidth)
            Text(unit)
                .foregroundStyle(.secondary)
                .frame(width: 58, alignment: .leading)
        }
        .frame(minWidth: fieldWidth + 70, alignment: .trailing)
    }

    private func modelRoleBinding(for index: Int) -> Binding<ModelRole?> {
        Binding(
            get: { viewModel.settings.models[index].configuredRole },
            set: { role in
                if let role {
                    viewModel.selectRole(role, for: index)
                }
            }
        )
    }

    private func imageBinding(for index: Int) -> Binding<ImageProfileSettings> {
        Binding(
            get: { viewModel.settings.models[index].image ?? .init() },
            set: { viewModel.settings.models[index].image = $0 }
        )
    }

    private func optionalStringBinding(_ value: Binding<String?>) -> Binding<String> {
        Binding(
            get: { value.wrappedValue ?? "" },
            set: { value.wrappedValue = $0.isEmpty ? nil : $0 }
        )
    }

    private func optionalIntegerField(_ title: String, value: Binding<Int?>) -> some View {
        LabeledContent(title) {
            TextField("Default", text: Binding(
                get: { value.wrappedValue.map(String.init) ?? "" },
                set: { value.wrappedValue = Int($0) }
            ))
            .labelsHidden()
            .textFieldStyle(.roundedBorder)
            .multilineTextAlignment(.trailing)
            .frame(width: 120)
        }
    }

    private func optionalBooleanPicker(_ title: String, value: Binding<Bool?>) -> some View {
        Picker(title, selection: value) {
            Text("Engine default").tag(nil as Bool?)
            Text("On").tag(true as Bool?)
            Text("Off").tag(false as Bool?)
        }
    }

}

private struct ModelCardView: View {
    private let blocks: [ModelTextBlock]

    init(markdown: String) {
        blocks = ModelTextMarkup.blocks(from: markdown)
    }

    var body: some View {
        LazyVStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func blockView(_ block: ModelTextBlock) -> some View {
        switch block {
        case let .heading(level, text):
            Text(ModelTextMarkup.attributedString(from: text))
                .font(headingFont(level))
                .fontWeight(level <= 2 ? .bold : .semibold)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, level <= 2 ? 5 : 2)
        case let .paragraph(text):
            Text(ModelTextMarkup.attributedString(from: text))
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        case let .unorderedItem(text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("•")
                    .fontWeight(.semibold)
                Text(ModelTextMarkup.attributedString(from: text))
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case let .orderedItem(number, text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("\(number).")
                    .fontWeight(.semibold)
                    .monospacedDigit()
                Text(ModelTextMarkup.attributedString(from: text))
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case let .quote(text):
            HStack(alignment: .top, spacing: 9) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.accentColor.opacity(0.55))
                    .frame(width: 3)
                Text(ModelTextMarkup.attributedString(from: text))
                    .italic()
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case let .code(text):
            ScrollView(.horizontal) {
                Text(text)
                    .font(.system(.caption, design: .monospaced))
                    .fixedSize(horizontal: true, vertical: true)
                    .padding(10)
            }
            .background(.quaternary.opacity(0.55), in: RoundedRectangle(cornerRadius: 6))
        case .rule:
            Divider()
                .padding(.vertical, 3)
        }
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: .title2
        case 2: .title3
        case 3: .headline
        default: .subheadline
        }
    }
}

private struct ExistingModelImporterView: View {
    @ObservedObject var viewModel: SettingsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Add Existing Models", systemImage: "folder.badge.plus")
                        .font(.title2.weight(.semibold))
                    Text("Adopt selected GGUF and MLX models in place—without copying or loading their weights.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if viewModel.isScanningLocalModels || viewModel.isImportingLocalModels {
                    ProgressView().controlSize(.small)
                }
            }
            .padding(20)

            Divider()
            existingModelContent
            Divider()

            if !viewModel.localModelImportError.isEmpty {
                Label(
                    viewModel.localModelImportError,
                    systemImage: "exclamationmark.triangle"
                )
                .font(.caption)
                .foregroundStyle(.red)
                .padding(.horizontal, 16)
                .padding(.top, 10)
            }

            HStack(spacing: 10) {
                if let scan = viewModel.localModelScan {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(scan.root)
                            .font(.system(.caption, design: .monospaced))
                            .lineLimit(1)
                        if let mount = scan.mountPath {
                            Text("On \(mount)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                Spacer()
                Button("Cancel") {
                    viewModel.showLocalModelImporter = false
                }
                .disabled(viewModel.isImportingLocalModels)
                Button(localImportButtonTitle) {
                    Task { await viewModel.importSelectedLocalModels() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    viewModel.selectedLocalModelIDs.isEmpty
                        || viewModel.isScanningLocalModels
                        || viewModel.isImportingLocalModels
                )
            }
            .padding(16)
        }
        .frame(minWidth: 820, minHeight: 600)
    }

    @ViewBuilder
    private var existingModelContent: some View {
        if viewModel.isScanningLocalModels, viewModel.localModelScan == nil {
            VStack(spacing: 12) {
                ProgressView()
                Text("Scanning for GGUF and MLX models…")
                    .foregroundStyle(.secondary)
                Text("This only reads file metadata and GGUF headers.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if !viewModel.localModelScanError.isEmpty {
            VStack(spacing: 12) {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 30))
                    .foregroundStyle(.orange)
                Text("That folder could not be scanned")
                    .font(.headline)
                Text(viewModel.localModelScanError)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Text("Close this window and choose a narrower or currently mounted folder.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(40)
        } else if let scan = viewModel.localModelScan, scan.models.isEmpty {
            ContentUnavailableView(
                "No Compatible Model Files",
                systemImage: "shippingbox",
                description: Text(
                    "The selected folder did not contain GGUF files or complete local MLX model directories."
                )
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let scan = viewModel.localModelScan {
            List(scan.models) { candidate in
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .top, spacing: 12) {
                        Toggle(
                            isOn: viewModel.localModelSelectionBinding(candidate.id)
                        ) {
                            EmptyView()
                        }
                        .labelsHidden()
                        .toggleStyle(.checkbox)
                        .disabled(!candidate.isImportable)

                        Image(
                            systemName: candidate.engine == .llamaCpp
                                ? "shippingbox.fill" : "cpu"
                        )
                        .foregroundStyle(.secondary)
                        .frame(width: 22)

                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 7) {
                                Text(candidate.displayName).fontWeight(.medium)
                                modelBadge(
                                    candidate.engine.displayName,
                                    color: candidate.engine == .llamaCpp ? .blue : .purple
                                )
                                modelBadge(
                                    candidate.compatibility.uppercased(),
                                    color: candidate.compatibility == "unavailable"
                                        ? .orange : .green
                                )
                                if let alias = candidate.existingAlias,
                                   !candidate.alreadyImported {
                                    modelBadge("Migrate \(alias)", color: .blue)
                                }
                                if candidate.alreadyImported {
                                    modelBadge("Already added", color: .secondary)
                                }
                            }
                            Text(candidate.modelPath)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                            HStack(spacing: 9) {
                                if let quant = candidate.quantization {
                                    Text(quant)
                                }
                                Text(
                                    ByteCountFormatter.string(
                                        fromByteCount: candidate.sizeBytes,
                                        countStyle: .file
                                    )
                                )
                                if candidate.shardCount > 1 {
                                    Text("\(candidate.shardCount) shards")
                                }
                                if let context = candidate.contextLength {
                                    Text("\(context.formatted()) token context")
                                }
                                if let architecture = candidate.architecture {
                                    Text(architecture)
                                }
                                if !candidate.projectorOptions.isEmpty {
                                    Text("\(candidate.projectorOptions.count) projector option\(candidate.projectorOptions.count == 1 ? "" : "s")")
                                }
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            Text(candidate.compatibilityReason)
                                .font(.caption)
                                .foregroundStyle(
                                    candidate.compatibility == "unavailable"
                                        ? Color.orange : Color.secondary
                                )
                            if let summary = candidate.summary {
                                Text(summary)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(3)
                            }
                        }
                    }

                    if viewModel.selectedLocalModelIDs.contains(candidate.id) {
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            TextField(
                                "Alias",
                                text: viewModel.localModelAliasBinding(candidate.id)
                            )
                            .textFieldStyle(.roundedBorder)
                            .frame(minWidth: 190, maxWidth: 280)

                            if !candidate.projectorOptions.isEmpty {
                                Picker(
                                    "Vision projector",
                                    selection: viewModel.localModelProjectorBinding(
                                        candidate.id
                                    )
                                ) {
                                    Text("Text only (opt out)").tag("")
                                    ForEach(candidate.projectorOptions) { projector in
                                        Text(
                                            "\(projector.filename) — "
                                                + ByteCountFormatter.string(
                                                    fromByteCount: projector.sizeBytes,
                                                    countStyle: .file
                                                )
                                        )
                                        .tag(projector.id)
                                    }
                                }
                                .frame(maxWidth: 420)
                            }
                            Spacer()
                        }
                        .padding(.leading, 56)
                    }
                }
                .padding(.vertical, 6)
            }
            .listStyle(.inset)
        }
    }

    private var localImportButtonTitle: String {
        let count = viewModel.selectedLocalModelIDs.count
        return count == 1 ? "Add 1 Model" : "Add \(count) Models"
    }

    private func modelBadge(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12), in: Capsule())
    }
}
