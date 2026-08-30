import Foundation
import CryptoKit
import Darwin

private enum TrashError: Error, CustomStringConvertible {
    case invalidArguments(String)
    case invalidRoot(String)
    case unsafePath(String)
    case rollbackFailed(String)

    var description: String {
        switch self {
        case let .invalidArguments(message),
             let .invalidRoot(message),
             let .unsafePath(message),
             let .rollbackFailed(message):
            message
        }
    }
}

private struct Arguments {
    let root: String
    let expectedVolumeUUID: String?
    let paths: [String]
    let verifyManifestFromStandardInput: Bool
    let expectedDirectoryDevice: UInt64?
    let expectedDirectoryInode: UInt64?
}

private struct ManifestFile: Decodable {
    let path: String
    let sizeBytes: Int64
    let sha256: String

    private enum CodingKeys: String, CodingKey {
        case path
        case sizeBytes = "size_bytes"
        case sha256
    }
}

private struct TrashResponse: Encodable {
    let ok: Bool
    let trashed: [String]?
    let skipped: [String]?
    let error: String?
}

private struct TrashedItem {
    let original: URL
    let trashed: URL
}

private func parseArguments() throws -> Arguments {
    var root: String?
    var expectedVolumeUUID: String?
    var paths: [String] = []
    var verifyManifestFromStandardInput = false
    var expectedDirectoryDevice: UInt64?
    var expectedDirectoryInode: UInt64?
    var index = 1
    while index < CommandLine.arguments.count {
        let argument = CommandLine.arguments[index]
        if argument == "--verify-manifest-stdin" {
            guard !verifyManifestFromStandardInput else {
                throw TrashError.invalidArguments(
                    "--verify-manifest-stdin may be provided only once"
                )
            }
            verifyManifestFromStandardInput = true
            index += 1
            continue
        }
        guard index + 1 < CommandLine.arguments.count else {
            throw TrashError.invalidArguments("\(argument) requires a value")
        }
        let value = CommandLine.arguments[index + 1]
        switch argument {
        case "--root":
            guard root == nil else {
                throw TrashError.invalidArguments("--root may be provided only once")
            }
            root = value
        case "--expected-volume-uuid":
            guard expectedVolumeUUID == nil else {
                throw TrashError.invalidArguments(
                    "--expected-volume-uuid may be provided only once"
                )
            }
            expectedVolumeUUID = value
        case "--path":
            paths.append(value)
        case "--expected-directory-device":
            guard expectedDirectoryDevice == nil,
                  let parsed = UInt64(value), parsed > 0
            else {
                throw TrashError.invalidArguments(
                    "--expected-directory-device must be one positive integer"
                )
            }
            expectedDirectoryDevice = parsed
        case "--expected-directory-inode":
            guard expectedDirectoryInode == nil,
                  let parsed = UInt64(value), parsed > 0
            else {
                throw TrashError.invalidArguments(
                    "--expected-directory-inode must be one positive integer"
                )
            }
            expectedDirectoryInode = parsed
        default:
            throw TrashError.invalidArguments("unknown argument: \(argument)")
        }
        index += 2
    }
    guard let root, !root.isEmpty else {
        throw TrashError.invalidArguments("--root is required")
    }
    guard !paths.isEmpty else {
        throw TrashError.invalidArguments("at least one --path is required")
    }
    if verifyManifestFromStandardInput && paths.count != 1 {
        throw TrashError.invalidArguments(
            "manifest verification requires exactly one model destination"
        )
    }
    guard (expectedDirectoryDevice == nil) == (expectedDirectoryInode == nil)
    else {
        throw TrashError.invalidArguments(
            "expected directory device and inode must be provided together"
        )
    }
    if expectedDirectoryDevice != nil && !verifyManifestFromStandardInput {
        throw TrashError.invalidArguments(
            "expected directory identity requires manifest verification"
        )
    }
    return Arguments(
        root: root,
        expectedVolumeUUID: expectedVolumeUUID,
        paths: paths,
        verifyManifestFromStandardInput: verifyManifestFromStandardInput,
        expectedDirectoryDevice: expectedDirectoryDevice,
        expectedDirectoryInode: expectedDirectoryInode
    )
}

