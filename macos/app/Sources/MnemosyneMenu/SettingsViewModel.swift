import AppKit
import Foundation
import MnemosyneAppCore
import SwiftUI

@MainActor
final class SettingsViewModel: ObservableObject {
    enum Section: String, CaseIterable, Identifiable {
        case setup = "Setup & Health"
        case general = "General"
        case hub = "Hub Mode"
        case pool = "Inference Pool"
        case engines = "Engines"
        case updates = "Runtime Updates"
        case storage = "Storage"
        case library = "Model Library"
        case models = "Models"
        case lifecycle = "Migration & Removal"
        case usage = "Usage"
        case credentials = "Credentials"

        var id: String { rawValue }

        var symbol: String {
            switch self {
            case .setup: "checklist"
            case .general: "gearshape"
            case .hub: "network"
            case .pool: "point.3.connected.trianglepath.dotted"
            case .engines: "cpu"
            case .updates: "arrow.triangle.2.circlepath.circle"
            case .storage: "externaldrive"
            case .library: "square.and.arrow.down"
            case .models: "shippingbox"
            case .lifecycle: "shippingbox.and.arrow.backward"
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

    private enum DesiredInstallMutationAction {
        case approve
        case refuse
        case cancel

        var description: String {
            switch self {
            case .approve: "approve the download"
            case .refuse: "refuse the download"
            case .cancel: "stop the download"
            }
        }

        func isPermitted(for item: DesiredInstallItem) -> Bool {
            switch self {
            case .approve: item.canApprove
            case .refuse: item.canRefuse
            case .cancel: item.canCancel
            }
        }
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
    @Published var confirmDS4GLM53PreviewInstall = false
    @Published var confirmAppleDeveloperToolsInstall = false
    @Published var confirmOMLXCacheReset = false
    @Published var confirmDiscardRejectedFleetPairingAttempt = false
    @Published var confirmRevokeFleetPairing = false
    @Published var fleetPairingHubAddress = ""
    @Published var showAdvancedFleetPairing = false
    @Published var confirmPrepareNativeLifecycle = false
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
    @Published private var modelLibraryDownload = ModelLibraryDownloadViewModel()
    @Published private(set) var libraryFileOptions: [LibraryModel] = []
    @Published private(set) var modelInstalls: [ModelInstall] = []
    @Published private(set) var modelCleanupDecision: ModelCleanupDecision =
        .refused(.preparationRequired)
    @Published private(set) var runtimeUpdateSnapshot: RuntimeUpdateSnapshot?
    @Published private(set) var benchmarkSnapshot: EngineBenchmarkSnapshot?
    @Published private(set) var benchmarkingAlias: String?
    @Published private(set) var contextSnapshot: ContextWindowSnapshot?
    @Published private(set) var profilingContextAlias: String?
    @Published private(set) var omlxCacheHealth: OMLXCacheHealth?
    @Published private(set) var readinessSnapshot: ReadinessSnapshot?
    @Published private(set) var appleDeveloperToolsInstalled: Bool?
    @Published private(set) var lastSelfTest: ModelSelfTestResult?
    @Published private(set) var fleetPairing: FleetPairingSnapshot?
    @Published var fleetPairingCeremony = FleetPairingCeremonyState()
    @Published private(set) var desiredInstalls = DesiredInstallViewModel()
    @Published private(set) var desiredInstallInFlightJobIDs: Set<String> = []
    @Published private(set) var desiredInstallError = ""
    @Published private(set) var nativeLifecycleStatus:
        NativeLifecycleStatusSnapshot?
    @Published private(set) var nativeMigrationPreview:
        NativeLifecycleMigrationPreview?
    @Published private(set) var nativeUninstallPreviews:
        [NativeLifecycleRetentionMode: NativeLifecycleUninstallPreview] = [:]
    @Published private(set) var nativeUninstallPreviewErrors:
        [NativeLifecycleRetentionMode: String] = [:]
    @Published private(set) var preparedNativeLifecycleTransaction:
        NativeLifecycleTransaction?
    @Published private(set) var authorizedNativeLifecycleTransaction:
        NativeLifecycleTransaction?
    @Published private(set) var nativeLifecycleAuthorizationStatuses:
        [String: NativeLifecycleAuthorizationStatus] = [:]
    @Published private(set) var nativeLifecycleMessage = ""
    @Published private(set) var isAdvancingFleetPairing = false
    @Published private(set) var isRequestingFleetPairing = false
    @Published private(set) var fleetPairingPresencePIN: String?
    @Published private(set) var fleetPairingPresenceExpiresAt: Double?
    @Published private(set) var isDiscardingFleetPairingAttempt = false
    @Published private(set) var isRevokingFleetPairing = false
    @Published private(set) var isRefreshingDesiredInstalls = false
    @Published private(set) var isRefreshingNativeLifecycle = false
    @Published private(set) var isPreparingNativeLifecycle = false
    @Published private(set) var isAuthorizingNativeLifecycle = false
    @Published private(set) var isRefreshingReadiness = false
    @Published private(set) var isReconciling = false
    @Published private(set) var isRunningSelfTest = false
    @Published private(set) var tokenReportingNodeID = ""
    @Published private(set) var tokenReportingIdentitySource = "computer_name"
    @Published private(set) var isCheckingRuntimeUpdates = false
    @Published private(set) var updatingRuntimeEngine: InferenceEngine?
    @Published private(set) var isInstallingOMLXWithHomebrew = false
    @Published private(set) var isRequestingAppleDeveloperTools = false
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
    private var pendingModelCleanupProfile: ModelProfileSettings?
    private var fleetPairingTask: Task<Void, Never>?
    private var fleetPairingTaskGeneration = 0
    private var fleetPairingPresenceRequestID: String?
    private var fleetSelfRevokeRequestID: String?
    private let tailscale = HubTailscaleManager()
    private var configurationRefreshPending = false
    private var pendingNativeLifecyclePreview: NativeLifecycleUninstallPreview?

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

    var inferenceAPIKeyWillBeConfigured: Bool {
        if credentialsToClear.contains(.inferenceAPIKey) {
            return false
        }
        if let draft = credentialDrafts[.inferenceAPIKey],
           !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        {
            return true
        }
        return configuredCredentials.contains(.inferenceAPIKey)
    }

    var pairingOwnsFleetCredentials: Bool {
        fleetPairing?.ownsFleetCredentials == true
    }

    var canDiscardRejectedFleetPairingAttempt: Bool {
        fleetPairing?.canDiscardTerminalAttempt == true
    }

    var canRequestFleetPairing: Bool {
        !fleetPairingHubAddress.trimmingCharacters(
            in: .whitespacesAndNewlines
        ).isEmpty
            && !isRequestingFleetPairing
            && !isAdvancingFleetPairing
    }

    var fleetPairingPresencePINDisplay: String? {
        fleetPairingPresencePIN.map {
            $0.map(String.init).joined(separator: " ")
        }
    }

    var pendingNativeLifecycleMode: NativeLifecycleRetentionMode? {
        pendingNativeLifecyclePreview?.plan.retentionMode
    }

    var nativeMigrationIncompleteCount: Int {
        nativeLifecycleStatus?.incomplete.filter { $0.kind == .migration }.count
            ?? 0
    }

    var nativeUninstallIncompleteCount: Int {
        nativeLifecycleStatus?.incomplete.filter { $0.kind == .uninstall }.count
            ?? 0
    }

    var selectedLibraryStorage: String {
        get { modelLibraryDownload.selectedStorageKey }
        set { modelLibraryDownload.selectStorage(newValue) }
    }

    var libraryModels: [LibraryModel] {
        get { modelLibraryDownload.searchResults }
        set { modelLibraryDownload.applySearchResults(newValue) }
    }

    var ds4RuntimeUpdate: EngineRuntimeUpdate? {
        runtimeUpdateSnapshot?.engines.first { $0.engine == .ds4 }
    }

    var ds4GLM53FlashPreview: ManagedRuntimeChannel? {
        ds4RuntimeUpdate?.ds4GLM53FlashPreview
    }

    var shouldOfferGLM53PreviewRuntimeInstall: Bool {
        GLM53PreviewPresentation.shouldOfferRuntimeInstall(
            query: libraryQuery,
            models: libraryModels,
            ds4Update: ds4RuntimeUpdate
        )
    }

    var selectedModelRuntimePreparation: ModelRuntimePreparation? {
        guard let model = selectedLibraryModel ?? selectedLibrarySearchResult else {
            return nil
        }
        return ModelRuntimePreparationPlanner.plan(
            engine: model.engine,
            family: model.family,
            engineEnabled: engineIsEnabled(model.engine),
            runtimeUpdates: runtimeUpdateSnapshot,
            readiness: readinessSnapshot?.engines.first {
                $0.engine == model.engine
            },
            restartRequired: requiresRestart,
            engineEnablePendingSave: engineIsEnabled(model.engine)
                && !engineIsEnabled(model.engine, in: savedSettings),
            appleDeveloperToolsInstalled: appleDeveloperToolsInstalled
        )
    }

    var libraryDetails: LibraryModelDetails? {
        get { modelLibraryDownload.details }
        set { modelLibraryDownload.applyDetails(newValue) }
    }

    var libraryStorageBinding: Binding<String> {
        Binding(
            get: { self.selectedLibraryStorage },
            set: { self.selectedLibraryStorage = $0 }
        )
    }

    var fleetPairingInvitationIDBinding: Binding<String> {
        Binding(
            get: { self.fleetPairingCeremony.invitationID },
            set: { self.fleetPairingCeremony.setInvitationID($0) }
        )
    }

    var fleetPairingSecretBinding: Binding<String> {
        Binding(
            get: { self.fleetPairingCeremony.pairingSecretForSecureEntry },
            set: { self.fleetPairingCeremony.setPairingSecret($0) }
        )
    }

    var fleetPairingHubOriginBinding: Binding<String> {
        Binding(
            get: { self.fleetPairingCeremony.hubOrigin },
            set: { self.fleetPairingCeremony.setHubOrigin($0) }
        )
    }

    var fleetPairingLocatorBinding: Binding<String> {
        Binding(
            get: { self.fleetPairingCeremony.locator },
            set: { self.fleetPairingCeremony.setLocator($0) }
        )
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
            async let pairingRequest = try? client.fleetPairing()
            let snapshot = try await configurationRequest
            let loaded = snapshot.config
            let serviceStatus = try? await statusRequest
            let pairingStatus = await pairingRequest
            let credentialStatus = try credentialStore.status()
            settings = loaded
            savedSettings = loaded
            configurationRevision = snapshot.revision
            appliedConfigurationRevision = snapshot.appliedRevision
            configurationRefreshPending = false
            modelLibraryDownload.initialize(
                defaultStorageKey: loaded.storage.default
            )
            configuredCredentials = credentialStatus.configured
            credentialDrafts = [:]
            credentialsToClear = []
            updateFleetPairing(pairingStatus)
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
            Task { await refreshContexts() }
            Task { await refreshDesiredInstalls() }
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

    func refreshContexts() async {
        do {
            contextSnapshot = try await client.contexts(alias: nil)
        } catch {
            // Context metadata is additive. An older service must not make
            // the rest of Settings unusable.
            contextSnapshot = nil
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

        // Refresh this authority boundary immediately before saving. Pairing
        // may have completed while the Settings window was already open.
        if let pairingStatus = try? await client.fleetPairing() {
            updateFleetPairing(pairingStatus)
        }
        guard hasUnsavedChanges else {
            setStatus(
                "Hub pairing now manages the Fleet credentials; their stale drafts were discarded.",
                tone: .normal
            )
            return
        }

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

    func refreshFleetPairing() async {
        do {
            let snapshot = try await client.refreshFleetPairingAttempt()
            updateFleetPairing(snapshot)
            setPairingStatusMessage()
            if snapshot.state == "paired" {
                await refreshDesiredInstalls()
            }
        } catch {
            fleetPairingCeremony.recordFailure(.localServiceUnavailable)
            setPairingStatusMessage(tone: .error)
        }
    }

    func revokeFleetPairing() async {
        guard !isRevokingFleetPairing,
              let pairing = fleetPairing,
              pairing.legacyCredentialsPresent != true,
              pairing.state == "paired" || pairing.selfRevoke != nil
        else { return }

        isRevokingFleetPairing = true
        let requestID = pairing.selfRevoke?.requestID
            ?? fleetSelfRevokeRequestID
            ?? UUID().uuidString.lowercased()
        fleetSelfRevokeRequestID = requestID
        setStatus(
            pairing.selfRevoke == nil
                ? "Removing this Mac from Hub…"
                : "Resuming this Mac's pending Hub removal…",
            tone: .normal
        )
        defer { isRevokingFleetPairing = false }

        do {
            let response = try await client.revokeFleetPairing(
                requestID: requestID
            )
            fleetSelfRevokeRequestID = nil
            updateFleetPairing(response.pairing)
            setStatus(
                "This Mac was removed from Hub. Local inference, model files, storage locations, and token history were not changed.",
                tone: .success
            )
        } catch {
            // The Hub may have committed even if the local HTTP response was
            // lost. Refreshing recovers the durable service-owned request ID;
            // the next button press must replay it rather than allocate a new
            // revocation intent.
            if let refreshed = try? await client.fleetPairing() {
                updateFleetPairing(refreshed)
            }
            setStatus(
                "Could not confirm removal from Hub. Pooled routing is denied locally; use Retry Removal to resume the exact request.",
                tone: .warning
            )
        }
    }

    func advanceFleetPairing() {
        guard !isAdvancingFleetPairing else { return }
        let submission: FleetPairingCeremonySubmission
        do {
            submission = try fleetPairingCeremony.prepareSubmission()
        } catch {
            setStatus(
                FleetPairingCeremonyInputError.incompleteInvitation
                    .localizedDescription,
                tone: .warning
            )
            return
        }

        fleetPairingTaskGeneration += 1
        let generation = fleetPairingTaskGeneration
        let autoResume = fleetPairingCeremony.isPresenceCeremony
        isAdvancingFleetPairing = true
        setPairingStatusMessage()
        fleetPairingTask = Task { [weak self] in
            await self?.performFleetPairing(
                submission,
                generation: generation,
                autoResume: autoResume
            )
        }
    }

    func requestFleetPairing() async {
        guard !isRequestingFleetPairing, !isAdvancingFleetPairing else { return }
        guard settings.server.allowsLocalNetworkInference,
              savedSettings.server.allowsLocalNetworkInference,
              !requiresRestart
        else {
            setStatus(
                "Enable ‘Allow inference from the local network’ in General, save, and restart the service before pairing this Mac.",
                tone: .warning
            )
            return
        }

        let requestID = fleetPairingPresenceRequestID
            ?? UUID().uuidString.lowercased()
        fleetPairingPresenceRequestID = requestID
        isRequestingFleetPairing = true
        setStatus("Contacting the Hub and preparing a pairing code…", tone: .normal)
        defer { isRequestingFleetPairing = false }

        do {
            let discovery = try await tailscale.discover()
            let locator = try discovery.inferenceOrigin(
                port: settings.server.inferencePort
            )
            let response = try await client.requestFleetPairing(
                FleetPairingPresenceRequest(
                    requestID: requestID,
                    hubOrigin: fleetPairingHubAddress,
                    locator: locator
                )
            )
            fleetPairingPresenceRequestID = nil
            fleetPairingPresencePIN = response.presencePIN
            fleetPairingPresenceExpiresAt = response.expiresAt
            let submission = try fleetPairingCeremony.preparePresenceSubmission(
                response
            )
            fleetPairingTaskGeneration += 1
            let generation = fleetPairingTaskGeneration
            isAdvancingFleetPairing = true
            setPairingStatusMessage()
            fleetPairingTask = Task { [weak self] in
                await self?.performFleetPairing(
                    submission,
                    generation: generation,
                    autoResume: true
                )
            }
        } catch {
            fleetPairingCeremony.recordFailure(fleetPairingFailure(for: error))
            setPairingStatusMessage(tone: .error)
        }
    }

    private func performFleetPairing(
        _ submission: FleetPairingCeremonySubmission,
        generation: Int,
        autoResume: Bool
    ) async {
        defer {
            if fleetPairingTaskGeneration == generation {
                isAdvancingFleetPairing = false
                fleetPairingTask = nil
            }
        }
        do {
            var response: FleetPairingOperationResponse
            switch submission.operation {
            case .begin:
                response = try await client.beginFleetPairing(
                    submission.request
                )
            case .resume:
                response = try await client.resumeFleetPairing(
                    submission.request
                )
            }
            while true {
                guard !Task.isCancelled,
                      fleetPairingTaskGeneration == generation
                else { return }
                fleetPairingCeremony.apply(response)
                if let returnedPairing = response.pairing {
                    updateFleetPairing(returnedPairing)
                }
                if let refreshed = try? await client.fleetPairing() {
                    guard !Task.isCancelled,
                          fleetPairingTaskGeneration == generation
                    else { return }
                    updateFleetPairing(refreshed)
                }
                setPairingStatusMessage(
                    tone: fleetPairingCeremony.stage == .paired
                        ? .success : .warning
                )
                if fleetPairingCeremony.stage == .paired {
                    fleetPairingPresencePIN = nil
                    fleetPairingPresenceExpiresAt = nil
                    await refreshDesiredInstalls()
                    return
                }
                guard autoResume else { return }
                if let expiresAt = fleetPairingPresenceExpiresAt,
                   Date().timeIntervalSince1970 >= expiresAt
                {
                    throw FleetPairingAPIError(
                        statusCode: 410,
                        code: "pairing_expired",
                        retryable: false
                    )
                }
                try await Task.sleep(for: .seconds(2))
                response = try await client.resumeFleetPairing(
                    submission.request
                )
            }
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled,
                  fleetPairingTaskGeneration == generation
            else { return }
            fleetPairingCeremony.recordFailure(
                fleetPairingFailure(for: error)
            )
            setPairingStatusMessage(tone: .error)
        }
    }

    func clearFleetPairingCeremony() {
        cancelFleetPairingTask()
        fleetPairingCeremony.cancel()
        fleetPairingPresencePIN = nil
        fleetPairingPresenceExpiresAt = nil
        fleetPairingPresenceRequestID = nil
        setStatus(
            "Pairing invitation details were cleared from this app.",
            tone: .normal
        )
    }

    func discardRejectedFleetPairingAttempt() async {
        guard canDiscardRejectedFleetPairingAttempt,
              !isDiscardingFleetPairingAttempt,
              !isAdvancingFleetPairing
        else { return }

        cancelFleetPairingTask()
        isDiscardingFleetPairingAttempt = true
        setStatus("Discarding the rejected pairing attempt…", tone: .normal)
        defer { isDiscardingFleetPairingAttempt = false }

        do {
            let snapshot = try await client.discardTerminalFleetPairingAttempt()
            fleetPairingCeremony = FleetPairingCeremonyState()
            updateFleetPairing(snapshot)
            setStatus(
                "Stale pairing attempt discarded. Request a new pairing code.",
                tone: .success
            )
        } catch {
            if let refreshed = try? await client.fleetPairing() {
                updateFleetPairing(refreshed)
            }
            setStatus(
                "Could not discard the stale pairing attempt: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func fleetPairingViewDidDisappear() {
        cancelFleetPairingTask()
        fleetPairingCeremony.viewDidDisappear()
        fleetPairingPresencePIN = nil
        fleetPairingPresenceExpiresAt = nil
        fleetPairingPresenceRequestID = nil
    }

    private func cancelFleetPairingTask() {
        fleetPairingTaskGeneration += 1
        fleetPairingTask?.cancel()
        fleetPairingTask = nil
        isAdvancingFleetPairing = false
    }

    func refreshDesiredInstalls() async {
        guard !isRefreshingDesiredInstalls,
              desiredInstallInFlightJobIDs.isEmpty
        else { return }
        isRefreshingDesiredInstalls = true
        defer { isRefreshingDesiredInstalls = false }
        do {
            let snapshot = try await client.desiredInstalls(
                offset: 0,
                // The local journal admits at most 256 active jobs and sorts
                // them before terminal history, so one bounded fetch cannot
                // hide a request that still needs this Mac owner's action.
                limit: 256
            )
            var updated = desiredInstalls
            updated.apply(snapshot)
            desiredInstalls = updated
            desiredInstallError = ""
        } catch {
            desiredInstallError = desiredInstallFailureMessage(
                action: "refresh requests",
                error: error
            )
        }
    }

    func refreshNativeLifecycle() async {
        guard !isRefreshingNativeLifecycle,
              !isPreparingNativeLifecycle,
              !isAuthorizingNativeLifecycle
        else {
            return
        }
        isRefreshingNativeLifecycle = true
        nativeLifecycleMessage = "Inspecting current local lifecycle evidence…"
        defer { isRefreshingNativeLifecycle = false }

        do {
            let status = try await client.nativeLifecycleStatus()
            nativeLifecycleStatus = status
            guard status.available else {
                nativeMigrationPreview = nil
                nativeUninstallPreviews = [:]
                nativeUninstallPreviewErrors = [:]
                nativeLifecycleAuthorizationStatuses = [:]
                nativeLifecycleMessage = nativeLifecycleFailureMessage(
                    fallback: "Lifecycle planning is unavailable.",
                    code: status.errorCode
                )
                return
            }

            var previews: [
                NativeLifecycleRetentionMode: NativeLifecycleUninstallPreview
            ] = [:]
            var errors: [NativeLifecycleRetentionMode: String] = [:]
            for mode in NativeLifecycleRetentionMode.allCases {
                do {
                    previews[mode] = try await client.previewNativeUninstall(
                        retentionMode: mode
                    )
                } catch {
                    errors[mode] = nativeLifecycleFailureMessage(error)
                }
            }
            nativeUninstallPreviews = previews
            nativeUninstallPreviewErrors = errors

            var authorizationStatuses: [
                String: NativeLifecycleAuthorizationStatus
            ] = [:]
            if status.authorizationAvailable {
                for transaction in status.incomplete
                where transaction.contractVersion == 2
                    && [
                        NativeLifecyclePhase.helperStaged,
                        NativeLifecyclePhase.authorized,
                    ].contains(transaction.phase)
                {
                    do {
                        authorizationStatuses[transaction.transactionID] =
                            try await client.nativeLifecycleAuthorizationStatus(
                                transactionID: transaction.transactionID
                            )
                    } catch {
                        // Authorization is failure-isolated from previews and
                        // cannot make lifecycle inventory disappear.
                    }
                }
            }
            nativeLifecycleAuthorizationStatuses = authorizationStatuses

            if status.migrationPreviewAvailable {
                do {
                    nativeMigrationPreview = try await client.previewNativeMigration()
                } catch {
                    nativeMigrationPreview = nil
                    nativeLifecycleMessage = nativeLifecycleFailureMessage(error)
                    return
                }
            } else {
                nativeMigrationPreview = nil
            }

            nativeLifecycleMessage = errors.isEmpty
                ? "Current path-free lifecycle previews are ready."
                : "Some retention previews are blocked by current local evidence."
        } catch {
            nativeLifecycleStatus = nil
            nativeMigrationPreview = nil
            nativeUninstallPreviews = [:]
            nativeUninstallPreviewErrors = [:]
            nativeLifecycleAuthorizationStatuses = [:]
            nativeLifecycleMessage = nativeLifecycleFailureMessage(error)
        }
    }

    func requestNativeLifecyclePreparation(
        _ mode: NativeLifecycleRetentionMode
    ) {
        guard !isRefreshingNativeLifecycle,
              !isPreparingNativeLifecycle,
              let preview = nativeUninstallPreviews[mode],
              preview.preparable
        else { return }
        pendingNativeLifecyclePreview = preview
        confirmPrepareNativeLifecycle = true
    }

    func cancelNativeLifecyclePreparation() {
        pendingNativeLifecyclePreview = nil
    }

    func prepareConfirmedNativeLifecycle() async {
        guard !isRefreshingNativeLifecycle,
              !isPreparingNativeLifecycle,
              let confirmed = pendingNativeLifecyclePreview
        else { return }
        pendingNativeLifecyclePreview = nil
        isPreparingNativeLifecycle = true
        nativeLifecycleMessage =
            "Rechecking the exact local plan before recording it…"
        defer { isPreparingNativeLifecycle = false }

        let mode = confirmed.plan.retentionMode
        do {
            // The visible confirmation is fenced against a fresh path-free
            // preview. A changed private-manifest receipt forces another
            // owner review instead of silently preparing stale effects.
            let fresh = try await client.previewNativeUninstall(
                retentionMode: mode
            )
            nativeUninstallPreviews[mode] = fresh
            nativeUninstallPreviewErrors.removeValue(forKey: mode)
            guard confirmed.plan.hasSamePreparedEffects(as: fresh.plan) else {
                throw NativeLifecycleRequestError.changedBeforePreparation
            }

            let prepared = try await client.prepareNativeUninstall(
                transactionID: fresh.plan.transactionID,
                retentionMode: mode
            )
            let reread = try await client.nativeLifecycleTransaction(
                transactionID: fresh.plan.transactionID
            )
            guard prepared.prepared,
                  prepared.transaction.transactionID == fresh.plan.transactionID,
                  prepared.transaction.phase == .prepared,
                  prepared.transaction.plan.uninstall == fresh.plan,
                  reread == prepared.transaction
            else {
                throw NativeLifecycleRequestError.invalidPreparedTransaction
            }
            preparedNativeLifecycleTransaction = reread
            nativeLifecycleStatus = try await client.nativeLifecycleStatus()
            nativeLifecycleMessage =
                "Plan prepared and re-read from the private journal. Execution remains unavailable; inference, the background service, the app, and all model files are unchanged."
        } catch {
            nativeLifecycleMessage = nativeLifecycleFailureMessage(error)
        }
    }

    func authorizeNativeLifecycle(
        _ transaction: NativeLifecycleTransaction
    ) async {
        guard !isRefreshingNativeLifecycle,
              !isPreparingNativeLifecycle,
              !isAuthorizingNativeLifecycle,
              transaction.contractVersion == 2,
              transaction.phase == .helperStaged
        else { return }
        isAuthorizingNativeLifecycle = true
        nativeLifecycleMessage =
            "Waiting for device-owner authorization from the signed helper…"
        defer { isAuthorizingNativeLifecycle = false }

        do {
            let accepted = try await NativeLifecycleAuthorizationSession(
                service: client
            ).authorize(transactionID: transaction.transactionID)
            let reread = try await client.nativeLifecycleTransaction(
                transactionID: transaction.transactionID
            )
            guard accepted.authorized,
                  accepted.transaction == reread,
                  reread.phase == .authorized,
                  reread.transactionID == transaction.transactionID
            else {
                throw NativeLifecycleRequestError.invalidAuthorizationResponse
            }
            authorizedNativeLifecycleTransaction = reread
            nativeLifecycleStatus = try await client.nativeLifecycleStatus()
            nativeLifecycleAuthorizationStatuses[transaction.transactionID] =
                try await client.nativeLifecycleAuthorizationStatus(
                    transactionID: transaction.transactionID
                )
            nativeLifecycleMessage =
                "Owner authorization was recorded for the exact staged helper and private manifest. Effects remain disabled; inference, storage, runtimes, and every model weight are unchanged."
        } catch {
            // A submit failure may be an ambiguous successful commit. Never
            // cancel or replay blindly; refresh the durable status instead.
            if let durable = try? await client.nativeLifecycleTransaction(
                transactionID: transaction.transactionID
            ), durable.phase == .authorized {
                authorizedNativeLifecycleTransaction = durable
                nativeLifecycleMessage =
                    "Owner authorization is durably recorded. Effects remain disabled."
            } else {
                nativeLifecycleMessage = nativeLifecycleFailureMessage(error)
            }
        }
    }

    private func nativeLifecycleFailureMessage(_ error: Error) -> String {
        if let lifecycleError = error as? NativeLifecycleAPIError {
            return lifecycleError.localizedDescription
        }
        if let requestError = error as? NativeLifecycleRequestError {
            return requestError.localizedDescription
        }
        if let controlError = error as? ControlAPIError {
            switch controlError {
            case .invalidResponse:
                return "The local service returned an invalid lifecycle response."
            case let .unexpectedStatus(status), let .rejected(status, _):
                return "Lifecycle planning is unavailable (HTTP \(status))."
            case .unsupportedConfigurationSchema:
                return "Update Unified Inference before using lifecycle planning."
            }
        }
        return "The local lifecycle planning service is unavailable."
    }

    private func nativeLifecycleFailureMessage(
        fallback: String,
        code: String?
    ) -> String {
        guard let code else { return fallback }
        return NativeLifecycleAPIError(statusCode: 503, code: code)
            .localizedDescription
    }

    func approveDesiredInstall(_ item: DesiredInstallItem) async {
        await mutateDesiredInstall(item, action: .approve)
    }

    func refuseDesiredInstall(_ item: DesiredInstallItem) async {
        await mutateDesiredInstall(item, action: .refuse)
    }

    func cancelDesiredInstall(_ item: DesiredInstallItem) async {
        await mutateDesiredInstall(item, action: .cancel)
    }

    func isDesiredInstallInFlight(_ item: DesiredInstallItem) -> Bool {
        desiredInstallInFlightJobIDs.contains(item.job.jobID)
    }

    private func mutateDesiredInstall(
        _ item: DesiredInstallItem,
        action: DesiredInstallMutationAction
    ) async {
        guard !isRefreshingDesiredInstalls else { return }
        guard let current = desiredInstalls.item(
            jobID: item.job.jobID,
            jobRevision: item.job.jobRevision
        ), action.isPermitted(for: current)
        else {
            desiredInstallError =
                "This request changed. Refresh the list before trying again."
            return
        }

        let jobID = current.job.jobID
        let jobRevision = current.job.jobRevision
        guard desiredInstallInFlightJobIDs.insert(jobID).inserted else { return }
        defer { desiredInstallInFlightJobIDs.remove(jobID) }

        do {
            let snapshot: DesiredInstallDetailSnapshot
            switch action {
            case .approve:
                snapshot = try await client.approveDesiredInstall(
                    jobID: jobID,
                    jobRevision: jobRevision
                )
            case .refuse:
                snapshot = try await client.refuseDesiredInstall(
                    jobID: jobID,
                    jobRevision: jobRevision
                )
            case .cancel:
                snapshot = try await client.cancelDesiredInstall(
                    jobID: jobID,
                    jobRevision: jobRevision
                )
            }
            var updated = desiredInstalls
            updated.apply(snapshot)
            desiredInstalls = updated
            desiredInstallError = ""
        } catch {
            let message = desiredInstallFailureMessage(
                action: action.description,
                error: error
            )
            if let refreshed = try? await client.desiredInstall(jobID: jobID) {
                var updated = desiredInstalls
                updated.apply(refreshed)
                desiredInstalls = updated
            }
            desiredInstallError = message
        }
    }

    private func desiredInstallFailureMessage(
        action: String,
        error: Error
    ) -> String {
        if let requestError = error as? DesiredInstallRequestError {
            return requestError.localizedDescription
        }
        if let controlError = error as? ControlAPIError {
            switch controlError {
            case .invalidResponse:
                return "Could not \(action): the local control service returned an invalid response."
            case let .unexpectedStatus(status), let .rejected(status, _):
                return "Could not \(action): the local control service returned HTTP \(status). Refresh the request before trying again."
            case .unsupportedConfigurationSchema:
                return "Could not \(action): update Unified Inference before managing Hub downloads."
            }
        }
        return "Could not \(action): the local control service is unavailable."
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
            modelLibraryDownload.setStorageStatus(StorageStatus(
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
            ), for: name)
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
        modelLibraryDownload.removeStorageStatus(for: name)
        if settings.storage.default == name {
            settings.storage.default = settings.storage.locations[0].name
        }
    }

    func refreshStorageStatuses() async {
        do {
            let snapshot = try await client.storageLocations()
            modelLibraryDownload.applyStorageStatuses(Dictionary(
                uniqueKeysWithValues: snapshot.locations.compactMap { status in
                    status.name.map { ($0, status) }
                }
            ))
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
            let foundModels = try await found
            libraryModels = GLM53PreviewPresentation.visibleModels(
                query: libraryQuery,
                models: foundModels
            )
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
        guard settings.storage.locations.contains(where: {
            $0.name == selectedLibraryStorage
        }), modelLibraryDownload.selectedStorageIsAvailable else {
            setStatus(
                "The selected Download-to folder is unavailable. Choose an available folder explicitly before downloading.",
                tone: .warning
            )
            return
        }
        guard availableLibraryRoles.contains(selectedLibraryRole),
              model.isInstallable
        else {
            setStatus(
                "This model and role cannot be installed with the current selection.",
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

    func performSelectedModelRuntimePreparation() async {
        guard let preparation = selectedModelRuntimePreparation else { return }
        switch preparation.action {
        case .none, .restartService:
            return
        case .refresh:
            await refreshRuntimeUpdates(force: true)
            await refreshReadiness()
        case let .installManaged(engine, version):
            guard
                let update = runtimeUpdateSnapshot?.engines.first(where: {
                    $0.engine == engine
                }),
                update.canInstall,
                update.availableVersion == version
            else {
                setStatus(
                    "Runtime availability changed. Checking the current official release…",
                    tone: .warning
                )
                await refreshRuntimeUpdates(force: true)
                return
            }
            await installRuntimeUpdate(update)
            await refreshReadiness()
        case let .installDS4GLM53Preview(version, channel):
            guard
                channel == ManagedRuntimeChannel.ds4GLM53FlashChannel,
                ds4GLM53FlashPreview?.availableVersion == version,
                ds4GLM53FlashPreview?.canInstall == true
            else {
                setStatus(
                    "Preview runtime availability changed. Checking the exact official channel…",
                    tone: .warning
                )
                await refreshRuntimeUpdates(force: true)
                return
            }
            requestDS4GLM53PreviewInstall()
        case .installAppleDeveloperTools:
            confirmAppleDeveloperToolsInstall = true
        case let .downloadOfficialOMLX(installerURL):
            guard let url = URL(string: installerURL) else { return }
            if NSWorkspace.shared.open(url) {
                setStatus(
                    "Opened the official oMLX installer. Return here and check runtime health after starting its loopback server.",
                    tone: .normal
                )
            } else {
                setStatus("Could not open the official oMLX installer.", tone: .error)
            }
        case .enableEngine:
            guard let model = selectedLibraryModel ?? selectedLibrarySearchResult
            else { return }
            setEngineEnabled(model.engine, true)
            setStatus(
                "\(model.engine.displayName) is staged to be enabled. Save changes and restart the background service; model weights and storage remain unchanged.",
                tone: .normal
            )
        case .saveSettings:
            await save()
        case let .openOMLXApplication(path):
            guard
                let update = runtimeUpdateSnapshot?.engines.first(where: {
                    $0.engine == .omlx
                }),
                update.installedPath == path
            else { return }
            openOMLXApplication(update)
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
        appleDeveloperToolsInstalled =
            await AppleDeveloperToolsInstaller.isInstalled()
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

    func requestAppleDeveloperToolsInstallation() async {
        guard !isRequestingAppleDeveloperTools else { return }
        isRequestingAppleDeveloperTools = true
        defer { isRequestingAppleDeveloperTools = false }
        if await AppleDeveloperToolsInstaller.isInstalled() {
            appleDeveloperToolsInstalled = true
            setStatus("Apple's developer tools are already installed.", tone: .success)
            return
        }
        do {
            try await AppleDeveloperToolsInstaller.requestInstallation()
            // xcode-select only confirms that Apple's GUI was requested. It
            // does not prove that the user completed the system installation.
            // Return to unknown until a later --print-path probe succeeds.
            appleDeveloperToolsInstalled = nil
            setStatus(
                "Apple's Command Line Tools installer opened. Finish the system installation, then choose Check Prerequisites on the model card.",
                tone: .normal
            )
        } catch {
            setStatus(
                "Could not open Apple's developer-tools installer: \(error.localizedDescription)",
                tone: .error
            )
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

    func requestDS4GLM53PreviewInstall() {
        guard ds4GLM53FlashPreview?.canInstall == true else {
            setStatus(
                "The official DS4 GLM 5.3 preview runtime is not currently installable. Check runtime updates and try again.",
                tone: .warning
            )
            return
        }
        confirmDS4GLM53PreviewInstall = true
    }

    func installDS4GLM53PreviewRuntime() async {
        guard
            let channel = ds4GLM53FlashPreview,
            channel.channel == ManagedRuntimeChannel.ds4GLM53FlashChannel,
            channel.sourceBranch == ManagedRuntimeChannel.ds4GLM53FlashChannel,
            channel.releaseTier == "experimental",
            channel.canInstall,
            let version = channel.availableVersion,
            !version.isEmpty,
            updatingRuntimeEngine == nil,
            !isResettingOMLXCache
        else {
            setStatus(
                "The exact DS4 GLM 5.3 preview release is not available. Check runtime updates and try again.",
                tone: .warning
            )
            return
        }

        updatingRuntimeEngine = .ds4
        setStatus(
            "Preparing the experimental DS4 GLM 5.3 Flash runtime…",
            tone: .normal
        )
        defer { updatingRuntimeEngine = nil }
        do {
            runtimeUpdateSnapshot = try await client.installRuntimeUpdate(
                engine: .ds4,
                version: version,
                channel: channel.channel
            )
            await refreshModelLibrary()
            setStatus(
                "The DS4 GLM 5.3 Flash preview runtime is active. Choose Q2 or Q4_K in Model Library; downloading remains a separate explicit action.",
                tone: .success
            )
        } catch {
            setStatus(
                "Could not install the DS4 GLM 5.3 Flash preview runtime: \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func openGLM53PreviewRuntimeUpdates() {
        selectedSection = .updates
        if runtimeUpdateSnapshot == nil {
            Task { await refreshRuntimeUpdates(force: true) }
        }
    }

    func searchGLM53PreviewModels() {
        libraryQuery = "GLM 5.3 Flash"
        selectedSection = .library
        Task { await refreshModelLibrary() }
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
            return []
        }
    }

    var canInstallSelectedLibraryModel: Bool {
        guard let model = selectedLibraryModel else { return false }
        return !isWorking
            && !hasUnsavedChanges
            && !requiresRestart
            && model.isInstallable
            && availableLibraryRoles.contains(selectedLibraryRole)
            && settings.storage.locations.contains(where: {
                $0.name == selectedLibraryStorage
            })
            && modelLibraryDownload.selectedStorageIsAvailable
    }

    func storageStatus(for name: String) -> StorageStatus? {
        guard settings.storage.locations.contains(where: { $0.name == name })
        else { return nil }
        return modelLibraryDownload.storageStatuses[name]
    }

    private func setEngineEnabled(_ engine: InferenceEngine, _ enabled: Bool) {
        switch engine {
        case .llamaCpp: settings.engines.llamaCpp.enabled = enabled
        case .omlx: settings.engines.omlx.enabled = enabled
        case .ds4: settings.engines.ds4.enabled = enabled
        case .mflux: settings.engines.mflux.enabled = enabled
        case .mlxcel, .mistralRs: break
        }
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
        pendingModelCleanupProfile = nil
        modelCleanupDecision = .refused(.preparationRequired)
        if settings.models.isEmpty {
            selectedModelIndex = nil
        } else {
            selectedModelIndex = min(index, settings.models.count - 1)
        }
    }

    func prepareSelectedModelRemoval() async {
        guard
            let index = selectedModelIndex,
            settings.models.indices.contains(index),
            !isWorking
        else {
            return
        }
        let profile = settings.models[index]
        pendingModelCleanupProfile = profile

        guard !hasUnsavedChanges else {
            modelCleanupDecision = .refused(.unsavedSettings)
            confirmRemoveModel = true
            return
        }

        isWorking = true
        setStatus("Verifying \(profile.alias)'s install identity…", tone: .normal)
        defer { isWorking = false }
        do {
            let installHistory = try await client.modelInstallHistory()
            modelCleanupDecision = ModelCleanupResolver.resolve(
                profile: profile,
                storageLocations: settings.storage.locations,
                installs: installHistory
            )
        } catch {
            modelCleanupDecision = .refused(.installHistoryUnavailable)
        }
        confirmRemoveModel = true
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

        let profile = settings.models[index]
        guard pendingModelCleanupProfile == profile,
              modelCleanupDecision.permitsFileCleanup
        else {
            setStatus(
                "File cleanup was refused because the selected profile does not have one current, verified cleanup identity.",
                tone: .warning
            )
            return
        }

        let alias = profile.alias
        let cleanupDecision = modelCleanupDecision
        isWorking = true
        setStatus("Cleaning up \(alias) and its model files…", tone: .normal)
        defer { isWorking = false }
        do {
            let result = try await client.deleteManagedModel(
                alias: alias,
                revision: configurationRevision,
                installationID: cleanupDecision.installationID
            )
            settings = result.config
            savedSettings = result.config
            configurationRevision = result.revision
            appliedConfigurationRevision = result.revision
            requiresRestart = result.restartRequired
            configurationRefreshPending = false
            modelInstalls = cleanupDecision.retainingUnrelatedInstalls(
                from: modelInstalls
            )
            pendingModelCleanupProfile = nil
            modelCleanupDecision = .refused(.preparationRequired)
            if settings.models.isEmpty {
                selectedModelIndex = nil
            } else {
                selectedModelIndex = min(index, settings.models.count - 1)
            }
            setStatus(
                ModelCleanupDecision.successMessage(
                    alias: alias,
                    filesDisposition: result.filesDisposition
                ),
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
        engineIsEnabled(engine, in: settings)
    }

    private func engineIsEnabled(
        _ engine: InferenceEngine,
        in candidate: NativeSettings
    ) -> Bool {
        switch engine {
        case .llamaCpp: candidate.engines.llamaCpp.enabled
        case .omlx: candidate.engines.omlx.enabled
        case .ds4: candidate.engines.ds4.enabled
        case .mflux: candidate.engines.mflux.enabled
        case .mlxcel, .mistralRs: false
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
            context: source.context,
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
                && candidate.context == alternative.context
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
                    context: alternative.context,
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

    func runContextProfile(alias: String) async {
        guard profilingContextAlias == nil, !hasUnsavedChanges else { return }
        profilingContextAlias = alias
        setStatus("Profiling the usable context window for \(alias)…", tone: .normal)
        defer { profilingContextAlias = nil }
        do {
            let run = try await client.profileContext(alias: alias, targetTokens: nil)
            contextSnapshot = try await client.contexts(alias: nil)
            benchmarkSnapshot = try? await client.benchmarks(alias: nil)
            let best = run.results.map(\.verifiedTokens).max()
            let detail = best.map { " Verified \($0.formatted()) tokens." } ?? ""
            let failures = run.failures.count
            setStatus(
                "Context profiling finished for \(alias).\(detail)",
                tone: failures == 0 ? .success : .warning
            )
        } catch {
            setStatus(
                "Could not profile context for \(alias): \(error.localizedDescription)",
                tone: .error
            )
        }
    }

    func credentialBinding(_ credential: ManagedCredential) -> Binding<String> {
        Binding(
            get: { self.credentialDrafts[credential, default: ""] },
            set: {
                guard !self.pairingOwnsFleetCredentials
                    || !credential.isFleetPairingCredential
                else { return }
                self.credentialDrafts[credential] = $0
                if !$0.isEmpty { self.credentialsToClear.remove(credential) }
            }
        )
    }

    func clearCredential(_ credential: ManagedCredential) {
        guard !pairingOwnsFleetCredentials
            || !credential.isFleetPairingCredential
        else { return }
        credentialDrafts[credential] = ""
        credentialsToClear.insert(credential)
    }

    func undoCredentialClear(_ credential: ManagedCredential) {
        credentialsToClear.remove(credential)
    }

    private func updateFleetPairing(_ snapshot: FleetPairingSnapshot?) {
        fleetPairing = snapshot
        if let requestID = snapshot?.selfRevoke?.requestID {
            fleetSelfRevokeRequestID = requestID
        } else if snapshot?.state == "revoked" {
            fleetSelfRevokeRequestID = nil
        }
        if let snapshot {
            fleetPairingCeremony.synchronize(with: snapshot)
        }
        guard pairingOwnsFleetCredentials else { return }
        for credential in ManagedCredential.allCases
        where credential.isFleetPairingCredential {
            credentialDrafts.removeValue(forKey: credential)
            credentialsToClear.remove(credential)
        }
    }

    private func setPairingStatusMessage(
        tone: StatusTone = .normal
    ) {
        setStatus(
            "\(fleetPairingCeremony.statusText). "
                + fleetPairingCeremony.nextActionText,
            tone: tone
        )
    }

    private func fleetPairingFailure(
        for error: Error
    ) -> FleetPairingCeremonyFailure {
        if let pairingError = error as? FleetPairingAPIError {
            switch pairingError.code {
            case "pairing_static_credentials_present":
                return .staticCredentialsPresent
            case "pairing_expired":
                return .invitationExpired
            case "pairing_claim_rejected", "pairing_activation_rejected",
                 "pairing_payload_mismatch", "pairing_local_identity_invalid":
                return .invitationRejected
            case "pairing_state_conflict", "pairing_no_attempt":
                return .stateConflict
            case "pairing_invalid_response", "pairing_hub_response_invalid",
                 "pairing_hub_response_too_large":
                return .invalidResponse
            default:
                return .localServiceUnavailable
            }
        }
        guard let controlError = error as? ControlAPIError else {
            return .localServiceUnavailable
        }
        let status: Int
        switch controlError {
        case let .unexpectedStatus(value), let .rejected(value, _):
            status = value
        case .invalidResponse, .unsupportedConfigurationSchema:
            return .invalidResponse
        }
        switch status {
        case 401, 403:
            return .invitationRejected
        case 409:
            return .stateConflict
        case 410:
            return .invitationExpired
        case 422:
            return .staticCredentialsPresent
        default:
            return .localServiceUnavailable
        }
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
