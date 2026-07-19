import SwiftUI

@main
struct MnemosyneMenuApp: App {
    @StateObject private var viewModel = MenuViewModel()
    @StateObject private var registration = LaunchAgentRegistration()

    var body: some Scene {
        MenuBarExtra("Mnemosyne", systemImage: "brain.head.profile") {
            MenuContentView(
                viewModel: viewModel,
                registration: registration
            )
        }
        .menuBarExtraStyle(.window)
    }
}