private func isDescendant(_ candidate: URL, of root: URL) -> Bool {
    let rootComponents = root.standardizedFileURL.pathComponents
    let candidateComponents = candidate.standardizedFileURL.pathComponents
    return candidateComponents.count > rootComponents.count
        && candidateComponents.starts(with: rootComponents)
}

private func validateRoot(
    _ arguments: Arguments
) throws -> (lexical: URL, resolved: URL, volumeUUID: String?) {
    let fileManager = FileManager.default
    let lexical = URL(fileURLWithPath: arguments.root).standardizedFileURL
    guard lexical.path != "/" else {
        throw TrashError.invalidRoot("refusing to use the filesystem root as model storage")
    }
    var isDirectory: ObjCBool = false
    guard fileManager.fileExists(atPath: lexical.path, isDirectory: &isDirectory),
          isDirectory.boolValue
    else {
        throw TrashError.invalidRoot("selected model storage is unavailable")
    }
    let values = try lexical.resourceValues(forKeys: [.volumeUUIDStringKey])
    let actualVolumeUUID = values.volumeUUIDString
    if let expected = arguments.expectedVolumeUUID?.trimmingCharacters(
        in: .whitespacesAndNewlines
    ), !expected.isEmpty {
        guard let actual = actualVolumeUUID,
              actual.caseInsensitiveCompare(expected) == .orderedSame
        else {
            throw TrashError.invalidRoot(
                "selected model storage is not on the volume originally registered"
            )
        }
    }
    return (
        lexical,
        lexical.resolvingSymlinksInPath().standardizedFileURL,
        actualVolumeUUID
    )
}

private func validateTarget(
    _ value: String,
    lexicalRoot: URL,
    resolvedRoot: URL,
    rootVolumeUUID: String?
) throws -> URL? {
    let fileManager = FileManager.default
    let target = URL(fileURLWithPath: value).standardizedFileURL
    let pathRoot: URL
    if isDescendant(target, of: lexicalRoot) {
        pathRoot = lexicalRoot
    } else if isDescendant(target, of: resolvedRoot) {
        // The service deliberately preserves a user-selected lexical root,
        // including a symlink, while its bounded scanner reports canonical
        // child paths. The root grant remains the authority in either form.
        pathRoot = resolvedRoot
    } else {
        throw TrashError.unsafePath("path escapes selected model storage: \(target.path)")
    }

    let relativeComponents = target.pathComponents.dropFirst(
        pathRoot.pathComponents.count
    )
    var cursor = pathRoot
    for component in relativeComponents {
        cursor.append(path: component)
        if !fileManager.fileExists(atPath: cursor.path) {
            return nil
        }
        let attributes = try fileManager.attributesOfItem(atPath: cursor.path)
        if attributes[.type] as? FileAttributeType == .typeSymbolicLink {
            throw TrashError.unsafePath(
                "refusing to trash a model path containing a symlink: \(cursor.path)"
            )
        }
    }

    let resolvedTarget = target.resolvingSymlinksInPath().standardizedFileURL
    guard isDescendant(resolvedTarget, of: resolvedRoot) else {
        throw TrashError.unsafePath(
            "resolved model path escapes selected storage: \(target.path)"
        )
    }
    if let rootVolumeUUID {
        let targetVolumeUUID = try resolvedTarget.resourceValues(
            forKeys: [.volumeUUIDStringKey]
        ).volumeUUIDString
        guard targetVolumeUUID?.caseInsensitiveCompare(rootVolumeUUID)
            == .orderedSame
        else {
            throw TrashError.unsafePath(
                "model cleanup target is on a different volume: \(target.path)"
            )
        }
    }
    return target
}

