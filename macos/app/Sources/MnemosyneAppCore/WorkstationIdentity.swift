import Foundation
import SystemConfiguration

/// Resolves the friendly name shown by the menu app for the current Mac.
///
/// The app bundle keeps the portable Mnemosyne product name. At runtime, the
/// controller uses the macOS Computer Name so the same build identifies itself
/// as Theseus, Metis, Athena, or whatever the Mac is named in System Settings.
public enum WorkstationIdentity {
    public static let overrideEnvironmentKey = "MNEMOSYNE_WORKSTATION_NAME"

    public static var current: String {
        resolve(
            environment: ProcessInfo.processInfo.environment,
            computerName: SCDynamicStoreCopyComputerName(nil, nil) as String?,
            localizedHostName: Host.current().localizedName
        )
    }

    public static func resolve(
        environment: [String: String],
        computerName: String?,
        localizedHostName: String?
    ) -> String {
        let candidates = [
            environment[overrideEnvironmentKey],
            computerName,
            localizedHostName,
        ]

        for candidate in candidates {
            if let name = normalized(candidate) {
                return name
            }
        }
        return "Mnemosyne"
    }

    private static func normalized(_ candidate: String?) -> String? {
        guard var name = candidate?.trimmingCharacters(in: .whitespacesAndNewlines),
              !name.isEmpty
        else {
            return nil
        }

        if name.lowercased().hasSuffix(".local") {
            name.removeLast(".local".count)
        }
        return name.isEmpty ? nil : name
    }
}
