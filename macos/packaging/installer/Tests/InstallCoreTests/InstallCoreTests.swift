import Foundation
import XCTest
@testable import InstallCore

final class InstallCoreTests: XCTestCase {
    var root: URL!
    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: root) }

    func bundle(_ name: String, _ content: String) throws -> URL {
        let url = root.appendingPathComponent(name)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)
        try Data(content.utf8).write(to: url.appendingPathComponent("executable"))
        return url
    }
    func contents(_ url: URL) throws -> String { try String(contentsOf: url.appendingPathComponent("executable"), encoding: .utf8) }

    func testExistingInstallExchangesWholeDirectoriesAndRetainsOriginalInodes() throws {
        let installed = try bundle("installed.app", "old")
        let candidate = try bundle("candidate.app", "new")
        let old = try FileIdentity.read(installed)
        let new = try FileIdentity.read(candidate)
        let oldFile = try FileManager.default.attributesOfItem(atPath: installed.appendingPathComponent("executable").path)[.systemFileNumber] as? NSNumber
        let tx = try FreshBundleTransaction(candidate: candidate, destination: installed)
        try tx.commit { XCTAssertEqual(try contents(installed), "new") }
        XCTAssertEqual(try FileIdentity.read(installed), new)
        XCTAssertEqual(try FileIdentity.read(candidate), old)
        XCTAssertEqual(try contents(candidate), "old")
        XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: candidate.appendingPathComponent("executable").path)[.systemFileNumber] as? NSNumber, oldFile)
    }

    func testFinalVerificationFailureRestoresOldAppWithoutModifyingEitherBundle() throws {
        let installed = try bundle("installed.app", "old")
        let candidate = try bundle("candidate.app", "new")
        let old = try FileIdentity.read(installed)
        let new = try FileIdentity.read(candidate)
        let tx = try FreshBundleTransaction(candidate: candidate, destination: installed)
        XCTAssertThrowsError(try tx.commit { throw InstallFailure("Gatekeeper rejected") })
        XCTAssertEqual(try FileIdentity.read(installed), old)
        XCTAssertEqual(try FileIdentity.read(candidate), new)
        XCTAssertEqual(try contents(installed), "old")
        XCTAssertEqual(try contents(candidate), "new")
    }

    func testFreshInstallAndFreshInstallFailure() throws {
        let destination = root.appendingPathComponent("installed.app")
        let candidate = try bundle("candidate.app", "new")
        let tx = try FreshBundleTransaction(candidate: candidate, destination: destination)
        XCTAssertThrowsError(try tx.commit { throw InstallFailure("rejected") })
        XCTAssertNil(try FileIdentity.read(destination))
        XCTAssertEqual(try contents(candidate), "new")
        let retry = try FreshBundleTransaction(candidate: candidate, destination: destination)
        try retry.commit {}
        XCTAssertEqual(try contents(destination), "new")
        XCTAssertNil(try FileIdentity.read(candidate))
    }

    func testChangedDestinationIsNeverOverwritten() throws {
        let installed = try bundle("installed.app", "old")
        let candidate = try bundle("candidate.app", "new")
        let tx = try FreshBundleTransaction(candidate: candidate, destination: installed)
        try FileManager.default.moveItem(at: installed, to: root.appendingPathComponent("moved.app"))
        _ = try bundle("installed.app", "other update")
        XCTAssertThrowsError(try tx.commit {})
        XCTAssertEqual(try contents(installed), "other update")
        XCTAssertEqual(try contents(candidate), "new")
    }

    func testChangedDestinationAfterCommitIsPreservedForManualRecovery() throws {
        let installed = try bundle("installed.app", "old")
        let candidate = try bundle("candidate.app", "new")
        let tx = try FreshBundleTransaction(candidate: candidate, destination: installed)
        XCTAssertThrowsError(try tx.commit {
            try FileManager.default.moveItem(at: installed, to: root.appendingPathComponent("moved.app"))
            _ = try bundle("installed.app", "external change")
            throw InstallFailure("verification failed")
        })
        XCTAssertEqual(try contents(installed), "external change")
        XCTAssertEqual(try contents(candidate), "old")
    }

    func testSymlinkTargetRejected() throws {
        let existing = try bundle("existing.app", "old")
        let link = root.appendingPathComponent("installed.app")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: existing)
        let candidate = try bundle("candidate.app", "new")
        XCTAssertThrowsError(try FreshBundleTransaction(candidate: candidate, destination: link))
        XCTAssertEqual(try contents(existing), "old")
    }

    func testFinalIdentityCheckRejectsConcurrentChangeEvenWhenValidationReturnedSuccess() throws {
        let installed = try bundle("installed.app", "old")
        let candidate = try bundle("candidate.app", "new")
        let tx = try FreshBundleTransaction(candidate: candidate, destination: installed)
        XCTAssertThrowsError(try tx.commit {
            try FileManager.default.moveItem(at: installed, to: root.appendingPathComponent("moved.app"))
            _ = try bundle("installed.app", "external change")
        })
        XCTAssertEqual(try contents(installed), "external change")
        XCTAssertEqual(try contents(candidate), "old")
    }

    func testInventoryIncludesHiddenFilesAndSymlinksAndDetectsExtraOrChangedFiles() throws {
        let app = try bundle("app", "new")
        try Data("hidden".utf8).write(to: app.appendingPathComponent(".hidden"))
        try FileManager.default.createSymbolicLink(atPath: app.appendingPathComponent("link").path, withDestinationPath: "executable")
        let baseline = try BundleInventory.read(app)
        XCTAssertEqual(baseline.count, 3)
        XCTAssertEqual(baseline["link"]?.kind, "link", "\(baseline.keys.sorted())")
        XCTAssertEqual(baseline[".hidden"]?.kind, "file")
        try Data("changed".utf8).write(to: app.appendingPathComponent("executable"))
        XCTAssertNotEqual(try BundleInventory.read(app), baseline)
        try Data().write(to: app.appendingPathComponent("extra"))
        XCTAssertEqual(try BundleInventory.read(app).count, 4)
    }

    func testInventoryRejectsLinksOutsideBundleAndBrokenLinks() throws {
        let app = try bundle("app", "new")
        let link = app.appendingPathComponent("link")
        try FileManager.default.createSymbolicLink(atPath: link.path, withDestinationPath: "../outside")
        XCTAssertThrowsError(try BundleInventory.read(app))
        try FileManager.default.removeItem(at: link)
        try FileManager.default.createSymbolicLink(atPath: link.path, withDestinationPath: "absent")
        XCTAssertThrowsError(try BundleInventory.read(app))
    }

    func testFrameworkLinksDoNotHideLaterVersionDirectories() throws {
        let app = try bundle("app", "code")
        let framework = app.appendingPathComponent("Framework.framework")
        let version = framework.appendingPathComponent("Versions/B")
        try FileManager.default.createDirectory(at: version, withIntermediateDirectories: true)
        try Data("framework".utf8).write(to: version.appendingPathComponent("Framework"))
        try FileManager.default.createSymbolicLink(atPath: framework.appendingPathComponent("Framework").path, withDestinationPath: "Versions/Current/Framework")
        try FileManager.default.createSymbolicLink(atPath: framework.appendingPathComponent("Versions/Current").path, withDestinationPath: "B")
        let entries = try BundleInventory.read(app)
        XCTAssertEqual(entries["Framework.framework/Versions/B"]?.kind, "directory")
        XCTAssertEqual(entries["Framework.framework/Versions/B/Framework"]?.kind, "file")
        XCTAssertEqual(entries["Framework.framework/Framework"]?.kind, "link")
        XCTAssertNil(entries["Framework.framework/Versions/Current/Framework"])
    }

    func testConcurrentInstallerLockAndSymlinkLockAreRejected() throws {
        var lock: InstallLock? = try InstallLock(parent: root)
        XCTAssertThrowsError(try InstallLock(parent: root))
        withExtendedLifetime(lock) {}
        lock = nil
        _ = try InstallLock(parent: root)
        let lockURL = root.appendingPathComponent(".Unified-Inference-Install.lock")
        try FileManager.default.removeItem(at: lockURL)
        try FileManager.default.createSymbolicLink(atPath: lockURL.path, withDestinationPath: "not-a-lock")
        XCTAssertThrowsError(try InstallLock(parent: root))
    }

    func testSignedArtifactUpgradeInDisposableApplications() throws {
        let env = ProcessInfo.processInfo.environment
        guard let installerPath = env["MNEMOSYNE_INSTALLER_ACCEPTANCE_BUNDLE"],
              let previousPath = env["MNEMOSYNE_INSTALLER_PREVIOUS_BUNDLE"] else {
            throw XCTSkip("Opt-in signed-artifact acceptance requires the notarized installer and previous bundle.")
        }
        let installer = try XCTUnwrap(Bundle(url: URL(fileURLWithPath: installerPath)))
        let workflow = try InstallWorkflow(installer: installer)
        let payloadEntries = try BundleInventory.read(workflow.payload)
        for key in Set(payloadEntries.keys).union(workflow.manifest.entries.keys).sorted() {
            if payloadEntries[key] != workflow.manifest.entries[key] {
                XCTFail("Inventory mismatch at \(key): actual \(String(describing: payloadEntries[key])), expected \(String(describing: workflow.manifest.entries[key]))")
                return
            }
        }
        let parent = root.appendingPathComponent("Applications", isDirectory: true).standardizedFileURL
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: false)
        let destination = parent.appendingPathComponent(InstallWorkflow.applicationName)
        try VerifiedCommand.run("/usr/bin/ditto", [previousPath, destination.path], timeout: 600)
        let previous = try BundleInventory.read(destination)
        let previousID = try FileIdentity.read(destination)
        let previousExecutable = destination.appendingPathComponent("Contents/MacOS/UnifiedInference")
        let previousFileID = try FileManager.default.attributesOfItem(atPath: previousExecutable.path)[.systemFileNumber] as? NSNumber
        let result = try workflow.install(in: parent, progress: { print("ARTIFACT ACCEPTANCE: \($0)") }, applicationIsRunning: { false })
        try workflow.manifest.verify(result.application)
        let recovery = try XCTUnwrap(result.recoveryFolder)
        let retained = recovery.appendingPathComponent(InstallWorkflow.applicationName)
        XCTAssertEqual(try BundleInventory.read(retained), previous)
        XCTAssertEqual(try FileIdentity.read(retained), previousID)
        XCTAssertNotEqual(try FileIdentity.read(result.application), previousID)
        XCTAssertNotEqual(try FileManager.default.attributesOfItem(atPath: previousExecutable.path)[.systemFileNumber] as? NSNumber, previousFileID)
        let receipt = try JSONDecoder().decode(InstallReceipt.self, from: Data(contentsOf: recovery.appendingPathComponent("Install Receipt.json")))
        XCTAssertEqual(receipt.phase, "installed")
        XCTAssertEqual(receipt.build, workflow.manifest.build)
        print("ARTIFACT ACCEPTANCE: whole-bundle replacement passed; previous payload and inode preserved; new executable inode; final Gatekeeper allowed.")
    }

    func testLargeScanOutputRetainsVerdictAndRejectsAlteredVerdict() throws {
        let output = root.appendingPathComponent("scan.txt")
        let progress = String(repeating: "Progress: 1/10000\r", count: 10_000)
        try (progress + "Scan completed and software is allowed by system policy.\n").write(to: output, atomically: true, encoding: .utf8)
        let tail = try VerifiedCommand.run("/bin/cat", [output.path])
        XCTAssertLessThanOrEqual(tail.utf8.count, 32_768)
        XCTAssertTrue(VerifiedCommand.scanAllowsExecution(tail))
        XCTAssertFalse(VerifiedCommand.scanAllowsExecution("Scan completed, but failed because the software has been altered."))
        XCTAssertFalse(VerifiedCommand.scanAllowsExecution("Progress: 100/100"))
    }

    func testSignedReleasePayloadInventory() throws {
        guard let path = ProcessInfo.processInfo.environment["MNEMOSYNE_INSTALLER_ACCEPTANCE_BUNDLE"] else {
            throw XCTSkip("The packaging builder supplies the signed installer for this artifact check.")
        }
        let workflow = try InstallWorkflow(installer: XCTUnwrap(Bundle(url: URL(fileURLWithPath: path))))
        try VerifiedCommand.verifySignature(workflow.installer.bundleURL, identifier: InstallWorkflow.installerIdentifier, team: workflow.manifest.team)
        try workflow.manifest.verify(workflow.payload)
        try VerifiedCommand.verifySignature(workflow.payload, identifier: InstallWorkflow.applicationIdentifier, team: workflow.manifest.team)
    }
}
