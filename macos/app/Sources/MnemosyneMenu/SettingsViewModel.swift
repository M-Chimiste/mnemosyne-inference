import AppKit
import Foundation
import MnemosyneAppCore
import SwiftUI

@MainActor
final class SettingsViewModel: ObservableObject {
    enum Section: String, CaseIterable, Identifiable {
        case setup = "Setup & Health"
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
            case .setup: "checklist"
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

    @Published var selectedSection: Section = .setup
    @Published var settings = NativeSettings()
    @Published var selectedModelIndex: Int?
    @Published var credentialDrafts: [ManagedCredential: String] = [:]
    @Published var credentialsToClear: Set<ManagedCredential> = []
    @Published var confirmDiscard = false
    @Published var confirmRemoveModel = false
    @Published var confirmHomebrewOMLXInstall = false
    @Published var confirmHomebrewOMLXUpgrade = false
    @Published var confirmOMLXCacheReset = false
    @Published var pendingOMLXUpgrade: EngineRuntimeUpdate?
    @Published var showLocalModelImporter = false
    @Published var selectedLocalModelIDs: Set<String> = []
    @Published var localModelAliases: [String: String] = [:]
    @Published var localModelProjectors: [String: String] = [:]
    @Published var libraryQuery = ""
    @Published var selectedLibraryModelID: String?
    @Published var selectedLibraryFileID: String?
    @Published var selectedLibraryProjector = ""
    @Published var selectedLibraryRole: ModelRole = .generation
    @Published var selectedLibraryStorage = "internal"
    @Published private(set) var storageStatuses: [String: StorageStatus] = [:]
    @Published private(set) var libraryModels: [LibraryModel] = []
    @Published private(set) var libraryFileOptions: [LibraryModel] = []
    @Published private(set) var libraryDetails: LibraryModelDetails?
    @Published private(set) var modelInstalls: [ModelInstall] = []
    @Published private(set) var runtimeUpdateSnapshot: RuntimeUpdateSnapshot?
    @Published private(set) var benchmarkSnapshot: EngineBenchmarkSnapshot?
    @Published private(set) var benchmarkingAlias: String?
    @Published private(set) var omlxCacheHealth: OMLXCacheHealth?
    @Published private(set) var readinessSnapshot: ReadinessSnapshot?
    @Published private(set) var lastSelfTest: ModelSelfTestResult?
    @Published private(set) var isRefreshingReadiness = false
    @Published private(set) var isReconciling = false
    @Published private(set) var isRunningSelfTest = false
    @Published private(set) var tokenReportingNodeID = ""
    @Published private(set) var tokenReportingIdentitySource = "computer_name"
    @Published private(set) var isCheckingRuntimeUpdates = false
    @Published private(set) var updatingRuntimeEngine: InferenceEngine?
    @Published private(set) var isInstallingOMLXWithHomebrew = false
    @Published private(set) var isResettingOMLXCache = false
    @Published private(set) var isSearchingLibrary = false
    @Published private(set) var isLoadingLibraryFiles = false
    @Published private(set) var isLoadingLibraryDetails = false
    @Published private(set) var localModelSources: [LocalModelSource] = []
    @Published private(set) var localModelScan: LocalModelScanSnapshot?
    @Published private(set) var localModelScanError = ""
    @Published private(set) var localModelImportError = ""
    @Published private(set) var isScanningLocalModels = false
    @Published private(set) var isImportingLocalModels = false
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
    private var libraryDetailsRequestID: UUID?
    private var installMonitorTask: Task<Void, Never>?
    private var configurationRefreshPending = false

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
            configurationRefreshPending = false
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
            Task { await refreshReadiness() }
            Task { await refreshBenchmarks() }
        } catch {
            isLoaded = false
            setStatus("Could not load settings: \(error.localizedDescription)", tone: .error)
        }
    }

    func refreshReadiness() async {
        guard !isRefreshingReadiness else { return }
        isRefreshingReadiness = true
        defer { isRefreshingReadiness = false }
        do {
            readinessSnapshot = try await client.readiness()
        } catch {
            setStatus(
                "Could not refresh system health: \(error.localizedDescription)",
                tone: .warning
            )
        }
    }

    func refreshBenchmarks() async {
        do {
            benchmarkSnapshot = try await client.benchmarks(alias: nil)
        } catch {
            // Benchmark evidence is optional. Keep the rest of Settings fully
            // usable when an older or degraded service cannot return it.
            benchmarkSnapshot = nil
        }
    }

    func reconcileService() async {
        guard !isReconciling else { return }
        isReconciling = true
        setStatus("Reconciling engine residency…", tone: .normal)
        defer { isReconciling = false }
        do {
            _ = try await client.reconcile()
            await refreshReadiness()
            setStatus("Every enabled engine reported authoritative state.", tone: .success)
        } catch {
            setStatus(
                "Could not reconcile engine state: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    @discardableResult
    func runSelfTest(model: String) async -> Bool {
        guard !model.isEmpty, !isRunningSelfTest else { return false }
        isRunningSelfTest = true
        lastSelfTest = nil
        setStatus("Testing \(model) through the public inference API…", tone: .normal)
        defer { isRunningSelfTest = false }
        do {
            let result = try await client.selfTest(
                model: model,
                includeVision: true,
                unloadAfter: false
            )
            lastSelfTest = result
            await refreshReadiness()
            if result.usage == nil {
                setStatus(
                    "\(model) responded, but it returned no token usage. Choose a language model that reports usage to complete setup.",
                    tone: .warning
                )
            } else if result.usageRecorded != true {
                setStatus(
                    "\(model) responded, but its token usage was not recorded.",
                    tone: .warning
                )
            } else {
                setStatus(
                    "\(model) passed its \(result.vision ? "vision" : "inference") self-test.",
                    tone: .success
                )
            }
            return result.completesGuidedSetup
        } catch {
            setStatus(
                "Model self-test failed: \(error.localizedDescription)",
                tone: .error
            )
            return false
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
        if configurationRefreshPending {
            Task { await refreshModelsConfiguration() }
        }
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
            configurationRefreshPending = false
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
            Task { await refreshBenchmarks() }
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
        guard !isSearchingLibrary else { return }
        isSearchingLibrary = true
        defer { isSearchingLibrary = false }
        do {
            async let found = client.searchLibrary(query: libraryQuery)
            async let installs = client.modelInstalls()
            libraryModels = try await found
            modelInstalls = try await installs
            if modelInstalls.contains(where: \.isActive) {
                beginInstallMonitoring()
            }
            if !libraryModels.contains(where: { $0.id == selectedLibraryModelID }) {
                selectedLibraryModelID = libraryModels.first?.id
            }
            await refreshLibraryFilesForSelection()
            await refreshLibraryDetailsForSelection()
            synchronizeLibraryRole()
        } catch {
            setStatus("Could not browse models: \(error.localizedDescription)", tone: .error)
        }
    }

    func selectLibraryModel(id: String?) {
        guard selectedLibraryModelID != id else { return }
        selectedLibraryModelID = id
        libraryFileOptions = []
        libraryDetails = nil
        selectedLibraryFileID = nil
        selectedLibraryProjector = ""
        synchronizeLibraryRole()
        Task {
            await refreshLibraryFilesForSelection()
            await refreshLibraryDetailsForSelection()
        }
    }

    func selectLibraryFile(id: String?) {
        selectedLibraryFileID = id
        selectedLibraryProjector = selectedLibraryModel?.projectorFilename ?? ""
        synchronizeLibraryRole()
        Task { await refreshLibraryDetailsForSelection() }
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
                    includeProjector: model.availableProjectors.isEmpty
                        || !selectedLibraryProjector.isEmpty,
                    role: selectedLibraryRole
                )
            )
            modelInstalls.insert(install, at: 0)
            setStatus("Download started. You can close this window; the background service owns it.", tone: .success)
            beginInstallMonitoring()
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
            let retried = try await client.retryModelInstall(id: install.id)
            if let index = modelInstalls.firstIndex(where: { $0.id == retried.id }) {
                modelInstalls[index] = retried
            } else {
                modelInstalls.insert(retried, at: 0)
            }
            beginInstallMonitoring()
        } catch {
            setStatus("Could not retry download: \(error.localizedDescription)", tone: .error)
        }
    }

    func dismissInstall(_ install: ModelInstall) async {
        guard install.canDismiss else { return }
        do {
            try await client.dismissModelInstall(id: install.id)
            modelInstalls.removeAll { $0.id == install.id }
            setStatus("Removed \(install.alias) from download history.", tone: .success)
        } catch {
            setStatus(
                "Could not remove download history: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func clearCompletedInstalls() async {
        let completed = modelInstalls.filter { $0.status == "installed" }
        guard !completed.isEmpty else { return }
        var removedIDs: Set<String> = []
        do {
            for install in completed {
                try await client.dismissModelInstall(id: install.id)
                removedIDs.insert(install.id)
            }
            modelInstalls.removeAll { removedIDs.contains($0.id) }
            setStatus(
                "Cleared \(removedIDs.count) completed download\(removedIDs.count == 1 ? "" : "s") from history.",
                tone: .success
            )
        } catch {
            modelInstalls.removeAll { removedIDs.contains($0.id) }
            setStatus(
                "Some completed downloads could not be cleared: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func refreshModelsConfiguration() async {
        await refreshConfigurationFromService(
            announceModelChanges: false,
            deferWhenDirty: false
        )
    }

    func refreshRuntimeUpdates(force: Bool = false) async {
        guard
            !isCheckingRuntimeUpdates,
            updatingRuntimeEngine == nil,
            !isInstallingOMLXWithHomebrew,
            !isResettingOMLXCache
        else { return }
        isCheckingRuntimeUpdates = true
        defer { isCheckingRuntimeUpdates = false }
        do {
            runtimeUpdateSnapshot = force
                ? try await client.checkRuntimeUpdates()
                : try await client.runtimeUpdates(refresh: false)
            if settings.engines.omlx.enabled {
                omlxCacheHealth = try? await client.omlxCacheHealth()
            } else {
                omlxCacheHealth = nil
            }
            if force {
                setStatus("Runtime update check completed.", tone: .success)
            }
        } catch {
            setStatus("Could not check runtime updates: \(error.localizedDescription)", tone: .warning)
        }
    }

    func resetOMLXCache() async {
        guard
            !isResettingOMLXCache,
            updatingRuntimeEngine == nil,
            !isInstallingOMLXWithHomebrew
        else { return }
        isResettingOMLXCache = true
        setStatus("Draining inference and resetting the oMLX SSD cache…", tone: .normal)
        defer { isResettingOMLXCache = false }
        do {
            let result = try await client.resetOMLXCache()
            if let cache = result.cache {
                omlxCacheHealth = cache
            }
            setStatus(
                "Reset the oMLX SSD cache (\(result.deletedFiles) files). Model weights were not changed.",
                tone: .success
            )
        } catch {
            setStatus(
                "Could not reset the oMLX cache: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func installOMLXWithHomebrew() async {
        guard
            !isInstallingOMLXWithHomebrew,
            updatingRuntimeEngine == nil,
            !isResettingOMLXCache
        else { return }
        guard let executable = HomebrewOMLXInstaller.executableURL() else {
            setStatus(
                "Homebrew was not found. Use the recommended official oMLX app installer instead.",
                tone: .warning
            )
            return
        }

        isInstallingOMLXWithHomebrew = true
        defer { isInstallingOMLXWithHomebrew = false }
        do {
            setStatus("Adding the official oMLX Homebrew tap…", tone: .normal)
            _ = try await HomebrewOMLXInstaller.run(
                executableURL: executable,
                arguments: HomebrewOMLXInstaller.commands[0]
            )
            setStatus("Installing the stable oMLX Homebrew formula…", tone: .normal)
            _ = try await HomebrewOMLXInstaller.run(
                executableURL: executable,
                arguments: HomebrewOMLXInstaller.commands[1]
            )
            runtimeUpdateSnapshot = try await client.checkRuntimeUpdates()
            setStatus(
                "oMLX was installed with Homebrew. Configure and start it on 127.0.0.1:17322, then enable the engine.",
                tone: .success
            )
        } catch {
            setStatus(
                "Could not install oMLX with Homebrew: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func openOMLXApplication(_ update: EngineRuntimeUpdate) {
        guard
            update.engine == .omlx,
            let installedPath = update.installedPath,
            installedPath.hasSuffix(".app")
        else {
            setStatus(
                "The installed oMLX application could not be located.",
                tone: .warning
            )
            return
        }
        if NSWorkspace.shared.open(URL(fileURLWithPath: installedPath)) {
            setStatus(
                "Opened oMLX. Start its server on 127.0.0.1:17322, then check again.",
                tone: .success
            )
        } else {
            setStatus("Could not open oMLX at \(installedPath).", tone: .error)
        }
    }

    func installRuntimeUpdate(_ update: EngineRuntimeUpdate) async {
        guard
            update.canInstall,
            updatingRuntimeEngine == nil,
            !isResettingOMLXCache
        else { return }
        updatingRuntimeEngine = update.engine
        setStatus(
            update.engine == .omlx
                ? "Draining inference before the Homebrew-owned oMLX update…"
                : "Preparing the official \(update.displayName) runtime…",
            tone: .normal
        )
        defer { updatingRuntimeEngine = nil }
        do {
            runtimeUpdateSnapshot = try await client.installRuntimeUpdate(
                engine: update.engine,
                version: update.availableVersion ?? update.latestUpstreamVersion
            )
            await refreshModelLibrary()
            setStatus(
                update.engine == .omlx
                    ? "oMLX was drained, updated by Homebrew, restarted, and validated."
                    : "\(update.displayName) was updated. The previous runtime remains available for rollback.",
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
        guard
            update.canRollback,
            updatingRuntimeEngine == nil,
            !isResettingOMLXCache
        else { return }
        updatingRuntimeEngine = update.engine
        setStatus("Rolling back \(update.displayName)…", tone: .normal)
        defer { updatingRuntimeEngine = nil }
        do {
            runtimeUpdateSnapshot = try await client.rollbackRuntimeUpdate(
                engine: update.engine
            )
            await refreshModelLibrary()
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
        case .mlxcel, .mistralRs:
            return [.generation]
        }
    }

    func storageStatus(for name: String) -> StorageStatus? {
        storageStatuses[name]
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
            localModelProjectors = Dictionary(
                uniqueKeysWithValues: scan.models.compactMap { candidate in
                    candidate.recommendedProjectorId.map { (candidate.id, $0) }
                }
            )
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
            let projectorID = nonempty(localModelProjectors[candidate.id])
            return LocalModelImportSelection(
                candidateId: candidate.id,
                alias: nonempty(localModelAliases[candidate.id]),
                projectorId: projectorID,
                includeProjector: candidate.projectorOptions.isEmpty
                    || projectorID != nil
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

    func deleteSelectedModelFiles() async {
        guard
            let index = selectedModelIndex,
            settings.models.indices.contains(index)
        else {
            return
        }
        guard !hasUnsavedChanges else {
            setStatus(
                "Save or discard pending settings before deleting model files.",
                tone: .warning
            )
            return
        }

        let alias = settings.models[index].alias
        isWorking = true
        setStatus("Deleting \(alias) and its managed files…", tone: .normal)
        defer { isWorking = false }
        do {
            let result = try await client.deleteManagedModel(
                alias: alias,
                revision: configurationRevision
            )
            settings = result.config
            savedSettings = result.config
            configurationRevision = result.revision
            appliedConfigurationRevision = result.revision
            requiresRestart = result.restartRequired
            configurationRefreshPending = false
            modelInstalls.removeAll { $0.alias == alias }
            if settings.models.isEmpty {
                selectedModelIndex = nil
            } else {
                selectedModelIndex = min(index, settings.models.count - 1)
            }
            setStatus(
                "Deleted \(alias), removed its profile, and released its model storage.",
                tone: .success
            )
        } catch {
            setStatus(
                "Could not delete \(alias): \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func selectRole(_ role: ModelRole, for index: Int) {
        guard settings.models.indices.contains(index) else { return }
        settings.models[index].applyRole(role)
    }

    func compatibleAlternativeSources(for targetIndex: Int) -> [Int] {
        guard settings.models.indices.contains(targetIndex) else { return [] }
        let target = settings.models[targetIndex]
        guard
            target.kind == .language,
            target.configuredRole == .generation,
            engineIsEnabled(target.engine)
        else {
            return []
        }
        let existingEngines = Set(
            [target.engine] + target.alternatives.map(\.engine)
        )
        return settings.models.indices.filter { index in
            guard index != targetIndex else { return false }
            let candidate = settings.models[index]
            guard
                candidate.enabled,
                candidate.kind == .language,
                candidate.configuredRole == .generation,
                candidate.alternatives.isEmpty,
                candidate.selection.mode == "fixed",
                engineIsEnabled(candidate.engine),
                !existingEngines.contains(candidate.engine)
            else { return false }
            return true
        }
    }

    func canBenchmarkModel(at index: Int) -> Bool {
        guard settings.models.indices.contains(index) else { return false }
        let profile = settings.models[index]
        guard
            profile.enabled,
            profile.configuredRole == .generation,
            engineIsEnabled(profile.engine)
        else { return false }
        let enabledAlternatives = profile.alternatives.filter {
            $0.enabled && engineIsEnabled($0.engine)
        }
        return !enabledAlternatives.isEmpty
    }

    private func engineIsEnabled(_ engine: InferenceEngine) -> Bool {
        switch engine {
        case .llamaCpp: settings.engines.llamaCpp.enabled
        case .omlx: settings.engines.omlx.enabled
        case .ds4: settings.engines.ds4.enabled
        case .mflux: settings.engines.mflux.enabled
        case .mlxcel: settings.engines.mlxcel.enabled
        case .mistralRs: settings.engines.mistralRs.enabled
        }
    }

    func attachAlternative(sourceIndex: Int, to targetIndex: Int) {
        guard
            settings.models.indices.contains(sourceIndex),
            settings.models.indices.contains(targetIndex),
            sourceIndex != targetIndex,
            compatibleAlternativeSources(for: targetIndex).contains(sourceIndex)
        else { return }
        let source = settings.models[sourceIndex]
        let targetAlias = settings.models[targetIndex].alias
        let alternative = ModelEngineAlternativeSettings(
            engine: source.engine,
            model: source.model,
            sourceAlias: source.alias,
            storage: source.storage,
            servedModelName: source.servedModelName,
            capabilities: source.capabilities,
            load: source.load,
            enabled: source.enabled
        )
        settings.models[targetIndex].alternatives.append(alternative)
        // Keep the exact source profile as a disabled recovery record. It is
        // omitted from the callable catalog but makes attachment reversible
        // without reconstructing paths or downloading weights again.
        settings.models[sourceIndex].enabled = false
        selectedModelIndex = settings.models.firstIndex { $0.alias == targetAlias }
        setStatus(
            "Attached \(source.engine.displayName) as an alternative for \(targetAlias). Save, then benchmark the engines.",
            tone: .normal
        )
    }

    func detachAlternative(id: String, from targetIndex: Int) {
        guard settings.models.indices.contains(targetIndex),
              let alternativeIndex = settings.models[targetIndex].alternatives
                .firstIndex(where: { $0.id == id })
        else { return }
        let alternative = settings.models[targetIndex].alternatives[alternativeIndex]
        var restoredSource = false
        if let sourceIndex = settings.models.indices.first(where: { index in
            guard index != targetIndex else { return false }
            let candidate = settings.models[index]
            return !candidate.enabled
                && (
                    alternative.sourceAlias == nil
                    || candidate.alias == alternative.sourceAlias
                )
                && candidate.engine == alternative.engine
                && candidate.model == alternative.model
                && candidate.storage == alternative.storage
                && candidate.servedModelName == alternative.servedModelName
                && candidate.capabilities == alternative.capabilities
                && candidate.load == alternative.load
        }) {
            settings.models[sourceIndex].enabled = true
            restoredSource = true
        }
        if !restoredSource {
            let targetAlias = settings.models[targetIndex].alias
            let preferred = alternative.sourceAlias
                ?? "\(targetAlias)-\(alternative.engine.rawValue)"
            let usedAliases = Set(settings.models.map(\.alias))
            var restoredAlias = preferred
            var stem = String(preferred.prefix(110))
                .trimmingCharacters(in: CharacterSet(charactersIn: ".-"))
            if stem.isEmpty { stem = "restored-model" }
            if restoredAlias.count > 128 {
                restoredAlias = stem
            }
            var suffix = 2
            while usedAliases.contains(restoredAlias) {
                restoredAlias = "\(stem)-\(suffix)"
                suffix += 1
            }
            settings.models.append(
                ModelProfileSettings(
                    alias: restoredAlias,
                    engine: alternative.engine,
                    model: alternative.model,
                    storage: alternative.storage,
                    servedModelName: alternative.servedModelName,
                    capabilities: alternative.capabilities,
                    load: alternative.load,
                    kind: .language,
                    enabled: true
                )
            )
        }
        settings.models[targetIndex].alternatives.remove(at: alternativeIndex)
        if settings.models[targetIndex].alternatives.isEmpty
            || settings.models[targetIndex].selection.pinnedEngine
                == alternative.engine {
            settings.models[targetIndex].selection.mode = "fixed"
            settings.models[targetIndex].selection.pinnedEngine = nil
        }
        setStatus(
            "Detached \(alternative.engine.displayName). Its original profile and weights were preserved.",
            tone: .normal
        )
    }

    func runEngineBenchmark(alias: String) async {
        guard benchmarkingAlias == nil, !hasUnsavedChanges else { return }
        benchmarkingAlias = alias
        setStatus("Benchmarking every compatible engine for \(alias)…", tone: .normal)
        defer { benchmarkingAlias = nil }
        do {
            let sampleRuns = settings.models.first(where: { $0.alias == alias })?
                .selection.minimumSamples ?? 3
            let run = try await client.runBenchmark(
                alias: alias,
                sampleRuns: sampleRuns
            )
            benchmarkSnapshot = try await client.benchmarks(alias: nil)
            let failures = run.failures.count
            let suffix = failures == 0
                ? ""
                : " (\(failures) engine\(failures == 1 ? "" : "s") failed)"
            setStatus(
                "Selected \(run.decision.selectedEngine.displayName) for \(alias)\(suffix).",
                tone: failures == 0 ? .success : .warning
            )
        } catch {
            setStatus(
                "Could not benchmark \(alias): \(error.localizedDescription)",
                tone: .error
            )
        }
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
            libraryDetails = nil
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
            } else {
                selectedLibraryProjector = selectedLibraryModel?.projectorFilename ?? ""
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

    private func refreshLibraryDetailsForSelection() async {
        guard let model = selectedLibraryModel ?? selectedLibrarySearchResult else {
            libraryDetailsRequestID = nil
            libraryDetails = nil
            isLoadingLibraryDetails = false
            return
        }
        let requestID = UUID()
        libraryDetailsRequestID = requestID
        isLoadingLibraryDetails = true
        defer {
            if libraryDetailsRequestID == requestID {
                isLoadingLibraryDetails = false
            }
        }
        do {
            let details = try await client.libraryDetails(
                repoId: model.repoId,
                engine: model.engine,
                filename: model.filename,
                revision: model.resolvedRevision
            )
            guard libraryDetailsRequestID == requestID else { return }
            libraryDetails = details
        } catch {
            guard libraryDetailsRequestID == requestID else { return }
            libraryDetails = nil
        }
    }

    @discardableResult
    private func refreshInstalls() async -> Bool {
        do {
            modelInstalls = try await client.modelInstalls()
            return true
        } catch {
            setStatus("Could not refresh downloads: \(error.localizedDescription)", tone: .warning)
            return false
        }
    }

    private func beginInstallMonitoring() {
        guard installMonitorTask == nil else { return }
        installMonitorTask = Task { [weak self] in
            await self?.monitorActiveInstalls()
        }
    }

    private func monitorActiveInstalls() async {
        var observationState = ModelInstallMonitorState(installs: modelInstalls)
        defer { installMonitorTask = nil }

        while !Task.isCancelled {
            let refreshed = await refreshInstalls()
            if refreshed {
                let observation = observationState.observe(modelInstalls)
                var refreshedConfiguration = false
                if !observation.newlyInstalledAliases.isEmpty {
                    await refreshConfigurationFromService(
                        announceModelChanges: true,
                        deferWhenDirty: true
                    )
                    refreshedConfiguration = true
                }
                if !observation.hasActiveInstalls {
                    if !refreshedConfiguration {
                        await refreshConfigurationFromService(
                            announceModelChanges: true,
                            deferWhenDirty: true
                        )
                    }
                    return
                }
            }

            do {
                try await Task.sleep(for: .seconds(2))
            } catch {
                return
            }
        }
    }

    private func refreshConfigurationFromService(
        announceModelChanges: Bool,
        deferWhenDirty: Bool
    ) async {
        guard !hasUnsavedChanges else {
            if deferWhenDirty {
                configurationRefreshPending = true
                setStatus(
                    "A downloaded model is ready. Save or discard your pending settings to refresh Models safely.",
                    tone: .warning
                )
            }
            return
        }

        do {
            let snapshot = try await client.configuration()
            let previousAliases = Set(savedSettings.models.map(\.alias))
            let selectedAlias = selectedModelIndex.flatMap { index in
                settings.models.indices.contains(index)
                    ? settings.models[index].alias
                    : nil
            }
            let addedAliases = snapshot.config.models.map(\.alias).filter {
                !previousAliases.contains($0)
            }

            settings = snapshot.config
            savedSettings = snapshot.config
            configurationRevision = snapshot.revision
            appliedConfigurationRevision = snapshot.appliedRevision
            requiresRestart = snapshot.restartRequired
            selectedLibraryStorage = snapshot.config.storage.default
            configurationRefreshPending = false

            selectedModelIndex = selectedAlias.flatMap { alias in
                settings.models.firstIndex { $0.alias == alias }
            } ?? addedAliases.first.flatMap { alias in
                settings.models.firstIndex { $0.alias == alias }
            } ?? (settings.models.isEmpty ? nil : 0)

            if announceModelChanges, !addedAliases.isEmpty {
                let noun = addedAliases.count == 1 ? "model" : "models"
                setStatus(
                    "\(addedAliases.count) downloaded \(noun) added to Models.",
                    tone: .success
                )
            }
        } catch {
            if deferWhenDirty {
                configurationRefreshPending = true
            }
            setStatus(
                "Could not refresh installed models: \(error.localizedDescription)",
                tone: .warning
            )
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
