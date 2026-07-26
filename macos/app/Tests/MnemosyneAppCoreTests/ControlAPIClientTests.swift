import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("The menu app defaults to Mnemosyne's dedicated control port")
func defaultControlPort() {
    #expect(ControlAPIClient.defaultBaseURL.host == "127.0.0.1")
    #expect(ControlAPIClient.defaultBaseURL.port == 17_321)
    #expect(NativeSettings().engines.llamaCpp.enabled)
    #expect(NativeSettings().engines.llamaCpp.port == 17_325)
    #expect(NativeSettings().schemaVersion == 2)
    #expect(NativeSettings().tokenSidecar.enabled)
}

@Test("Endpoint paths are resolved below the control origin")
func endpointResolution() {
    let client = ControlAPIClient(baseURL: URL(string: "http://localhost:17321")!)
    #expect(client.endpointURL("/manager/status").absoluteString == "http://localhost:17321/manager/status")
    #expect(client.endpointURL("/manager/models").absoluteString == "http://localhost:17321/manager/models")
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

@Test("Structured configuration saves use the control endpoint and snake-case JSON")
func configurationSaveRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    var settings = NativeSettings()
    settings.server.inferencePort = 17_330
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
    #expect(server["inference_port"] as? Int == 17_330)
    #expect(config["schema_version"] as? Int == 2)
    #expect(load["context_length"] as? Int == 32_768)
    #expect(load["projector_path"] as? String == "/Volumes/Athena/models/mmproj.gguf")
    #expect(load["gpu_layers"] as? Int == 99)
    #expect(load["ubatch_size"] as? Int == 512)
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
    let settings = NativeSettings(schemaVersion: 3)

    #expect(throws: ControlAPIError.unsupportedConfigurationSchema(3)) {
        _ = try client.configurationSaveRequest(
            settings: settings,
            revision: String(repeating: "f", count: 64)
        )
    }
}

@Test("Structured configuration decodes every engine and image setting")
func configurationDecoding() throws {
    let payload = #"""
    {
      "schema_version": 2,
      "server": {"inference_bind":"127.0.0.1","inference_port":1240,"control_bind":"127.0.0.1","control_port":17321,"idle_unload_seconds":900,"startup_timeout_seconds":900,"swap_queue_timeout_seconds":300,"shutdown_grace_seconds":30,"reconcile_interval_seconds":30,"image_request_timeout_seconds":1800,"image_max_pixels":4194304,"startup_policy":"unload_all","inference_api_key_env":"INFERENCE_API_KEY","control_password_env":"ADMIN_PASSWORD"},
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
    #expect(install.capabilities == ModelRole.generation.capabilities)
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
          "display_name":"oMLX",
          "ownership":"external",
          "installed":true,
          "installed_version":"0.3.12",
          "installed_revision":null,
          "installed_path":"/Applications/oMLX.app",
          "latest_upstream_version":"0.3.13",
          "latest_upstream_revision":null,
          "latest_upstream_url":"https://github.com/jundot/omlx/releases/tag/v0.3.13",
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
    #expect(!snapshot.engines[0].canInstall)
    #expect(snapshot.engines[1].engine == .mflux)
    #expect(snapshot.engines[1].availableLabel == "0.19.0-ui1")
    #expect(snapshot.engines[1].canRollback)
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
      "token_sidecar": {
        "enabled": true,
        "node_id": "theseus",
        "node_id_source": "token_sidecar",
        "outbox_depth": 4,
        "last_flush_at": 1784462400.5,
        "future_field": "ignored"
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
