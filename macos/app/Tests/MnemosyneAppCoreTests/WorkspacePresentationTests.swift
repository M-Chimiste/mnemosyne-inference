import Foundation
import Testing
@testable import MnemosyneAppCore

private func workspaceSnapshot(_ state: String, active: Int = 0, error: String? = nil) -> ServiceSnapshot {
    ServiceSnapshot(status: state, residentAlias: "qwen", residentModel: nil,
                    residentEngine: "llama.cpp", inFlightRequests: active,
                    tokenSidecar: nil, startupError: error)
}

@Test("Disconnected snapshots cannot present live inference")
func workspaceStaleActivity() {
    let stale = WorkspaceActivity(snapshot: workspaceSnapshot("ready", active: 4), isLive: false)
    #expect(!stale.isWorking)
    #expect(stale.title == "Reconnecting")
    let live = WorkspaceActivity(snapshot: workspaceSnapshot("ready", active: 4), isLive: true)
    #expect(live.isWorking)
    #expect(live.title == "Running inference")
    #expect(live.detail.contains("4 active requests"))
}

@Test("Loading, idle, and empty diagnostics have distinct honest states")
func workspaceActivityPhases() {
    #expect(WorkspaceActivity(snapshot: workspaceSnapshot("loading"), isLive: true).title == "Preparing a model")
    #expect(WorkspaceActivity(snapshot: workspaceSnapshot("ready", error: ""), isLive: true).title == "Ready when you are")
    #expect(!WorkspaceActivity(snapshot: workspaceSnapshot("ready"), isLive: true).isWorking)
    #expect(WorkspaceActivity(snapshot: workspaceSnapshot("degraded"), isLive: true).needsAttention)
    #expect(WorkspaceActivity(snapshot: workspaceSnapshot("draining", active: 2), isLive: true).title == "Finishing 2 requests")
}

@Test("A configured image adapter must never imply a successful vision test")
func workspaceVisionEvidence() {
    var p = ModelProfileSettings(alias: "qwen", capabilities: ["chat", "vision"])
    #expect(ModelVisionReadiness(profile: p, verified: false) == .missingAdapter)
    p.load.projectorPath = "/Models/mmproj.gguf"
    #expect(ModelVisionReadiness(profile: p, verified: false) == .configured)
    #expect(ModelVisionReadiness(profile: p, verified: true) == .verified)
    p.load.projectorPath = " \n"
    #expect(ModelVisionReadiness(profile: p, verified: false) == .missingAdapter)
    p.capabilities = ["chat"]
    #expect(ModelVisionReadiness(profile: p, verified: false) == .notDeclared)
    p.kind = .image
    #expect(ModelVisionReadiness(profile: p, verified: true) == .imageGeneration)
}

@Test("Only explicit GGUF quantization tokens become model badges")
func workspaceQuantization() {
    #expect(WorkspaceModelMetadata.quantization(path: "/Models/Qwen3.8-Flash-Q8_0.gguf") == "Q8_0")
    #expect(WorkspaceModelMetadata.quantization(path: "/Models/Qwen3.8-IQ4_XS-00001-of-00003.gguf") == "IQ4_XS")
    #expect(WorkspaceModelMetadata.quantization(path: "/Models/model.bf16.gguf") == "BF16")
    #expect(WorkspaceModelMetadata.quantization(path: "/Models/4bit-MLX") == nil)
    #expect(WorkspaceModelMetadata.quantization(path: "/Q8_0/model.gguf") == nil)
}

@Test("Save impact matches the native manager's restart-sensitive settings")
func workspaceChangeImpact() {
    let saved = NativeSettings()
    var draft = saved
    #expect(ConfigurationChangeImpact(saved: saved, draft: draft, credentialsChanged: false) == .none)
    draft.models.append(ModelProfileSettings(alias: "new-model"))
    #expect(ConfigurationChangeImpact(saved: saved, draft: draft, credentialsChanged: false) == .immediate)
    draft.server.inferencePort = 1241
    #expect(ConfigurationChangeImpact(saved: saved, draft: draft, credentialsChanged: false) == .restart)
    #expect(ConfigurationChangeImpact(saved: saved, draft: saved, credentialsChanged: true) == .restart)
    draft = saved; draft.engines.llamaCpp.enabled.toggle()
    #expect(ConfigurationChangeImpact(saved: saved, draft: draft, credentialsChanged: false) == .restart)
    draft = saved; draft.storage.default = "other-disk"
    #expect(ConfigurationChangeImpact(saved: saved, draft: draft, credentialsChanged: false) == .restart)
}

@Test("ETA disappears for registration, missing totals, and stalled downloads")
func workspaceDownloadETA() throws {
    func install(status: String = "downloading", speed: Double = 100, total: Int? = 1000, downloaded: Int = 500) throws -> ModelInstall {
        var object: [String: Any] = ["id":"one", "repo_id":"org/model", "engine":"llama.cpp", "storage":"models", "alias":"qwen", "destination":"/Models", "status":status, "bytes_downloaded":downloaded, "download_speed_bps":speed, "created_at":0, "updated_at":0]
        object["total_bytes"] = total
        return try JSONDecoder.nativeSettingsDecoder().decode(ModelInstall.self, from: JSONSerialization.data(withJSONObject: object))
    }
    #expect(try install().estimatedSecondsRemaining == 5)
    #expect(try install(status: "registering").estimatedSecondsRemaining == nil)
    #expect(try install(speed: 0).estimatedSecondsRemaining == nil)
    #expect(try install(total: nil).estimatedSecondsRemaining == nil)
    #expect(try install(downloaded: 1100).estimatedSecondsRemaining == nil)
}

@Test("Endpoint metadata comes from the running service and stays backward compatible")
func workspaceRunningPorts() throws {
    let current = try JSONDecoder().decode(ServiceSnapshot.self, from: Data(#"{"status":"ready","ports":{"inference":1245,"control":17321}}"#.utf8))
    #expect(current.ports?.inference == 1245)
    let old = try JSONDecoder().decode(ServiceSnapshot.self, from: Data(#"{"status":"ready"}"#.utf8))
    #expect(old.ports == nil)
}

@Test("Native Fleet overview stays on the authenticated loopback endpoint")
func workspaceHubOverview() throws {
    let request = try HubPairingAdminClient(adminKey: "private-admin-test-key").workspaceOverviewRequest()
    #expect(request.url?.absoluteString == "http://127.0.0.1:17400/fleet/api/overview")
    #expect(request.httpMethod == "GET")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer private-admin-test-key")
    #expect(request.httpBody == nil)
    let data = Data(#"{"schema_version":1,"nodes":[{"node_id":"metis","enrollment_id":"paired:exact-id","online":true,"joined_state":"joined","active_requests":2,"resident_model":{"alias":"qwen","engine":"llama.cpp"}}]}"#.utf8)
    let overview = try JSONDecoder().decode(HubWorkspaceOverview.self, from: data)
    #expect(overview.nodes[0].id == "paired:exact-id")
    #expect(overview.nodes[0].activeRequests == 2)
    #expect(overview.nodes[0].residentModel?.alias == "qwen")
}
