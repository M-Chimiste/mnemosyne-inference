import Foundation

public struct NativeSettings: Codable, Equatable, Sendable {
    public static let supportedSchemaVersion = 1

    public var schemaVersion: Int
    public var server: ServerSettings
    public var engines: EngineSettings
    public var paths: PathSettings
    public var storage: ModelStorageSettings
    public var models: [ModelProfileSettings]
    public var tokenSidecar: TokenSidecarSettings

    public init(
        schemaVersion: Int = NativeSettings.supportedSchemaVersion,
        server: ServerSettings = .init(),
        engines: EngineSettings = .init(),
        paths: PathSettings = .init(),
        storage: ModelStorageSettings = .init(),
        models: [ModelProfileSettings] = [],
        tokenSidecar: TokenSidecarSettings = .init()
    ) {
        self.schemaVersion = schemaVersion
        self.server = server
        self.engines = engines
        self.paths = paths
        self.storage = storage
        self.models = models
        self.tokenSidecar = tokenSidecar
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion
        case server
        case engines
        case paths
        case storage
        case models
        case tokenSidecar
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try container.decodeIfPresent(Int.self, forKey: .schemaVersion)
            ?? NativeSettings.supportedSchemaVersion
        server = try container.decode(ServerSettings.self, forKey: .server)
        engines = try container.decode(EngineSettings.self, forKey: .engines)
        paths = try container.decode(PathSettings.self, forKey: .paths)
        storage = try container.decode(ModelStorageSettings.self, forKey: .storage)
        models = try container.decode([ModelProfileSettings].self, forKey: .models)
        tokenSidecar = try container.decode(TokenSidecarSettings.self, forKey: .tokenSidecar)
    }
}

public struct ServerSettings: Codable, Equatable, Sendable {
    public var inferenceBind = "127.0.0.1"
    public var inferencePort = 17_320
    public var controlBind = "127.0.0.1"
    public var controlPort = 17_321
    public var idleUnloadSeconds: Int? = 900
    public var startupTimeoutSeconds = 900.0
    public var swapQueueTimeoutSeconds = 300.0
    public var shutdownGraceSeconds = 30.0
    public var reconcileIntervalSeconds = 30.0
    public var imageRequestTimeoutSeconds = 1_800.0
    public var imageMaxPixels = 4_194_304
    public var startupPolicy = "unload_all"
    public var inferenceApiKeyEnv = "INFERENCE_API_KEY"
    public var controlPasswordEnv = "ADMIN_PASSWORD"

    public init() {}
}

public struct EngineSettings: Codable, Equatable, Sendable {
    public var lmstudio = LMStudioSettings()
    public var llamaCpp = LlamaCppSettings()
    public var omlx = OMLXSettings()
    public var ds4 = DS4Settings()
    public var mflux = MFluxSettings()

    public init() {}
}

public struct LMStudioSettings: Codable, Equatable, Sendable {
    public var enabled = false
    public var baseUrl = "http://127.0.0.1:1234"
    public var apiKeyEnv = "LMSTUDIO_API_KEY"
    public var requestTimeoutSeconds = 30.0

    public init() {}
}

public struct LlamaCppSettings: Codable, Equatable, Sendable {
    public var enabled = true
    public var host = "127.0.0.1"
    public var port = 17_325
    public var binary =
        "~/Library/Application Support/Mnemosyne/runtimes/llama.cpp/not-installed/llama-server"
    public var workingDirectory = "~/Library/Application Support/Mnemosyne"
    public var processStatePath =
        "~/Library/Application Support/Mnemosyne/state/llama-cpp-process.json"
    public var requestTimeoutSeconds = 30.0
    public var shutdownGraceSeconds = 30.0

    public init() {}
}

public struct OMLXSettings: Codable, Equatable, Sendable {
    public var enabled = true
    public var baseUrl = "http://127.0.0.1:17322"
    public var apiKeyEnv = "OMLX_API_KEY"
    public var adminSessionEnv = "OMLX_ADMIN_SESSION"
    public var requestTimeoutSeconds = 30.0
    public var modelDirectories: [String] = []

    public init() {}
}

public struct DS4Settings: Codable, Equatable, Sendable {
    public var enabled = true
    public var host = "127.0.0.1"
    public var port = 17_323
    public var binary = "/Applications/DwarfStar/ds4-server"
    public var workingDirectory = "/Applications/DwarfStar"
    public var processStatePath = "~/Library/Application Support/Mnemosyne/state/ds4-process.json"
    public var requestTimeoutSeconds = 30.0
    public var shutdownGraceSeconds = 30.0

    public init() {}
}

