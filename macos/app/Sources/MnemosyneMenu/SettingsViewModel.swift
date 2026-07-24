import AppKit
import Foundation
import MnemosyneAppCore
import SwiftUI

@MainActor
final class SettingsViewModel: ObservableObject {
    enum Section: String, CaseIterable, Identifiable {
        case general = "General"
        case engines = "Engines"
        case updates = "Runtime Updates"
        case storage = "Storage"
        case library = "Model Library"
        case models = "Models"
        case usage = "Usage"
        case credentials = "Credentials"

        var id: String { rawValue }

        var symbol: String {
            switch self {
            case .general: "gearshape"
            case .engines: "cpu"
            case .updates: "arrow.triangle.2.circlepath.circle"
            case .storage: "externaldrive"
            case .library: "square.and.arrow.down"
            case .models: "shippingbox"
            case .usage: "chart.bar"
            case .credentials: "key"
            }
        }
    }

    enum StatusTone {
        case normal
        case success
        case warning
        case error
    }

    @Published var selectedSection: Section = .general
    @Published var settings = NativeSettings()
    @Published var selectedModelIndex: Int?
    @Published var credentialDrafts: [ManagedCredential: String] = [:]
    @Published var credentialsToClear: Set<ManagedCredential> = []
    @Published var confirmDiscard = false
    @Published var confirmRemoveModel = false
    @Published var showLMStudioImporter = false
    @Published var showLocalModelImporter = false
    @Published var selectedLMStudioKeys: Set<String> = []
    @Published var selectedLocalModelIDs: Set<String> = []
    @Published var localModelAliases: [String: String] = [:]
    @Published var localModelProjectors: [String: String] = [:]
    @Published var libraryEngine: InferenceEngine = .omlx
    @Published var libraryQuery = ""
    @Published var selectedLibraryModelID: String?
    @Published var selectedLibraryFileID: String?
    @Published var selectedLibraryProjector = ""
    @Published var selectedLibraryRole: ModelRole = .generation
    @Published var selectedLibraryStorage = "internal"
    @Published private(set) var storageStatuses: [String: StorageStatus] = [:]
    @Published private(set) var libraryModels: [LibraryModel] = []
    @Published private(set) var libraryFileOptions: [LibraryModel] = []
    @Published private(set) var modelInstalls: [ModelInstall] = []
    @Published private(set) var runtimeUpdateSnapshot: RuntimeUpdateSnapshot?
    @Published private(set) var tokenReportingNodeID = ""
    @Published private(set) var tokenReportingIdentitySource = "computer_name"
    @Published private(set) var isCheckingRuntimeUpdates = false
    @Published private(set) var updatingRuntimeEngine: InferenceEngine?
    @Published private(set) var isSearchingLibrary = false
    @Published private(set) var isLoadingLibraryFiles = false
    @Published private(set) var localModelSources: [LocalModelSource] = []
    @Published private(set) var localModelScan: LocalModelScanSnapshot?
    @Published private(set) var localModelScanError = ""
    @Published private(set) var localModelImportError = ""
    @Published private(set) var isScanningLocalModels = false
    @Published private(set) var isImportingLocalModels = false
    @Published private(set) var lmStudioInventory: [LMStudioDiscoveredModel] = []
    @Published private(set) var lmStudioDiscoveryError = ""
    @Published private(set) var isDiscoveringLMStudio = false
    @Published private(set) var configuredCredentials: Set<ManagedCredential> = []
    @Published private(set) var isWorking = false
    @Published private(set) var isLoaded = false
    @Published private(set) var requiresRestart = false
    @Published private(set) var statusMessage = ""
    @Published private(set) var statusTone: StatusTone = .normal

    let configURL: URL
    let environmentURL: URL

    private var client: any ControlAPI
    private let credentialStore: CredentialStore
    private var savedSettings = NativeSettings()
    private var configurationRevision = ""
    private var appliedConfigurationRevision = ""
    private var libraryFileRequestID: UUID?

    init(
        configuration: ControlConnectionConfiguration = .load(),
        client: (any ControlAPI)? = nil
    ) {
        configURL = configuration.configURL
        environmentURL = configuration.environmentURL
        credentialStore = CredentialStore(environmentURL: configuration.environmentURL)
        self.client = client ?? ControlAPIClient(
            baseURL: configuration.baseURL,
            adminPassword: configuration.adminPassword
        )
    }

