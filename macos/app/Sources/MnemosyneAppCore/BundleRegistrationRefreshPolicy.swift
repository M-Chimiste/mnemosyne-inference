public enum BundleRegistrationRefreshAction: Equatable, Sendable {
    case none
    case refresh
    case preserveDisabled
    case retryDiscovery
}

/// Decides how an installed app should reconcile Service Management after
/// Finder replaces its bundle. A prior explicit disable remains authoritative,
/// while a transiently undiscoverable registration keeps the bundle refresh
/// pending instead of incorrectly accepting the new bundle as reconciled.
public enum BundleRegistrationRefreshPolicy {
    public static func action(
        bundleChanged: Bool,
        refreshPending: Bool,
        state: ManagedServiceRegistrationState
    ) -> BundleRegistrationRefreshAction {
        guard bundleChanged || refreshPending else { return .none }

        if refreshPending {
            switch state {
            case .notFound, .unknown:
                return .retryDiscovery
            case .notRegistered, .enabled, .requiresApproval:
                return .refresh
            }
        }

        switch state {
        case .enabled, .requiresApproval:
            return .refresh
        case .notRegistered:
            return .preserveDisabled
        case .notFound, .unknown:
            return .retryDiscovery
        }
    }
}
