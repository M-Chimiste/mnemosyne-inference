import AppKit
import InstallCore
import SwiftUI

@MainActor
final class InstallerModel: ObservableObject {
    @Published var busy = false
    @Published var status = "A fresh start for your next update."
    @Published var failure: String?
    @Published var result: InstallResult?
    let workflow: InstallWorkflow?
    let initializationError: String?

    init() {
        do { workflow = try InstallWorkflow(); initializationError = nil }
        catch { workflow = nil; initializationError = error.localizedDescription }
    }

    func install() {
        guard let workflow, !busy else { return }
        busy = true
        failure = nil
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let installed = try workflow.install(progress: { message in
                    DispatchQueue.main.async { self.status = message }
                }, applicationIsRunning: {
                    DispatchQueue.main.sync {
                        NSWorkspace.shared.runningApplications.contains {
                            $0.bundleIdentifier == InstallWorkflow.applicationIdentifier
                        }
                    }
                })
                DispatchQueue.main.async { self.result = installed; self.busy = false; self.status = "Installed and verified by Gatekeeper." }
            } catch {
                DispatchQueue.main.async { self.failure = error.localizedDescription; self.busy = false; self.status = "The installation could not finish." }
            }
        }
    }
}

struct InstallerView: View {
    @ObservedObject var model: InstallerModel
    private let violet = Color(red: 0.48, green: 0.40, blue: 1)

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 15) {
                Image(systemName: model.result == nil ? "sparkle" : "checkmark")
                    .font(.system(size: 31, weight: .medium)).foregroundStyle(.white)
                    .frame(width: 64, height: 64)
                    .background(LinearGradient(colors: [violet.opacity(0.7), violet], startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 19))
                VStack(alignment: .leading, spacing: 5) {
                    Text(model.result == nil ? "Install Unified Inference" : "You're ready to go")
                        .font(.system(size: 25, weight: .semibold))
                    Text(model.workflow.map { "Version \($0.manifest.version) · Build \($0.manifest.build)" } ?? "Unified Inference")
                        .foregroundStyle(.secondary)
                }
            }
            Text(model.result == nil ? "Your models. Your settings. A clean application update." : "The new application is in Applications. Open it to reconnect your services.")
                .font(.system(size: 15)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 18) {
                detail("checkmark.shield", "Verified before and after", "Every copied file is checked against this signed release.")
                detail("arrow.triangle.2.circlepath", "A fresh bundle, every time", "The complete app is exchanged without merging old files.")
                detail("externaldrive", "Your workspace stays with you", "Models, settings, credentials, and history stay in place.")
            }
            .padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 15))
            if let failure = model.failure ?? model.initializationError {
                ScrollView { Text(failure).font(.system(size: 12)).foregroundStyle(.red).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled) }
                    .frame(maxHeight: 100)
            } else if model.result == nil {
                Text("Finish any inference or downloads and quit Unified Inference before installing. Enabled services reconnect when you open the new app. The previous app is retained for recovery.")
                    .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 12) {
                if model.busy { ProgressView().controlSize(.small) }
                Text(model.status).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 10)
                if let result = model.result {
                    if let recovery = result.recoveryFolder {
                        Button("Previous App") { NSWorkspace.shared.open(recovery) }
                    }
                    Button("Open Unified Inference") {
                        let configuration = NSWorkspace.OpenConfiguration()
                        NSWorkspace.shared.openApplication(at: result.application, configuration: configuration) { _, error in
                            DispatchQueue.main.async {
                                if let error { model.failure = error.localizedDescription }
                                else { NSApplication.shared.terminate(nil) }
                            }
                        }
                    }.buttonStyle(.borderedProminent)
                } else {
                    Button("Install") { model.install() }
                        .buttonStyle(.borderedProminent).disabled(model.busy || model.workflow == nil)
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(32).frame(width: 610).tint(violet)
        .interactiveDismissDisabled(model.busy)
    }

    private func detail(_ icon: String, _ title: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon).font(.system(size: 20)).foregroundStyle(violet).frame(width: 25)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 14, weight: .semibold))
                Text(text).font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var model: InstallerModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // The candidate and previous app are journaled, but don't allow routine Quit
        // to interrupt the exchange and its final Gatekeeper assessment.
        MainActor.assumeIsolated { model?.busy == true ? .terminateCancel : .terminateNow }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        MainActor.assumeIsolated { model?.busy != true }
    }
}

@main
struct InstallUnifiedInferenceApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = InstallerModel()
    var body: some Scene {
        Window("Install Unified Inference", id: "install") {
            InstallerView(model: model).onAppear {
                delegate.model = model
                NSApp.windows.first { $0.title == "Install Unified Inference" }?.delegate = delegate
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        .windowResizability(.contentSize)
        .commands { CommandGroup(replacing: .newItem) {} }
    }
}
