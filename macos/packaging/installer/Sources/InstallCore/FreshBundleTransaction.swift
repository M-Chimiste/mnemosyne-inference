import Darwin
import Foundation

public struct FileIdentity: Codable, Equatable {
    public let device: Int32
    public let inode: UInt64

    public static func read(_ url: URL) throws -> Self? {
        var info = stat()
        guard lstat(url.path, &info) == 0 else {
            if errno == ENOENT { return nil }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        guard info.st_mode & S_IFMT == S_IFDIR else {
            throw InstallFailure("Expected a real application directory at \(url.path).")
        }
        return Self(device: info.st_dev, inode: info.st_ino)
    }
}

/// A same-volume directory exchange. It never writes into an existing bundle.
/// The caller validates both identities and the complete candidate before commit.
public final class FreshBundleTransaction {
    public let candidate: URL
    public let destination: URL
    public let previous: FileIdentity?
    public let incoming: FileIdentity
    private var exchanged = false
    private var retained: FileIdentity?

    public init(candidate: URL, destination: URL) throws {
        guard candidate.standardizedFileURL != destination.standardizedFileURL,
              let incoming = try FileIdentity.read(candidate) else {
            throw InstallFailure("A separate, complete candidate is required.")
        }
        self.candidate = candidate
        self.destination = destination
        self.incoming = incoming
        self.previous = try FileIdentity.read(destination)
    }

    public func commit(validateInstalled: () throws -> Void) throws {
        guard !exchanged,
              try FileIdentity.read(candidate) == incoming,
              try FileIdentity.read(destination) == previous else {
            throw InstallFailure("The application changed while preparing this update. Nothing was replaced; try again after other installers finish.")
        }
        let flags = previous == nil ? UInt32(RENAME_EXCL) : UInt32(RENAME_SWAP)
        guard renameatx_np(AT_FDCWD, candidate.path, AT_FDCWD, destination.path, flags) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        exchanged = true
        do {
            retained = try FileIdentity.read(candidate)
            guard retained == previous else {
                throw InstallFailure("Another installer changed the previous application during replacement.")
            }
            guard try FileIdentity.read(destination) == incoming else {
                throw InstallFailure("Another installer changed the destination during verification.")
            }
            try validateInstalled()
            guard try FileIdentity.read(destination) == incoming,
                  try FileIdentity.read(candidate) == retained else {
                throw InstallFailure("A bundle changed during the final installed-app check.")
            }
        } catch {
            let original = error
            do { try rollback() }
            catch {
                throw InstallFailure("Installation needs manual recovery. No bundles were deleted. Keep \(candidate.deletingLastPathComponent().path). \(original.localizedDescription) Recovery: \(error.localizedDescription)")
            }
            let recovery = previous == nil ? "The failed new installation was returned to its staging folder." : "The previous installation was restored."
            throw InstallFailure("The update was not activated. \(recovery) \(original.localizedDescription)")
        }
    }

    private func rollback() throws {
        guard try FileIdentity.read(destination) == incoming,
              try FileIdentity.read(candidate) == retained else {
            throw InstallFailure("A bundle changed after replacement; automatic recovery would overwrite an unknown change.")
        }
        let flags = retained == nil ? UInt32(RENAME_EXCL) : UInt32(RENAME_SWAP)
        guard renameatx_np(AT_FDCWD, destination.path, AT_FDCWD, candidate.path, flags) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        exchanged = false
    }
}

/// flock is released by the kernel on exit, including a crash. No stale PID guessing.
public final class InstallLock {
    private let descriptor: Int32
    public init(parent: URL) throws {
        descriptor = open(parent.appendingPathComponent(".Unified-Inference-Install.lock").path,
                          O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else {
            throw InstallFailure("Your account cannot install in Applications. Ask an administrator to run this installer.")
        }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            close(descriptor)
            throw InstallFailure("Another Unified Inference installer is already running.")
        }
    }
    deinit { close(descriptor) }
}
