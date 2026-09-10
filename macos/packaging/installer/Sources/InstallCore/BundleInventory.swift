import CryptoKit
import Foundation

public struct InstallFailure: LocalizedError {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

public struct BundleEntry: Codable, Equatable, Sendable {
    public let kind: String
    public let value: String
    public let mode: Int
}

public struct ReleaseManifest: Codable, Sendable {
    public let format: Int
    public let version: String
    public let build: String
    public let team: String
    public let entries: [String: BundleEntry]

    public init(contentsOf url: URL) throws {
        self = try JSONDecoder().decode(Self.self, from: Data(contentsOf: url))
        guard format == 1, team.range(of: "^[A-Z0-9]{10}$", options: .regularExpression) != nil,
              !entries.isEmpty else { throw InstallFailure("The installer release manifest is invalid.") }
    }

    public func verify(_ bundle: URL) throws {
        let actual = try BundleInventory.read(bundle)
        guard actual == entries else {
            let different = Set(actual.keys).union(entries.keys).sorted().first { actual[$0] != entries[$0] } ?? "bundle"
            throw InstallFailure("The application copy does not match this release: \(different). Download a fresh disk image.")
        }
    }
}

public enum BundleInventory {
    public static func read(_ root: URL) throws -> [String: BundleEntry] {
        let fm = FileManager.default
        let rootPath = root.standardizedFileURL.path
        guard root.resolvingSymlinksInPath().path == rootPath else {
            throw InstallFailure("The application folder must not be a symbolic link.")
        }
        var result: [String: BundleEntry] = [:]
        // Enumerate lexical names ourselves. NSDirectoryEnumerator's descendant
        // skipping around framework links can omit a later real Versions directory.
        // Directory symlinks are recorded but never enqueued for traversal.
        var pending: [(URL, String)] = [(root.standardizedFileURL, "")]
        while let (folder, prefix) = pending.popLast() {
          for name in try fm.contentsOfDirectory(atPath: folder.path).sorted() {
            let url = folder.appendingPathComponent(name)
            let relative = prefix + name
            let attributes = try fm.attributesOfItem(atPath: url.path)
            let mode = (attributes[.posixPermissions] as? NSNumber)?.intValue ?? -1
            switch attributes[.type] as? FileAttributeType {
            case .typeSymbolicLink:
                let destination = try fm.destinationOfSymbolicLink(atPath: url.path)
                let resolved = url.resolvingSymlinksInPath().path
                guard !destination.hasPrefix("/"), resolved.hasPrefix(rootPath + "/"),
                      fm.fileExists(atPath: resolved) else {
                    throw InstallFailure("The application contains an external or broken symbolic link: \(relative).")
                }
                result[relative] = BundleEntry(kind: "link", value: destination, mode: mode)
            case .typeDirectory:
                result[relative] = BundleEntry(kind: "directory", value: "", mode: mode)
                pending.append((url, relative + "/"))
            case .typeRegular:
                let handle = try FileHandle(forReadingFrom: url)
                defer { try? handle.close() }
                var hash = SHA256()
                while let chunk = try handle.read(upToCount: 1_048_576), !chunk.isEmpty { hash.update(data: chunk) }
                let digest = hash.finalize().map { String(format: "%02x", $0) }.joined()
                result[relative] = BundleEntry(kind: "file", value: digest, mode: mode)
            default:
                throw InstallFailure("The application contains an unsupported file: \(relative).")
            }
          }
        }
        return result
    }
}
