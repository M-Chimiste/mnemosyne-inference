import Darwin
import Foundation

public struct AppleDeveloperToolsCommandFailure: Error, LocalizedError, Sendable {
    public let status: Int32
    public let output: String

    public var errorDescription: String? {
        let detail = output.trimmingCharacters(in: .whitespacesAndNewlines)
        return detail.isEmpty
            ? "Apple's developer-tools request exited with status \(status)."
            : "Apple's developer-tools request exited with status \(status): \(detail)"
    }
}

public enum AppleDeveloperToolsInstallerError: Error, LocalizedError, Sendable {
    case invalidExecutable
    case invalidCommand
    case timedOut

    public var errorDescription: String? {
        switch self {
        case .invalidExecutable:
            "The requested developer-tools executable is not an approved Apple system tool."
        case .invalidCommand:
            "The requested developer-tools command is not an approved status or installation step."
        case .timedOut:
            "Apple's developer-tools request did not finish before the safety deadline."
        }
    }
}

/// Bounded access to Apple's own GUI Command Line Tools installer.
///
/// DS4 is built from an exact upstream commit and therefore needs Apple's
/// compiler toolchain. The app invokes only xcode-select's status probe or
/// installation dialog; it never runs a shell, accepts arbitrary arguments,
/// or installs a repository-owned compiler.
public enum AppleDeveloperToolsInstaller {
    public static let executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
    public static let compilerProbeExecutableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
    public static let statusArguments = ["--print-path"]
    public static let compilerProbeArguments = ["--find", "clang"]
    public static let installArguments = ["--install"]
    public static let operationTimeout: Duration = .seconds(15)

    public static func isInstalled() async -> Bool {
        guard let output = try? await run(
            executableURL: executableURL,
            arguments: statusArguments
        ) else { return false }
        let path = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            path.hasPrefix("/"),
            !path.contains("\n"),
            FileManager.default.fileExists(atPath: path),
            FileManager.default.isExecutableFile(atPath: "/usr/bin/make"),
            let clangOutput = try? await run(
                executableURL: compilerProbeExecutableURL,
                arguments: compilerProbeArguments
            )
        else { return false }
        let clang = clangOutput.trimmingCharacters(in: .whitespacesAndNewlines)
        return clang.hasPrefix("/")
            && !clang.contains("\n")
            && FileManager.default.isExecutableFile(atPath: clang)
    }

    public static func requestInstallation() async throws {
        _ = try await run(
            executableURL: executableURL,
            arguments: installArguments
        )
    }

    public static func run(
        executableURL: URL,
        arguments: [String]
    ) async throws -> String {
        let executableIsApproved = [
            self.executableURL.path,
            compilerProbeExecutableURL.path,
        ].contains(executableURL.path)
        guard executableIsApproved else {
            throw AppleDeveloperToolsInstallerError.invalidExecutable
        }
        let commandIsApproved =
            (
                executableURL.path == self.executableURL.path
                    && (arguments == statusArguments || arguments == installArguments)
            )
            || (
                executableURL.path == compilerProbeExecutableURL.path
                    && arguments == compilerProbeArguments
            )
        guard commandIsApproved else {
            throw AppleDeveloperToolsInstallerError.invalidCommand
        }

        return try await runLaunchedProcess(
            executableURL: executableURL,
            arguments: arguments,
            timeout: operationTimeout
        )
    }

    /// Runs an already validated command with a bounded child lifetime.
    ///
    /// This is internal so tests can prove timeout and cancellation behavior
    /// without weakening the public Apple-command allowlist.
    static func runLaunchedProcess(
        executableURL: URL,
        arguments: [String],
        timeout: Duration
    ) async throws -> String {
        try Task.checkCancellation()
        let process = Process()
        process.executableURL = executableURL
        process.arguments = arguments
        let outputPipe = Pipe()
        process.standardOutput = outputPipe
        process.standardError = outputPipe
        try process.run()

        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        do {
            while process.isRunning {
                try Task.checkCancellation()
                guard clock.now < deadline else {
                    throw AppleDeveloperToolsInstallerError.timedOut
                }
                try await Task.sleep(for: .milliseconds(25))
            }
        } catch {
            terminateAndReapExactChild(process)
            throw error
        }

        process.waitUntilExit()
        let data = outputPipe.fileHandleForReading.readDataToEndOfFile()
        let result = (
            process.terminationStatus,
            String(decoding: data, as: UTF8.self)
        )

        guard result.0 == 0 else {
            throw AppleDeveloperToolsCommandFailure(
                status: result.0,
                output: String(result.1.suffix(2_000))
            )
        }
        return result.1
    }

    /// Stop only the `Process` instance launched above. A child cannot have
    /// its PID reused until it has been reaped, so the exact PID remains safe
    /// to escalate from TERM to KILL if it ignores cancellation or timeout.
    private static func terminateAndReapExactChild(_ process: Process) {
        guard process.isRunning else {
            process.waitUntilExit()
            return
        }
        let pid = process.processIdentifier
        guard pid > 0 else { return }
        _ = Darwin.kill(pid, SIGTERM)
        if process.isRunning {
            _ = Darwin.kill(pid, SIGKILL)
        }
        process.waitUntilExit()
    }
}
