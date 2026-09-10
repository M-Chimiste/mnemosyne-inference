import Foundation

/// Presentation derived from observed service state, never from a running animation.
public struct WorkspaceActivity: Equatable, Sendable {
    public let title: String
    public let detail: String
    public let symbol: String
    public let isWorking: Bool
    public let needsAttention: Bool

    public init(snapshot: ServiceSnapshot?, isLive: Bool) {
        let alias = snapshot?.residentAlias ?? "your model"
        let count = snapshot?.inFlightRequests ?? 0
        guard isLive, let snapshot else {
            title = "Reconnecting"
            detail = "Waiting for a fresh update from this Mac."
            symbol = "arrow.triangle.2.circlepath"
            isWorking = false
            needsAttention = true
            return
        }
        needsAttention = !(snapshot.startupError?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true) || snapshot.status == "degraded"
        switch snapshot.status {
        case "loading", "verifying":
            title = "Preparing a model"
            detail = "The engine is getting ready for your request."
            symbol = "sparkles"
            isWorking = true
        case "draining", "unloading", "stopping":
            title = count > 0 ? "Finishing \(count) requests" : "Releasing the model"
            detail = "Waiting for the current work to finish."
            symbol = "hourglass"
            isWorking = true
        default:
            if needsAttention {
                title = "Needs attention"
                detail = "Open Setup & Health for the next step."
                symbol = "exclamationmark.triangle"
                isWorking = false
            } else if count > 0 {
                title = "Running inference"
                detail = "\(alias) · \(count) active request\(count == 1 ? "" : "s")"
                symbol = "waveform"
                isWorking = true
            } else {
                title = "Ready when you are"
                detail = snapshot.residentAlias == nil
                    ? "Models load automatically when a request arrives."
                    : "\(alias) is in memory and ready."
                symbol = "sparkles"
                isWorking = false
            }
        }
    }
}

public enum ConfigurationChangeImpact: Equatable, Sendable {
    case none, immediate, restart

    public init(saved: NativeSettings, draft: NativeSettings, credentialsChanged: Bool) {
        if credentialsChanged || saved.server != draft.server
            || saved.engines != draft.engines || saved.paths != draft.paths
            || saved.storage != draft.storage || saved.tokenSidecar != draft.tokenSidecar {
            self = .restart
        } else if saved != draft {
            self = .immediate
        } else {
            self = .none
        }
    }

    public var label: String {
        switch self {
        case .none: "All changes saved"
        case .immediate: "Applies when saved"
        case .restart: "Requires service restart"
        }
    }
}

/// An adapter configuration is not evidence that image inference succeeded.
public enum ModelVisionReadiness: Equatable, Sendable {
    case imageGeneration, verified, configured, missingAdapter, notDeclared, untested, metadataDetected

    public init(profile: ModelProfileSettings, verified: Bool, visionMetadata: Bool? = nil) {
        if profile.kind == .image { self = .imageGeneration }
        else if verified { self = .verified }
        else if profile.engine == .llamaCpp {
            if let path = profile.load.projectorPath, !path.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                self = .configured
            } else if profile.capabilities?.contains("vision") == true {
                self = .missingAdapter
            } else { self = .notDeclared }
        } else if profile.engine == .omlx {
            self = visionMetadata == true ? .metadataDetected : .untested
        } else if profile.capabilities?.contains("vision") == true {
            self = .configured
        } else { self = .notDeclared }
    }

    public var label: String {
        switch self {
        case .imageGeneration: "Image generation"
        case .verified: "Vision test passed"
        case .configured: "Vision configured · untested"
        case .missingAdapter: "Vision adapter missing"
        case .notDeclared: "Vision not configured"
        case .untested: "Vision not yet tested"
        case .metadataDetected: "Vision metadata detected · untested"
        }
    }
}

public extension ModelInstall {
    var estimatedSecondsRemaining: Double? {
        guard status == "downloading", let totalBytes, totalBytes > bytesDownloaded,
              bytesDownloaded > 0, let speed = downloadSpeedBps,
              speed.isFinite, speed > 0 else { return nil }
        let seconds = Double(totalBytes - max(0, bytesDownloaded)) / speed
        return seconds.isFinite && seconds < 7 * 86_400 ? seconds : nil
    }
}

public enum WorkspaceModelMetadata {
    public static func quantization(path: String) -> String? {
        let filename = URL(fileURLWithPath: path).lastPathComponent.uppercased()
        guard filename.hasSuffix(".GGUF"),
              let range = filename.range(
                of: #"(?:^|[.\-_])((?:IQ|Q)[0-9]+(?:_[A-Z0-9]+)*|BF16|F16|F32)(?=[.\-]|$)"#,
                options: .regularExpression
              ) else { return nil }
        return String(filename[range]).trimmingCharacters(in: CharacterSet(charactersIn: ".-_"))
    }
}
