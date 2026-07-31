import Foundation

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
    var index = 1
    while index < CommandLine.arguments.count {
        let argument = CommandLine.arguments[index]
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
    return Arguments(
        root: root,
        expectedVolumeUUID: expectedVolumeUUID,
        paths: paths
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

private func trash(_ arguments: Arguments) throws -> TrashResponse {
    let fileManager = FileManager.default
    let (targets, skipped) = try validatedTargets(arguments)
    var moved: [TrashedItem] = []
    do {
        for target in targets {
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
