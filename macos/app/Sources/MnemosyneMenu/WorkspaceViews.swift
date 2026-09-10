import AppKit
import MnemosyneAppCore
import SwiftUI

struct WorkspaceCard<Content: View>: View {
    let title: String
    var subtitle: String? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if !title.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.headline)
                    if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary) }
                }
            }
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(22)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 18))
        .overlay { RoundedRectangle(cornerRadius: 18).strokeBorder(.primary.opacity(0.06)) }
    }
}

struct WorkspaceBadge: View {
    let text: String
    var color: Color = .secondary
    var body: some View {
        Text(text).font(.system(size: 10, weight: .medium))
            .padding(.horizontal, 9).padding(.vertical, 5)
            .foregroundStyle(color).background(color.opacity(0.09), in: Capsule())
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct WorkspaceMetric: View {
    let label: String
    let value: String
    let symbol: String
    var body: some View {
        WorkspaceCard(title: "") {
            HStack { Text(label).font(.caption).foregroundStyle(.secondary); Spacer(); Image(systemName: symbol).foregroundStyle(.indigo) }
            Text(value).font(.system(size: 29, weight: .medium, design: .rounded)).monospacedDigit()
        }
    }
}

struct WorkspaceHero: View {
    let activity: WorkspaceActivity
    let name: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        HStack(spacing: 30) {
            VStack(alignment: .leading, spacing: 12) {
                Text("\(name.uppercased()) · THIS MAC").font(.system(size: 10, weight: .semibold)).tracking(2).foregroundStyle(.white.opacity(0.65))
                Text(activity.title).font(.system(size: 30, weight: .semibold, design: .rounded))
                Text(activity.detail).font(.callout).foregroundStyle(.white.opacity(0.8)).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            ZStack {
                Circle().stroke(.white.opacity(0.08), lineWidth: 1).frame(width: 142, height: 142)
                Circle().fill(.white.opacity(0.06)).frame(width: 114, height: 114)
                Image(systemName: "desktopcomputer").font(.system(size: 55, weight: .ultraLight)).foregroundStyle(.white.opacity(0.85))
                Image(systemName: activity.symbol).font(.system(size: 18, weight: .medium))
                    .padding(10).background(.indigo, in: Circle()).offset(x: 46, y: 36)
                    .symbolEffect(.pulse, options: .repeating, isActive: activity.isWorking && !reduceMotion)
            }.accessibilityHidden(true)
        }
        .foregroundStyle(.white).padding(30).frame(maxWidth: .infinity, minHeight: 205, alignment: .leading)
        .background(LinearGradient(colors: [Color(red: 0.16, green: 0.13, blue: 0.26), Color(red: 0.31, green: 0.24, blue: 0.46)], startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 22))
    }
}

extension SettingsView {
    var workspaceOverview: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                WorkspaceHero(activity: live.activity, name: WorkstationIdentity.current)
                HStack(spacing: 14) {
                    WorkspaceMetric(label: "Active requests", value: live.isLive ? String(live.snapshot?.inFlightRequests ?? 0) : "—", symbol: "waveform")
                    WorkspaceMetric(label: "Callable models", value: live.isLive ? String(live.models.count) : "—", symbol: "square.stack.3d.up")
                    WorkspaceMetric(label: "Unified memory", value: workspaceBytes(Int64(ProcessInfo.processInfo.physicalMemory)), symbol: "memorychip")
                }
                WorkspaceCard(title: "In memory", subtitle: "Ready for the next request. Models can also load automatically.") {
                    HStack(spacing: 14) {
                        Image(systemName: "square.stack.3d.up.fill").font(.title).foregroundStyle(.indigo)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(live.snapshot?.residentAlias ?? "No model loaded").font(.title3.weight(.medium)).textSelection(.enabled)
                            Text(live.isLive ? (live.snapshot?.residentEngine ?? "Choose a model, or send an API request.") : "Last known state · waiting for an update").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if live.snapshot?.residentAlias != nil {
                            Button("Unload") { Task { await live.unloadResidentModel() } }
                                .disabled(!live.isLive || live.mutationInProgress)
                        }
                        Button("Explore Models") { viewModel.selectedSection = .models }.buttonStyle(.borderedProminent)
                    }
                    if !live.actionError.isEmpty { workspaceError(live.actionError) }
                }
                HStack(alignment: .top, spacing: 16) {
                    WorkspaceCard(title: "Part of something bigger", subtitle: "This Mac's contribution to Fleet.") {
                        Label(poolStatus, systemImage: "point.3.connected.trianglepath.dotted").font(.title3.weight(.medium))
                        Text(live.isLive ? "\(live.fleetParticipation?.activeRequests ?? 0) active Fleet request\(live.fleetParticipation?.activeRequests == 1 ? "" : "s")" : "Waiting for participation status").font(.caption).foregroundStyle(.secondary)
                        Button("View Fleet") { viewModel.selectedSection = .fleet }
                    }
                    WorkspaceCard(title: "Getting ready", subtitle: "Downloads continue while you work.") {
                        Text("\(viewModel.modelInstalls.filter(\.isActive).count) active download\(viewModel.modelInstalls.filter(\.isActive).count == 1 ? "" : "s")").font(.title3.weight(.medium))
                        Text(viewModel.updatingRuntimeEngine == nil ? "Models and runtime updates in one place." : "Preparing \(viewModel.updatingRuntimeEngine!.displayName)…").font(.caption).foregroundStyle(.secondary)
                        Button("View Downloads") { viewModel.selectedSection = .downloads }
                    }
                }
                if let storage = viewModel.readinessSnapshot?.storage.first(where: { !$0.available || !$0.volumeMatches }) {
                    WorkspaceCard(title: "Reconnect your model storage", subtitle: "\(storage.name) is unavailable. Models on this disk need the original volume.") {
                        Button("Review Storage") { viewModel.selectedSection = .storage }
                    }
                }
                if live.activity.needsAttention {
                    WorkspaceCard(title: "Let's get this Mac ready", subtitle: "Health checks explain the next step without changing your models.") {
                        Button("Open Setup & Health") { viewModel.selectedSection = .setup }
                    }
                }
                HStack {
                    Label("Local API", systemImage: "link").foregroundStyle(.secondary)
                    Text(live.inferenceEndpoint?.absoluteString ?? "Available after the service connects").font(.caption.monospaced()).textSelection(.enabled)
                    Spacer()
                    Button("Copy Endpoint", systemImage: "doc.on.doc") { copyWorkspaceEndpoint() }.disabled(live.inferenceEndpoint == nil)
                }.font(.caption).padding(.horizontal, 4)
            }.padding(26)
        }.scrollPosition($overviewScroll)
    }

    var workspaceModels: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                TextField("Search your models", text: $viewModel.modelSearch).textFieldStyle(.roundedBorder)
                Picker("Show models", selection: $viewModel.modelFilter) {
                    ForEach(["All", "Favorites", "Callable", "Vision"], id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: 145)
                Button("Find Models", systemImage: "plus") { viewModel.selectedSection = .library }
            }.padding(22)
            if !viewModel.modelTestError.isEmpty { workspaceError(viewModel.modelTestError).padding(.horizontal, 24).padding(.bottom, 12) }
            if !live.actionError.isEmpty { workspaceError(live.actionError).padding(.horizontal, 24).padding(.bottom, 12) }
            ScrollView {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 320), spacing: 16)], spacing: 16) {
                    ForEach(filteredWorkspaceModelIndices, id: \.self) { index in workspaceModelCard(index) }
                }.padding(.horizontal, 24).padding(.bottom, 24)
                if filteredWorkspaceModelIndices.isEmpty {
                    ContentUnavailableView("No matching models", systemImage: "square.stack.3d.up", description: Text("Try another search, change the filter, or find a model in the library."))
                    Button("Browse Model Library") { viewModel.selectedSection = .library }.padding(.bottom, 30)
                }
            }.scrollPosition($modelsScroll)
        }
    }

    var filteredWorkspaceModelIndices: [Int] {
        viewModel.settings.models.indices.filter { index in
            let p = viewModel.settings.models[index]
            let query = viewModel.modelSearch.trimmingCharacters(in: .whitespacesAndNewlines)
            let matches = query.isEmpty || "\(p.alias) \(p.engine.displayName) \(p.model)".localizedStandardContains(query)
            let filter = viewModel.modelFilter
            return matches && (filter == "All" || (filter == "Favorites" && preferences.favorites.contains(p.alias))
                || (filter == "Callable" && live.models.contains { $0.id == p.alias })
                || (filter == "Vision" && [.configured, .verified, .metadataDetected].contains(viewModel.visionReadiness(p))))
        }.sorted { a, b in
            let x = viewModel.settings.models[a], y = viewModel.settings.models[b]
            if preferences.favorites.contains(x.alias) != preferences.favorites.contains(y.alias) { return preferences.favorites.contains(x.alias) }
            return x.alias.localizedStandardCompare(y.alias) == .orderedAscending
        }
    }

    func workspaceModelCard(_ index: Int) -> some View {
        let profile = viewModel.settings.models[index]
        let callable = live.isLive && live.models.contains { $0.id == profile.alias }
        let vision = viewModel.visionReadiness(profile)
        return WorkspaceCard(title: "") {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: profile.kind == .image ? "photo.stack" : "square.stack.3d.up").font(.title2).foregroundStyle(.indigo)
                    .frame(width: 42, height: 42).background(.indigo.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
                VStack(alignment: .leading, spacing: 5) {
                    Text(profile.alias).font(.headline).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    Text(profile.engine.displayName).font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Button { preferences.toggleFavorite(profile.alias) } label: {
                    Image(systemName: preferences.favorites.contains(profile.alias) ? "star.fill" : "star")
                        .foregroundStyle(preferences.favorites.contains(profile.alias) ? Color.orange : .secondary)
                }.buttonStyle(.plain).help(preferences.favorites.contains(profile.alias) ? "Remove favorite" : "Favorite model")
                    .accessibilityLabel("Favorite \(profile.alias)")
            }
            HStack(spacing: 6) {
                WorkspaceBadge(text: !profile.enabled ? "Disabled" : callable ? "Callable" : live.isLive ? "Unavailable" : "Checking", color: callable ? .green : .secondary)
                if live.isLive && live.snapshot?.residentAlias == profile.alias { WorkspaceBadge(text: "In memory", color: .indigo) }
                if let quant = WorkspaceModelMetadata.quantization(path: profile.model) { WorkspaceBadge(text: quant) }
            }
            WorkspaceBadge(text: vision.label, color: vision == .verified ? .green : vision == .missingAdapter ? .orange : .secondary)
            if vision == .missingAdapter {
                Text("This profile declares vision but has no adapter configured. Review the model's components before testing images.")
                    .font(.caption).foregroundStyle(.orange)
            }
            if let install = viewModel.modelInstalls.first(where: { $0.alias == profile.alias && $0.status == "installed" && (profile.model == $0.destination || profile.model.hasPrefix($0.destination + "/")) }) {
                Label(workspaceBytes(install.bytesDownloaded) + " downloaded", systemImage: "internaldrive").font(.caption).foregroundStyle(.secondary)
            }
            if viewModel.testingAlias == profile.alias {
                HStack { ProgressView().controlSize(.small); Text("Loading and testing a real response…").font(.caption) }
            } else if let result = viewModel.lastSelfTest, result.model == profile.alias,
                      viewModel.verifiedModelProfiles[profile.alias] == profile {
                Text("Last test this session · \(result.responseMs / 1000, specifier: "%.1f") seconds")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Configure") { selectWorkspaceModel(index) }
                Spacer()
                Menu("Test") {
                    Button("Test Model") { Task { _ = await viewModel.runSelfTest(model: profile.alias); await live.refresh() } }
                    if [.llamaCpp, .omlx].contains(profile.engine),
                       profile.capabilities == nil || profile.capabilities?.contains("chat/completions") == true {
                        Button("Test with Image") { Task { _ = await viewModel.runSelfTest(model: profile.alias, requireVision: true); await live.refresh() } }
                    }
                }
                    .disabled(!callable || viewModel.isRunningSelfTest || viewModel.hasUnsavedChanges || viewModel.requiresRestart)
                    .help("Runs a real request, including vision when configured. May load this model.")
                Button("Load") { live.selectedAlias = profile.alias; Task { await live.loadSelectedModel() } }
                    .buttonStyle(.borderedProminent).disabled(!callable || live.mutationInProgress || viewModel.hasUnsavedChanges || viewModel.requiresRestart)
            }
        }
    }

    var workspaceDownloads: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(spacing: 14) {
                    WorkspaceMetric(label: "In progress", value: String(viewModel.modelInstalls.filter(\.isActive).count), symbol: "arrow.down")
                    WorkspaceMetric(label: "Ready to use", value: String(viewModel.modelInstalls.filter { $0.status == "installed" }.count), symbol: "checkmark.circle")
                    WorkspaceMetric(label: "Runtime updates", value: String(viewModel.runtimeUpdateSnapshot?.engines.filter(\.updateAvailable).count ?? 0), symbol: "arrow.triangle.2.circlepath")
                }
                WorkspaceCard(title: "Model downloads", subtitle: "Download → register → ready. Your inference keeps running.") {
                    HStack {
                        Picker("Show downloads", selection: $viewModel.downloadFilter) {
                            ForEach(["All", "Active", "Needs attention", "Completed"], id: \.self) { Text($0).tag($0) }
                        }.frame(width: 220)
                        Spacer()
                        Button("Refresh") { Task { await viewModel.refreshWorkspaceDownloads() } }
                        Button("Find Models") { viewModel.selectedSection = .library }
                    }
                    ForEach(filteredWorkspaceDownloads) { install in
                        VStack(alignment: .leading, spacing: 10) {
                            downloadInstallRow(install)
                            if let seconds = install.estimatedSecondsRemaining {
                                Text("About \(Duration.seconds(seconds).formatted(.units(allowed: [.hours, .minutes, .seconds], width: .abbreviated, maximumUnitCount: 1))) remaining")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            if install.status == "installed" {
                                Button("Show in Models") {
                                    viewModel.modelSearch = install.alias
                                    viewModel.modelFilter = "All"
                                    viewModel.selectedSection = .models
                                }.font(.caption)
                            }
                            Divider()
                        }
                    }
                    if filteredWorkspaceDownloads.isEmpty {
                        Label("Nothing here yet. Find a model to get started.", systemImage: "tray").foregroundStyle(.secondary).padding(.vertical, 20)
                    }
                    Toggle("Notify me when a model is ready", isOn: Binding(get: { preferences.notifyWhenReady }, set: { enabled in Task { await preferences.setNotifications(enabled) } }))
                        .font(.caption)
                    if !preferences.notificationMessage.isEmpty { Text(preferences.notificationMessage).font(.caption).foregroundStyle(.secondary) }
                }
                WorkspaceCard(title: "Engine updates", subtitle: "Your inference engines update independently of the Mac app.") {
                    HStack {
                        Text(viewModel.updatingRuntimeEngine.map { "Preparing \($0.displayName)…" } ?? "Updates are checked directly with each engine's official source.")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button("Check Now") { Task { await viewModel.refreshRuntimeUpdates(force: true) } }.disabled(viewModel.isCheckingRuntimeUpdates)
                    }
                    if let snapshot = viewModel.runtimeUpdateSnapshot {
                        ForEach(snapshot.engines.filter { $0.engine.isSupported }) { update in runtimeUpdateCard(update) }
                    } else { Text("Check for updates to see installed versions.").foregroundStyle(.secondary) }
                }
            }.padding(26)
        }.scrollPosition($downloadsScroll)
    }

    var filteredWorkspaceDownloads: [ModelInstall] {
        viewModel.modelInstalls.filter { install in
            switch viewModel.downloadFilter {
            case "Active": install.isActive
            case "Needs attention": ["failed", "partial", "downloaded"].contains(install.status)
            case "Completed": install.status == "installed"
            default: true
            }
        }.sorted { $0.updatedAt > $1.updatedAt }
    }

    var poolStatus: String {
        guard live.isLive else { return "Checking connection" }
        guard let pairing = live.fleetPairing, pairing.permitsParticipationControl else { return "Not paired with a hub" }
        switch live.fleetParticipation?.state {
        case "joined": return "Contributing to Fleet"
        case "draining": return "Finishing Fleet requests"
        case "paused": return "Contribution paused"
        default: return "Checking participation"
        }
    }

    var workspaceFleet: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                WorkspaceCard(title: "Your connection", subtitle: "Pairing connects this Mac. Participation decides when it shares capacity.") {
                    HStack(spacing: 20) {
                        fleetIdentity(WorkstationIdentity.current, symbol: "desktopcomputer", detail: "This Mac")
                        VStack(spacing: 8) {
                            Image(systemName: "arrow.left.arrow.right").font(.title2).foregroundStyle(live.fleetPairing?.permitsParticipationControl == true ? Color.indigo : .secondary)
                            Text(poolStatus).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        }.frame(maxWidth: .infinity)
                        fleetIdentity("Fleet hub", symbol: "point.3.connected.trianglepath.dotted", detail: live.fleetPairing?.state == "paired" ? "Paired" : "Enrollment")
                    }.padding(.vertical, 16)
                    HStack {
                        Toggle("Contribute this Mac", isOn: Binding(get: { live.fleetParticipation?.enabled ?? false }, set: { enabled in Task { await live.setFleetParticipation(enabled: enabled) } }))
                            .toggleStyle(.switch).disabled(!live.isLive || live.fleetPairing?.permitsParticipationControl != true || live.participationMutationInProgress)
                        Spacer()
                        Text(live.isLive ? "\(live.fleetParticipation?.activeRequests ?? 0) active Fleet request\(live.fleetParticipation?.activeRequests == 1 ? "" : "s")" : "Status unavailable").font(.caption).foregroundStyle(.secondary)
                    }
                    if live.fleetParticipation?.state == "draining" { Text("New Fleet requests are paused. Existing requests finish before this Mac becomes idle.").font(.caption).foregroundStyle(.secondary) }
                    if !live.actionError.isEmpty { workspaceError(live.actionError) }
                    HStack {
                        Button(live.fleetPairing?.permitsParticipationControl == true ? "Manage Enrollment" : "Join a Hub") { viewModel.selectedSection = .pool }

                    }
                }
                if hubMode.configuration != nil {
                    WorkspaceCard(title: "This Mac hosts a hub", subtitle: hubMode.configuration?.publicOrigin) {
                        HStack {
                            WorkspaceBadge(text: hubMode.hubHealthy ? "Hub responding" : "Hub not responding", color: hubMode.hubHealthy ? .green : .orange)
                            Spacer()
                            Button("Open Fleet Dashboard", systemImage: "arrow.up.right.square") { hubMode.openDashboard() }
                            Button("Hub Settings") { viewModel.selectedSection = .hub }
                        }
                        if !hubMode.pendingPairingClaims.isEmpty {
                            Label("\(hubMode.pendingPairingClaims.count) Mac(s) waiting for approval", systemImage: "person.badge.plus").foregroundStyle(.indigo)
                            Button("Review Pairing Requests") { viewModel.selectedSection = .hub }.buttonStyle(.borderedProminent)
                        }
                        if !hubMode.workspaceOverviewError.isEmpty { Text(hubMode.workspaceOverviewError).font(.caption).foregroundStyle(.orange) }
                        ForEach(hubMode.workspaceOverview?.nodes ?? []) { node in
                            HStack(spacing: 12) {
                                Image(systemName: "desktopcomputer").foregroundStyle(.indigo)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(node.nodeID.capitalized).font(.subheadline.weight(.medium))
                                    Text(node.residentModel?.alias ?? "No model in memory").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                WorkspaceBadge(text: !hubMode.workspaceIsLive ? "Last observed" : !node.online ? "Offline" : node.activeRequests > 0 ? "\(node.activeRequests) Fleet requests" : node.joinedState == "paused" ? "Paused" : "Connected", color: !hubMode.workspaceIsLive || !node.online ? .secondary : node.activeRequests > 0 ? .indigo : .green)
                            }.padding(.vertical, 10)
                        }
                        if hubMode.workspaceOverview?.nodes.isEmpty == true { Text("Pair a Mac to see its live activity here.").font(.caption).foregroundStyle(.secondary) }

                    }
                } else {
                    WorkspaceCard(title: "Bring your Macs together", subtitle: "An always-available Mac can host a hub and give every model one shared endpoint.") {
                        Button("Set Up Hub Mode") { viewModel.selectedSection = .hub }
                    }
                }
                WorkspaceCard(title: "Downloads from your hub", subtitle: "Review requests before this Mac downloads anything.") {
                    Text("\(viewModel.desiredInstalls.total) requests").font(.title3)
                    Button("Review Download Requests") { viewModel.selectedSection = .pool }
                }
            }.padding(26)
        }.scrollPosition($fleetScroll)
    }

    func fleetIdentity(_ title: String, symbol: String, detail: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: symbol).font(.system(size: 31, weight: .light)).foregroundStyle(.indigo)
                .frame(width: 72, height: 65).background(.indigo.opacity(0.07), in: RoundedRectangle(cornerRadius: 16))
            Text(title).font(.headline).lineLimit(1)
            Text(detail).font(.caption).foregroundStyle(.secondary)
        }.frame(width: 155)
    }

    func sidebarGroup(_ title: String, sections: [SettingsViewModel.Section]) -> some View {
        let filtered = sections.filter { viewModel.workspaceSearch.isEmpty || $0.searchTerms.localizedStandardContains(viewModel.workspaceSearch) }
        return VStack(alignment: .leading, spacing: 5) {
            if !filtered.isEmpty {
                Text(title).font(.system(size: 9, weight: .semibold)).tracking(1.4).foregroundStyle(.tertiary).padding(.horizontal, 10).padding(.bottom, 4)
                ForEach(filtered) { section in
                    Button { viewModel.selectedSection = section } label: {
                        HStack(spacing: 10) {
                            Image(systemName: section.symbol).frame(width: 18)
                            Text(section.rawValue).font(.system(size: 12, weight: viewModel.selectedSection == section ? .semibold : .regular))
                            Spacer(minLength: 0)
                            if section == .downloads, viewModel.modelInstalls.contains(where: \.isActive) {
                                Text(String(viewModel.modelInstalls.filter(\.isActive).count)).font(.caption2).foregroundStyle(.indigo)
                            }
                        }.padding(.horizontal, 10).padding(.vertical, 9)
                            .foregroundStyle(viewModel.selectedSection == section ? Color.indigo : .primary)
                            .background(viewModel.selectedSection == section ? Color.indigo.opacity(0.1) : .clear, in: RoundedRectangle(cornerRadius: 9))
                    }.buttonStyle(.plain)
                }
            }
        }
    }

    var sidebarModelResults: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("MODELS").font(.caption2).foregroundStyle(.secondary)
            ForEach(viewModel.settings.models.indices.filter { viewModel.settings.models[$0].alias.localizedStandardContains(viewModel.workspaceSearch) }.prefix(12), id: \.self) { index in
                Button(viewModel.settings.models[index].alias) { selectWorkspaceModel(index) }.buttonStyle(.plain).font(.caption).lineLimit(2)
            }
        }.padding(.horizontal, 10)
    }

    func navigateFirstSearchResult() {
        if let section = SettingsViewModel.Section.allCases.first(where: { $0.searchTerms.localizedStandardContains(viewModel.workspaceSearch) }) { viewModel.selectedSection = section }
        else if let index = viewModel.settings.models.firstIndex(where: { $0.alias.localizedStandardContains(viewModel.workspaceSearch) }) { selectWorkspaceModel(index) }
    }

    func selectWorkspaceModel(_ index: Int) {
        viewModel.selectedModelIndex = index
        viewModel.selectedSection = .modelSettings
    }

    func copyWorkspaceEndpoint() {
        guard let endpoint = live.inferenceEndpoint else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(endpoint.absoluteString, forType: .string)
    }

    func workspaceBytes(_ value: Int64) -> String { ByteCountFormatter.string(fromByteCount: value, countStyle: .memory) }

    func workspaceError(_ detail: String) -> some View {
        let issue = WorkspaceIssue(detail)
        return VStack(alignment: .leading, spacing: 8) {
            Label(issue.title, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            Text(issue.explanation).foregroundStyle(.secondary)
            DisclosureGroup("Details") { Text(detail).font(.caption).textSelection(.enabled) }
            Button(issue.action) { viewModel.selectedSection = issue.section }
        }.font(.caption)
    }
}

private struct WorkspaceIssue {
    let title: String
    let explanation: String
    let action: String
    let section: SettingsViewModel.Section
    init(_ detail: String) {
        let code = detail.lowercased()
        if code.contains("mmproj") || code.contains("projector") || code.contains("image input is not supported") {
            title = "Vision needs its adapter"
            explanation = "Check this model's vision setup, then run its self-test."
            action = "Review Models"; section = .models
        } else if code.contains("storage_unavailable") || code.contains("volume_mismatch") || code.contains("scope_unavailable") {
            title = "Model storage is unavailable"
            explanation = "Reconnect the original disk or review access to its model folder."
            action = "Review Storage"; section = .storage
        } else if code.contains("not_installed") || code.contains("engine_unavailable") {
            title = "The inference engine needs attention"
            explanation = "Check the installed runtime and its status."
            action = "Review Runtimes"; section = .updates
        } else {
            title = "This action couldn't finish"
            explanation = "Check this Mac's health for the next step."
            action = "Open Setup & Health"; section = .setup
        }
    }
}