private func validatedTargets(_ arguments: Arguments) throws -> ([URL], [String]) {
    let roots = try validateRoot(arguments)
    var targets: [URL] = []
    var skipped: [String] = []
    var seen: Set<String> = []
    for value in arguments.paths {
        if !seen.insert(URL(fileURLWithPath: value).standardizedFileURL.path).inserted {
            continue
        }
        if let target = try validateTarget(
            value,
            lexicalRoot: roots.lexical,
            resolvedRoot: roots.resolved,
            rootVolumeUUID: roots.volumeUUID
        ) {
            targets.append(target)
        } else {
            skipped.append(value)
        }
    }
    for (index, target) in targets.enumerated() {
        for other in targets.dropFirst(index + 1) {
            if isDescendant(target, of: other) || isDescendant(other, of: target) {
                throw TrashError.unsafePath(
                    "refusing overlapping model cleanup targets"
                )
            }
        }
    }
    return (targets, skipped)
}

private func readManifest() throws -> [ManifestFile] {
    let maximumBytes = 1_048_576
    var data = Data()
    while data.count <= maximumBytes {
        let remaining = maximumBytes + 1 - data.count
        guard let chunk = try FileHandle.standardInput.read(
            upToCount: min(64 * 1_024, remaining)
        ), !chunk.isEmpty else {
            break
        }
        data.append(chunk)
    }
    guard !data.isEmpty, data.count <= maximumBytes else {
        throw TrashError.unsafePath("managed model manifest is missing or too large")
    }
    let files: [ManifestFile]
    do {
        files = try JSONDecoder().decode([ManifestFile].self, from: data)
    } catch {
        throw TrashError.unsafePath("managed model manifest is invalid")
    }
    guard !files.isEmpty, files.count <= 4_096 else {
        throw TrashError.unsafePath("managed model manifest is invalid")
    }
    var seen: Set<String> = []
    for file in files {
        let utf8Count = file.path.lengthOfBytes(using: .utf8)
        let parts = file.path.split(separator: "/", omittingEmptySubsequences: false)
        let digest = file.sha256.dropFirst("sha256:".count)
        guard utf8Count > 0,
              utf8Count <= 512,
              !file.path.hasPrefix("/"),
              !file.path.contains("\\"),
              !parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }),
              file.sizeBytes >= 0,
              file.sha256.hasPrefix("sha256:"),
              digest.utf8.count == 64,
              digest.utf8.allSatisfy({ byte in
                  (48...57).contains(byte) || (97...102).contains(byte)
              })
        else {
            throw TrashError.unsafePath("managed model manifest is invalid")
        }
        guard seen.insert(file.path).inserted else {
            throw TrashError.unsafePath("managed model manifest is invalid")
        }
    }
    let sortedPaths = files.map(\.path).sorted()
    for (index, path) in sortedPaths.enumerated() {
        for other in sortedPaths.dropFirst(index + 1) {
            if path.hasPrefix(other + "/") || other.hasPrefix(path + "/") {
                throw TrashError.unsafePath("managed model manifest is invalid")
            }
        }
    }
    return files
}

private func directoryIdentity(_ target: URL) throws -> (UInt64, UInt64) {
    var metadata = stat()
    guard lstat(target.path, &metadata) == 0 else {
        throw TrashError.unsafePath(
            "managed model destination identity is unavailable"
        )
    }
    guard (metadata.st_mode & S_IFMT) == S_IFDIR else {
        throw TrashError.unsafePath(
            "managed model destination is not a directory"
        )
    }
    return (UInt64(metadata.st_dev), UInt64(metadata.st_ino))
}