public struct MFluxSettings: Codable, Equatable, Sendable {
    public var enabled = false
    public var host = "127.0.0.1"
    public var port = 17_324
    public var python: String?
    public var pythonEnv = "MNEMOSYNE_MFLUX_PYTHON"
    public var sourcePathEnv = "MNEMOSYNE_MFLUX_PYTHONPATH"
    public var requestTimeoutSeconds = 30.0
    public var shutdownGraceSeconds = 30.0

    public init() {}
}

public struct PathSettings: Codable, Equatable, Sendable {
    public var stateDatabase = "~/Library/Application Support/Mnemosyne/state/mnemosyne.db"
    public var logDirectory = "~/Library/Application Support/Mnemosyne/logs"

    public init() {}
}

public struct ModelStorageSettings: Codable, Equatable, Sendable {
    public var `default`: String
    public var locations: [StorageLocationSettings]

    public init(
        default: String = "internal",
        locations: [StorageLocationSettings] = [.internalDefault]
    ) {
        self.default = `default`
        self.locations = locations
    }
}

public struct StorageLocationSettings: Codable, Equatable, Identifiable, Sendable {
    public var name: String
    public var path: String
    public var volumeUuid: String?
    public var scopeId: String?

    public var id: String { name }

    public init(
        name: String,
        path: String,
        volumeUuid: String? = nil,
        scopeId: String? = nil
    ) {
        self.name = name
        self.path = path
        self.volumeUuid = volumeUuid
        self.scopeId = scopeId
    }

    public static let internalDefault = StorageLocationSettings(
        name: "internal",
        path: "~/Library/Application Support/Mnemosyne/models"
    )
}

public enum InferenceEngine: String, Codable, CaseIterable, Identifiable, Sendable {
    case lmstudio
    case llamaCpp = "llama.cpp"
    case omlx
    case ds4
    case mflux

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .lmstudio: "LM Studio"
        case .llamaCpp: "llama.cpp"
        case .omlx: "oMLX"
        case .ds4: "DS4"
        case .mflux: "MFLUX"
        }
    }
}

public enum ModelKindSetting: String, Codable, Sendable {
    case language
    case image
}

public struct ModelProfileSettings: Codable, Equatable, Sendable {
    public var alias: String
    public var engine: InferenceEngine
    public var model: String
    public var storage: String?
    public var servedModelName: String?
    public var capabilities: [String]?
    public var load: ModelLoadSettings
    public var kind: ModelKindSetting
    public var image: ImageProfileSettings?
    public var enabled: Bool

    public init(
        alias: String = "new-model",
        engine: InferenceEngine = .llamaCpp,
        model: String = "/path/to/model.gguf",
        storage: String? = nil,
        servedModelName: String? = nil,
        capabilities: [String]? = nil,
        load: ModelLoadSettings = .init(),
        kind: ModelKindSetting = .language,
        image: ImageProfileSettings? = nil,
        enabled: Bool = true
    ) {
        self.alias = alias
        self.engine = engine
        self.model = model
        self.storage = storage
        self.servedModelName = servedModelName
        self.capabilities = capabilities
        self.load = load
        self.kind = kind
        self.image = image
        self.enabled = enabled
    }
}

public struct ModelLoadSettings: Codable, Equatable, Sendable {
    public var contextLength: Int?
    public var evalBatchSize: Int?
    public var flashAttention: Bool?
    public var numExperts: Int?
    public var offloadKvCacheToGpu: Bool?
    public var projectorPath: String?
    public var gpuLayers: Int?
    public var ubatchSize: Int?
    public var threads: Int?
    public var parallel: Int?
    public var pooling: String?
    public var kvDiskDirectory: String?
    public var kvDiskSpaceMb: Int?
    public var extraArgs: [String]

    public init(
        contextLength: Int? = nil,
        evalBatchSize: Int? = nil,
        flashAttention: Bool? = nil,
        numExperts: Int? = nil,
        offloadKvCacheToGpu: Bool? = nil,
        projectorPath: String? = nil,
        gpuLayers: Int? = nil,
        ubatchSize: Int? = nil,
        threads: Int? = nil,
        parallel: Int? = nil,
        pooling: String? = nil,
        kvDiskDirectory: String? = nil,
        kvDiskSpaceMb: Int? = nil,
        extraArgs: [String] = []
    ) {
        self.contextLength = contextLength
        self.evalBatchSize = evalBatchSize
        self.flashAttention = flashAttention
        self.numExperts = numExperts
        self.offloadKvCacheToGpu = offloadKvCacheToGpu
        self.projectorPath = projectorPath
        self.gpuLayers = gpuLayers
        self.ubatchSize = ubatchSize
        self.threads = threads
        self.parallel = parallel
        self.pooling = pooling
        self.kvDiskDirectory = kvDiskDirectory
        self.kvDiskSpaceMb = kvDiskSpaceMb
        self.extraArgs = extraArgs
    }
}

