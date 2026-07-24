import MnemosyneAppCore
import SwiftUI

struct SettingsView: View {
    @ObservedObject var viewModel: SettingsViewModel
    let restartService: () -> Void

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
        .alert("Remove this model profile?", isPresented: $viewModel.confirmRemoveModel) {
            Button("Cancel", role: .cancel) {}
            Button("Remove Model", role: .destructive) {
                viewModel.removeSelectedModel()
            }
        } message: {
            Text("The downloaded model is not deleted; only its Unified Inference profile is removed.")
        }
        .sheet(isPresented: $viewModel.showLMStudioImporter) {
            LMStudioImporterView(viewModel: viewModel)
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
            Text("Unified Inference")
                .font(.caption)
                .foregroundStyle(.secondary)
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
            VStack(spacing: 14) {
                Image(systemName: "gearshape.2")
                    .font(.system(size: 36))
                    .foregroundStyle(.secondary)
                Text(viewModel.statusMessage)
                    .foregroundStyle(viewModel.statusColor)
                    .multilineTextAlignment(.center)
                Button("Try Again") { Task { await viewModel.load() } }
                    .disabled(viewModel.isWorking)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(40)
        } else {
            switch viewModel.selectedSection {
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
                LabeledContent("Shutdown grace period") {
                    secondsField($viewModel.settings.server.shutdownGraceSeconds)
                }
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
                engineHeader("DS4", detail: "DeepSeek GGUF models")
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
                    "Keep LM Studio available during migration",
                    isOn: $viewModel.settings.engines.lmstudio.enabled
                )
                TextField(
                    "LM Studio API address",
                    text: $viewModel.settings.engines.lmstudio.baseUrl
                )
                LabeledContent("Request timeout") {
                    secondsField($viewModel.settings.engines.lmstudio.requestTimeoutSeconds)
                }
                Text("Legacy migration source only. Use Add Existing Models to adopt its GGUF and MLX files in place, then disable this adapter for the soak period.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                engineHeader("LM Studio", detail: "Temporary migration bridge")
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
                        Text("llama.cpp comes from official GitHub releases, MFLUX from official PyPI releases, and DS4 from the official antirez repository. oMLX remains owned by its app or Homebrew installation.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if viewModel.isCheckingRuntimeUpdates {
                        ProgressView().controlSize(.small)
                    }
                    Button("Check Now") {
                        Task { await viewModel.refreshRuntimeUpdates(force: true) }
                    }
                    .disabled(
                        viewModel.isCheckingRuntimeUpdates
                            || viewModel.updatingRuntimeEngine != nil
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
                            description: Text("Choose Check Now to inspect llama.cpp, oMLX, MFLUX, and DS4.")
                    )
                    .frame(maxWidth: .infinity, minHeight: 260)
                }

                Text("Official downloads are staged and import/build-validated while inference remains available. Activation waits for active requests, unloads the current model through the residency coordinator, and atomically switches runtimes. The previous managed runtime is retained for rollback.")
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
                if update.updateAvailable {
                    Text(update.canInstall ? "OFFICIAL UPDATE" : "UPSTREAM UPDATE")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(update.canInstall ? Color.green : Color.blue)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(
                            (update.canInstall ? Color.green : Color.blue).opacity(0.12),
                            in: Capsule()
                        )
                } else if update.installed {
                    Text("CURRENT")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if viewModel.updatingRuntimeEngine == update.engine {
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
            }

            Text(update.managementNote)
                .font(.callout)
                .foregroundStyle(.secondary)

            if let diagnostic = update.diagnostic {
                Label(diagnostic, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack {
                if update.canInstall {
                    Button("Install \(update.availableVersion ?? "Update")") {
                        Task { await viewModel.installRuntimeUpdate(update) }
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
                Spacer()
                if let target = update.releaseNotesUrl ?? update.latestUpstreamUrl,
                   let url = URL(string: target) {
                    Link(update.engine == .omlx ? "Open Official Update" : "Release Notes", destination: url)
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
            HStack(spacing: 10) {
                Picker("Engine", selection: Binding(
                    get: { viewModel.libraryEngine },
                    set: { viewModel.selectLibraryEngine($0) }
                )) {
                    Text("llama.cpp").tag(InferenceEngine.llamaCpp)
                    Text("oMLX").tag(InferenceEngine.omlx)
                    Text("DS4").tag(InferenceEngine.ds4)
                    Text("MFLUX").tag(InferenceEngine.mflux)
                }
                .frame(width: 170)

                TextField("Search Hugging Face", text: $viewModel.libraryQuery)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await viewModel.refreshModelLibrary() } }
                Button("Search") { Task { await viewModel.refreshModelLibrary() } }
                    .disabled(viewModel.isSearchingLibrary)
                if viewModel.isSearchingLibrary {
                    ProgressView().controlSize(.small)
                }
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
                                compatibilityBadge(model.compatibility)
                            }
                            Text(model.repoId)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                            HStack(spacing: 9) {
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

                VStack(alignment: .leading, spacing: 14) {
                    if let searchResult = viewModel.selectedLibrarySearchResult {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(searchResult.displayName)
                                .font(.title3.weight(.semibold))
                            Text(searchResult.repoId)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                            Text(searchResult.compatibilityReason)
                                .font(.callout)
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
                                        Text("Text only (no projector)").tag("")
                                        ForEach(model.availableProjectors, id: \.self) {
                                            Text($0).tag($0)
                                        }
                                    }
                                    Text("Select a matching projector only for multimodal GGUF models. Pairing is explicit because a nearby filename alone does not prove compatibility.")
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
                            description: Text("Search results are filtered for the selected inference engine.")
                        )
                    }
                    Spacer()
                }
                .padding(18)
                .frame(minWidth: 350, maxWidth: .infinity, alignment: .topLeading)
            }

            if !viewModel.modelInstalls.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: 7) {
                    Text("Recent downloads").font(.caption.weight(.semibold))
                    ForEach(viewModel.modelInstalls.prefix(3)) { install in
                        HStack {
                            Image(systemName: install.isActive ? "arrow.down.circle" : install.status == "installed" ? "checkmark.circle.fill" : "exclamationmark.circle")
                                .foregroundStyle(install.status == "installed" ? Color.green : Color.secondary)
                            Text(install.alias).fontWeight(.medium)
                            Text(install.status.capitalized).foregroundStyle(.secondary)
                            Text(byteCount(install.bytesDownloaded)).foregroundStyle(.secondary)
                            Spacer()
                            if install.isActive {
                                Button("Cancel") { Task { await viewModel.cancelInstall(install) } }
                            } else if install.canRetry {
                                Button("Retry") { Task { await viewModel.retryInstall(install) } }
                            }
                        }
                        .font(.caption)
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

                if viewModel.settings.engines.lmstudio.enabled {
                    Button {
                        viewModel.showLMStudioImporter = true
                        Task { await viewModel.discoverLMStudioModels() }
                    } label: {
                        Label(
                            "Legacy LM Studio Inventory…",
                            systemImage: "arrow.triangle.branch"
                        )
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .padding(.horizontal, 10)
                    .disabled(!viewModel.canDiscoverLMStudioInventory)
                    .help(
                        viewModel.lmStudioInventoryAvailability.guidance
                            ?? "Create temporary LM Studio profiles during the migration soak."
                    )
                    if let guidance = viewModel.lmStudioInventoryAvailability.guidance {
                        Label(guidance, systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(.orange)
                            .padding(.horizontal, 10)
                    }
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
                        Label("Download from Model Library", systemImage: "arrow.down.circle")
                    }
                    .help("Download a compatible model from Hugging Face.")
                    Button {
                        viewModel.confirmRemoveModel = true
                    } label: {
                        Label("Remove", systemImage: "minus")
                    }
                    .disabled(viewModel.selectedModelIndex == nil)
                    Spacer()
                }
                .labelStyle(.iconOnly)
                .padding(.horizontal, 10)
                .padding(.bottom, 8)
            }
            .frame(width: 230)
            Divider()
            modelEditor
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
    private func languageModelOptions(_ index: Int) -> some View {
        let engine = viewModel.settings.models[index].engine
        if engine != .omlx {
            Section("Loading") {
                optionalIntegerField(
                    "Context length",
                    value: $viewModel.settings.models[index].load.contextLength
                )
                if engine == .lmstudio || engine == .llamaCpp {
                    optionalIntegerField(
                        "Evaluation batch size",
                        value: $viewModel.settings.models[index].load.evalBatchSize
                    )
                    optionalBooleanPicker(
                        "Flash attention",
                        value: $viewModel.settings.models[index].load.flashAttention
                    )
                    if engine == .lmstudio {
                        optionalIntegerField(
                            "Active experts",
                            value: $viewModel.settings.models[index].load.numExperts
                        )
                    }
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
            }
        } else {
            Section("Loading") {
                Text("oMLX manages model-specific loading options in its own application.")
                    .foregroundStyle(.secondary)
            }
        }
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
                    $0 != .lmStudioAPIKey
                        || viewModel.settings.engines.lmstudio.enabled
                        || viewModel.configuredCredentials.contains(.lmStudioAPIKey)
                }
            ) { credential in
                Section {
                    if viewModel.credentialsToClear.contains(credential) {
                        HStack {
                            Label("Will be removed when you save", systemImage: "trash")
                                .foregroundStyle(.orange)
                            Spacer()
                            Button("Undo") { viewModel.undoCredentialClear(credential) }
                        }
                    } else {
                        LabeledContent(
                            viewModel.configuredCredentials.contains(credential)
                                ? "Replacement value"
                                : "New value"
                        ) {
                            SecureField(
                                viewModel.configuredCredentials.contains(credential)
                                    ? "Enter a replacement"
                                    : "Enter a credential",
                                text: viewModel.credentialBinding(credential)
                            )
                            .textFieldStyle(.roundedBorder)
                            .frame(minWidth: 320)
                            .privacySensitive()
                            .accessibilityLabel("\(credential.displayName) value")
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
                } header: {
                    Text(credential.displayName)
                }
            }
        }
        .formStyle(.grouped)
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

    private func storageDisplayName(_ name: String) -> String {
        name.split(separator: "-").map { $0.capitalized }.joined(separator: " ")
    }

    private func byteCount(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
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
                                    Text("Text only (no projector)").tag("")
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

private struct LMStudioImporterView: View {
    @ObservedObject var viewModel: SettingsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Label(
                        "Legacy LM Studio Profiles",
                        systemImage: "shippingbox.and.arrow.backward"
                    )
                        .font(.title2.weight(.semibold))
                    Text("Create temporary LM Studio-backed profiles for the migration soak. Use Add Existing Models for native llama.cpp or oMLX adoption.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if viewModel.isDiscoveringLMStudio {
                    ProgressView().controlSize(.small)
                }
            }
            .padding(20)

            Divider()

            importerContent

            Divider()

            HStack(spacing: 10) {
                Button("Select Unprofiled") {
                    viewModel.selectAllUnprofiledLMStudioModels()
                }
                .disabled(viewModel.lmStudioInventory.isEmpty)

                Button("Refresh") {
                    Task { await viewModel.discoverLMStudioModels() }
                }
                .disabled(viewModel.isDiscoveringLMStudio)

                Spacer()

                Button("Cancel") {
                    viewModel.showLMStudioImporter = false
                }
                Button(importButtonTitle) {
                    viewModel.importSelectedLMStudioModels()
                }
                .buttonStyle(.borderedProminent)
                .disabled(viewModel.selectedLMStudioKeys.isEmpty)
            }
            .padding(16)
        }
        .frame(minWidth: 720, minHeight: 520)
    }

    @ViewBuilder
    private var importerContent: some View {
        if !viewModel.lmStudioDiscoveryError.isEmpty {
            VStack(spacing: 12) {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 30))
                    .foregroundStyle(.orange)
                Text("LM Studio inventory is unavailable")
                    .font(.headline)
                Text(viewModel.lmStudioDiscoveryError)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Text("Make sure LM Studio's local server is running, then choose Refresh.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(40)
        } else if viewModel.isDiscoveringLMStudio && viewModel.lmStudioInventory.isEmpty {
            VStack(spacing: 12) {
                ProgressView()
                Text("Reading downloaded models from LM Studio…")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if viewModel.lmStudioInventory.isEmpty {
            VStack(spacing: 12) {
                Image(systemName: "shippingbox")
                    .font(.system(size: 30))
                    .foregroundStyle(.secondary)
                Text("No downloaded LM Studio models were found.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            List(viewModel.lmStudioInventory) { model in
                Toggle(isOn: viewModel.lmStudioSelectionBinding(model.key)) {
                    HStack(spacing: 12) {
                        Image(systemName: model.type == "embedding" ? "point.3.connected.trianglepath.dotted" : "text.bubble")
                            .foregroundStyle(.secondary)
                            .frame(width: 24)
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 7) {
                                Text(model.displayName).fontWeight(.medium)
                                if model.loaded {
                                    inventoryBadge("Loaded", color: .green)
                                }
                                if viewModel.isLMStudioModelProfiled(model.key) {
                                    inventoryBadge("Already profiled", color: .secondary)
                                }
                                if !model.isImportable {
                                    inventoryBadge("Unsupported type", color: .orange)
                                }
                            }
                            Text(model.key)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                            Text(metadata(for: model))
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                                .lineLimit(1)
                        }
                    }
                    .padding(.vertical, 4)
                }
                .toggleStyle(.checkbox)
                .disabled(
                    viewModel.isLMStudioModelProfiled(model.key) || !model.isImportable
                )
            }
            .listStyle(.inset)
        }
    }

    private var importButtonTitle: String {
        let count = viewModel.selectedLMStudioKeys.count
        return count == 1 ? "Create 1 Legacy Profile" : "Create \(count) Legacy Profiles"
    }

    private func inventoryBadge(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12), in: Capsule())
    }

    private func metadata(for model: LMStudioDiscoveredModel) -> String {
        var parts = [model.type.uppercased()]
        if let format = model.format { parts.append(format.uppercased()) }
        if let quantization = model.quantizationName { parts.append(quantization) }
        if let parameters = model.paramsString { parts.append(parameters) }
        if let size = model.sizeBytes {
            parts.append(ByteCountFormatter.string(fromByteCount: size, countStyle: .file))
        }
        if model.vision == true { parts.append("Vision") }
        if model.trainedForToolUse == true { parts.append("Tools") }
        return parts.joined(separator: "  •  ")
    }
}
