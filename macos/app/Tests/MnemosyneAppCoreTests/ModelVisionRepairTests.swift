import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("Existing GGUF profiles remain selectable for an explicit projector update")
func existingModelProjectorUpdate() throws {
    var object: [String: Any] = [
        "id": "model", "source_key": "unsloth/GLM-5.3-Flash-GGUF", "engine": "llama.cpp",
        "display_name": "GLM", "model_path": "/Models/GLM/model.gguf",
        "all_paths": ["/Models/GLM/model.gguf"], "shard_count": 1, "size_bytes": 100,
        "compatibility": "structural", "compatibility_reason": "Header valid", "capabilities": ["chat/completions"],
        "existing_alias": "glm-5.3-flash", "already_imported": true,
        "projector_options": [["id": "projector", "path": "/Models/GLM/mmproj.gguf", "filename": "mmproj.gguf", "size_bytes": 20]],
    ]
    func candidate() throws -> LocalModelCandidate {
        try JSONDecoder.nativeSettingsDecoder().decode(LocalModelCandidate.self, from: JSONSerialization.data(withJSONObject: object))
    }
    #expect(try candidate().isImportable)
    #expect(try candidate().canUpdateProjector)
    let selected = ModelProfileSettings(alias: "glm-5.3-flash-2", engine: .llamaCpp, model: "/Models/GLM/model.gguf")
    #expect(try candidate().matches(selected))
    #expect(try !candidate().matches(ModelProfileSettings(alias: selected.alias, model: "/Models/other.gguf")))
    object["compatibility"] = "unavailable"
    #expect(try !candidate().isImportable)
    object["compatibility"] = "structural"
    object["projector_options"] = [] as [String]
    #expect(try !candidate().canUpdateProjector)
    #expect(try !candidate().isImportable)
}

@Test("Projector repair sends an exact update alias through the existing importer")
func projectorRepairRequest() throws {
    let client = ControlAPIClient(baseURL: URL(string: "http://localhost:17321")!, adminPassword: nil)
    let selection = LocalModelImportSelection(candidateId: "model", projectorId: "projector", updateAlias: "glm-5.3-flash-2")
    let request = try client.localModelImportRequest(LocalModelImportRequest(path: "/Models/GLM", selections: [selection]))
    let body = try #require(request.httpBody)
    let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    let selections = try #require(object["selections"] as? [[String: Any]])
    #expect(selections[0]["update_alias"] as? String == "glm-5.3-flash-2")
    #expect(selections[0]["projector_id"] as? String == "projector")
}

@Test("Complete-download search preserves the engine and managed repository hint")
func completeDownloadNavigation() throws {
    let profile = ModelProfileSettings(alias: "custom-alias-2", engine: .llamaCpp,
        model: "/Volumes/Athena/Models/llama.cpp/unsloth/GLM-5.3-Flash-GGUF/UD-Q4_K_XL/GLM-5.3-Flash-UD-Q4_K_XL-00001-of-00006.gguf")
    let destination = ModelLibraryNavigation(profile: profile, installs: [])
    #expect(destination.engine == .llamaCpp)
    #expect(destination.query == "unsloth/GLM-5.3-Flash-GGUF")
    let client = ControlAPIClient(baseURL: URL(string: "http://localhost:17321")!, adminPassword: nil)
    let request = client.librarySearchRequest(query: destination.query, engine: destination.engine)
    let url = try #require(request.url)
    let query = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
    #expect(query.contains(URLQueryItem(name: "engine", value: "llama.cpp")))
    #expect(query.contains(URLQueryItem(name: "q", value: "unsloth/GLM-5.3-Flash-GGUF")))
    #expect(!GLM53PreviewPresentation.shouldOfferRuntimeInstall(query: destination.query, models: [], ds4Update: nil, engine: destination.engine))
}

@Test("MLX search and image testing do not depend on a GGUF projector")
func mlxVisionNavigationAndTest() throws {
    let profile = ModelProfileSettings(alias: "my-vision", engine: .omlx, model: "Qwen3-VL-8B-4bit")
    let destination = ModelLibraryNavigation(profile: profile, installs: [])
    #expect(destination.engine == .omlx)
    #expect(destination.query == profile.model)
    #expect(ModelVisionReadiness(profile: profile, verified: false) == .untested)
    #expect(ModelVisionReadiness(profile: profile, verified: false, visionMetadata: true) == .metadataDetected)
    #expect(ModelVisionReadiness(profile: profile, verified: true) == .verified)
    let client = ControlAPIClient(baseURL: URL(string: "http://localhost:17321")!, adminPassword: nil)
    let request = try client.selfTestRequest(model: profile.alias, includeVision: true, unloadAfter: false, requireVision: true)
    let body = try #require(request.httpBody)
    let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    #expect(object["require_vision"] as? Bool == true)
    #expect(object["include_vision"] as? Bool == true)
}

@Test("Existing MLX models can be reviewed without any GGUF projector options")
func existingMLXReview() throws {
    let object: [String: Any] = [
        "id": "mlx", "source_key": "publisher/vision-mlx", "engine": "omlx",
        "display_name": "Vision MLX", "model_path": "/Models/publisher/vision-mlx",
        "all_paths": ["/Models/publisher/vision-mlx/model.safetensors"],
        "shard_count": 1, "size_bytes": 100, "compatibility": "structural",
        "compatibility_reason": "MLX metadata detected", "capabilities": ["chat/completions"],
        "existing_alias": "my-vision", "already_imported": true,
        "projector_options": [] as [String], "vision_components": true,
    ]
    let candidate = try JSONDecoder.nativeSettingsDecoder().decode(LocalModelCandidate.self,
        from: JSONSerialization.data(withJSONObject: object))
    #expect(candidate.isImportable)
    #expect(candidate.canUpdateExisting)
    #expect(!candidate.canUpdateProjector)
    #expect(candidate.visionComponents == true)
    #expect(candidate.matches(ModelProfileSettings(alias: "other-alias", engine: .omlx, model: "vision-mlx")))
    #expect(!candidate.matches(ModelProfileSettings(alias: "my-vision", engine: .omlx, model: "other-mlx")))
}
