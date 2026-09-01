import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("The menu app defaults to Mnemosyne's dedicated control port")
func defaultControlPort() {
    #expect(ControlAPIClient.defaultBaseURL.host == "127.0.0.1")
    #expect(ControlAPIClient.defaultBaseURL.port == 17_321)
    #expect(NativeSettings().engines.llamaCpp.enabled)
    #expect(NativeSettings().engines.llamaCpp.port == 17_325)
    #expect(NativeSettings().server.inferencePort == 1_240)
    #expect(!NativeSettings().engines.omlx.enabled)
    #expect(!NativeSettings().engines.ds4.enabled)
    #expect(!NativeSettings().engines.mflux.enabled)
    #expect(!NativeSettings().engines.mlxcel.enabled)
    #expect(!NativeSettings().engines.mistralRs.enabled)
    #expect(NativeSettings().schemaVersion == 6)
    #expect(NativeSettings().server.maxConcurrency == nil)
    #expect(NativeSettings().server.maxQueueDepth == 128)
    #expect(NativeSettings().server.idleUnloadSeconds == nil)
    #expect(NativeSettings().server.fleetApiKeyEnv == "FLEET_API_KEY")
    #expect(
        NativeSettings().server.fleetInferenceApiKeyEnv
            == "FLEET_INFERENCE_API_KEY"
    )
    #expect(NativeSettings().tokenSidecar.enabled)
}

@Test("The default model destination is an expanded absolute lexical path")
func defaultModelDestinationIsExpanded() {
    let expected = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Mnemosyne/models")
        .path
    #expect(StorageLocationSettings.internalDefault.path == expected)
    #expect(StorageLocationSettings.internalDefault.path.hasPrefix("/"))
    #expect(!StorageLocationSettings.internalDefault.path.contains("~"))
}

@Test("Endpoint paths are resolved below the control origin")
func endpointResolution() {
    let client = ControlAPIClient(baseURL: URL(string: "http://localhost:17321")!)
    #expect(client.endpointURL("/manager/status").absoluteString == "http://localhost:17321/manager/status")
    #expect(client.endpointURL("/manager/models").absoluteString == "http://localhost:17321/manager/models")
}