private func sha256OfRegularFile(
    _ path: URL,
    expectedSize: Int64
) throws -> String {
    let descriptor = open(path.path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
    guard descriptor >= 0 else {
        throw TrashError.unsafePath(
            "managed model file could not be opened without following links"
        )
    }
    defer { close(descriptor) }

    var before = stat()
    guard fstat(descriptor, &before) == 0,
          (before.st_mode & S_IFMT) == S_IFREG,
          before.st_size == expectedSize
    else {
        throw TrashError.unsafePath("managed model file identity changed")
    }

    var hasher = SHA256()
    var buffer = [UInt8](repeating: 0, count: 1_048_576)
    var totalRead: Int64 = 0
    while true {
        let count = buffer.withUnsafeMutableBytes { rawBuffer in
            read(descriptor, rawBuffer.baseAddress, rawBuffer.count)
        }
        if count < 0 {
            if errno == EINTR { continue }
            throw TrashError.unsafePath("managed model file could not be hashed")
        }
        if count == 0 { break }
        totalRead += Int64(count)
        hasher.update(data: Data(buffer[0..<count]))
    }

    var after = stat()
    guard fstat(descriptor, &after) == 0,
          before.st_dev == after.st_dev,
          before.st_ino == after.st_ino,
          before.st_size == after.st_size,
          totalRead == expectedSize
    else {
        throw TrashError.unsafePath("managed model file changed while hashing")
    }
    let hexadecimal = hasher.finalize().map {
        String(format: "%02x", $0)
    }.joined()
    return "sha256:" + hexadecimal
}

private func verifyManifest(
    _ files: [ManifestFile],
    at target: URL,
    expectedDirectoryDevice: UInt64?,
    expectedDirectoryInode: UInt64?
) throws {
    let fileManager = FileManager.default
    let initialIdentity = try directoryIdentity(target)
    if let expectedDirectoryDevice, let expectedDirectoryInode {
        guard initialIdentity.0 == expectedDirectoryDevice,
              initialIdentity.1 == expectedDirectoryInode
        else {
            throw TrashError.unsafePath(
                "managed model destination directory identity changed"
            )
        }
    }
    let rootValues = try target.resourceValues(
        forKeys: [.isDirectoryKey, .isSymbolicLinkKey]
    )
    guard rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
        throw TrashError.unsafePath("managed model destination is not a directory")
    }

    let expectedFiles = Dictionary(
        uniqueKeysWithValues: files.map { ($0.path, $0) }
    )
    var expectedDirectories: Set<String> = []
    for file in files {
        let components = file.path.split(separator: "/").map(String.init)
        if components.count > 1 {
            for count in 1..<components.count {
                expectedDirectories.insert(components.prefix(count).joined(separator: "/"))
            }
        }
    }

    let keys: [URLResourceKey] = [
        .isDirectoryKey,
        .isRegularFileKey,
        .isSymbolicLinkKey,
        .fileSizeKey,
    ]
    var enumerationFailed = false
    guard let enumerator = fileManager.enumerator(
        at: target,
        includingPropertiesForKeys: keys,
        options: [],
        errorHandler: { _, _ in
            enumerationFailed = true
            return false
        }
    ) else {
        throw TrashError.unsafePath("managed model manifest could not be inspected")
    }
    let rootComponents = target.standardizedFileURL.pathComponents
    var observedFiles: Set<String> = []
    while let item = enumerator.nextObject() as? URL {
        let standardized = item.standardizedFileURL
        let components = standardized.pathComponents
        guard components.count > rootComponents.count,
              components.starts(with: rootComponents)
        else {
            throw TrashError.unsafePath("managed model manifest escaped its destination")
        }
        let relative = components.dropFirst(rootComponents.count).joined(separator: "/")
        let values = try standardized.resourceValues(forKeys: Set(keys))
        if values.isSymbolicLink == true {
            enumerator.skipDescendants()
            throw TrashError.unsafePath(
                "managed model manifest contains a symlink: \(relative)"
            )
        }
        if values.isDirectory == true {
            guard expectedDirectories.contains(relative) else {
                throw TrashError.unsafePath(
                    "managed model manifest contains an extra entry: \(relative)"
                )
            }
            continue
        }
        guard values.isRegularFile == true else {
            throw TrashError.unsafePath(
                "managed model manifest contains a special entry: \(relative)"
            )
        }
        guard let expected = expectedFiles[relative] else {
            throw TrashError.unsafePath(
                "managed model manifest contains an extra entry: \(relative)"
            )
        }
        guard Int64(values.fileSize ?? -1) == expected.sizeBytes else {
            throw TrashError.unsafePath(
                "managed model file size changed: \(relative)"
            )
        }
        guard try sha256OfRegularFile(
            standardized,
            expectedSize: expected.sizeBytes
        ) == expected.sha256 else {
            throw TrashError.unsafePath(
                "managed model file digest changed: \(relative)"
            )
        }
        observedFiles.insert(relative)
    }
    guard !enumerationFailed, observedFiles == Set(expectedFiles.keys) else {
        throw TrashError.unsafePath(
            "managed model manifest is missing a proven file"
        )
    }
    guard try directoryIdentity(target) == initialIdentity else {
        throw TrashError.unsafePath(
            "managed model destination directory identity changed"
        )
    }
}

