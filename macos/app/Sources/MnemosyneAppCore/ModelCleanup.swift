import Foundation

public enum ModelCleanupRefusal: Equatable, Sendable {
    case preparationRequired
    case unsavedSettings
    case installHistoryUnavailable
    case profileStorageUnavailable
    case ambiguousManagedRecords
    case managedIdentityMismatch
    case managedInstallNotReady
    case invalidInstallationIdentity
}

public enum ModelCleanupDecision: Equatable, Sendable {
    case imported
    case managed(installationID: String)
    case refused(ModelCleanupRefusal)

    public var installationID: String? {
        guard case let .managed(installationID) = self else { return nil }
        return installationID
    }

    public var permitsFileCleanup: Bool {
        switch self {
        case .imported, .managed:
            true
        case .refused:
            false
        }
    }

    public var confirmationMessage: String {
        let keep = "Keep Files removes only the profile after you save."
        switch self {
        case .imported:
            return "\(keep) Move Files to Trash immediately removes the profile and asks the service to freshly scan its registered storage folder for one exact matching model. Files are moved to Trash only after those checks succeed."
        case .managed:
            return "\(keep) Move Files to Trash immediately removes the profile and asks the service to revalidate this exact managed installation, its storage binding, and its owned files before moving them to Trash."
        case let .refused(reason):
            return "\(keep) \(reason.message) No model files will be changed."
        }
    }

    public func retainingUnrelatedInstalls(
        from installs: [ModelInstall]
    ) -> [ModelInstall] {
        guard case let .managed(installationID) = self else { return installs }
        return installs.filter { $0.id != installationID }
    }

    public static func successMessage(
        alias: String,
        filesDisposition: String?
    ) -> String {
        if filesDisposition == "trashed" {
            return "Moved \(alias)'s model files to Trash and removed its profile."
        }
        return "Removed \(alias)'s model files and profile."
    }
}

public enum ModelCleanupResolver {
    public static func resolve(
        profile: ModelProfileSettings,
        storageLocations: [StorageLocationSettings],
        installs: [ModelInstall]
    ) -> ModelCleanupDecision {
        guard let storage = profile.storage else {
            return .refused(.profileStorageUnavailable)
        }
        let locations = storageLocations.filter { $0.name == storage }
        guard locations.count == 1,
              let storageRoot = lexicalAbsolutePath(locations[0].path)
        else {
            return .refused(.profileStorageUnavailable)
        }

        let current = installs.filter {
            $0.status != "deleted" && $0.status != "trashed"
        }
        let structuralMatches = current.filter {
            matchesProfileStructure(
                profile,
                install: $0,
                storageRoot: storageRoot
            )
        }

        if structuralMatches.count > 1 {
            return .refused(.ambiguousManagedRecords)
        }
        if let match = structuralMatches.first,
           match.alias == profile.alias
        {
            guard match.status == "installed" else {
                return .refused(.managedInstallNotReady)
            }
            guard canonicalUUID(match.id) != nil else {
                return .refused(.invalidInstallationIdentity)
            }
            return .managed(installationID: match.id)
        }

        if !structuralMatches.isEmpty
            || current.contains(where: { $0.alias == profile.alias })
        {
            return .refused(.managedIdentityMismatch)
        }
        return .imported
    }

    private static func matchesProfileStructure(
        _ profile: ModelProfileSettings,
        install: ModelInstall,
        storageRoot: String
    ) -> Bool {
        guard profile.engine == install.engine,
              profile.storage == install.storage,
              let destination = lexicalAbsolutePath(install.destination),
              path(destination, isStrictlyWithin: storageRoot)
        else {
            return false
        }

        switch profile.engine {
        case .llamaCpp, .ds4:
            guard let filename = safeRelativePath(install.filename),
                  lexicalAbsolutePath(profile.model)
                    == lexicalAbsolutePath(destination + "/" + filename)
            else {
                return false
            }
            switch (profile.load.projectorPath, install.projectorFilename) {
            case (nil, nil):
                return true
            case let (profileProjector?, installProjector?):
                guard let projector = safeRelativePath(installProjector)
                else { return false }
                return lexicalAbsolutePath(profileProjector)
                    == lexicalAbsolutePath(destination + "/" + projector)
            default:
                return false
            }
        case .omlx:
            return profile.model == (destination as NSString).lastPathComponent
                && profile.model == (profile.model as NSString).lastPathComponent
        case .mflux, .mlxcel, .mistralRs:
            return lexicalAbsolutePath(profile.model) == destination
        }
    }

    private static func canonicalUUID(_ value: String) -> UUID? {
        guard let uuid = UUID(uuidString: value),
              uuid.uuidString.lowercased() == value
        else { return nil }
        return uuid
    }

    private static func lexicalAbsolutePath(_ value: String) -> String? {
        let expanded = (value as NSString).expandingTildeInPath
        guard expanded.hasPrefix("/") else { return nil }
        return (expanded as NSString).standardizingPath
    }

    private static func safeRelativePath(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        let normalized = value.replacingOccurrences(of: "\\", with: "/")
        let parts = normalized.split(separator: "/", omittingEmptySubsequences: false)
        guard !normalized.hasPrefix("/"),
              !parts.isEmpty,
              parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." })
        else {
            return nil
        }
        return normalized
    }

    private static func path(_ candidate: String, isStrictlyWithin root: String) -> Bool {
        guard candidate != root else { return false }
        return candidate.hasPrefix(root == "/" ? root : root + "/")
    }
}

private extension ModelCleanupRefusal {
    var message: String {
        switch self {
        case .preparationRequired:
            "File cleanup has not been prepared from current install history."
        case .unsavedSettings:
            "File cleanup is unavailable while other settings changes are pending."
        case .installHistoryUnavailable:
            "File cleanup is unavailable because current install history could not be verified."
        case .profileStorageUnavailable:
            "File cleanup is unavailable because this profile does not identify exactly one configured storage folder."
        case .ambiguousManagedRecords:
            "File cleanup is refused because multiple managed install records match this profile."
        case .managedIdentityMismatch:
            "File cleanup is refused because managed install history and this profile do not have one exact matching identity."
        case .managedInstallNotReady:
            "File cleanup is refused because the matching managed installation is not in the installed state."
        case .invalidInstallationIdentity:
            "File cleanup is refused because the matching managed installation does not have a canonical installation ID."
        }
    }
}