public struct ImageFamily: RawRepresentable, Codable, Hashable, Identifiable, Sendable {
    public let rawValue: String

    public init(rawValue: String) {
        self.rawValue = rawValue
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        rawValue = try container.decode(String.self)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }

    public static let schnell = ImageFamily(rawValue: "schnell")
    public static let dev = ImageFamily(rawValue: "dev")
    public static let kreaDev = ImageFamily(rawValue: "krea-dev")
    public static let flux2Klein4B = ImageFamily(rawValue: "flux2-klein-4b")
    public static let flux2Klein9B = ImageFamily(rawValue: "flux2-klein-9b")
    public static let flux2Klein9BKV = ImageFamily(rawValue: "flux2-klein-9b-kv")
    public static let flux2KleinBase4B = ImageFamily(rawValue: "flux2-klein-base-4b")
    public static let flux2KleinBase9B = ImageFamily(rawValue: "flux2-klein-base-9b")
    public static let qwenImage = ImageFamily(rawValue: "qwen-image")
    public static let krea2 = ImageFamily(rawValue: "krea-2")
    public static let fibo = ImageFamily(rawValue: "fibo")
    public static let fiboLite = ImageFamily(rawValue: "fibo-lite")
    public static let zImage = ImageFamily(rawValue: "z-image")
    public static let zImageTurbo = ImageFamily(rawValue: "z-image-turbo")
    public static let ernieImage = ImageFamily(rawValue: "ernie-image")
    public static let ernieImageTurbo = ImageFamily(rawValue: "ernie-image-turbo")
    public static let ideogram4FP8 = ImageFamily(rawValue: "ideogram-4-fp8")

    public static let allCases: [ImageFamily] = [
        .schnell, .dev, .kreaDev,
        .flux2Klein4B, .flux2Klein9B, .flux2Klein9BKV,
        .flux2KleinBase4B, .flux2KleinBase9B,
        .qwenImage, .krea2, .fibo, .fiboLite,
        .zImage, .zImageTurbo, .ernieImage, .ernieImageTurbo,
        .ideogram4FP8,
    ]

    public var id: String { rawValue }
    public var displayName: String {
        switch rawValue {
        case "schnell": "FLUX.1 Schnell"
        case "dev": "FLUX.1 Dev"
        case "krea-dev": "FLUX.1 Krea Dev"
        case "flux2-klein-4b": "FLUX.2 Klein 4B"
        case "flux2-klein-9b": "FLUX.2 Klein 9B"
        case "flux2-klein-9b-kv": "FLUX.2 Klein 9B KV"
        case "flux2-klein-base-4b": "FLUX.2 Klein Base 4B"
        case "flux2-klein-base-9b": "FLUX.2 Klein Base 9B"
        case "qwen-image": "Qwen Image"
        case "krea-2": "Krea 2 Turbo"
        case "fibo": "FIBO"
        case "fibo-lite": "FIBO Lite"
        case "z-image": "Z-Image"
        case "z-image-turbo": "Z-Image Turbo"
        case "ernie-image": "ERNIE Image"
        case "ernie-image-turbo": "ERNIE Image Turbo"
        case "ideogram-4-fp8": "Ideogram 4 FP8"
        default: rawValue.split(separator: "-").map { $0.capitalized }.joined(separator: " ")
        }
    }
}

public struct ImageProfileSettings: Codable, Equatable, Sendable {
    public var family: ImageFamily
    public var quantize: Int?
    public var width: Int
    public var height: Int
    public var numInferenceSteps: Int
    public var guidanceScale: Double

    public init(
        family: ImageFamily = .qwenImage,
        quantize: Int? = 8,
        width: Int = 1_024,
        height: Int = 1_024,
        numInferenceSteps: Int = 30,
        guidanceScale: Double = 4
    ) {
        self.family = family
        self.quantize = quantize
        self.width = width
        self.height = height
        self.numInferenceSteps = numInferenceSteps
        self.guidanceScale = guidanceScale
    }
}

public struct TokenSidecarSettings: Codable, Equatable, Sendable {
    public var enabled = true
    // Empty migrates the stable node ID from the previous token sidecar.
    public var nodeId = ""
    public var flushIntervalSeconds = 30
    public var batchSize = 500
    public var maxOutboxRows = 100_000
    public var connectTimeoutSeconds = 5.0

    public init() {}
}

public extension JSONDecoder {
    static func nativeSettingsDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}

public extension JSONEncoder {
    static func nativeSettingsEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }
}
