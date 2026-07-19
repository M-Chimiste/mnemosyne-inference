import SwiftUI

struct ConfigurationEditorView: View {
    @ObservedObject var viewModel: ConfigurationEditorViewModel
    let restartService: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Picker("Document", selection: $viewModel.selectedTab) {
                ForEach(ConfigurationEditorViewModel.Tab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            editor
            status
            Divider()
            actions
        }
        .padding(16)
        .frame(minWidth: 720, minHeight: 540)
        .onAppear {
            if !viewModel.hasUnsavedChanges {
                viewModel.loadFromDisk()
            }
        }
        .confirmationDialog(
            "Save without schema validation?",
            isPresented: $viewModel.confirmUnvalidatedSave
        ) {
            Button("Save Without Validation", role: .destructive) {
                viewModel.saveWithoutValidation()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "Use this only to repair a configuration while the control service is unavailable. Invalid YAML can prevent the service from starting."
            )
        }
        .alert(
            "Discard unsaved changes?",
            isPresented: $viewModel.confirmReloadFromDisk
        ) {
            Button("Cancel", role: .cancel) {}
            Button("Reload from Disk", role: .destructive) {
                viewModel.loadFromDisk()
            }
        } message: {
            Text("This replaces both editor drafts with the current files on disk.")
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Unified Inference Configuration", systemImage: "slider.horizontal.3")
                .font(.title2.weight(.semibold))
            Text(activeURL.path)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    @ViewBuilder
    private var editor: some View {
        switch viewModel.selectedTab {
        case .configuration:
            TextEditor(text: $viewModel.configYAML)
                .font(.system(.body, design: .monospaced))
                .textEditorStyle(.plain)
                .overlay(editorBorder)
                .accessibilityLabel("config.yaml editor")
        case .environment:
            VStack(alignment: .leading, spacing: 6) {
                Label(
                    "This file contains secrets. Values are saved locally with mode 0600.",
                    systemImage: "lock.fill"
                )
                .font(.caption)
                .foregroundStyle(.orange)
                TextEditor(text: $viewModel.environment)
                    .font(.system(.body, design: .monospaced))
                    .textEditorStyle(.plain)
                    .overlay(editorBorder)
                    .accessibilityLabel("environment file editor")
                    .privacySensitive()
            }
        }
    }

    private var editorBorder: some View {
        RoundedRectangle(cornerRadius: 6)
            .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
    }

    @ViewBuilder
    private var status: some View {
        if viewModel.isWorking {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(viewModel.statusMessage)
            }
            .font(.caption)
        } else if !viewModel.statusMessage.isEmpty {
            Text(viewModel.statusMessage)
                .font(.caption)
                .foregroundStyle(viewModel.statusColor)
                .textSelection(.enabled)
        }
    }

    private var actions: some View {
        HStack {
            Button("Reload from Disk") {
                if viewModel.hasUnsavedChanges {
                    viewModel.confirmReloadFromDisk = true
                } else {
                    viewModel.loadFromDisk()
                }
            }
            .disabled(viewModel.isWorking)

            if viewModel.canSaveWithoutValidation {
                Button("Save Without Validation…") {
                    viewModel.confirmUnvalidatedSave = true
                }
                .disabled(viewModel.isWorking)
            }

            if viewModel.requiresRestart {
                Button("Restart Service") {
                    restartService()
                }
                .disabled(viewModel.isWorking)
            }

            Spacer()

            Button("Validate, Save & Apply") {
                Task { await viewModel.validateSaveAndApply() }
            }
            .keyboardShortcut("s", modifiers: .command)
            .disabled(!viewModel.hasUnsavedChanges || viewModel.isWorking)
        }
    }

    private var activeURL: URL {
        switch viewModel.selectedTab {
        case .configuration:
            viewModel.configURL
        case .environment:
            viewModel.environmentURL
        }
    }
}
