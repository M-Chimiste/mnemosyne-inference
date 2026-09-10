import AppKit
import MnemosyneAppCore
import ServiceManagement
import SwiftUI

struct MenuContentView: View {
    let workstationName: String
    @ObservedObject var viewModel: MenuViewModel
    @ObservedObject var registration: LaunchAgentRegistration
    @ObservedObject var startup: ServiceStartupCoordinator
    @ObservedObject private var preferences = WorkspacePreferences.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let openConfiguration: () -> Void
    let checkForUpdates: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 15) {
            if startup.state == .ready {
                header
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Image(systemName: viewModel.activity.symbol).foregroundStyle(.indigo)
                            .symbolEffect(.pulse, options: .repeating, isActive: viewModel.isLive && viewModel.activity.isWorking && !reduceMotion)
                        Text(viewModel.activity.title).font(.subheadline.weight(.semibold))
                        Spacer()
                        if viewModel.isLive { Circle().fill(.green).frame(width: 6, height: 6) }
                    }
                    Text(viewModel.snapshot?.residentAlias ?? "No model in memory")
                        .font(.callout).lineLimit(2).textSelection(.enabled)
                    Text(viewModel.isLive ? "\(viewModel.snapshot?.inFlightRequests ?? 0) active requests" : "Last known state · waiting for an update")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
                    .background(.indigo.opacity(0.07), in: RoundedRectangle(cornerRadius: 14))
                modelController
                Divider()
                Toggle("Contribute to Fleet", isOn: Binding(
                    get: { viewModel.fleetParticipation?.enabled ?? false },
                    set: { enabled in Task { await viewModel.setFleetParticipation(enabled: enabled) } }
                )).toggleStyle(.switch)
                    .disabled(!viewModel.isLive || viewModel.fleetPairing?.permitsParticipationControl != true || viewModel.participationMutationInProgress)
                if let participation = viewModel.fleetParticipation {
                    Text(viewModel.isLive ? participationExplanation(participation.state) : "Refreshing participation status…")
                        .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if !viewModel.actionError.isEmpty {
                    DisclosureGroup("Action needs attention") {
                        Text(viewModel.actionError).font(.caption).textSelection(.enabled)
                    }.font(.caption).foregroundStyle(.orange)
                }
            } else { ServiceStartupView(startup: startup, registration: registration) }
            Divider()
            HStack {
                Button("Open Unified Inference") { openConfiguration() }.buttonStyle(.borderedProminent)
                Spacer()
                Button { copyEndpoint() } label: { Image(systemName: "link") }
                    .help("Copy local API endpoint").disabled(viewModel.inferenceEndpoint == nil)
            }
            DisclosureGroup("Service & diagnostics") {
                VStack(alignment: .leading, spacing: 12) {
                    backgroundService
                    if startup.state == .ready { usageDelivery; loadedModel }
                    actions
                }.padding(.top, 10)
            }.font(.caption)
        }.padding(18).frame(width: 360).tint(.indigo)
        .task(id: startup.state) {
            guard startup.state == .ready else { return }
            registration.refresh()
            while !Task.isCancelled {
                await viewModel.refresh()
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            }
        }
    }

    private func copyEndpoint() {
        guard let url = viewModel.inferenceEndpoint else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(url.absoluteString, forType: .string)
    }

    private var poolParticipation: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(
                "Contribute this Mac to the pool",
                isOn: Binding(
                    get: { viewModel.fleetParticipation?.enabled ?? false },
                    set: { enabled in
                        Task {
                            await viewModel.setFleetParticipation(enabled: enabled)
                        }
                    }
                )
            )
            .toggleStyle(.switch)
            .disabled(
                viewModel.fleetParticipation == nil
                    || viewModel.fleetPairing?.permitsParticipationControl != true
                    || viewModel.participationMutationInProgress
                    || viewModel.connection != .online
            )

            if let pairing = viewModel.fleetPairing {
                LabeledContent("Hub enrollment", value: pairingLabel(pairing))
                    .font(.caption)
                    .foregroundStyle(
                        pairing.state == "recovery_required"
                            ? Color.orange : Color.secondary
                    )
            }

            if let participation = viewModel.fleetParticipation {
                HStack {
                    Text(participationStateLabel(participation.state))
                    Spacer()
                    Text(
                        "\(participation.activeRequests) active Fleet "
                            + "request\(participation.activeRequests == 1 ? "" : "s")"
                    )
                }
                .font(.caption)
                .foregroundStyle(.secondary)

                Text(participationExplanation(participation.state))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if viewModel.participationMutationInProgress {
                ProgressView()
                    .controlSize(.small)
            }
        }
    }

    @ViewBuilder
    private var modelController: some View {
        if viewModel.models.isEmpty {
            LabeledContent("Configured models", value: "None")
        } else {
            VStack(alignment: .leading, spacing: 7) {
                Text("Load model")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack {
                    Picker("Model", selection: $viewModel.selectedAlias) {
                        ForEach(viewModel.models.sorted { a, b in
                            let x = preferences.favorites.contains(a.id), y = preferences.favorites.contains(b.id)
                            return x == y ? a.id < b.id : x
                        }) { model in
                            Text((preferences.favorites.contains(model.id) ? "★ " : "") + modelLabel(model))
                                .tag(model.id)
                        }
                    }
                    .labelsHidden()
                    Button { preferences.toggleFavorite(viewModel.selectedAlias) } label: {
                        Image(systemName: preferences.favorites.contains(viewModel.selectedAlias) ? "star.fill" : "star")
                    }.buttonStyle(.plain).help("Favorite selected model").disabled(viewModel.selectedAlias.isEmpty)
                    Button("Load") {
                        Task { await viewModel.loadSelectedModel() }
                    }
                    .disabled(
                        viewModel.selectedAlias.isEmpty
                            || !viewModel.isLive || viewModel.mutationInProgress
                    )
                }
            }
        }
    }

    private var header: some View {
        HStack(spacing: 9) {
            Image(systemName: connectionSymbol)
                .foregroundStyle(connectionColor)
            VStack(alignment: .leading, spacing: 2) {
                Text(workstationName)
                    .font(.headline)
                Text(connectionLabel)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer()
            if case .checking = viewModel.connection {
                ProgressView()
                    .controlSize(.small)
            }
        }
    }

    @ViewBuilder
    private var usageDelivery: some View {
        if let tokenSidecar = viewModel.snapshot?.tokenSidecar,
           tokenSidecar.enabled == true
        {
            LabeledContent("Usage outbox", value: String(tokenSidecar.outboxDepth ?? 0))
            if let error = tokenSidecar.lastError, !error.isEmpty {
                Text("Usage reporting: \(error)").font(.caption2).foregroundStyle(.orange)
            }
        }
    }

    private var loadedModel: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text("Loaded model")
                Text(loadedModelLabel)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(loadedModelLabel)
                Button("Unload") {
                    Task { await viewModel.unloadResidentModel() }
                }
                .disabled(!hasLoadedModel || viewModel.mutationInProgress)
            }
            if let inFlight = viewModel.snapshot?.inFlightRequests,
               inFlight > 0
            {
                Text("\(inFlight) request\(inFlight == 1 ? "" : "s") in flight")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if let alias = viewModel.snapshot?.residentAlias,
               let metrics = viewModel.snapshot?.performance?.byModel.first(
                    where: { $0.alias == alias }
               ) {
                HStack(spacing: 10) {
                    Text("P50 \(duration(metrics.p50TotalMs))")
                    Text("P95 \(duration(metrics.p95TotalMs))")
                    if let rate = metrics.averageOutputTokensPerSecond {
                        Text(String(format: "%.1f tok/s", rate))
                    }
                    if metrics.coldStarts > 0 {
                        Text("\(metrics.coldStarts) cold")
                    }
                }
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
            }
        }
    }

    private func duration(_ milliseconds: Double?) -> String {
        guard let milliseconds else { return "—" }
        if milliseconds >= 1_000 {
            return String(format: "%.1fs", milliseconds / 1_000)
        }
        return "\(Int(milliseconds.rounded()))ms"
    }

    private func pairingLabel(_ pairing: FleetPairingSnapshot) -> String {
        if pairing.legacyCredentialsPresent == true {
            return "Static configuration"
        }
        switch pairing.state {
        case "paired":
            return "Paired"
        case "pending":
            return "Pairing pending"
        case "revoked":
            return "Revoked"
        case "recovery_required":
            return "Needs attention"
        default:
            return "Not paired"
        }
    }

    private var backgroundService: some View {
        VStack(alignment: .leading, spacing: 8) {
            LabeledContent(
                "Background service",
                value: registration.label(for: registration.agentStatus)
            )
            HStack {
                Group {
                    if registration.agentStatus == .enabled
                        || registration.agentStatus == .requiresApproval
                    {
                        Button("Disable Service") {
                            Task {
                                startup.prepare()
                                await registration.disableAgent()
                                await startup.connect(registration: registration.startupRegistrationState)
                            }
                        }
                    } else {
                        Button("Enable Service") {
                            Task {
                                startup.prepare()
                                await registration.enableAgent()
                                await startup.connect(registration: registration.startupRegistrationState)
                            }
                        }
                    }
                }
                .disabled(registration.isChangingRegistration || startup.state.isWaiting)
                Spacer()
                if registration.agentStatus == .requiresApproval {
                    Button("Open Login Items") {
                        registration.openLoginItemsSettings()
                    }
                }
            }

            Toggle(
                "Open Unified Inference at login",
                isOn: Binding(
                    get: {
                        registration.menuLoginStatus == .enabled
                            || registration.menuLoginStatus == .requiresApproval
                    },
                    set: { enabled in
                        if enabled {
                            Task { await registration.enableMenuAtLogin() }
                        } else {
                            Task { await registration.disableMenuAtLogin() }
                        }
                    }
                )
            )
            .toggleStyle(.switch)
            .disabled(registration.isChangingRegistration)

            if registration.menuLoginStatus == .requiresApproval {
                Button("Approve Login Items in System Settings") {
                    registration.openLoginItemsSettings()
                }
                .font(.caption)
            }

            if let error = registration.lastError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    private var actions: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button("Refresh") {
                    Task { await viewModel.refresh() }
                }
                .disabled(startup.state != .ready)
                if let checkForUpdates {
                    Button("Check for App Updates…") {
                        checkForUpdates()
                    }
                }
            }
            HStack {
                Button("Logs") {
                    openApplicationSupport(subdirectory: "logs")
                }
                Button("Settings…") {
                    openConfiguration()
                }
                Spacer()
                Button("Quit") {
                    NSApplication.shared.terminate(nil)
                }
            }
        }
    }

    private var connectionSymbol: String {
        switch viewModel.connection {
        case .online:
            serviceDiagnostic == nil
                ? "checkmark.circle.fill"
                : "exclamationmark.triangle.fill"
        case .checking:
            "clock"
        case .offline:
            "exclamationmark.triangle.fill"
        }
    }

    private var connectionColor: Color {
        switch viewModel.connection {
        case .online:
            serviceDiagnostic == nil ? .green : .orange
        case .checking:
            .secondary
        case .offline:
            .orange
        }
    }

    private var connectionLabel: String {
        switch viewModel.connection {
        case .online:
            if let serviceDiagnostic {
                "Degraded — \(serviceDiagnostic)"
            } else {
                "Connected to this Mac"
            }
        case .checking:
            "Connecting to this Mac…"
        case .offline:
            "Reconnecting to this Mac…"
        }
    }

    private var serviceDiagnostic: String? {
        guard let snapshot = viewModel.snapshot else {
            return nil
        }
        if let startupError = snapshot.startupError, !startupError.isEmpty {
            return startupError
        }
        if let diagnostic = snapshot.diagnostic, !diagnostic.isEmpty {
            return diagnostic
        }

        return nil
    }

    private var hasLoadedModel: Bool {
        viewModel.snapshot?.residentAlias != nil
            || viewModel.snapshot?.residentModel != nil
    }

    private var loadedModelLabel: String {
        guard let model = viewModel.snapshot?.residentAlias
                ?? viewModel.snapshot?.residentModel
        else {
            return "None"
        }
        guard let engine = viewModel.snapshot?.residentEngine,
              !engine.isEmpty
        else {
            return model
        }
        return "\(model) · \(engine)"
    }

    private func openApplicationSupport(subdirectory: String?) {
        var url = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0].appending(path: "Mnemosyne", directoryHint: .isDirectory)
        if let subdirectory {
            url.append(path: subdirectory, directoryHint: .isDirectory)
        }
        try? FileManager.default.createDirectory(
            at: url,
            withIntermediateDirectories: true
        )
        NSWorkspace.shared.open(url)
    }

    private func modelLabel(_ model: ModelSummary) -> String {
        guard let engine = model.engine, !engine.isEmpty else { return model.id }
        return "\(model.id) · \(engine)"
    }

    private func participationStateLabel(_ state: String) -> String {
        switch state {
        case "joined":
            "Joined"
        case "draining":
            "Draining"
        case "paused":
            "Paused"
        default:
            state.capitalized
        }
    }

    private func participationExplanation(_ state: String) -> String {
        switch state {
        case "joined":
            "The Hub can route eligible pooled requests to this Mac."
        case "draining":
            "Finishing current pooled requests before pausing; local inference remains available."
        case "paused":
            "Paused only for pooled requests; this Mac stays registered and local inference remains available."
        default:
            "Pool participation does not change local inference or model storage."
        }
    }
}
