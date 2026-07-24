import AppKit
import MnemosyneAppCore
import ServiceManagement
import SwiftUI

struct MenuContentView: View {
    let workstationName: String
    @ObservedObject var viewModel: MenuViewModel
    @ObservedObject var registration: LaunchAgentRegistration
    let openConfiguration: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            residentModel
            modelController
            usageDelivery
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
    private var residentModel: some View {
        if let model = viewModel.snapshot?.residentAlias
            ?? viewModel.snapshot?.residentModel
        {
            LabeledContent("Resident model", value: model)
            if let engine = viewModel.snapshot?.residentEngine {
                LabeledContent("Engine", value: engine)
            }
            if let inFlight = viewModel.snapshot?.inFlightRequests {
                LabeledContent("In flight", value: String(inFlight))
            }
        } else {
            LabeledContent("Resident model", value: "None")
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
                Button("Unload Model") {
                    Task { await viewModel.unloadResidentModel() }
                }
                .disabled(
                    viewModel.snapshot?.residentAlias == nil
                        && viewModel.snapshot?.residentModel == nil
                        || viewModel.mutationInProgress
                )
            }
            HStack {
                Button("Open Logs") {
                    openApplicationSupport(subdirectory: "logs")
                }
                Button("Settings…") {
                    openConfiguration()
                }
                Spacer()
                Button("Quit Menu App") {
                    NSApplication.shared.terminate(nil)
                }
            }
        }
    }

    private var connectionSymbol: String {
        switch viewModel.connection {
        case .online:
            "checkmark.circle.fill"
        case .checking:
            "clock"
        case .offline:
            "exclamationmark.triangle.fill"
        }
    }

    private var connectionColor: Color {
        switch viewModel.connection {
        case .online:
            .green
        case .checking:
            .secondary
        case .offline:
            .orange
        }
    }

    private var connectionLabel: String {
        switch viewModel.connection {
        case .online:
            viewModel.snapshot?.status
                ?? "Control service online at \(viewModel.controlBaseURL.absoluteString)"
        case .checking:
            "Checking \(viewModel.controlBaseURL.absoluteString)"
        case let .offline(message):
            message
        }
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
