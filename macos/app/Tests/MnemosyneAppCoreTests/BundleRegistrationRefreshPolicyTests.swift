import MnemosyneAppCore
import Testing

@Test("An unconfigured optional Hub does not block replacement reconciliation")
func unconfiguredHubDoesNotRequireDiscovery() {
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: true,
            refreshPending: false,
            state: .notFound,
            discoveryRequired: false
        ) == .none
    )
}

@Test("Optional Hub discovery preserves enabled, disabled, and recovery states")
func optionalHubPreservesRegistrationAuthority() {
    for (state, expected) in [
        (ManagedServiceRegistrationState.enabled, BundleRegistrationRefreshAction.refresh),
        (.requiresApproval, .refresh),
        (.notRegistered, .preserveDisabled),
        (.unknown, .retryDiscovery),
    ] {
        #expect(
            BundleRegistrationRefreshPolicy.action(
                bundleChanged: true,
                refreshPending: false,
                state: state,
                discoveryRequired: false
            ) == expected
        )
    }
    for (state, expected) in [
        (ManagedServiceRegistrationState.notFound, BundleRegistrationRefreshAction.retryDiscovery),
        (.unknown, .retryDiscovery),
        (.notRegistered, .refresh),
        (.enabled, .refresh),
        (.requiresApproval, .refresh),
    ] {
        #expect(
            BundleRegistrationRefreshPolicy.action(
                bundleChanged: false,
                refreshPending: true,
                state: state,
                discoveryRequired: false
            ) == expected
        )
    }
}

@Test("A replaced bundle refreshes registrations that were enabled")
func changedBundleRefreshesEnabledRegistration() {
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: true,
            refreshPending: false,
            state: .enabled
        ) == .refresh
    )
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: true,
            refreshPending: false,
            state: .requiresApproval
        ) == .refresh
    )
}

@Test("A replaced bundle preserves an explicitly disabled registration")
func changedBundlePreservesDisabledRegistration() {
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: true,
            refreshPending: false,
            state: .notRegistered
        ) == .preserveDisabled
    )
}

@Test("An undiscoverable replacement remains pending for a later retry")
func changedBundleRetriesDiscovery() {
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: true,
            refreshPending: false,
            state: .notFound
        ) == .retryDiscovery
    )
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: true,
            refreshPending: false,
            state: .unknown
        ) == .retryDiscovery
    )
}

@Test("A durable refresh intent survives the unregister boundary")
func pendingRefreshReregistersFromDisabledState() {
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: false,
            refreshPending: true,
            state: .notRegistered
        ) == .refresh
    )
}

@Test("An unchanged settled registration needs no work")
func unchangedBundleDoesNothing() {
    #expect(
        BundleRegistrationRefreshPolicy.action(
            bundleChanged: false,
            refreshPending: false,
            state: .enabled
        ) == .none
    )
}