    var hasUnsavedChanges: Bool {
        settings != savedSettings
            || credentialDrafts.values.contains(where: { !$0.isEmpty })
            || !credentialsToClear.isEmpty
    }

    var configurationSchemaIsSupported: Bool {
        settings.schemaVersion <= NativeSettings.supportedSchemaVersion
    }

    var lmStudioInventoryAvailability: LMStudioInventoryAvailability {
        LMStudioInventoryAvailability.evaluate(
            draft: settings.engines.lmstudio,
            saved: savedSettings.engines.lmstudio,
            savedRevision: configurationRevision,
            appliedRevision: appliedConfigurationRevision,
            restartRequired: requiresRestart
        )
    }

    var canDiscoverLMStudioInventory: Bool {
        lmStudioInventoryAvailability.canOpen && !isDiscoveringLMStudio
    }

    var statusColor: Color {
        switch statusTone {
        case .normal: .secondary
        case .success: .green
        case .warning: .orange
        case .error: .red
        }
    }

    var tokenReportingIdentityDescription: String {
        switch tokenReportingIdentitySource {
        case "token_sidecar":
            "Migrated from the previous token sidecar"
        case "configured":
            "Explicit reporting override"
        default:
            "Derived from this Mac's computer name"
        }
    }

    func load() async {
        guard !isWorking else { return }
        isWorking = true
        setStatus("Loading settings…", tone: .normal)
        defer { isWorking = false }
        do {
            async let configurationRequest = client.configuration()
            async let statusRequest = client.status()
            let snapshot = try await configurationRequest
            let loaded = snapshot.config
            let serviceStatus = try? await statusRequest
            let credentialStatus = try credentialStore.status()
            settings = loaded
            savedSettings = loaded
            configurationRevision = snapshot.revision
            appliedConfigurationRevision = snapshot.appliedRevision
            selectedLibraryStorage = loaded.storage.default
            configuredCredentials = credentialStatus.configured
            credentialDrafts = [:]
            credentialsToClear = []
            selectedModelIndex = loaded.models.isEmpty ? nil : 0
            requiresRestart = snapshot.restartRequired
            isLoaded = true
            tokenReportingNodeID = serviceStatus?.tokenSidecar?.nodeId
                ?? WorkstationIdentity.current.lowercased()
            tokenReportingIdentitySource = serviceStatus?.tokenSidecar?.nodeIdSource
                ?? "computer_name"
            if loaded.schemaVersion > NativeSettings.supportedSchemaVersion {
                setStatus(
                    "These settings use a newer schema. Update Unified Inference before editing or saving them.",
                    tone: .warning
                )
            } else if requiresRestart {
                setStatus(
                    "Settings are saved, but the background service must restart to apply all changes.",
                    tone: .warning
                )
            } else {
                setStatus("Settings are up to date.", tone: .normal)
            }
            Task { await refreshStorageStatuses() }
            Task { await refreshModelLibrary() }
            Task { await refreshLocalModelSources() }
            Task { await refreshRuntimeUpdates() }
        } catch {
            isLoaded = false
            setStatus("Could not load settings: \(error.localizedDescription)", tone: .error)
        }
    }

    func discardChanges() {
        settings = savedSettings
        credentialDrafts = [:]
        credentialsToClear = []
        if let index = selectedModelIndex, !settings.models.indices.contains(index) {
            selectedModelIndex = settings.models.isEmpty ? nil : 0
        }
        setStatus("Unsaved changes were discarded.", tone: .normal)
    }

    func save() async {
        guard !isWorking, hasUnsavedChanges else { return }
        isWorking = true
        setStatus("Validating and saving settings…", tone: .normal)
        defer { isWorking = false }

        normalizeProfiles()
        do {
            let result = try await client.saveConfiguration(
                settings,
                revision: configurationRevision
            )
            settings = result.config
            savedSettings = result.config
            configurationRevision = result.revision
            if result.applied {
                appliedConfigurationRevision = result.revision
            }

            let replacements = credentialDrafts.filter { !$0.value.isEmpty }
            let credentialsChanged = !replacements.isEmpty || !credentialsToClear.isEmpty
            if credentialsChanged {
                try credentialStore.apply(
                    replacements: replacements,
                    clearing: credentialsToClear
                )
                configuredCredentials = try credentialStore.status().configured
                credentialDrafts = [:]
                credentialsToClear = []
            }

            requiresRestart = result.restartRequired || credentialsChanged
            if requiresRestart {
                setStatus(
                    "Settings saved. Restart the background service to apply all changes.",
                    tone: .warning
                )
            } else {
                setStatus(
                    "Settings saved and \(result.modelCount) model profiles applied.",
                    tone: .success
                )
            }
        } catch {
            setStatus("Could not save settings: \(error.localizedDescription)", tone: .error)
        }
    }

