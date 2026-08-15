import AppKit
import MnemosyneAppCore
import ServiceManagement
import SwiftUI

struct MenuContentView: View {
    let workstationName: String
    @ObservedObject var viewModel: MenuViewModel
    @ObservedObject var registration: LaunchAgentRegistration
    let openConfiguration: () -> Void
    let checkForUpdates: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            modelController
            usageDelivery
            loadedModel
            Divider()
            backgroundService
            Divider()
            actions
        }
        .padding(14)
        .frame(width: 330)
        .task {
            registration.refresh()
            while !Task.isCancelled {
                await viewModel.refresh()
                try? await Task.sleep(for: .seconds(5))
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
                        ForEach(viewModel.models) { model in
                            Text(modelLabel(model))
                                .tag(model.id)
                        }
                    }
                    .labelsHidden()
                    Button("Load") {
                        Task { await viewModel.loadSelectedModel() }
                    }
                    .disabled(
                        viewModel.selectedAlias.isEmpty
                            || viewModel.mutationInProgress
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
            LabeledContent(
                "Usage outbox",
                value: String(tokenSidecar.outboxDepth ?? 0)
            )
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
                            Task { await registration.disableAgent() }
                        }
                    } else {
                        Button("Enable Service") {
                            Task { await registration.enableAgent() }
                        }
                    }
                }
                .disabled(registration.isChangingRegistration)
                Spacer()
                if registration.agentStatus == .requiresApproval {
                    Button("Open Login Items") {
                        registration.openLoginItemsSettings()
                    }
                }
            }

            Toggle(
                "Show menu app at login",
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
                viewModel.snapshot?.status
                    ?? "Control service online at \(viewModel.controlBaseURL.absoluteString)"
            }
        case .checking:
            "Checking \(viewModel.controlBaseURL.absoluteString)"
        case let .offline(message):
            message
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
        if let usageError = snapshot.tokenSidecar?.lastError, !usageError.isEmpty {
            return "usage reporting: \(usageError)"
        }
        if snapshot.tokenSidecar?.enabled == true,
           snapshot.tokenSidecar?.writerReady == false {
            return "usage reporting is not ready"
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
}
