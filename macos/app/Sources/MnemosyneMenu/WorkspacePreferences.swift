import Foundation
import SwiftUI
import UserNotifications

@MainActor
final class WorkspacePreferences: ObservableObject {
    static let shared = WorkspacePreferences()
    @Published private(set) var favorites: Set<String>
    @Published private(set) var notifyWhenReady: Bool
    @Published var notificationMessage = ""

    private init() {
        favorites = Set((UserDefaults.standard.stringArray(forKey: "workspaceFavoriteModels") ?? []).prefix(128))
        notifyWhenReady = UserDefaults.standard.bool(forKey: "workspaceNotifyWhenReady")
    }

    func toggleFavorite(_ alias: String) {
        if favorites.contains(alias) { favorites.remove(alias) }
        else if favorites.count < 128 { favorites.insert(alias) }
        UserDefaults.standard.set(favorites.sorted(), forKey: "workspaceFavoriteModels")
    }

    func setNotifications(_ enabled: Bool) async {
        notificationMessage = ""
        guard enabled else {
            notifyWhenReady = false
            UserDefaults.standard.set(false, forKey: "workspaceNotifyWhenReady")
            return
        }
        guard Bundle.main.bundleIdentifier != nil else {
            notificationMessage = "Notifications are available in the installed app."
            return
        }
        do {
            let allowed = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
            notifyWhenReady = allowed
            UserDefaults.standard.set(allowed, forKey: "workspaceNotifyWhenReady")
            if !allowed { notificationMessage = "Allow notifications for Unified Inference in System Settings." }
        } catch { notificationMessage = "Notifications could not be enabled. Check System Settings." }
    }

    func modelReady(_ alias: String) {
        guard notifyWhenReady, Bundle.main.bundleIdentifier != nil else { return }
        let content = UNMutableNotificationContent()
        content.title = "Your model is ready"
        content.body = "\(alias) finished downloading. Find it in Models."
        Task {
            try? await UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
            )
        }
    }
}
