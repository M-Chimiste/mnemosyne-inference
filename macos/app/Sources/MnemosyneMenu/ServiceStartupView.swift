import MnemosyneAppCore
import SwiftUI

extension ServiceStartupCoordinator {
    convenience init() {
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.timeoutIntervalForRequest = 2
        sessionConfiguration.timeoutIntervalForResource = 2
        let session = URLSession(configuration: sessionConfiguration)
        self.init {
            let configuration = ControlConnectionConfiguration.load()
            let client = ControlAPIClient(
                baseURL: configuration.baseURL,
                session: session,
                adminPassword: configuration.adminPassword
            )
            _ = try await client.status()
        }
    }
}

struct ServiceStartupView: View {
    @ObservedObject var startup: ServiceStartupCoordinator
    @ObservedObject var registration: LaunchAgentRegistration

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if startup.state.isWaiting {
                ProgressView().controlSize(.small)
            }
            Text(startup.state.message)
                .foregroundStyle(startup.state.isWaiting ? Color.secondary : .orange)
                .fixedSize(horizontal: false, vertical: true)
            if !startup.state.isWaiting {
                HStack {
                    if registration.agentStatus == .requiresApproval {
                        Button("Open Login Items") { registration.openLoginItemsSettings() }
                    }
                    if registration.agentStatus == .notRegistered {
                        Button("Enable Service") {
                            Task {
                                startup.prepare()
                                await registration.enableAgent()
                                await startup.connect(registration: registration.startupRegistrationState)
                            }
                        }
                    } else {
                        Button("Retry Connection") {
                            Task {
                                await startup.connect(registration: registration.startupRegistrationState)
                            }
                        }
                    }
                    Button("Open Logs") {
                        let support = FileManager.default.urls(
                            for: .applicationSupportDirectory, in: .userDomainMask
                        )[0]
                        NSWorkspace.shared.open(support.appending(path: "Mnemosyne/logs"))
                    }
                }
                .disabled(registration.isChangingRegistration)
            }
        }
    }
}