@Test("oMLX cache health remains metadata-only and decodes large byte counts")
func omlxCacheHealthDecoding() throws {
    let payload = #"""
    {
      "available":true,
      "total_requests":42,
      "total_cached_tokens":12000,
      "cache_efficiency":0.42,
      "ssd_file_count":80,
      "ssd_size_bytes":153545080832,
      "ssd_limit_bytes":274877906944,
      "hot_size_bytes":1073741824,
      "hot_limit_bytes":8589934592,
      "reset_recommended":false,
      "diagnostic":null
    }
    """#.data(using: .utf8)!

    let health = try JSONDecoder.nativeSettingsDecoder().decode(
        OMLXCacheHealth.self,
        from: payload
    )
    #expect(health.totalRequests == 42)
    #expect(health.ssdSizeBytes == 153_545_080_832)
    #expect(health.cacheEfficiency == 0.42)
    #expect(!health.resetRecommended)
}

@Test("Load requests use the control endpoint and exact JSON body")
func loadRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.loadRequest(model: "glm-5-2")

    #expect(request.url?.absoluteString == "http://localhost:17321/manager/load")
    #expect(request.httpMethod == "POST")
    #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Basic YWRtaW46c2VjcmV0")
    let body = try #require(request.httpBody)
    let payload = try JSONDecoder().decode(LoadModelRequest.self, from: body)
    #expect(payload == LoadModelRequest(model: "glm-5-2"))
}

@Test("Fleet participation reads use the authenticated control endpoint")
func fleetParticipationRequestEncoding() {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = client.fleetParticipationRequest()

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/fleet/participation"
    )
    #expect(request.httpMethod == "GET")
    #expect(request.value(forHTTPHeaderField: "Accept") == "application/json")
    #expect(
        request.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )
}

@Test("Fleet pairing status uses the authenticated secret-free control endpoint")
func fleetPairingRequestEncoding() {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = client.fleetPairingRequest()

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/fleet/pairing"
    )
    #expect(request.httpMethod == "GET")
    #expect(request.value(forHTTPHeaderField: "Accept") == "application/json")
    #expect(
        request.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )
}

@Test("Discarding a rejected claim uses a body-free local control mutation")
func fleetPairingDiscardRejectedRequestEncoding() {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = client.fleetPairingDiscardRejectedRequest()

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/fleet/pairing/discard-rejected"
    )
    #expect(request.httpMethod == "POST")
    #expect(request.httpBody == nil)
    #expect(
        request.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )
}

@Test("Fleet self-revocation carries only one restart-safe request identity")
func fleetPairingRevokeRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let requestID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    let request = try client.fleetPairingRevokeRequest(requestID: requestID)

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/fleet/pairing/revoke"
    )
    #expect(request.httpMethod == "POST")
    #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
    #expect(
        request.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )
    let body = try #require(request.httpBody)
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(object.count == 2)
    #expect(object["schema_version"] as? Int == 1)
    #expect(object["request_id"] as? String == requestID)
}

@Test("Fleet participation mutations send only the requested local state")
func setFleetParticipationRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.setFleetParticipationRequest(enabled: false)

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/fleet/participation"
    )
    #expect(request.httpMethod == "PUT")
    #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
    #expect(
        request.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )
    let body = try #require(request.httpBody)
    let payload = try JSONDecoder().decode(
        SetFleetParticipationRequest.self,
        from: body
    )
    #expect(payload == SetFleetParticipationRequest(enabled: false))
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(object.count == 1)
    #expect(object["enabled"] as? Bool == false)
}

@Test("Fleet participation snapshots decode drain progress")
func fleetParticipationDecoding() throws {
    let payload = #"""
    {
      "enabled": false,
      "state": "draining",
      "active_requests": 3,
      "updated_at": 1788027600.25
    }
    """#.data(using: .utf8)!

    let snapshot = try JSONDecoder().decode(
        FleetParticipationSnapshot.self,
        from: payload
    )

    #expect(!snapshot.enabled)
    #expect(snapshot.state == "draining")
    #expect(snapshot.activeRequests == 3)
    #expect(snapshot.updatedAt == 1_788_027_600.25)
}

@Test("Fleet pairing status decodes without any credential fields")
func fleetPairingDecoding() throws {
    let payload = #"""
    {
      "schema_version": 1,
      "available": true,
      "state": "paired",
      "device_id": "11111111-1111-4111-8111-111111111111",
      "pairing_id": "22222222-2222-4222-8222-222222222222",
      "reporting_node_id": "studio-mac",
      "credential_generation": 1,
      "credentials_configured": true,
      "legacy_credentials_present": false,
      "last_error_code": null,
      "updated_at": 1788027600.25,
      "paired_at": 1788027590.0,
      "revoked_at": null
    }
    """#.data(using: .utf8)!

    let snapshot = try JSONDecoder().decode(
        FleetPairingSnapshot.self,
        from: payload
    )

    #expect(snapshot.schemaVersion == 1)
    #expect(snapshot.available)
    #expect(snapshot.state == "paired")
    #expect(snapshot.pairingId == "22222222-2222-4222-8222-222222222222")
    #expect(snapshot.reportingNodeId == "studio-mac")
    #expect(snapshot.credentialGeneration == 1)
    #expect(snapshot.credentialsConfigured == true)
    #expect(snapshot.permitsParticipationControl)
    #expect(snapshot.selfRevoke == nil)
}

@Test("Fleet pairing status retains the exact pending self-revoke retry ID")
func fleetPairingSelfRevokeDecoding() throws {
    let payload = #"""
    {
      "schema_version": 1,
      "available": true,
      "state": "paired",
      "credentials_configured": true,
      "legacy_credentials_present": false,
      "self_revoke": {
        "schema_version": 1,
        "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "phase": "hub_committed"
      }
    }
    """#.data(using: .utf8)!

    let snapshot = try JSONDecoder().decode(
        FleetPairingSnapshot.self,
        from: payload
    )
    #expect(snapshot.selfRevoke?.requestID == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    #expect(snapshot.selfRevoke?.phase == "hub_committed")
}

@Test("Only paired or explicitly static enrollment enables pool controls")
func fleetPairingParticipationBoundary() throws {
    func decode(_ state: String, legacy: Bool) throws -> FleetPairingSnapshot {
        let data = try JSONSerialization.data(
            withJSONObject: [
                "schema_version": 1,
                "available": true,
                "state": state,
                "legacy_credentials_present": legacy,
            ]
        )
        return try JSONDecoder().decode(FleetPairingSnapshot.self, from: data)
    }

    #expect(try decode("paired", legacy: false).permitsParticipationControl)
    #expect(try decode("unpaired", legacy: true).permitsParticipationControl)
    #expect(!(try decode("pending", legacy: false)).permitsParticipationControl)
    #expect(!(try decode("revoked", legacy: false)).permitsParticipationControl)
}

@Test("Pairing owns Fleet credential slots throughout its durable lifecycle")
func fleetPairingCredentialOwnershipBoundary() throws {
    func decode(
        _ state: String,
        configured: Bool? = nil,
        legacy: Bool = false
    ) throws -> FleetPairingSnapshot {
        var object: [String: Any] = [
            "schema_version": 1,
            "available": true,
            "state": state,
            "legacy_credentials_present": legacy,
        ]
        if let configured {
            object["credentials_configured"] = configured
        }
        let data = try JSONSerialization.data(withJSONObject: object)
        return try JSONDecoder().decode(FleetPairingSnapshot.self, from: data)
    }

    #expect(try decode("pending").ownsFleetCredentials)
    #expect(try decode("paired", configured: true).ownsFleetCredentials)
    #expect(try decode("recovery_required").ownsFleetCredentials)
    #expect(try decode("revoked", configured: true).ownsFleetCredentials)
    #expect(try decode("unpaired", configured: true).ownsFleetCredentials)
    #expect(!(try decode("unpaired", configured: false)).ownsFleetCredentials)
    #expect(!(try decode("unpaired", configured: true, legacy: true)).ownsFleetCredentials)
}

@Test("Engine benchmarks use a bounded fixed suite request")
func benchmarkRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.benchmarkRequest(alias: "qwen-3.5", sampleRuns: 7)

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/benchmarks/qwen-3.5"
    )
    #expect(request.httpMethod == "POST")
    #expect(request.timeoutInterval == 3_600)
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Basic YWRtaW46c2VjcmV0")
    let body = try #require(request.httpBody)
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(object["warmup_runs"] as? Int == 1)
    #expect(object["sample_runs"] as? Int == 7)
    #expect(object["max_tokens"] as? Int == 128)
}

@Test("Context profiling uses the explicit long-running manager route")
func contextProfileRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.contextProfileRequest(
        alias: "qwen-3.5",
        targetTokens: 262_144
    )

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/contexts/qwen-3.5/profile"
    )
    #expect(request.httpMethod == "POST")
    #expect(request.timeoutInterval == 3_600)
    let body = try #require(request.httpBody)
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(object["target_tokens"] as? Int == 262_144)
}

@Test("Model self-test requests use the public-path verifier with bounded options")
func selfTestRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.selfTestRequest(
        model: "vision-model",
        includeVision: true,
        unloadAfter: false
    )

    #expect(request.url?.absoluteString == "http://localhost:17321/manager/self-test")
    #expect(request.httpMethod == "POST")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Basic YWRtaW46c2VjcmV0")
    let body = try #require(request.httpBody)
    let payload = try JSONDecoder.nativeSettingsDecoder().decode(
        ModelSelfTestRequest.self,
        from: body
    )
    #expect(payload.model == "vision-model")
    #expect(payload.includeVision)
    #expect(!payload.unloadAfter)
}

@Test("Structured configuration saves use the control endpoint and snake-case JSON")
func configurationSaveRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    var settings = NativeSettings()
    settings.server.inferencePort = 17_330
    settings.server.maxConcurrency = 3
    settings.storage = ModelStorageSettings(
        default: "athena-models",
        locations: [
            StorageLocationSettings(
                name: "athena-models",
                path: "/Volumes/Athena/models",
                volumeUuid: "ATHENA-UUID",
                scopeId: String(repeating: "a", count: 64)
            ),
        ]
    )
    settings.models = [
        ModelProfileSettings(
            alias: "local-model",
            engine: .llamaCpp,
            model: "/Volumes/Athena/models/model.gguf",
            load: ModelLoadSettings(
                contextLength: 32_768,
                projectorPath: "/Volumes/Athena/models/mmproj.gguf",
                gpuLayers: 99,
                ubatchSize: 512
            ),
            context: ModelContextSettings(
                mode: "fixed",
                nativeTokens: 131_072,
                fixedTokens: 65_536
            )
        )
    ]
    let revision = String(repeating: "f", count: 64)
    let request = try client.configurationSaveRequest(
        settings: settings,
        revision: revision
    )

    #expect(request.url?.absoluteString == "http://localhost:17321/manager/config")
    #expect(request.httpMethod == "PUT")
    #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Basic YWRtaW46c2VjcmV0")
    let body = try #require(request.httpBody)
    let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    #expect(object["revision"] as? String == revision)
    let config = try #require(object["config"] as? [String: Any])
    let server = try #require(config["server"] as? [String: Any])
    let models = try #require(config["models"] as? [[String: Any]])
    let storage = try #require(config["storage"] as? [String: Any])
    let locations = try #require(storage["locations"] as? [[String: Any]])
    let load = try #require(models.first?["load"] as? [String: Any])
    let context = try #require(models.first?["context"] as? [String: Any])
    #expect(server["inference_port"] as? Int == 17_330)
    #expect(config["schema_version"] as? Int == 6)
    #expect(server["max_concurrency"] as? Int == 3)
    #expect(server["max_queue_depth"] as? Int == 128)
    #expect(server["fleet_api_key_env"] as? String == "FLEET_API_KEY")
    #expect(
        server["fleet_inference_api_key_env"] as? String
            == "FLEET_INFERENCE_API_KEY"
    )
    #expect(load["context_length"] as? Int == 32_768)
    #expect(load["projector_path"] as? String == "/Volumes/Athena/models/mmproj.gguf")
    #expect(load["gpu_layers"] as? Int == 99)
    #expect(load["ubatch_size"] as? Int == 512)
    #expect(context["mode"] as? String == "fixed")
    #expect(context["native_tokens"] as? Int == 131_072)
    #expect(context["fixed_tokens"] as? Int == 65_536)
    #expect(models.first?["engine"] as? String == "llama.cpp")
    #expect(locations.first?["path"] as? String == "/Volumes/Athena/models")
    #expect(locations.first?["volume_uuid"] as? String == "ATHENA-UUID")
    #expect(locations.first?["scope_id"] as? String == String(repeating: "a", count: 64))
}

@Test("Legacy configuration payloads default to schema one")
func legacyConfigurationSchemaDecoding() throws {
    let settings = NativeSettings()
    var object = try #require(
        JSONSerialization.jsonObject(
            with: JSONEncoder.nativeSettingsEncoder().encode(settings)
        ) as? [String: Any]
    )
    object.removeValue(forKey: "schema_version")
    let data = try JSONSerialization.data(withJSONObject: object)

    let decoded = try JSONDecoder.nativeSettingsDecoder()
        .decode(NativeSettings.self, from: data)

    #expect(decoded.schemaVersion == NativeSettings.supportedSchemaVersion)
}

@Test("Configuration snapshots preserve pending restart state")
func configurationSnapshotRestartStateDecoding() throws {
    let settings = NativeSettings()
    let persistedRevision = String(repeating: "a", count: 64)
    let appliedRevision = String(repeating: "b", count: 64)
    let config = try #require(
        JSONSerialization.jsonObject(
            with: JSONEncoder.nativeSettingsEncoder().encode(settings)
        ) as? [String: Any]
    )
    let data = try JSONSerialization.data(
        withJSONObject: [
            "config": config,
            "revision": persistedRevision,
            "applied_revision": appliedRevision,
            "restart_required": true,
        ]
    )

    let snapshot = try JSONDecoder.nativeSettingsDecoder()
        .decode(ConfigurationSnapshot.self, from: data)

    #expect(snapshot.revision == persistedRevision)
    #expect(snapshot.appliedRevision == appliedRevision)
    #expect(snapshot.restartRequired)
}

@Test("Legacy configuration snapshots default to fully applied")
func legacyConfigurationSnapshotRestartStateDecoding() throws {
    let settings = NativeSettings()
    let revision = String(repeating: "c", count: 64)
    let config = try #require(
        JSONSerialization.jsonObject(
            with: JSONEncoder.nativeSettingsEncoder().encode(settings)
        ) as? [String: Any]
    )
    let data = try JSONSerialization.data(
        withJSONObject: [
            "config": config,
            "revision": revision,
        ]
    )

    let snapshot = try JSONDecoder.nativeSettingsDecoder()
        .decode(ConfigurationSnapshot.self, from: data)

    #expect(snapshot.revision == revision)
    #expect(snapshot.appliedRevision == revision)
    #expect(!snapshot.restartRequired)
}

@Test("A newer configuration schema cannot be overwritten by an older app")
func futureConfigurationSchemaSaveRefused() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!
    )
    let settings = NativeSettings(schemaVersion: 7)

    #expect(throws: ControlAPIError.unsupportedConfigurationSchema(7)) {
        _ = try client.configurationSaveRequest(
            settings: settings,
            revision: String(repeating: "f", count: 64)
        )
    }
}

@Test("Pinned engine selection round-trips through schema six")
func pinnedEngineSelectionRoundTrip() throws {
    let value = ModelSelectionSettings(
        mode: "pinned",
        pinnedEngine: .mlxcel,
        objective: "latency",
        minimumImprovementPercent: 12
    )

    let data = try JSONEncoder.nativeSettingsEncoder().encode(value)
    let object = try #require(
        JSONSerialization.jsonObject(with: data) as? [String: Any]
    )
    #expect(object["mode"] as? String == "pinned")
    #expect(object["pinned_engine"] as? String == "mlxcel")
    #expect(object["minimum_improvement_percent"] as? Double == 12)
    #expect(
        try JSONDecoder.nativeSettingsDecoder().decode(
            ModelSelectionSettings.self,
            from: data
        ) == value
    )
}

@Test("Product version and build have a compact sidebar label")
func productBuildIdentityFormatting() {
    let release = ProductBuildIdentity(version: " 0.9.0 ", build: "50")
    #expect(release.compactLabel == "0.9.0 (50)")
    #expect(release.accessibilityLabel == "Version 0.9.0, build 50")
    #expect(
        ProductBuildIdentity(version: nil, build: nil).compactLabel
            == "development"
    )
}

@Test("Structured configuration decodes supported engines and image settings")
func configurationDecoding() throws {
    let payload = #"""
    {
      "schema_version": 3,
      "server": {"inference_bind":"127.0.0.1","inference_port":1240,"control_bind":"127.0.0.1","control_port":17321,"idle_unload_seconds":900,"startup_timeout_seconds":900,"swap_queue_timeout_seconds":300,"max_concurrency":null,"max_queue_depth":128,"shutdown_grace_seconds":30,"reconcile_interval_seconds":30,"image_request_timeout_seconds":1800,"image_max_pixels":4194304,"startup_policy":"unload_all","inference_api_key_env":"INFERENCE_API_KEY","fleet_api_key_env":"FLEET_API_KEY","control_password_env":"ADMIN_PASSWORD"},
      "engines": {
        "llama_cpp":{"enabled":true,"host":"127.0.0.1","port":17325,"binary":"/runtime/llama-server","working_directory":"/runtime","process_state_path":"/state/llama.json","request_timeout_seconds":30,"shutdown_grace_seconds":30},
        "omlx":{"enabled":true,"base_url":"http://127.0.0.1:17322","api_key_env":"OMLX_API_KEY","admin_session_env":"OMLX_ADMIN_SESSION","request_timeout_seconds":30,"model_directories":["/Volumes/Athena/models"]},
        "ds4":{"enabled":true,"host":"127.0.0.1","port":17323,"binary":"/ds4","working_directory":"/","process_state_path":"/state","request_timeout_seconds":30,"shutdown_grace_seconds":30},
        "mflux":{"enabled":true,"host":"127.0.0.1","port":17324,"python":null,"python_env":"MFLUX_PYTHON","source_path_env":"MFLUX_PATH","request_timeout_seconds":30,"shutdown_grace_seconds":30}
      },
      "paths":{"state_database":"/state.db","log_directory":"/logs"},
      "storage":{"default":"athena-models","locations":[{"name":"athena-models","path":"/Volumes/Athena/models","volume_uuid":"ATHENA-UUID"}]},
      "models":[{"alias":"qwen-image","engine":"mflux","model":"Qwen/Qwen-Image","served_model_name":null,"capabilities":["images/generations"],"load":{"context_length":null,"eval_batch_size":null,"flash_attention":null,"offload_kv_cache_to_gpu":null,"kv_disk_directory":null,"kv_disk_space_mb":null,"extra_args":[]},"kind":"image","image":{"family":"qwen-image","quantize":8,"width":1024,"height":1024,"num_inference_steps":30,"guidance_scale":4},"enabled":true}],
      "migration":{"legacy_lmstudio_profiles":[]},
      "token_sidecar":{"enabled":false,"node_id":"","flush_interval_seconds":30,"batch_size":500,"max_outbox_rows":100000,"connect_timeout_seconds":5}
    }
    """#.data(using: .utf8)!

    let settings = try JSONDecoder.nativeSettingsDecoder().decode(NativeSettings.self, from: payload)

    #expect(settings.engines.mflux.port == 17_324)
    #expect(settings.engines.llamaCpp.port == 17_325)
    #expect(settings.engines.omlx.modelDirectories == ["/Volumes/Athena/models"])
    #expect(settings.storage.locations.first?.path == "/Volumes/Athena/models")
    #expect(settings.models.first?.image?.family == .qwenImage)
    #expect(settings.models.first?.image?.numInferenceSteps == 30)
    #expect(settings.tokenSidecar.maxOutboxRows == 100_000)
    #expect(settings.server.maxConcurrency == nil)
    #expect(settings.server.maxQueueDepth == 128)
    #expect(settings.server.fleetApiKeyEnv == "FLEET_API_KEY")
}

@Test("Retired engine settings decode for upgrades but remain disabled")
func retiredEngineSettingsRemainInert() throws {
    let baseline = try JSONEncoder.nativeSettingsEncoder().encode(EngineSettings())
    var legacyObject = try #require(
        JSONSerialization.jsonObject(with: baseline) as? [String: Any]
    )
    var mlxcel = try #require(
        JSONSerialization.jsonObject(
            with: JSONEncoder.nativeSettingsEncoder().encode(MLXcelSettings())
        ) as? [String: Any]
    )
    var mistralRs = try #require(
        JSONSerialization.jsonObject(
            with: JSONEncoder.nativeSettingsEncoder().encode(MistralRSSettings())
        ) as? [String: Any]
    )
    mlxcel["enabled"] = true
    mistralRs["enabled"] = true
    legacyObject["mlxcel"] = mlxcel
    legacyObject["mistral_rs"] = mistralRs
    let payload = try JSONSerialization.data(withJSONObject: legacyObject)

    let engines = try JSONDecoder.nativeSettingsDecoder()
        .decode(EngineSettings.self, from: payload)

    #expect(!engines.mlxcel.enabled)
    #expect(!engines.mistralRs.enabled)
    #expect(engines.mlxcel.port == 17_326)
    #expect(engines.mistralRs.port == 17_327)
    #expect(InferenceEngine.supportedCases == [.llamaCpp, .omlx, .ds4, .mflux])

    let encoded = try JSONEncoder.nativeSettingsEncoder().encode(engines)
    let object = try #require(
        JSONSerialization.jsonObject(with: encoded) as? [String: Any]
    )
    #expect(object["mlxcel"] == nil)
    #expect(object["mistral_rs"] == nil)
}

@Test("DS4 resident session capacity reuses the typed parallel setting")
func ds4ParallelSessionsRoundTrip() throws {
    let load = ModelLoadSettings(
        contextLength: 32_768,
        parallel: 3,
        kvDiskSpaceMb: 8_192
    )
    let encoded = try JSONEncoder.nativeSettingsEncoder().encode(load)
    let object = try #require(
        JSONSerialization.jsonObject(with: encoded) as? [String: Any]
    )

    #expect(object["parallel"] as? Int == 3)
    let decoded = try JSONDecoder.nativeSettingsDecoder().decode(
        ModelLoadSettings.self,
        from: encoded
    )
    #expect(decoded.parallel == 3)
    #expect(decoded.kvDiskSpaceMb == 8_192)
}

@Test("Local model scans preserve explicit projector and migration metadata")
func localModelScanDecoding() throws {
    let payload = #"""
    {
      "schema_version":1,
      "root":"/Volumes/Athena/models",
      "mount_path":"/Volumes/Athena",
      "volume_uuid":"ATHENA-UUID",
      "models":[{
        "id":"candidate-1",
        "source_key":"bartowski/Qwen-GGUF",
        "engine":"llama.cpp",
        "display_name":"Qwen-GGUF",
        "model_path":"/Volumes/Athena/models/Qwen/model-Q4_K_M.gguf",
        "all_paths":["/Volumes/Athena/models/Qwen/model-Q4_K_M.gguf"],
        "shard_count":1,
        "quantization":"Q4_K_M",
        "size_bytes":4294967296,
        "compatibility":"structural",
        "compatibility_reason":"Valid GGUF header.",
        "capabilities":["chat/completions","responses"],
        "architecture":"qwen2vl",
        "context_length":131072,
        "parameter_count":7615000000,
        "summary":"A local vision model.",
        "model_card_markdown":"# Qwen\n\nA local vision model.",
        "recommended_projector_id":"projector-1",
        "projector_options":[{
          "id":"projector-1",
          "path":"/Volumes/Athena/models/Qwen/mmproj-F16.gguf",
          "filename":"mmproj-F16.gguf",
          "size_bytes":1048576
        }],
        "existing_alias":"qwen",
        "already_imported":false
      }]
    }
    """#.data(using: .utf8)!

    let scan = try JSONDecoder.nativeSettingsDecoder()
        .decode(LocalModelScanSnapshot.self, from: payload)

    #expect(scan.root == "/Volumes/Athena/models")
    #expect(scan.models.first?.engine == .llamaCpp)
    #expect(scan.models.first?.projectorOptions.first?.id == "projector-1")
    #expect(scan.models.first?.recommendedProjectorId == "projector-1")
    #expect(scan.models.first?.contextLength == 131_072)
    #expect(scan.models.first?.existingAlias == "qwen")
    #expect(scan.models.first?.isImportable == true)
}

@Test("Local model scan and import requests use explicit selected rows")
func localModelRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let bookmark = Data("finder-bookmark".utf8)
    let scan = try client.localModelScanRequest(
        path: "/Volumes/Athena/models",
        bookmarkData: bookmark
    )
    #expect(
        scan.url?.absoluteString
            == "http://localhost:17321/manager/model-library/local-scan"
    )
    #expect(scan.httpMethod == "POST")
    let scanBody = try #require(scan.httpBody)
    let scanObject = try #require(
        JSONSerialization.jsonObject(with: scanBody) as? [String: Any]
    )
    #expect(scanObject["bookmark_data"] as? String == bookmark.base64EncodedString())

    let importPayload = LocalModelImportRequest(
        path: "/Volumes/Athena/models",
        scopeId: String(repeating: "b", count: 64),
        selections: [
            LocalModelImportSelection(
                candidateId: "candidate-1",
                alias: "qwen-local",
                projectorId: "projector-1"
            ),
        ]
    )
    let request = try client.localModelImportRequest(importPayload)
    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/model-library/imports"
    )
    let body = try #require(request.httpBody)
    #expect(
        try JSONDecoder.nativeSettingsDecoder()
            .decode(LocalModelImportRequest.self, from: body) == importPayload
    )
}

@Test("Detected local-model sources decode and use the read-only source route")
func localModelSourceDiscovery() throws {
    let payload = """
    {
      "schema_version": 1,
      "sources": [{
        "id": "lmstudio-downloads",
        "display_name": "LM Studio model folder",
        "path": "/Volumes/Athena/nested/models",
        "source": "lmstudio-settings"
      }]
    }
    """.data(using: .utf8)!
    let snapshot = try JSONDecoder.nativeSettingsDecoder()
        .decode(LocalModelSourcesSnapshot.self, from: payload)

    #expect(snapshot.sources.count == 1)
    #expect(snapshot.sources[0].path == "/Volumes/Athena/nested/models")
    #expect(snapshot.sources[0].source == "lmstudio-settings")

    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = client.localModelSourcesRequest()
    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/model-library/local-sources"
    )
    #expect(request.httpMethod == "GET")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Basic YWRtaW46c2VjcmV0")
}

@Test("llama.cpp repositories expose an exact GGUF quant and projector choice")
func llamaCppFileSelectionDecoding() throws {
    let payload = #"""
    {
      "repo_id":"org/model-GGUF",
      "engine":"llama.cpp",
      "display_name":"model-Q4_K_M.gguf",
      "model_kind":"language",
      "compatibility":"supported",
      "compatibility_reason":"Published GGUF.",
      "downloads":10,
      "likes":2,
      "size_bytes":4294967296,
      "quantization":"Q4_K_M",
      "filename":"model-Q4_K_M.gguf",
      "projector_filename":"mmproj-F16.gguf",
      "projector_options":["mmproj-F16.gguf","mmproj-Q5_K_M.gguf"],
      "download_files":["model-Q4_K_M.gguf"],
      "resolved_revision":"abc123",
      "requires_file_selection":false,
      "family":null,
      "recommended_memory_gb":null,
      "installable":true,
      "suggested_role":"generation",
      "default_quantize":null,
      "default_width":null,
      "default_height":null,
      "default_num_inference_steps":null,
      "default_guidance_scale":null,
      "architecture":"qwen2vl",
      "context_length":131072,
      "parameter_count":7615000000
    }
    """#.data(using: .utf8)!

    let model = try JSONDecoder.nativeSettingsDecoder().decode(LibraryModel.self, from: payload)
    #expect(model.engine == .llamaCpp)
    #expect(model.filename == "model-Q4_K_M.gguf")
    #expect(model.availableProjectors == ["mmproj-F16.gguf", "mmproj-Q5_K_M.gguf"])
    #expect(model.projectorFilename == "mmproj-F16.gguf")
    #expect(model.contextLength == 131_072)
    #expect(model.suggestedRole == .generation)

    let install = StartModelInstallRequest(
        model: model,
        storage: "athena-models",
        projectorFilename: "mmproj-F16.gguf",
        role: .generation
    )
    #expect(install.projectorFilename == "mmproj-F16.gguf")
    #expect(install.includeProjector)
    #expect(install.revision == "abc123")
    #expect(
        install.capabilities
            == ModelRole.generation.capabilities(for: .llamaCpp)
    )

    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.startModelInstallRequest(install)
    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/model-library/installs"
    )
    #expect(request.httpMethod == "POST")
    let body = try #require(request.httpBody)
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(object["storage"] as? String == "athena-models")
    #expect(object["repo_id"] as? String == "org/model-GGUF")
    #expect(object["filename"] as? String == "model-Q4_K_M.gguf")
}

@Test("GGUF file discovery preserves the llama.cpp engine value in the query")
func llamaCppFileSelectionRequest() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = client.libraryFilesRequest(
        repoId: "org/model-GGUF",
        engine: .llamaCpp,
        revision: "abc123"
    )
    let url = try #require(request.url)
    let components = try #require(
        URLComponents(url: url, resolvingAgainstBaseURL: false)
    )
    let query = Dictionary(
        uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name, $0.value) }
    )
    #expect(query["engine"] == "llama.cpp")
    #expect(query["repo_id"] == "org/model-GGUF")
    #expect(query["revision"] == "abc123")
}

@Test("Start-install URLRequest preserves the exact selected storage key")
func modelInstallRequestPreservesSelectedStorage() throws {
    let payload = #"""
    {
      "repo_id":"org/storage-proof-GGUF",
      "engine":"llama.cpp",
      "display_name":"storage-proof-Q4_K_M.gguf",
      "model_kind":"language",
      "compatibility":"supported",
      "compatibility_reason":"Published GGUF.",
      "filename":"storage-proof-Q4_K_M.gguf",
      "resolved_revision":"abc123",
      "installable":true,
      "suggested_role":"generation"
    }
    """#.data(using: .utf8)!
    let model = try JSONDecoder.nativeSettingsDecoder().decode(
        LibraryModel.self,
        from: payload
    )
    let install = StartModelInstallRequest(
        model: model,
        storage: "external-volume-nested-library",
        role: .generation
    )
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )

    let request = try client.startModelInstallRequest(install)
    let body = try #require(request.httpBody)
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )

    #expect(request.httpMethod == "POST")
    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/model-library/installs"
    )
    #expect(
        object["storage"] as? String
            == "external-volume-nested-library"
    )
    #expect(object.keys.contains("storage"))
}

@Test("Model library search requests one unified catalog without an engine filter")
func unifiedModelLibrarySearchRequest() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = client.librarySearchRequest(query: "qwen vision")
    let url = try #require(request.url)
    let components = try #require(
        URLComponents(url: url, resolvingAgainstBaseURL: false)
    )
    let query = Dictionary(
        uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name, $0.value) }
    )

    #expect(url.path == "/manager/model-library/search")
    #expect(query["q"] == "qwen vision")
    #expect(query["engine"] == nil)
    #expect(request.httpMethod == "GET")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Basic YWRtaW46c2VjcmV0")
}

@Test("Model library records preserve engine compatibility and nested destinations")
func modelLibraryDecoding() throws {
    let payload = #"""
    {
      "id":"install-1",
      "repo_id":"antirez/deepseek-v4-gguf",
      "engine":"ds4",
      "storage":"athena-models",
      "alias":"deepseek-v4-flash",
      "destination":"/Volumes/Athena/models/ds4/antirez/deepseek-v4-gguf",
      "status":"downloading",
      "revision":null,
      "filename":"DeepSeek-V4-Flash.gguf",
      "capabilities":["chat/completions","completions","responses","messages"],
      "family":null,
      "bytes_downloaded":1048576,
      "total_bytes":4194304,
      "download_speed_bps":524288.5,
      "error":null,
      "pid":123,
      "created_at":1,
      "updated_at":2
    }
    """#.data(using: .utf8)!

    let install = try JSONDecoder.nativeSettingsDecoder().decode(ModelInstall.self, from: payload)
    #expect(install.engine == .ds4)
    #expect(install.isActive)
    #expect(install.destination.hasPrefix("/Volumes/Athena/models/"))
    #expect(install.capabilities == ModelRole.generation.capabilities)
    #expect(install.progressFraction == 0.25)
    #expect(install.downloadSpeedBps == 524_288.5)
}

@Test("Download history removal and managed deletion use explicit DELETE requests")
func modelDeletionRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )

    let dismissed = client.dismissModelInstallRequest(id: "install-1")
    #expect(
        dismissed.url?.absoluteString
            == "http://localhost:17321/manager/model-library/installs/install-1"
    )
    #expect(dismissed.httpMethod == "DELETE")

    let revision = String(repeating: "a", count: 64)
    let deleted = try client.deleteManagedModelRequest(
        alias: "qwen-model",
        revision: revision
    )
    #expect(
        deleted.url?.absoluteString
            == "http://localhost:17321/manager/models/qwen-model"
    )
    #expect(deleted.httpMethod == "DELETE")
    #expect(deleted.value(forHTTPHeaderField: "Content-Type") == "application/json")
    let body = try #require(deleted.httpBody)
    #expect(
        try JSONDecoder.nativeSettingsDecoder()
            .decode(DeleteManagedModelRequest.self, from: body)
            == DeleteManagedModelRequest(revision: revision)
    )
    let legacyObject = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(legacyObject.count == 1)
    #expect(legacyObject["revision"] as? String == revision)
    #expect(legacyObject["installation_id"] == nil)

    let installationID = "11111111-1111-4111-8111-111111111111"
    let exact = try client.deleteManagedModelRequest(
        alias: "qwen-model",
        revision: revision,
        installationID: installationID
    )
    let exactBody = try #require(exact.httpBody)
    let exactObject = try #require(
        JSONSerialization.jsonObject(with: exactBody) as? [String: Any]
    )
    #expect(exactObject.count == 2)
    #expect(exactObject["revision"] as? String == revision)
    #expect(exactObject["installation_id"] as? String == installationID)
}

@Test("Cleanup history uses the hidden-inclusive evidence endpoint")
func modelInstallHistoryRequestAndDecoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = client.modelInstallHistoryRequest()

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/model-library/install-evidence?limit=500"
    )
    #expect(request.httpMethod == "GET")
    #expect(
        request.value(forHTTPHeaderField: "Authorization")
            == "Basic YWRtaW46c2VjcmV0"
    )

    let payload = #"""
    {
      "schema_version":1,
      "installs":[{
        "id":"11111111-1111-4111-8111-111111111111",
        "repo_id":"owner/model",
        "engine":"llama.cpp",
        "storage":"internal",
        "alias":"model",
        "destination":"/models/llama.cpp/owner/model",
        "status":"installed",
        "revision":"abc123",
        "filename":"model.gguf",
        "projector_filename":null,
        "context_length":null,
        "download_files":["model.gguf"],
        "capabilities":["generation"],
        "family":null,
        "bytes_downloaded":1,
        "total_bytes":1,
        "download_speed_bps":null,
        "error":null,
        "pid":null,
        "created_at":1,
        "updated_at":2,
        "dismissed":true,
        "events":[]
      }]
    }
    """#.data(using: .utf8)!
    let snapshot = try JSONDecoder.nativeSettingsDecoder().decode(
        ModelInstallEvidenceSnapshot.self,
        from: payload
    )

    #expect(snapshot.schemaVersion == 1)
    #expect(snapshot.installs.count == 1)
    #expect(snapshot.installs[0].id == "11111111-1111-4111-8111-111111111111")
    #expect(snapshot.installs[0].status == "installed")
}

@Test("Downloaded weights can retry registration without appearing active")
func downloadedInstallState() throws {
    let payload = #"""
    {
      "id":"install-2",
      "repo_id":"owner/model",
      "engine":"llama.cpp",
      "storage":"internal",
      "alias":"model",
      "destination":"/models/owner/model",
      "status":"downloaded",
      "revision":"abc123",
      "filename":"model-Q4_K_M.gguf",
      "family":null,
      "bytes_downloaded":1048576,
      "total_bytes":1048576,
      "error":"download completed but profile registration failed: config unavailable",
      "pid":null,
      "created_at":1,
      "updated_at":2
    }
    """#.data(using: .utf8)!

    let install = try JSONDecoder.nativeSettingsDecoder().decode(ModelInstall.self, from: payload)
    #expect(!install.isActive)
    #expect(install.canRetry)
}

@Test("Profile registration remains an active install phase")
func registeringInstallState() throws {
    let payload = #"""
    {
      "id":"install-3",
      "repo_id":"owner/model",
      "engine":"llama.cpp",
      "storage":"internal",
      "alias":"model",
      "destination":"/models/owner/model",
      "status":"registering",
      "revision":"abc123",
      "filename":"model-Q4_K_M.gguf",
      "family":null,
      "bytes_downloaded":1048576,
      "total_bytes":1048576,
      "error":null,
      "pid":null,
      "created_at":1,
      "updated_at":2
    }
    """#.data(using: .utf8)!

    let install = try JSONDecoder.nativeSettingsDecoder().decode(ModelInstall.self, from: payload)
    #expect(install.isActive)
    #expect(!install.canRetry)
}

@Test("MFLUX library records preserve support and generation defaults")
func mfluxLibraryModelDecoding() throws {
    let payload = #"""
    {
      "repo_id":"krea/Krea-2-Raw",
      "engine":"mflux",
      "display_name":"Krea 2 Raw",
      "model_kind":"image",
      "compatibility":"unavailable",
      "compatibility_reason":"The pinned loader only supports Turbo.",
      "downloads":null,
      "likes":null,
      "size_bytes":null,
      "quantization":null,
      "filename":null,
      "family":null,
      "recommended_memory_gb":null,
      "installable":false,
      "default_quantize":null,
      "default_width":1024,
      "default_height":1024,
      "default_num_inference_steps":52,
      "default_guidance_scale":3.5
    }
    """#.data(using: .utf8)!

    let model = try JSONDecoder.nativeSettingsDecoder().decode(LibraryModel.self, from: payload)
    #expect(model.engine == .mflux)
    #expect(!model.isInstallable)
    #expect(model.defaultNumInferenceSteps == 52)
    #expect(model.defaultGuidanceScale == 3.5)
}

@Test("Runtime update snapshots preserve external ownership and managed actions")
func runtimeUpdateDecoding() throws {
    let payload = #"""
    {
      "channel":"stable",
      "manifest_url":null,
      "checked_at":1784559600,
      "core_protocol":1,
      "engines":[
        {
          "engine":"omlx",
          "release_tier":"stable",
          "display_name":"oMLX",
          "ownership":"external",
          "installed":true,
          "installed_version":"0.3.12",
          "installed_revision":null,
          "installed_path":"/Applications/oMLX.app",
          "latest_upstream_version":"0.3.13",
          "latest_upstream_revision":null,
          "latest_upstream_url":"https://github.com/jundot/omlx/releases/tag/v0.3.13",
          "official_installer_url":"https://github.com/jundot/omlx/releases/download/v0.3.13/oMLX-0.3.13-macos15-sequoia.dmg",
          "available_version":null,
          "available_revision":null,
          "release_notes_url":"https://github.com/jundot/omlx/releases/tag/v0.3.13",
          "update_available":true,
          "can_install":false,
          "can_rollback":false,
          "management_note":"Updated by oMLX.",
          "diagnostic":null
        },
        {
          "engine":"mflux",
          "release_tier":"preview",
          "display_name":"MFLUX",
          "ownership":"managed_or_external",
          "installed":true,
          "installed_version":"0.18.0",
          "installed_revision":"97ac5e6",
          "installed_path":"/runtime/mflux",
          "latest_upstream_version":"0.19.0",
          "latest_upstream_revision":null,
          "latest_upstream_url":"https://github.com/filipstrand/mflux/releases",
          "available_version":"0.19.0-ui1",
          "available_revision":"abc123",
          "release_notes_url":"https://example.test/mflux",
          "update_available":true,
          "can_install":true,
          "can_rollback":true,
          "management_note":"Tested packs.",
          "diagnostic":null
        }
      ]
    }
    """#.data(using: .utf8)!

    let snapshot = try JSONDecoder.nativeSettingsDecoder()
        .decode(RuntimeUpdateSnapshot.self, from: payload)

    #expect(snapshot.coreProtocol == 1)
    #expect(snapshot.engines[0].engine == .omlx)
    #expect(snapshot.engines[0].releaseTierLabel == "STABLE")
    #expect(!snapshot.engines[0].canInstall)
    #expect(snapshot.engines[0].officialInstallerUrl?.hasSuffix(".dmg") == true)
    #expect(snapshot.engines[1].engine == .mflux)
    #expect(snapshot.engines[1].releaseTierLabel == "PREVIEW")
    #expect(snapshot.engines[1].availableLabel == "0.19.0-ui1")
    #expect(snapshot.engines[1].canRollback)
}

@Test("DS4 runtime snapshots preserve the exact GLM 5.3 experimental channel")
func ds4GLM53RuntimeChannelDecoding() throws {
    let payload = #"""
    {
      "channel":"official",
      "manifest_url":null,
      "checked_at":1784559600,
      "core_protocol":1,
      "engines":[{
        "engine":"ds4",
        "release_tier":"preview",
        "display_name":"DS4",
        "ownership":"managed_or_external",
        "installed":true,
        "installed_version":"main-123",
        "installed_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "installed_path":"/runtime/ds4",
        "installed_channel":"official",
        "installation_kind":"managed_or_configured",
        "upgrade_strategy":"managed",
        "latest_upstream_version":"main-124",
        "latest_upstream_revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "latest_upstream_url":"https://github.com/antirez/ds4",
        "official_installer_url":null,
        "available_version":"main-124",
        "available_revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "release_notes_url":"https://github.com/antirez/ds4",
        "update_available":true,
        "can_install":true,
        "can_rollback":false,
        "management_note":"Official source.",
        "diagnostic":null,
        "managed_channels":[{
          "channel":"glm-5.3-flash",
          "source_branch":"glm-5.3-flash",
          "release_tier":"experimental",
          "available_version":"cccccccccccc",
          "available_revision":"cccccccccccccccccccccccccccccccccccccccc",
          "release_notes_url":"https://github.com/antirez/ds4/commit/cccccccccccccccccccccccccccccccccccccccc",
          "update_available":true,
          "can_install":true,
          "diagnostic":null
        }]
      }]
    }
    """#.data(using: .utf8)!

    let snapshot = try JSONDecoder.nativeSettingsDecoder()
        .decode(RuntimeUpdateSnapshot.self, from: payload)
    let ds4 = try #require(snapshot.engines.first)
    let preview = try #require(ds4.ds4GLM53FlashPreview)

    #expect(ds4.installedChannel == "official")
    #expect(preview.channel == "glm-5.3-flash")
    #expect(preview.sourceBranch == "glm-5.3-flash")
    #expect(preview.releaseTierLabel == "EXPERIMENTAL")
    #expect(preview.availableVersion == "cccccccccccc")
    #expect(preview.canInstall)
}

@Test("Runtime install requests target the engine and requested official version")
func runtimeInstallRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.runtimeInstallRequest(
        engine: .mflux,
        version: "0.19.0-ui1"
    )

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/runtime-updates/mflux/install"
    )
    #expect(request.httpMethod == "POST")
    let body = try #require(request.httpBody)
    let payload = try JSONDecoder.nativeSettingsDecoder()
        .decode(InstallRuntimeUpdateRequest.self, from: body)
    #expect(payload.version == "0.19.0-ui1")
    #expect(payload.channel == nil)
}

@Test("GLM 5.3 runtime install requests select only the exact DS4 preview channel")
func ds4GLM53RuntimeInstallRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.runtimeInstallRequest(
        engine: .ds4,
        version: "cccccccccccc",
        channel: ManagedRuntimeChannel.ds4GLM53FlashChannel
    )

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/runtime-updates/ds4/install"
    )
    let body = try #require(request.httpBody)
    let object = try #require(
        JSONSerialization.jsonObject(with: body) as? [String: Any]
    )
    #expect(object.count == 2)
    #expect(object["version"] as? String == "cccccccccccc")
    #expect(object["channel"] as? String == "glm-5.3-flash")
}

@Test("GLM 5.3 search waits for the exact DS4 preview instead of showing generic matches")
func glm53SearchRuntimeGate() throws {
    let updatePayload = #"""
    {
      "engine":"ds4",
      "display_name":"DS4",
      "ownership":"managed_or_external",
      "installed":true,
      "update_available":false,
      "can_install":false,
      "can_rollback":false,
      "management_note":"Official source.",
      "managed_channels":[{
        "channel":"glm-5.3-flash",
        "source_branch":"glm-5.3-flash",
        "release_tier":"experimental",
        "available_version":"cccccccccccc",
        "available_revision":"cccccccccccccccccccccccccccccccccccccccc",
        "release_notes_url":"https://github.com/antirez/ds4/commit/cccccccccccccccccccccccccccccccccccccccc",
        "update_available":true,
        "can_install":true,
        "diagnostic":null
      }]
    }
    """#.data(using: .utf8)!
    let modelPayload = #"""
    [
      {
        "repo_id":"third-party/GLM-5.3-Flash-MLX",
        "engine":"omlx",
        "display_name":"GLM-5.3-Flash-MLX",
        "model_kind":"language",
        "compatibility":"likely",
        "compatibility_reason":"Generic Hub metadata only."
      },
      {
        "repo_id":"antirez/glm-5.2-gguf",
        "engine":"ds4",
        "display_name":"GLM 5.2",
        "model_kind":"language",
        "compatibility":"verified",
        "compatibility_reason":"Declared by main."
      }
    ]
    """#.data(using: .utf8)!
    let update = try JSONDecoder.nativeSettingsDecoder()
        .decode(EngineRuntimeUpdate.self, from: updatePayload)
    let models = try JSONDecoder.nativeSettingsDecoder()
        .decode([LibraryModel].self, from: modelPayload)

    let visible = GLM53PreviewPresentation.visibleModels(
        query: "GLM 5.3 Flash",
        models: models
    )

    #expect(visible.map(\.displayName) == ["GLM 5.2"])
    #expect(
        GLM53PreviewPresentation.shouldOfferRuntimeInstall(
            query: "glm-5.3-flash",
            models: visible,
            ds4Update: update
        )
    )
    #expect(!GLM53PreviewPresentation.queryTargetsPreview("GLM 5.2"))
    #expect(GLM53PreviewPresentation.q2MinimumMemoryGB == 128)
    #expect(GLM53PreviewPresentation.q4MinimumMemoryGB == 256)

    let exactPayload = #"""
    [{
      "repo_id":"antirez/glm-5.3-flash-gguf",
      "engine":"ds4",
      "display_name":"GLM 5.3 Flash — Q2 (Experimental Preview)",
      "model_kind":"language",
      "compatibility":"experimental",
      "compatibility_reason":"Exact source-bound preview.",
      "family":"glm-5.3-flash",
      "release_tier":"experimental",
      "recommended_memory_gb":128
    }]
    """#.data(using: .utf8)!
    let exact = try JSONDecoder.nativeSettingsDecoder()
        .decode([LibraryModel].self, from: exactPayload)
    #expect(
        GLM53PreviewPresentation.visibleModels(
            query: "GLM 5.3",
            models: exact
        ) == exact
    )
    #expect(
        !GLM53PreviewPresentation.shouldOfferRuntimeInstall(
            query: "GLM 5.3",
            models: exact,
            ds4Update: update
        )
    )
}

@Test("llama.cpp runtime updates retain the dotted official engine identifier")
func llamaCppRuntimeInstallRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.runtimeInstallRequest(
        engine: .llamaCpp,
        version: "b6000"
    )

    #expect(
        request.url?.absoluteString
            == "http://localhost:17321/manager/runtime-updates/llama.cpp/install"
    )
}

@Test("The status decoder ignores future fields")
func statusDecodeIsForwardCompatible() throws {
    let payload = #"""
    {
      "status": "running",
      "resident_alias": "glm-5-2",
      "resident_model": "glm-5-2",
      "resident_engine": "omlx",
      "in_flight_requests": 2,
      "diagnostic": "engine state needs attention",
      "startup_error": "oMLX authentication failed",
      "token_sidecar": {
        "enabled": true,
        "node_id": "theseus",
        "node_id_source": "token_sidecar",
        "outbox_depth": 4,
        "last_flush_at": 1784462400.5,
        "writer_ready": false,
        "last_error": "Postgres is unavailable",
        "future_field": "ignored"
      },
      "performance": {
        "window_limit": 512,
        "sample_count": 3,
        "oldest_observed_at": 1,
        "newest_observed_at": 2,
        "by_model": [{
          "alias": "glm-5-2",
          "engine": "omlx",
          "requests": 3,
          "errors": 0,
          "cold_starts": 1,
          "average_admission_ms": 12.5,
          "average_upstream_headers_ms": 15.0,
          "average_first_byte_ms": 42.0,
          "average_total_ms": 950.0,
          "average_output_tokens_per_second": 25.5,
          "p50_total_ms": 900.0,
          "p95_total_ms": 1200.0
        }],
        "recent": []
      },
      "another_future_field": true
    }
    """#.data(using: .utf8)!

    let snapshot = try JSONDecoder().decode(ServiceSnapshot.self, from: payload)
    #expect(snapshot.status == "running")
    #expect(snapshot.residentAlias == "glm-5-2")
    #expect(snapshot.residentModel == "glm-5-2")
    #expect(snapshot.residentEngine == "omlx")
    #expect(snapshot.inFlightRequests == 2)
    #expect(snapshot.tokenSidecar?.nodeId == "theseus")
    #expect(snapshot.tokenSidecar?.nodeIdSource == "token_sidecar")
    #expect(snapshot.tokenSidecar?.outboxDepth == 4)
    #expect(snapshot.tokenSidecar?.lastFlushAt == 1_784_462_400.5)
    #expect(snapshot.tokenSidecar?.writerReady == false)
    #expect(snapshot.tokenSidecar?.lastError == "Postgres is unavailable")
    #expect(snapshot.performance?.sampleCount == 3)
    #expect(snapshot.performance?.byModel.first?.p50TotalMs == 900)
    #expect(snapshot.performance?.byModel.first?.coldStarts == 1)
    #expect(snapshot.performance?.byModel.first?.averageOutputTokensPerSecond == 25.5)
    #expect(snapshot.diagnostic == "engine state needs attention")
    #expect(snapshot.startupError == "oMLX authentication failed")
}

@Test("Readiness and self-test payloads preserve actionable V1 health")
func readinessAndSelfTestDecoding() throws {
    let readinessPayload = #"""
    {
      "schema_version": 1,
      "product_version": "0.9.0",
      "core": {
        "ready": true,
        "state": "idle",
        "diagnostic": null,
        "startup_error": null,
        "resident_alias": null,
        "in_flight_requests": 0,
        "queued_requests": 0,
        "omlx_model_directory_sync_pending": false
      },
      "engines": [{
        "engine": "llama.cpp",
        "release_tier": "stable",
        "enabled": true,
        "installed": true,
        "installed_version": "b10107",
        "installed_path": "/runtimes/llama.cpp",
        "service_state": "ready",
        "authoritative": true,
        "resident_models": [],
        "ready": true,
        "diagnostic": null
      }, {
        "engine": "ds4",
        "release_tier": "preview",
        "enabled": false,
        "installed": true,
        "installed_version": "abc123",
        "installed_path": "/runtimes/ds4",
        "service_state": "disabled",
        "authoritative": true,
        "resident_models": [],
        "ready": false,
        "diagnostic": null
      }],
      "storage": [{
        "name": "internal",
        "path": "/Models",
        "available": true,
        "writable": true,
        "volume_matches": true,
        "free_bytes": 1234,
        "diagnostic": null
      }],
      "models": {"configured": 1, "callable": 1},
      "downloads": {"active": 0, "items": []},
      "usage": {
        "enabled": true,
        "node_id": "theseus",
        "node_id_source": "computer_name",
        "writer_ready": true,
        "outbox_pending": 0,
        "outbox_depth": 0,
        "last_flush_at": null,
        "last_flush_count": 0,
        "last_error": null
      },
      "ready_for_inference": true
    }
    """#.data(using: .utf8)!
    let readiness = try JSONDecoder.nativeSettingsDecoder().decode(
        ReadinessSnapshot.self,
        from: readinessPayload
    )
    #expect(readiness.readyForInference)
    #expect(readiness.productVersion == "0.9.0")
    #expect(readiness.engines[0].isStable)
    #expect(readiness.engines[1].releaseTier == "preview")
    #expect(readiness.storage[0].freeBytes == 1234)

    let selfTestPayload = #"""
    {
      "schema_version": 1,
      "success": true,
      "model": "alpaca",
      "engine": "llama.cpp",
      "release_tier": "stable",
      "endpoint": "/v1/chat/completions",
      "vision": false,
      "response_preview": "Alpacas are camelids.",
      "response_ms": 1500,
      "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19
      },
      "usage_recorded": true,
      "usage_delivery": {
        "enabled": true,
        "node_id": "theseus",
        "node_id_source": "computer_name",
        "writer_ready": true,
        "outbox_pending": 1,
        "outbox_depth": 1,
        "last_flush_at": null,
        "last_flush_count": 0,
        "last_error": null
      }
    }
    """#.data(using: .utf8)!
    let result = try JSONDecoder.nativeSettingsDecoder().decode(
        ModelSelfTestResult.self,
        from: selfTestPayload
    )
    #expect(result.success)
    #expect(result.engine == .llamaCpp)
    #expect(result.usage?.totalTokens == 19)
    #expect(result.usageRecorded == true)
    #expect(result.completesGuidedSetup)

    let imageOnlyPayload = #"""
    {
      "schema_version": 1,
      "success": true,
      "model": "preview-image",
      "engine": "mflux",
      "release_tier": "preview",
      "endpoint": "/v1/images/generations",
      "vision": false,
      "response_preview": "1 image result(s)",
      "response_ms": 900,
      "usage": null,
      "usage_recorded": null,
      "usage_delivery": {
        "enabled": true,
        "node_id": "theseus",
        "node_id_source": "computer_name",
        "writer_ready": true,
        "outbox_pending": 0,
        "outbox_depth": 0,
        "last_flush_at": null,
        "last_flush_count": 0,
        "last_error": null
      }
    }
    """#.data(using: .utf8)!
    let imageOnly = try JSONDecoder.nativeSettingsDecoder().decode(
        ModelSelfTestResult.self,
        from: imageOnlyPayload
    )
    #expect(imageOnly.success)
    #expect(!imageOnly.completesGuidedSetup)
}

@Test("The catalog decoder preserves aliases and ignores future fields")
func modelCatalogDecodeIsForwardCompatible() throws {
    let payload = #"""
    {
      "models": [
        {
          "id": "deepseek-v4",
          "object": "model",
          "owned_by": "mnemosyne",
          "engine": "ds4",
          "upstream_model": "mlx-community/DeepSeek-V4",
          "capabilities": ["chat/completions"],
          "load_config_digest": "ignored"
        }
      ],
      "resident_alias": null,
      "future_field": true
    }
    """#.data(using: .utf8)!

    let catalog = try JSONDecoder().decode(ModelCatalogSnapshot.self, from: payload)
    #expect(catalog.models.map(\.id) == ["deepseek-v4"])
    #expect(catalog.models.first?.engine == "ds4")
    #expect(catalog.residentAlias == nil)
}
