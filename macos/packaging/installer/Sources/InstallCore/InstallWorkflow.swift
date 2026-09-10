import Foundation

public enum VerifiedCommand {
    @discardableResult
    public static func run(_ executable: String, _ arguments: [String], timeout: TimeInterval = 300) throws -> String {
        let outputURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        guard FileManager.default.createFile(atPath: outputURL.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
            throw InstallFailure("Cannot create a temporary verification log.")
        }
        defer { try? FileManager.default.removeItem(at: outputURL) }
        let output = try FileHandle(forWritingTo: outputURL)
        defer { try? output.close() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "LANG": "C"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if process.isRunning {
            process.terminate()
            // Only the exact child created here; no process discovery or port-based signals.
            let stopDeadline = Date().addingTimeInterval(2)
            while process.isRunning && Date() < stopDeadline { Thread.sleep(forTimeInterval: 0.05) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            process.waitUntilExit()
            throw InstallFailure("\(URL(fileURLWithPath: executable).lastPathComponent) timed out. The installation could not be verified.")
        }
        process.waitUntilExit()
        let reader = try FileHandle(forReadingFrom: outputURL)
        defer { try? reader.close() }
        // gktool emits a progress record for each bundle member. Keep the bounded
        // tail, which contains the policy verdict, rather than truncating it away.
        let length = try reader.seekToEnd()
        try reader.seek(toOffset: length > 32_768 ? length - 32_768 : 0)
        let data = try reader.read(upToCount: 32_768) ?? Data()
        let result = String(decoding: data, as: UTF8.self)
        guard process.terminationStatus == 0 else {
            throw InstallFailure("\(URL(fileURLWithPath: executable).lastPathComponent) failed: \(result.trimmingCharacters(in: .whitespacesAndNewlines))")
        }
        return result
    }

    public static func verifySignature(_ app: URL, identifier: String, team: String) throws {
        guard identifier.range(of: "^[A-Za-z0-9.-]+$", options: .regularExpression) != nil,
              team.range(of: "^[A-Z0-9]{10}$", options: .regularExpression) != nil else {
            throw InstallFailure("Invalid signing identity in the release manifest.")
        }
        let requirement = "anchor apple generic and identifier \"\(identifier)\" and certificate leaf[subject.OU] = \"\(team)\" and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
        try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", "-R", "=" + requirement, app.path])
    }

    public static func gatekeeper(_ app: URL) throws {
        let scan = try run("/usr/bin/gktool", ["scan", app.path])
        // gktool can exit successfully while describing a policy rejection.
        guard scanAllowsExecution(scan) else {
            let summary = scan.split(whereSeparator: \.isNewline).filter { !$0.hasPrefix("Progress:") }.suffix(3).joined(separator: "\n")
            throw InstallFailure("Gatekeeper did not allow this copy. \(summary)")
        }
        try run("/usr/sbin/spctl", ["--assess", "--type", "execute", app.path])
    }

    static func scanAllowsExecution(_ output: String) -> Bool {
        output.split(whereSeparator: \.isNewline).contains("Scan completed and software is allowed by system policy.")
    }
}

public struct InstallReceipt: Codable {
    public var phase: String
    public let build: String
    public let incoming: FileIdentity
    public let previous: FileIdentity?
}

public struct InstallResult: Sendable {
    public let application: URL
    public let recoveryFolder: URL?
}

public final class InstallWorkflow: Sendable {
    public static let applicationName = "Unified Inference.app"
    public static let applicationIdentifier = "com.mnemosyne.inference.menu"
    public static let installerIdentifier = "com.mnemosyne.inference.installer"
    public static let applications = URL(fileURLWithPath: "/Applications", isDirectory: true)
    public let installer: Bundle
    public let manifest: ReleaseManifest
    public let payload: URL

    public init(installer: Bundle = .main) throws {
        guard let resources = installer.resourceURL else { throw InstallFailure("The installer is incomplete.") }
        self.installer = installer
        self.manifest = try ReleaseManifest(contentsOf: resources.appendingPathComponent("Release.json"))
        self.payload = resources.appendingPathComponent(Self.applicationName, isDirectory: true)
    }

    /// Production has no command-line/environment destination override, elevated helper,
    /// shell, service API or access to Application Support. Only the fixed app is replaced.
    public func install(progress: (String) -> Void, applicationIsRunning: () -> Bool) throws -> InstallResult {
        try install(in: Self.applications, progress: progress, applicationIsRunning: applicationIsRunning)
    }

    // Internal seam for signed-artifact acceptance in a disposable Applications
    // directory. The shipped UI can only call the fixed-destination public method.
    func install(in parent: URL, progress: (String) -> Void, applicationIsRunning: () -> Bool) throws -> InstallResult {
        let fm = FileManager.default
        let target = parent.appendingPathComponent(Self.applicationName)
        guard parent.resolvingSymlinksInPath() == parent,
              try FileIdentity.read(parent) != nil else {
            throw InstallFailure("Applications must be a real local directory.")
        }
        let lock = try InstallLock(parent: parent)
        defer { withExtendedLifetime(lock) {} }
        // A crash during exchange retains both bundles and blocks a second transaction.
        // Recovery is deliberately explicit; never infer permission to remove either copy.
        for folder in try fm.contentsOfDirectory(at: parent, includingPropertiesForKeys: nil)
            where folder.lastPathComponent.hasPrefix(".Unified-Inference-Recovery-") {
            let receiptURL = folder.appendingPathComponent("Install Receipt.json")
            guard fm.fileExists(atPath: receiptURL.path) else { continue }
            let receipt = try JSONDecoder().decode(InstallReceipt.self, from: Data(contentsOf: receiptURL))
            guard !["replacing", "needs_review"].contains(receipt.phase) else {
                throw InstallFailure("An interrupted installation needs review first. Both bundles have been retained. Recovery folder: \(folder.path)")
            }
        }
        guard !applicationIsRunning() else {
            throw InstallFailure("Quit Unified Inference, then choose Install again. Its background service can remain enabled.")
        }
        progress("Checking the signed release…")
        try VerifiedCommand.verifySignature(installer.bundleURL, identifier: Self.installerIdentifier, team: manifest.team)
        try manifest.verify(payload)
        try VerifiedCommand.verifySignature(payload, identifier: Self.applicationIdentifier, team: manifest.team)
        let previous = try FileIdentity.read(target)
        if previous != nil {
            try VerifiedCommand.verifySignature(target, identifier: Self.applicationIdentifier, team: manifest.team)
        }
        let recovery = parent.appendingPathComponent(".Unified-Inference-Recovery-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: recovery, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        let staged = recovery.appendingPathComponent(Self.applicationName, isDirectory: true)
        progress("Creating a fresh application copy…")
        try VerifiedCommand.run("/usr/bin/ditto", [payload.path, staged.path], timeout: 600)
        progress("Verifying every copied file and checking Gatekeeper…")
        try manifest.verify(staged)
        try VerifiedCommand.verifySignature(staged, identifier: Self.applicationIdentifier, team: manifest.team)
        try VerifiedCommand.gatekeeper(staged)
        guard !applicationIsRunning(), try FileIdentity.read(target) == previous else {
            throw InstallFailure("The installed app opened or changed during preparation. Quit it and try again. The verified candidate is retained at \(recovery.path).")
        }
        let transaction = try FreshBundleTransaction(candidate: staged, destination: target)
        var receipt = InstallReceipt(phase: "replacing", build: manifest.build, incoming: transaction.incoming, previous: previous)
        let receiptURL = recovery.appendingPathComponent("Install Receipt.json")
        try JSONEncoder().encode(receipt).write(to: receiptURL, options: .atomic)
        progress("Installing the verified copy…")
        do {
            try transaction.commit {
                progress("Checking the installed application…")
                try self.manifest.verify(target)
                try VerifiedCommand.verifySignature(target, identifier: Self.applicationIdentifier, team: self.manifest.team)
                try VerifiedCommand.gatekeeper(target)
            }
        } catch {
            do { receipt.phase = try FileIdentity.read(target) == previous ? "rolled_back" : "needs_review" }
            catch { receipt.phase = "needs_review" }
            try? JSONEncoder().encode(receipt).write(to: receiptURL, options: .atomic)
            throw error
        }
        receipt.phase = "installed"
        // If writing completion fails, the conservative 'replacing' receipt keeps the
        // next installer from making another change until the retained copies are reviewed.
        try JSONEncoder().encode(receipt).write(to: receiptURL, options: .atomic)
        return InstallResult(application: target, recoveryFolder: previous == nil ? nil : recovery)
    }
}
