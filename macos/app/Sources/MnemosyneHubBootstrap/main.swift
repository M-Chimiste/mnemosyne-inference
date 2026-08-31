import Darwin
import Foundation

private enum BootstrapError: Error, CustomStringConvertible {
    case outerBundleNotFound(URL)
    case pythonNotFound(URL)
    case sourceNotFound(URL)
    case configurationMissing(URL)
    case environmentInvalid(URL)

    var description: String {
        switch self {
        case let .outerBundleNotFound(executable):
            "could not locate Unified Inference.app from \(executable.path)"
        case let .pythonNotFound(resources):
            "bundled Python was not found below \(resources.path)"
        case let .sourceNotFound(resources):
            "bundled mnemosyne_fleet sources were not found below \(resources.path)"
        case let .configurationMissing(path):
            "Hub Mode is not configured at \(path.path)"
        case let .environmentInvalid(path):
            "Hub Mode private environment is invalid at \(path.path)"
        }
    }
}

private struct PythonRuntime {
    let executable: URL
    let home: URL?
}

private func executableURL() -> URL {
    var size: UInt32 = 0
    _ = _NSGetExecutablePath(nil, &size)
    var buffer = [CChar](repeating: 0, count: Int(size) + 1)
    _ = _NSGetExecutablePath(&buffer, &size)
    let bytes = buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }
    return URL(fileURLWithPath: String(decoding: bytes, as: UTF8.self))
        .resolvingSymlinksInPath()
}

private func outerContents(startingAt executable: URL) throws -> URL {
    let manager = FileManager.default
    var cursor = executable.deletingLastPathComponent()
    for _ in 0 ..< 10 {
        let resources = cursor.appending(path: "Resources", directoryHint: .isDirectory)
        if cursor.lastPathComponent == "Contents",
           manager.fileExists(
               atPath: resources.appending(path: "Fleet/mnemosyne_fleet").path
           )
        {
            return cursor
        }
        cursor.deleteLastPathComponent()
    }
    throw BootstrapError.outerBundleNotFound(executable)
}

private func bundledPython(in resources: URL) throws -> PythonRuntime {
    let manager = FileManager.default
    let root = resources.appending(path: "Python", directoryHint: .isDirectory)
    let direct = root.appending(path: "bin/python3")
    if manager.isExecutableFile(atPath: direct.path) {
        return PythonRuntime(executable: direct, home: root)
    }
    let children = try? manager.contentsOfDirectory(
        at: root,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    )
    for child in (children ?? []).sorted(by: {
        $0.lastPathComponent < $1.lastPathComponent
    }) where child.lastPathComponent.hasPrefix("cpython-") {
        let candidate = child.appending(path: "bin/python3")
        if manager.isExecutableFile(atPath: candidate.path) {
            return PythonRuntime(executable: candidate, home: child)
        }
    }
    if let override = ProcessInfo.processInfo.environment["MNEMOSYNE_PYTHON_OVERRIDE"] {
        let candidate = URL(fileURLWithPath: override).standardizedFileURL
        if manager.isExecutableFile(atPath: candidate.path) {
            return PythonRuntime(executable: candidate, home: nil)
        }
    }
    throw BootstrapError.pythonNotFound(resources)
}

private func sitePackages(in resources: URL) -> [String] {
    let manager = FileManager.default
    let root = resources.appending(path: "Python", directoryHint: .isDirectory)
    var paths: [String] = []
    let customize = root.appending(path: "__venvstacks__/site-customize")
    if manager.fileExists(atPath: customize.path) { paths.append(customize.path) }
    let layer = root.appending(
        path: "framework-mnemosyne-base",
        directoryHint: .isDirectory
    )
    let lib = layer.appending(path: "lib", directoryHint: .isDirectory)
    let versions = (try? manager.contentsOfDirectory(
        at: lib,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    )) ?? []
    for version in versions where version.lastPathComponent.hasPrefix("python") {
        let path = version.appending(path: "site-packages", directoryHint: .isDirectory)
        if manager.fileExists(atPath: path.path) { paths.append(path.path) }
    }
    return paths
}

