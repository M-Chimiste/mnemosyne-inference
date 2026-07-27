import Foundation

public struct HomebrewCommandFailure: Error, LocalizedError, Sendable {
    public let status: Int32
    public let output: String

    public var errorDescription: String? {
        let detail = output.trimmingCharacters(in: .whitespacesAndNewlines)
        return detail.isEmpty
            ? "Homebrew exited with status \(status)."
            : "Homebrew exited with status \(status): \(detail)"
    }
}

public enum HomebrewOMLXInstallerError: Error, LocalizedError, Sendable {
    case invalidExecutable
    case invalidCommand

    public var errorDescription: String? {
        switch self {
        case .invalidExecutable:
            "The requested Homebrew executable is not an approved path."
        case .invalidCommand:
            "The requested Homebrew command is not an approved oMLX install step."
        }
    }
}

public enum HomebrewOMLXInstaller {
    public static let executableCandidates = [
        "/opt/homebrew/bin/brew",
        "/usr/local/bin/brew",
    ]

    public static let commands = [
        ["tap", "jundot/omlx", "https://github.com/jundot/omlx"],
        ["install", "omlx"],
    ]

    public static func executableURL(
        fileManager: FileManager = .default
    ) -> URL? {
        executableCandidates.first {
            fileManager.isExecutableFile(atPath: $0)
        }.map {
            URL(fileURLWithPath: $0)
        }
    }

    public static func run(
        executableURL: URL,
        arguments: [String]
    ) async throws -> String {
        guard executableCandidates.contains(executableURL.path) else {
            throw HomebrewOMLXInstallerError.invalidExecutable
        }
        guard commands.contains(arguments) else {
            throw HomebrewOMLXInstallerError.invalidCommand
        }

        let result = try await Task.detached(priority: .userInitiated) {
            let process = Process()
            process.executableURL = executableURL
            process.arguments = arguments
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = [
                "/opt/homebrew/bin",
                "/opt/homebrew/sbin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ].joined(separator: ":")
            process.environment = environment

            let outputPipe = Pipe()
            process.standardOutput = outputPipe
            process.standardError = outputPipe
            try process.run()
            let data = outputPipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (
                process.terminationStatus,
                String(decoding: data, as: UTF8.self)
            )
        }.value

        guard result.0 == 0 else {
            throw HomebrewCommandFailure(
                status: result.0,
                output: String(result.1.suffix(2_000))
            )
        }
        return result.1
    }
}
