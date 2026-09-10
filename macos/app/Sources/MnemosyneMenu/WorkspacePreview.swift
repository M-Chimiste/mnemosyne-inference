import AppKit
import MnemosyneAppCore
import SwiftUI

/// Debug-only visual QA entry point. Release builds never load preview fixtures.
@MainActor
enum WorkspacePreview {
    static var isRequested: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("--workspace-preview") || Bundle.main.bundleIdentifier == "com.mnemosyne.workspace.preview"
        #else
        false
        #endif
    }

    #if DEBUG
    private static var window: NSWindow?
    private static var model: SettingsViewModel?
    #endif

    static func navigate(_ section: SettingsViewModel.Section? = nil) {
        #if DEBUG
        if let section { model?.selectedSection = section }
        window?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        #endif
    }

    static func show() {
        #if DEBUG
        do {
            let args = ProcessInfo.processInfo.arguments
            let file: URL
            if let index = args.firstIndex(of: "--workspace-preview"), args.indices.contains(index + 1) {
                file = URL(fileURLWithPath: args[index + 1])
            } else if let bundled = Bundle.main.url(forResource: "workspace", withExtension: "json") { file = bundled }
            else { return }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let fixture = try decoder.decode(WorkspacePreviewFixture.self, from: Data(contentsOf: file))
            let config = ControlConnectionConfiguration(baseURL: URL(string: "http://127.0.0.1:9")!, passwordEnvironmentKey: "PREVIEW_PASSWORD", adminPassword: nil,
                configURL: file.deletingLastPathComponent().appending(path: "preview-config.yaml"),
                environmentURL: file.deletingLastPathComponent().appending(path: "preview.env"))
            let settings = SettingsViewModel(configuration: config)
            settings.loadWorkspacePreview(fixture)
            model = settings
            let live = MenuViewModel(connectionConfiguration: config)
            live.loadWorkspacePreview(fixture)
            let startup = ServiceStartupCoordinator(probe: {})
            let registration = LaunchAgentRegistration()
            let hub = HubModeViewModel(configuration: config)
            Task {
                await startup.connect(registration: .enabled)
                let content = VStack(spacing: 0) {
                    Text("DESIGN PREVIEW · SAMPLE DATA · No live service connection").font(.caption).foregroundStyle(.secondary).padding(8)
                    SettingsView(viewModel: settings, registration: registration, startup: startup, markSetupCompleted: {}, restartService: {}, hubMode: hub, live: live)
                }
                let result = NSWindow(contentViewController: NSHostingController(rootView: content))
                result.title = "Unified Inference — Design Preview"
                result.styleMask = [.titled, .closable, .miniaturizable, .resizable]
                result.setContentSize(NSSize(width: 1140, height: 840))
                if args.contains("--light") { result.appearance = NSAppearance(named: .aqua) }
                result.isReleasedWhenClosed = false
                result.center()
                window = result
                NSApplication.shared.setActivationPolicy(.regular)
                result.makeKeyAndOrderFront(nil)
                NSApplication.shared.activate(ignoringOtherApps: true)
            }
        } catch { NSLog("Workspace preview could not load: %@", String(describing: error)) }
        #endif
    }
}

#if DEBUG
struct WorkspacePreviewFixture: Decodable {
    let config: NativeSettings
    let readiness: ReadinessSnapshot
    let installs: [ModelInstall]
    let updates: RuntimeUpdateSnapshot?
    let status: ServiceSnapshot
    let catalog: ModelCatalogSnapshot
    let pairing: FleetPairingSnapshot?
    let participation: FleetParticipationSnapshot?

    enum CodingKeys: String, CodingKey { case config, readiness, installs, updates, status, catalog, pairing, participation }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var settings = NativeSettings()
        settings.models = try c.decode([ModelProfileSettings].self, forKey: .config)
        config = settings
        readiness = try c.decode(ReadinessSnapshot.self, forKey: .readiness)
        installs = try c.decode([ModelInstall].self, forKey: .installs)
        updates = try c.decodeIfPresent(RuntimeUpdateSnapshot.self, forKey: .updates)
        // Wire snapshots use explicit snake-case CodingKeys and their own decoder.
        let raw = try c.decode([String: String].self, forKey: .status)
        func wire<T: Decodable>(_ type: T.Type, _ key: String) throws -> T {
            guard let value = raw[key] else { throw DecodingError.dataCorrupted(.init(codingPath: c.codingPath, debugDescription: "Missing preview snapshot: \(key)")) }
            return try JSONDecoder().decode(type, from: Data(value.utf8))
        }
        status = try wire(ServiceSnapshot.self, "status")
        catalog = try wire(ModelCatalogSnapshot.self, "catalog")
        pairing = try wire(FleetPairingSnapshot.self, "pairing")
        participation = try wire(FleetParticipationSnapshot.self, "participation")
    }
}
#endif