    func serviceRestartStarted() -> Bool {
        guard !isWorking else { return false }
        isWorking = true
        requiresRestart = true
        setStatus(
            "Restarting the background service safely…",
            tone: .normal
        )
        return true
    }

    func serviceRestartRequested(succeeded: Bool, error: String?) async {
        defer { isWorking = false }
        guard succeeded else {
            requiresRestart = true
            let detail = error ?? "The background service could not be re-registered."
            setStatus(
                "\(detail) The saved changes still require a restart.",
                tone: .error
            )
            return
        }

        requiresRestart = true
        setStatus(
            "Background service re-registered. Waiting for it to apply the saved settings…",
            tone: .normal
        )

        let expectedRevision = configurationRevision
        let configuration = ControlConnectionConfiguration.load()
        let restartedClient = ControlAPIClient(
            baseURL: configuration.baseURL,
            adminPassword: configuration.adminPassword
        )
        client = restartedClient

        do {
            let snapshot = try await ServiceRestartConfirmation.waitForAppliedConfiguration(
                expectedRevision: expectedRevision
            ) {
                // Status verifies that the complete control service is responding,
                // not merely that its configuration route has appeared.
                _ = try await restartedClient.status()
                return try await restartedClient.configuration()
            }
            configurationRevision = snapshot.revision
            appliedConfigurationRevision = snapshot.appliedRevision
            requiresRestart = false
            setStatus(
                "Background service restarted with the saved settings.",
                tone: .success
            )
        } catch {
            requiresRestart = true
            setStatus(error.localizedDescription, tone: .error)
        }
    }

