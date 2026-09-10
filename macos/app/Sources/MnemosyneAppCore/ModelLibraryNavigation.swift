import Foundation

/// Search hints only. They never authorize an install or assert compatibility.
public struct ModelLibraryNavigation: Equatable, Sendable {
    public let query: String
    public let engine: InferenceEngine

    public init(profile: ModelProfileSettings, installs: [ModelInstall]) {
        engine = profile.engine
        let path = (profile.model as NSString).standardizingPath
        if let install = installs.first(where: { install in
            guard install.engine == profile.engine else { return false }
            if profile.engine == .omlx, install.storage == profile.storage,
               (install.destination as NSString).lastPathComponent == profile.model { return true }
            let source = install.filename.map {
                (install.destination as NSString).appendingPathComponent($0)
            } ?? install.destination
            return (source as NSString).standardizingPath == path
        }) {
            query = install.repoId
            return
        }
        // A managed GGUF path has <storage>/llama.cpp/<owner>/<repo>/...
        // Use those two components as a search hint when the install row is absent.
        let components = (path as NSString).pathComponents
        if profile.engine == .llamaCpp,
           let marker = components.lastIndex(of: "llama.cpp"),
           components.count > marker + 3 {
            query = components[marker + 1] + "/" + components[marker + 2]
        } else {
            query = profile.engine == .omlx && !profile.model.isEmpty
                ? (profile.model as NSString).lastPathComponent : profile.alias
        }
    }
}