private func privatePaths() throws -> (config: URL, environment: URL) {
    let root = FileManager.default.urls(
        for: .applicationSupportDirectory,
        in: .userDomainMask
    )[0]
        .appending(path: "Mnemosyne", directoryHint: .isDirectory)
        .appending(path: "hub", directoryHint: .isDirectory)
    let config = root.appending(path: "config.toml")
    let environment = root.appending(path: ".env")
    guard FileManager.default.fileExists(atPath: config.path) else {
        throw BootstrapError.configurationMissing(config)
    }
    guard FileManager.default.fileExists(atPath: environment.path) else {
        throw BootstrapError.environmentInvalid(environment)
    }
    for path in [root, config, environment] {
        let values = try path.resourceValues(forKeys: [
            .isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey,
        ])
        guard values.isSymbolicLink != true else {
            throw BootstrapError.environmentInvalid(path)
        }
        if path == root {
            guard values.isDirectory == true else {
                throw BootstrapError.environmentInvalid(path)
            }
        } else if values.isRegularFile != true {
            throw BootstrapError.environmentInvalid(path)
        }
    }
    return (config, environment)
}

private func loadPrivateEnvironment(_ url: URL) throws -> [String: String] {
    let raw = try String(contentsOf: url, encoding: .utf8)
    guard raw.utf8.count <= 64 * 1_024 else {
        throw BootstrapError.environmentInvalid(url)
    }
    var values: [String: String] = [:]
    for line in raw.split(separator: "\n", omittingEmptySubsequences: false) {
        let text = String(line).trimmingCharacters(in: .whitespaces)
        if text.isEmpty || text.hasPrefix("#") { continue }
        let parts = text.split(
            separator: "=",
            maxSplits: 1,
            omittingEmptySubsequences: false
        )
        guard parts.count == 2 else {
            throw BootstrapError.environmentInvalid(url)
        }
        let key = String(parts[0])
        let value = String(parts[1])
        guard key.range(of: #"^[A-Z][A-Z0-9_]{0,127}$"#,
                        options: .regularExpression) != nil,
              values[key] == nil,
              !value.isEmpty,
              value.utf8.count <= 8 * 1_024,
              !value.contains(where: { $0.isNewline || $0 == "\0" })
        else { throw BootstrapError.environmentInvalid(url) }
        values[key] = value
    }
    return values
}

private func execFleet() throws -> Never {
    let executable = executableURL()
    let contents = try outerContents(startingAt: executable)
    let resources = contents.appending(path: "Resources", directoryHint: .isDirectory)
    let fleetSource = resources.appending(path: "Fleet", directoryHint: .isDirectory)
    guard FileManager.default.fileExists(
        atPath: fleetSource.appending(path: "mnemosyne_fleet").path
    ) else { throw BootstrapError.sourceNotFound(resources) }

    let python = try bundledPython(in: resources)
    let paths = try privatePaths()
    let privateEnvironment = try loadPrivateEnvironment(paths.environment)
    var environment = ProcessInfo.processInfo.environment
    let bundled = python.home != nil
    if let home = python.home {
        for key in environment.keys where key.hasPrefix("PYTHON") {
            environment.removeValue(forKey: key)
        }
        environment.removeValue(forKey: "MNEMOSYNE_PYTHON_OVERRIDE")
        environment["PYTHONHOME"] = home.path
    }
    for (key, value) in privateEnvironment { environment[key] = value }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["MNEMOSYNE_FLEET_CONFIG"] = paths.config.path
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    var pythonPath = [fleetSource.path]
    pythonPath.append(contentsOf: sitePackages(in: resources))
    if !bundled, let existing = environment["PYTHONPATH"], !existing.isEmpty {
        pythonPath.append(existing)
    }
    environment["PYTHONPATH"] = pythonPath.joined(separator: ":")

    var arguments = [
        python.executable.path, "-B", "-P", "-s", "-m",
        "mnemosyne_fleet.main", "--config", paths.config.path,
    ]
    arguments.append(contentsOf: CommandLine.arguments.dropFirst())
    let environmentEntries = environment.map { "\($0.key)=\($0.value)" }.sorted()
    let argumentPointers = arguments.map { strdup($0) } + [nil]
    let environmentPointers = environmentEntries.map { strdup($0) } + [nil]
    defer {
        argumentPointers.dropLast().forEach { free($0) }
        environmentPointers.dropLast().forEach { free($0) }
    }
    var mutableArguments = argumentPointers
    var mutableEnvironment = environmentPointers
    let result = python.executable.path.withCString { path in
        mutableArguments.withUnsafeMutableBufferPointer { argv in
            mutableEnvironment.withUnsafeMutableBufferPointer { envp in
                execve(path, argv.baseAddress, envp.baseAddress)
            }
        }
    }
    let code = errno
    throw NSError(
        domain: NSPOSIXErrorDomain,
        code: Int(code),
        userInfo: [
            NSLocalizedDescriptionKey:
                "execve returned \(result): \(String(cString: strerror(code)))",
        ]
    )
}

do {
    try execFleet()
} catch {
    fputs("mnemosyne-hub-bootstrap: \(error)\n", stderr)
    exit(78)
}