    func chooseStorageFolder(replacing existingName: String? = nil) async {
        let panel = NSOpenPanel()
        panel.title = existingName == nil ? "Choose a Model Library Folder" : "Change Model Library Folder"
        panel.message = "Choose the exact folder Unified Inference should use. Nested folders such as /Volumes/Athena/models are supported."
        panel.prompt = "Choose Folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        if let existingName,
           let location = settings.storage.locations.first(where: { $0.name == existingName }) {
            panel.directoryURL = URL(fileURLWithPath: NSString(string: location.path).expandingTildeInPath)
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let startedScope = url.startAccessingSecurityScopedResource()
        defer {
            if startedScope {
                url.stopAccessingSecurityScopedResource()
            }
        }

        do {
            let bookmark = try url.bookmarkData(
                options: [],
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            let status = try await client.inspectStorage(
                path: url.path,
                bookmarkData: bookmark
            )
            guard status.isAvailable else {
                throw ControlAPIError.rejected(400, status.diagnostic ?? "That folder is unavailable.")
            }
            let name = existingName ?? uniqueStorageName(for: url)
            let location = StorageLocationSettings(
                name: name,
                path: status.path,
                volumeUuid: status.volumeUuid,
                scopeId: status.scopeId
            )
            if let index = settings.storage.locations.firstIndex(where: { $0.name == name }) {
                settings.storage.locations[index] = location
            } else {
                settings.storage.locations.append(location)
            }
            storageStatuses[name] = StorageStatus(
                name: name,
                path: status.path,
                exists: status.exists,
                isDirectory: status.isDirectory,
                writable: status.writable,
                mountPath: status.mountPath,
                volumeUuid: status.volumeUuid,
                scopeId: status.scopeId,
                expectedVolumeUuid: status.volumeUuid,
                volumeMatches: true,
                totalBytes: status.totalBytes,
                freeBytes: status.freeBytes,
                diagnostic: status.diagnostic
            )
            settings.storage.default = name
            selectedLibraryStorage = name
            setStatus("Model folder selected. Save settings before downloading models.", tone: .normal)
        } catch {
            setStatus("Could not use that folder: \(error.localizedDescription)", tone: .error)
        }
    }

    func removeStorageLocation(_ name: String) {
        guard settings.storage.locations.count > 1 else { return }
        guard !settings.models.contains(where: { $0.storage == name }) else {
            setStatus("That folder is still assigned to a model profile.", tone: .warning)
            return
        }
        settings.storage.locations.removeAll { $0.name == name }
        storageStatuses.removeValue(forKey: name)
        if settings.storage.default == name {
            settings.storage.default = settings.storage.locations[0].name
        }
        if selectedLibraryStorage == name {
            selectedLibraryStorage = settings.storage.default
        }
    }

    func refreshStorageStatuses() async {
        do {
            let snapshot = try await client.storageLocations()
            storageStatuses = Dictionary(
                uniqueKeysWithValues: snapshot.locations.compactMap { status in
                    status.name.map { ($0, status) }
                }
            )
        } catch {
            setStatus("Could not inspect model storage: \(error.localizedDescription)", tone: .warning)
        }
    }

    func refreshModelLibrary() async {
        guard libraryEngine != .lmstudio, !isSearchingLibrary else { return }
        isSearchingLibrary = true
        defer { isSearchingLibrary = false }
        do {
            async let found = client.searchLibrary(query: libraryQuery, engine: libraryEngine)
            async let installs = client.modelInstalls()
            libraryModels = try await found
            modelInstalls = try await installs
            if !libraryModels.contains(where: { $0.id == selectedLibraryModelID }) {
                selectedLibraryModelID = libraryModels.first?.id
            }
            await refreshLibraryFilesForSelection()
            synchronizeLibraryRole()
        } catch {
            setStatus("Could not browse models: \(error.localizedDescription)", tone: .error)
        }
    }

    func selectLibraryEngine(_ engine: InferenceEngine) {
        guard engine != .lmstudio else { return }
        libraryEngine = engine
        libraryModels = []
        libraryFileOptions = []
        selectedLibraryModelID = nil
        selectedLibraryFileID = nil
        selectedLibraryProjector = ""
        selectedLibraryRole = defaultLibraryRole(for: engine)
        Task { await refreshModelLibrary() }
    }

    func selectLibraryModel(id: String?) {
        guard selectedLibraryModelID != id else { return }
        selectedLibraryModelID = id
        libraryFileOptions = []
        selectedLibraryFileID = nil
        selectedLibraryProjector = ""
        synchronizeLibraryRole()
        Task { await refreshLibraryFilesForSelection() }
    }

    func selectLibraryFile(id: String?) {
        selectedLibraryFileID = id
        selectedLibraryProjector = ""
        synchronizeLibraryRole()
    }

    func selectLibraryProjector(_ filename: String) {
        selectedLibraryProjector = filename
        synchronizeLibraryRole()
    }

    func installSelectedLibraryModel() async {
        guard let model = selectedLibraryModel else { return }
        guard settings == savedSettings else {
            setStatus("Save storage changes before starting a model download.", tone: .warning)
            return
        }
        guard !requiresRestart else {
            setStatus(
                "Restart the background service before downloading to a newly configured folder.",
                tone: .warning
            )
            return
        }
        isWorking = true
        setStatus("Starting \(model.displayName)…", tone: .normal)
        defer { isWorking = false }
        do {
            let install = try await client.startModelInstall(
                StartModelInstallRequest(
                    model: model,
                    storage: selectedLibraryStorage,
                    projectorFilename: nonempty(selectedLibraryProjector),
                    role: selectedLibraryRole
                )
            )
            modelInstalls.insert(install, at: 0)
            setStatus("Download started. You can close this window; the background service owns it.", tone: .success)
            Task { await monitorActiveInstalls() }
        } catch {
            setStatus("Could not start download: \(error.localizedDescription)", tone: .error)
        }
    }

    func cancelInstall(_ install: ModelInstall) async {
        do {
            _ = try await client.cancelModelInstall(id: install.id)
            await refreshInstalls()
        } catch {
            setStatus("Could not cancel download: \(error.localizedDescription)", tone: .error)
        }
    }

    func retryInstall(_ install: ModelInstall) async {
        do {
            _ = try await client.retryModelInstall(id: install.id)
            await monitorActiveInstalls()
        } catch {
            setStatus("Could not retry download: \(error.localizedDescription)", tone: .error)
        }
    }

    func refreshRuntimeUpdates(force: Bool = false) async {
        guard !isCheckingRuntimeUpdates, updatingRuntimeEngine == nil else { return }
        isCheckingRuntimeUpdates = true
        defer { isCheckingRuntimeUpdates = false }
        do {
            runtimeUpdateSnapshot = force
                ? try await client.checkRuntimeUpdates()
                : try await client.runtimeUpdates(refresh: false)
            if force {
                setStatus("Runtime update check completed.", tone: .success)
            }
        } catch {
            setStatus("Could not check runtime updates: \(error.localizedDescription)", tone: .warning)
        }
    }

    func installRuntimeUpdate(_ update: EngineRuntimeUpdate) async {
        guard update.canInstall, updatingRuntimeEngine == nil else { return }
        updatingRuntimeEngine = update.engine
        setStatus(
            "Preparing the official \(update.displayName) runtime…",
            tone: .normal
        )
        defer { updatingRuntimeEngine = nil }
        do {
            runtimeUpdateSnapshot = try await client.installRuntimeUpdate(
                engine: update.engine,
                version: update.availableVersion
            )
            if update.engine == libraryEngine {
                await refreshModelLibrary()
            }
            setStatus(
                "\(update.displayName) was updated. The previous runtime remains available for rollback.",
                tone: .success
            )
        } catch {
            setStatus(
                "Could not update \(update.displayName): \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func rollbackRuntimeUpdate(_ update: EngineRuntimeUpdate) async {
        guard update.canRollback, updatingRuntimeEngine == nil else { return }
        updatingRuntimeEngine = update.engine
        setStatus("Rolling back \(update.displayName)…", tone: .normal)
        defer { updatingRuntimeEngine = nil }
        do {
            runtimeUpdateSnapshot = try await client.rollbackRuntimeUpdate(
                engine: update.engine
            )
            if update.engine == libraryEngine {
                await refreshModelLibrary()
            }
            setStatus(
                "\(update.displayName) was rolled back to the previous managed runtime.",
                tone: .success
            )
        } catch {
            setStatus(
                "Could not roll back \(update.displayName): \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    var selectedLibraryModel: LibraryModel? {
        if selectedLibrarySearchResult?.needsFileSelection == true {
            return libraryFileOptions.first { $0.id == selectedLibraryFileID }
        }
        return selectedLibrarySearchResult
    }

    var selectedLibrarySearchResult: LibraryModel? {
        libraryModels.first { $0.id == selectedLibraryModelID }
    }

    var availableLibraryRoles: [ModelRole] {
        guard let model = selectedLibraryModel ?? selectedLibrarySearchResult else {
            return []
        }
        switch model.engine {
        case .llamaCpp:
            return selectedLibraryProjector.isEmpty
                ? [.generation, .embeddings, .rerank]
                : [.generation]
        case .omlx:
            return [.generation, .embeddings, .rerank]
        case .ds4:
            return [.generation]
        case .mflux:
            return [.image]
        case .lmstudio:
            return []
        }
    }

    func storageStatus(for name: String) -> StorageStatus? {
        storageStatuses[name]
    }

    func discoverLMStudioModels() async {
        guard !isDiscoveringLMStudio else { return }
        guard lmStudioInventoryAvailability.canOpen else {
            let guidance = lmStudioInventoryAvailability.guidance
                ?? "LM Studio inventory is not available yet."
            lmStudioInventory = []
            selectedLMStudioKeys = []
            lmStudioDiscoveryError = guidance
            setStatus(guidance, tone: .warning)
            return
        }
        isDiscoveringLMStudio = true
        lmStudioDiscoveryError = ""
        defer { isDiscoveringLMStudio = false }
        do {
            lmStudioInventory = try await client.lmStudioModels().sorted {
                $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending
            }
            selectedLMStudioKeys = []
        } catch {
            lmStudioInventory = []
            selectedLMStudioKeys = []
            lmStudioDiscoveryError = error.localizedDescription
        }
    }

    func isLMStudioModelProfiled(_ key: String) -> Bool {
        profiledLMStudioKeys.contains(key)
    }

    func lmStudioSelectionBinding(_ key: String) -> Binding<Bool> {
        Binding(
            get: { self.selectedLMStudioKeys.contains(key) },
            set: { selected in
                if selected {
                    self.selectedLMStudioKeys.insert(key)
                } else {
                    self.selectedLMStudioKeys.remove(key)
                }
            }
        )
    }

    func selectAllUnprofiledLMStudioModels() {
        let profiled = profiledLMStudioKeys
        selectedLMStudioKeys = Set(
            lmStudioInventory.lazy
                .filter { $0.isImportable && !profiled.contains($0.key) }
                .map(\.key)
        )
    }

    func importSelectedLMStudioModels() {
        var aliases = Set(settings.models.map(\.alias))
        let profiled = profiledLMStudioKeys
        let selected = lmStudioInventory.filter {
            selectedLMStudioKeys.contains($0.key) && !profiled.contains($0.key)
        }
        guard !selected.isEmpty else { return }

        var firstAddedIndex: Int?
        for model in selected {
            let alias = uniqueAlias(for: model.key, existing: &aliases)
            let capabilities = model.type == "embedding"
                ? ModelRole.embeddings.capabilities
                : ModelRole.generation.capabilities
            settings.models.append(
                ModelProfileSettings(
                    alias: alias,
                    engine: .lmstudio,
                    model: model.key,
                    capabilities: capabilities
                )
            )
            firstAddedIndex = firstAddedIndex ?? settings.models.count - 1
        }

        if let firstAddedIndex {
            selectedModelIndex = firstAddedIndex
        }
        selectedLMStudioKeys = []
        showLMStudioImporter = false
        setStatus(
            "Added \(selected.count) LM Studio profiles. Save settings to apply them.",
            tone: .normal
        )
    }

    func refreshLocalModelSources() async {
        do {
            localModelSources = try await client.localModelSources()
        } catch {
            // Source hints are optional. The ordinary Finder picker remains
            // available even when LM Studio has never been installed.
            localModelSources = []
        }
    }

    func chooseExistingModelsFolder(source: LocalModelSource? = nil) async {
        guard !isScanningLocalModels, !isImportingLocalModels else { return }
        guard !hasUnsavedChanges else {
            setStatus(
                "Save or discard current settings changes before adding existing models.",
                tone: .warning
            )
            return
        }
        let panel = NSOpenPanel()
        panel.title = source.map { "Scan \($0.displayName)" } ?? "Add Existing Models"
        panel.message =
            "Choose a folder containing GGUF or MLX models. Unified Inference scans it in place and does not copy or load anything."
        panel.prompt = "Scan Folder"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = false
        panel.allowsMultipleSelection = false
        if let source {
            panel.directoryURL = URL(
                fileURLWithPath: NSString(string: source.path).expandingTildeInPath,
                isDirectory: true
            )
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let startedScope = url.startAccessingSecurityScopedResource()
        defer {
            if startedScope {
                url.stopAccessingSecurityScopedResource()
            }
        }

        showLocalModelImporter = true
        localModelScan = nil
        localModelScanError = ""
        localModelImportError = ""
        selectedLocalModelIDs = []
        localModelAliases = [:]
        localModelProjectors = [:]
        isScanningLocalModels = true
        defer { isScanningLocalModels = false }
        do {
            let bookmark = try url.bookmarkData(
                options: [],
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            let scan = try await client.scanLocalModels(
                path: url.path,
                bookmarkData: bookmark
            )
            localModelScan = scan
            var usedAliases = Set(settings.models.map(\.alias))
            var aliases: [String: String] = [:]
            for candidate in scan.models {
                if let existingAlias = candidate.existingAlias {
                    aliases[candidate.id] = existingAlias
                    continue
                }
                let base = suggestedAlias(
                    for: candidate.displayName,
                    fallback: "local-model"
                )
                var alias = base
                var suffix = 2
                while usedAliases.contains(alias) {
                    alias = "\(base)-\(suffix)"
                    suffix += 1
                }
                usedAliases.insert(alias)
                aliases[candidate.id] = alias
            }
            localModelAliases = aliases
            // Selection is deliberately empty. Model adoption is always an
            // explicit per-row choice, even when every candidate is compatible.
            selectedLocalModelIDs = []
        } catch {
            localModelScanError = error.localizedDescription
        }
    }

    func localModelSelectionBinding(_ id: String) -> Binding<Bool> {
        Binding(
            get: { self.selectedLocalModelIDs.contains(id) },
            set: { selected in
                if selected {
                    self.selectedLocalModelIDs.insert(id)
                } else {
                    self.selectedLocalModelIDs.remove(id)
                }
            }
        )
    }

    func localModelAliasBinding(_ id: String) -> Binding<String> {
        Binding(
            get: { self.localModelAliases[id, default: ""] },
            set: { self.localModelAliases[id] = $0 }
        )
    }

    func localModelProjectorBinding(_ id: String) -> Binding<String> {
        Binding(
            get: { self.localModelProjectors[id, default: ""] },
            set: { self.localModelProjectors[id] = $0 }
        )
    }

    func importSelectedLocalModels() async {
        guard let scan = localModelScan,
              !selectedLocalModelIDs.isEmpty,
              !isImportingLocalModels
        else { return }
        let selected = scan.models.filter { selectedLocalModelIDs.contains($0.id) }
        let selections = selected.map { candidate in
            LocalModelImportSelection(
                candidateId: candidate.id,
                alias: nonempty(localModelAliases[candidate.id]),
                projectorId: nonempty(localModelProjectors[candidate.id])
            )
        }
        isImportingLocalModels = true
        localModelImportError = ""
        setStatus("Adopting selected models in place…", tone: .normal)
        defer { isImportingLocalModels = false }
        do {
            let result = try await client.importLocalModels(
                LocalModelImportRequest(
                    path: scan.root,
                    scopeId: scan.scopeId,
                    selections: selections
                )
            )
            settings = result.config
            savedSettings = result.config
            configurationRevision = result.revision
            if !result.restartRequired {
                appliedConfigurationRevision = result.revision
            }
            selectedLibraryStorage = result.config.storage.default
            requiresRestart = result.restartRequired
            if let firstAlias = result.imported.first?.alias {
                selectedModelIndex = settings.models.firstIndex { $0.alias == firstAlias }
            }
            showLocalModelImporter = false
            selectedLocalModelIDs = []
            await refreshStorageStatuses()
            setStatus(
                "Added \(result.imported.count) existing model\(result.imported.count == 1 ? "" : "s") without copying or loading weights."
                    + (result.restartRequired ? " Restart the background service to finish applying the change." : ""),
                tone: result.restartRequired ? .warning : .success
            )
        } catch {
            localModelImportError = error.localizedDescription
            setStatus(
                "Could not add existing models: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func removeSelectedModel() {
        guard let index = selectedModelIndex, settings.models.indices.contains(index) else { return }
        settings.models.remove(at: index)
        if settings.models.isEmpty {
            selectedModelIndex = nil
        } else {
            selectedModelIndex = min(index, settings.models.count - 1)
        }
    }

    func selectRole(_ role: ModelRole, for index: Int) {
        guard settings.models.indices.contains(index) else { return }
        settings.models[index].applyRole(role)
    }

    func credentialBinding(_ credential: ManagedCredential) -> Binding<String> {
        Binding(
            get: { self.credentialDrafts[credential, default: ""] },
            set: {
                self.credentialDrafts[credential] = $0
                if !$0.isEmpty { self.credentialsToClear.remove(credential) }
            }
        )
    }

    func clearCredential(_ credential: ManagedCredential) {
        credentialDrafts[credential] = ""
        credentialsToClear.insert(credential)
    }

    func undoCredentialClear(_ credential: ManagedCredential) {
        credentialsToClear.remove(credential)
    }

    private func synchronizeLibraryRole() {
        let roles = availableLibraryRoles
        guard !roles.isEmpty else { return }
        if let suggested = (selectedLibraryModel ?? selectedLibrarySearchResult)?.suggestedRole,
           roles.contains(suggested) {
            selectedLibraryRole = suggested
        } else if !roles.contains(selectedLibraryRole) {
            selectedLibraryRole = roles[0]
        }
    }

    private func defaultLibraryRole(for engine: InferenceEngine) -> ModelRole {
        switch engine {
        case .mflux:
            .image
        case .lmstudio, .llamaCpp, .omlx, .ds4:
            .generation
        }
    }

    private func normalizeProfiles() {
        settings.engines.mflux.python = nonempty(settings.engines.mflux.python)
        settings.engines.omlx.modelDirectories = settings.engines.omlx.modelDirectories
            .compactMap(nonempty)
        for index in settings.models.indices {
            settings.models[index].servedModelName = nonempty(
                settings.models[index].servedModelName
            )
            settings.models[index].load.kvDiskDirectory = nonempty(
                settings.models[index].load.kvDiskDirectory
            )
            settings.models[index].load.projectorPath = nonempty(
                settings.models[index].load.projectorPath
            )
            settings.models[index].load.pooling = nonempty(
                settings.models[index].load.pooling
            )
            if settings.models[index].engine == .mflux {
                settings.models[index].kind = .image
                settings.models[index].image = settings.models[index].image ?? .init()
                settings.models[index].load = .init()
                settings.models[index].capabilities = ["images/generations"]
            } else {
                settings.models[index].kind = .language
                settings.models[index].image = nil
            }
        }
    }

    private func refreshLibraryFilesForSelection() async {
        guard let model = selectedLibrarySearchResult, model.needsFileSelection else {
            libraryFileRequestID = nil
            isLoadingLibraryFiles = false
            libraryFileOptions = []
            selectedLibraryFileID = nil
            selectedLibraryProjector = ""
            synchronizeLibraryRole()
            return
        }
        guard model.engine == .llamaCpp else { return }
        let requestID = UUID()
        libraryFileRequestID = requestID
        isLoadingLibraryFiles = true
        defer {
            if libraryFileRequestID == requestID {
                isLoadingLibraryFiles = false
            }
        }
        do {
            let files = try await client.libraryFiles(
                repoId: model.repoId,
                engine: model.engine,
                revision: model.resolvedRevision
            )
            guard libraryFileRequestID == requestID,
                  selectedLibrarySearchResult?.id == model.id
            else { return }
            libraryFileOptions = files
            if !files.contains(where: { $0.id == selectedLibraryFileID }) {
                selectedLibraryFileID = nil
                selectedLibraryProjector = ""
            }
            synchronizeLibraryRole()
        } catch {
            guard libraryFileRequestID == requestID else { return }
            libraryFileOptions = []
            selectedLibraryFileID = nil
            selectedLibraryProjector = ""
            synchronizeLibraryRole()
            setStatus(
                "Could not read GGUF quants for \(model.displayName): \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    private func refreshInstalls() async {
        do {
            modelInstalls = try await client.modelInstalls()
        } catch {
            setStatus("Could not refresh downloads: \(error.localizedDescription)", tone: .warning)
        }
    }

    private func monitorActiveInstalls() async {
        for _ in 0 ..< 240 {
            await refreshInstalls()
            if !modelInstalls.contains(where: \.isActive) { return }
            try? await Task.sleep(for: .seconds(2))
            if Task.isCancelled { return }
        }
    }

    private func uniqueStorageName(for url: URL) -> String {
        var label = url.lastPathComponent
        if label.caseInsensitiveCompare("models") == .orderedSame {
            label = "\(url.deletingLastPathComponent().lastPathComponent)-models"
        }
        var base = label.lowercased()
            .map { $0.isASCII && ($0.isLetter || $0.isNumber) ? $0 : "-" }
            .reduce(into: "") { result, character in
                if character != "-" || result.last != "-" { result.append(character) }
            }
            .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        if base.isEmpty { base = "model-library" }
        var candidate = base
        var suffix = 2
        let existing = Set(settings.storage.locations.map(\.name))
        while existing.contains(candidate) {
            candidate = "\(base)-\(suffix)"
            suffix += 1
        }
        return candidate
    }

    private var profiledLMStudioKeys: Set<String> {
        Set(
            settings.models.lazy
                .filter { $0.engine == .lmstudio }
                .map(\.model)
        )
    }

    private func uniqueAlias(for modelKey: String, existing: inout Set<String>) -> String {
        var alias = suggestedAlias(for: modelKey, fallback: "lmstudio-model")
        let base = alias
        var suffix = 2
        while existing.contains(alias) {
            alias = "\(base)-\(suffix)"
            suffix += 1
        }
        existing.insert(alias)
        return alias
    }

    private func suggestedAlias(for name: String, fallback: String) -> String {
        var source = name.lowercased()
        for suffix in ["-safetensors", ".safetensors", "_safetensors", "-gguf", ".gguf", "_gguf", "-mlx", ".mlx", "_mlx"] {
            if source.hasSuffix(suffix) {
                source.removeLast(suffix.count)
                break
            }
        }
        var alias = source
            .map { character -> Character in
                if character.isASCII && (character.isLetter || character.isNumber) {
                    return character
                }
                return character == "." ? "." : "-"
            }
            .reduce(into: "") { result, character in
                if (character != "-" && character != ".") || result.last != character {
                    result.append(character)
                }
            }
            .trimmingCharacters(in: CharacterSet(charactersIn: ".-"))
        if alias.isEmpty { alias = fallback }
        return alias
    }

    private func nonempty(_ value: String?) -> String? {
        guard let value, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        return value
    }

    private func setStatus(_ message: String, tone: StatusTone) {
        statusMessage = message
        statusTone = tone
    }
}