private func trash(_ arguments: Arguments) throws -> TrashResponse {
    let fileManager = FileManager.default
    let (targets, skipped) = try validatedTargets(arguments)
    let manifest = arguments.verifyManifestFromStandardInput
        ? try readManifest()
        : nil
    let verificationRoots = manifest == nil ? nil : try validateRoot(arguments)
    var moved: [TrashedItem] = []
    do {
        for target in targets {
            if let manifest, let verificationRoots {
                // Revalidate in the process that performs the move, directly
                // before FileManager hands the exact directory to Trash. This
                // repeats the descendant-symlink and volume checks after stdin
                // parsing instead of relying on the earlier target snapshot.
                guard let revalidated = try validateTarget(
                    target.path,
                    lexicalRoot: verificationRoots.lexical,
                    resolvedRoot: verificationRoots.resolved,
                    rootVolumeUUID: verificationRoots.volumeUUID
                ), revalidated.standardizedFileURL == target.standardizedFileURL
                else {
                    throw TrashError.unsafePath(
                        "managed model destination changed before Trash"
                    )
                }
                try verifyManifest(
                    manifest,
                    at: revalidated,
                    expectedDirectoryDevice: arguments.expectedDirectoryDevice,
                    expectedDirectoryInode: arguments.expectedDirectoryInode
                )
            }
            var result: NSURL?
            try fileManager.trashItem(at: target, resultingItemURL: &result)
            guard let trashed = result as URL? else {
                throw TrashError.unsafePath(
                    "Trash did not return a recovery location for \(target.lastPathComponent)"
                )
            }
            moved.append(TrashedItem(original: target, trashed: trashed))
        }
    } catch {
        var rollbackErrors: [String] = []
        for item in moved.reversed() {
            do {
                try fileManager.moveItem(at: item.trashed, to: item.original)
            } catch {
                rollbackErrors.append(item.original.lastPathComponent)
            }
        }
        if !rollbackErrors.isEmpty {
            throw TrashError.rollbackFailed(
                "model cleanup failed and could not restore: "
                    + rollbackErrors.joined(separator: ", ")
            )
        }
        throw error
    }
    return TrashResponse(
        ok: true,
        trashed: moved.map(\.original.path),
        skipped: skipped,
        error: nil
    )
}

private func emit(_ response: TrashResponse, exitCode: Int32) -> Never {
    let encoder = JSONEncoder()
    let data = (try? encoder.encode(response))
        ?? Data(#"{"ok":false,"error":"could not encode Trash result"}"#.utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
    exit(exitCode)
}

do {
    emit(try trash(parseArguments()), exitCode: 0)
} catch {
    emit(
        TrashResponse(
            ok: false,
            trashed: nil,
            skipped: nil,
            error: String(describing: error)
        ),
        exitCode: 2
    )
}
