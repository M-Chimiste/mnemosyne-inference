import Testing
@testable import MnemosyneAppCore

@Test("Typed roles map to safe endpoint groups")
func modelRoleCapabilitiesAreCanonical() {
    #expect(
        ModelRole.generation.capabilities
            == ["chat/completions", "completions", "responses", "messages"]
    )
    #expect(ModelRole.embeddings.capabilities == ["embeddings"])
    #expect(ModelRole.rerank.capabilities == ["rerank"])
    #expect(ModelRole.image.capabilities == ["images/generations"])
    #expect(
        ModelRole.generation.capabilities(for: .llamaCpp)
            == ["chat/completions", "completions", "responses"]
    )
    #expect(
        ModelRole.generation.capabilities(for: .omlx)
            == ModelRole.generation.capabilities
    )
}

@Test("Role choices are limited by engine and multimodal projector")
func availableModelRolesAreEngineAware() {
    let ds4 = ModelProfileSettings(
        alias: "deepseek",
        engine: .ds4,
        model: "/models/deepseek.gguf"
    )
    #expect(ds4.availableRoles == [.generation])

    let omlx = ModelProfileSettings(
        alias: "embedder",
        engine: .omlx,
        model: "embedder",
        capabilities: ModelRole.embeddings.capabilities
    )
    #expect(omlx.availableRoles == [.generation, .embeddings, .rerank])

    let multimodal = ModelProfileSettings(
        alias: "vision",
        engine: .llamaCpp,
        model: "/models/vision.gguf",
        load: ModelLoadSettings(projectorPath: "/models/mmproj.gguf")
    )
    #expect(multimodal.availableRoles == [.generation])
}

@Test("Applying a role cannot create mixed generation and specialized routes")
func applyingModelRoleIsAtomic() {
    var profile = ModelProfileSettings(
        alias: "reranker",
        engine: .llamaCpp,
        model: "/models/reranker.gguf"
    )

    profile.applyRole(.rerank)

    #expect(profile.configuredRole == .rerank)
    #expect(profile.capabilities == ["rerank"])
    #expect(profile.load.pooling == "rank")
    #expect(profile.kind == .language)

    profile.applyRole(.generation)
    #expect(profile.configuredRole == .generation)
    #expect(
        profile.capabilities
            == ModelRole.generation.capabilities(for: .llamaCpp)
    )
    #expect(profile.load.pooling == nil)
}

@Test("Explicit llama.cpp Messages capability survives typed role editing")
func explicitLlamaMessagesCapabilityIsPreserved() {
    var profile = ModelProfileSettings(
        alias: "anthropic",
        engine: .llamaCpp,
        model: "/models/anthropic.gguf",
        capabilities: ModelRole.generation.capabilities
    )

    #expect(profile.configuredRole == .generation)
    profile.applyRole(.generation)
    #expect(profile.capabilities == ModelRole.generation.capabilities)
}

@Test("Legacy mixed endpoint combinations require a deliberate role choice")
func legacyCustomCapabilitiesAreNotSilentlyRewritten() {
    let profile = ModelProfileSettings(
        alias: "legacy",
        engine: .omlx,
        model: "legacy",
        capabilities: ["chat/completions", "embeddings"]
    )

    #expect(profile.configuredRole == nil)
    #expect(profile.capabilities == ["chat/completions", "embeddings"])
}

@Test("Generation requires the complete canonical endpoint set")
func generationRoleDoesNotAcceptEndpointSubsets() {
    var profile = ModelProfileSettings(
        alias: "legacy-chat-only",
        engine: .ds4,
        model: "/models/deepseek.gguf",
        capabilities: ["chat/completions"]
    )

    #expect(profile.availableRoles == [.generation])
    #expect(profile.configuredRole == nil)

    profile.applyRole(.generation)
    #expect(profile.configuredRole == .generation)
    #expect(profile.capabilities == ModelRole.generation.capabilities)
}
