import Darwin
import Foundation

private enum BootstrapError: Error, CustomStringConvertible {
    case outerBundleNotFound(URL)
    case pythonNotFound(URL)
    case sourceNotFound(URL)

    var description: String {
        switch self {
        case let .outerBundleNotFound(executable):
            "could not locate the outer Unified Inference.app from \(executable.path)"
        case let .pythonNotFound(resources):
            "bundled Python was not found below \(resources.path); rebuild without --bare"
        case let .sourceNotFound(resources):
            "bundled mnemosyne_macos sources were not found below \(resources.path)"
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

/// The bootstrap is installed in the outer app's `Contents/MacOS`.
/// Walk upward instead of assuming a fixed number of parents so local bundle
/// layouts can change without baking an absolute path into the LaunchAgent.
private func outerContents(startingAt executable: URL) throws -> URL {
    let fileManager = FileManager.default
    var cursor = executable.deletingLastPathComponent()
    for _ in 0 ..< 10 {
        let resources = cursor.appending(path: "Resources", directoryHint: .isDirectory)
        if cursor.lastPathComponent == "Contents",
           fileManager.fileExists(atPath: resources.appending(path: "Service").path)
        {
            return cursor
        }
        cursor.deleteLastPathComponent()
    }
    throw BootstrapError.outerBundleNotFound(executable)
}

private func bundledPython(in resources: URL) throws -> PythonRuntime {
    let fileManager = FileManager.default
    let pythonRoot = resources.appending(path: "Python", directoryHint: .isDirectory)
    let direct = pythonRoot.appending(path: "bin/python3")
    if fileManager.isExecutableFile(atPath: direct.path) {
        return PythonRuntime(executable: direct, home: pythonRoot)
    }

    let children = try? fileManager.contentsOfDirectory(
        at: pythonRoot,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    )
    for child in (children ?? []).sorted(by: { $0.lastPathComponent < $1.lastPathComponent })
        where child.lastPathComponent.hasPrefix("cpython-")
    {
        let candidate = child.appending(path: "bin/python3")
        if fileManager.isExecutableFile(atPath: candidate.path) {
            return PythonRuntime(executable: candidate, home: child)
        }
    }

    // A complete app must always select its sealed runtime, regardless of the
    // caller's shell or launchctl environment. The override exists only so an
    // intentionally bare development bundle can point at a virtualenv.
    if let override = ProcessInfo.processInfo.environment["MNEMOSYNE_PYTHON_OVERRIDE"] {
        let url = URL(fileURLWithPath: override).standardizedFileURL
        if fileManager.isExecutableFile(atPath: url.path) {
            // Setting PYTHONHOME to a virtualenv breaks stdlib discovery, so
            // leave its environment under the development caller's control.
            return PythonRuntime(executable: url, home: nil)
        }
    }
    throw BootstrapError.pythonNotFound(resources)
}

private func sitePackages(in resources: URL, layerName: String) -> [String] {
    let fileManager = FileManager.default
    let pythonRoot = resources.appending(path: "Python", directoryHint: .isDirectory)
    var paths: [String] = []
    let siteCustomize = pythonRoot.appending(path: "__venvstacks__/site-customize")
    if fileManager.fileExists(atPath: siteCustomize.path) {
        paths.append(siteCustomize.path)
    }

    let layers = (try? fileManager.contentsOfDirectory(
        at: pythonRoot,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    )) ?? []
    for layer in layers where layer.lastPathComponent == layerName {
        let lib = layer.appending(path: "lib", directoryHint: .isDirectory)
        let versions = (try? fileManager.contentsOfDirectory(
            at: lib,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        )) ?? []
        for version in versions where version.lastPathComponent.hasPrefix("python") {
            let candidate = version.appending(path: "site-packages", directoryHint: .isDirectory)
            if fileManager.fileExists(atPath: candidate.path) {
                paths.append(candidate.path)
            }
        }
    }
    return paths
}

private func prepareApplicationSupport(resources: URL) throws -> (config: URL, environment: URL) {
    let fileManager = FileManager.default
    let supportRoot = fileManager.urls(
        for: .applicationSupportDirectory,
        in: .userDomainMask
    )[0].appending(path: "Mnemosyne", directoryHint: .isDirectory)
    for directory in [
        supportRoot,
        supportRoot.appending(path: "logs", directoryHint: .isDirectory),
        supportRoot.appending(path: "state", directoryHint: .isDirectory),
        supportRoot.appending(path: "models", directoryHint: .isDirectory),
    ] {
        try fileManager.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
    }

    let config = supportRoot.appending(path: "config.yaml")
    let environment = supportRoot.appending(path: ".env")
    let examples = [
        (config, resources.appending(path: "config.yaml.example")),
        (environment, resources.appending(path: ".env.example")),
    ]
    for (destination, source) in examples
        where !fileManager.fileExists(atPath: destination.path)
            && fileManager.fileExists(atPath: source.path)
    {
        try fileManager.copyItem(at: source, to: destination)
        try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
    }
    return (config, environment)
}

private func execPython() throws -> Never {
    let fileManager = FileManager.default
    let executable = executableURL()
    let contents = try outerContents(startingAt: executable)
    let resources = contents.appending(path: "Resources", directoryHint: .isDirectory)
    let serviceSource = resources.appending(path: "Service", directoryHint: .isDirectory)
    guard fileManager.fileExists(atPath: serviceSource.appending(path: "mnemosyne_macos").path) else {
        throw BootstrapError.sourceNotFound(resources)
    }

    let python = try bundledPython(in: resources)
    let paths = try prepareApplicationSupport(resources: resources)
    var environment = ProcessInfo.processInfo.environment
    let usesBundledRuntime = python.home != nil
    if let pythonHome = python.home {
        // A signed production bundle must be independent of a user's shell,
        // pyenv, Homebrew, or previous Mnemosyne installation. Remove every
        // ambient Python control before defining the app-owned runtime below.
        let ambientPythonKeys = environment.keys.filter { $0.hasPrefix("PYTHON") }
        for key in ambientPythonKeys {
            environment.removeValue(forKey: key)
        }
        environment.removeValue(forKey: "MNEMOSYNE_PYTHON_OVERRIDE")
        environment["PYTHONHOME"] = pythonHome.path
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["MNEMOSYNE_MACOS_CONFIG_PATH"] =
        environment["MNEMOSYNE_MACOS_CONFIG_PATH"] ?? paths.config.path
    environment["MNEMOSYNE_MACOS_ENV_PATH"] =
        environment["MNEMOSYNE_MACOS_ENV_PATH"] ?? paths.environment.path
    environment["MNEMOSYNE_FILE_TRASH_HELPER"] =
        environment["MNEMOSYNE_FILE_TRASH_HELPER"]
            ?? contents.appending(path: "MacOS/mnemosyne-file-trash").path
    // Lifecycle authorization is service-mediated: never let an ambient
    // LaunchAgent or shell value select a different helper peer. The helper
    // independently verifies this sealed service Python through the bundled
    // peer manifest before it reads the challenge.
    environment["MNEMOSYNE_LIFECYCLE_HELPER"] =
        contents.appending(
            path: "Helpers/MnemosyneLifecycleAuthorization.app/Contents/MacOS/mnemosyne-lifecycle-helper"
        ).path
    environment["PATH"] = environment["PATH"]
        ?? "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    var pythonPath = [serviceSource.path]
    pythonPath.append(contentsOf: sitePackages(
        in: resources,
        layerName: "framework-mnemosyne-base"
    ))
    if !usesBundledRuntime,
       let existing = environment["PYTHONPATH"],
       !existing.isEmpty
    {
        pythonPath.append(existing)
    }
    environment["PYTHONPATH"] = pythonPath.joined(separator: ":")

    let imageSource = resources.appending(path: "ImageWorker", directoryHint: .isDirectory)
    let imagePython = resources
        .appending(path: "Python", directoryHint: .isDirectory)
        .appending(path: "framework-mnemosyne-image", directoryHint: .isDirectory)
        .appending(path: "bin/python3")
    if fileManager.fileExists(atPath: imageSource.appending(path: "mnemosyne_mflux_worker").path) {
        environment["MNEMOSYNE_MFLUX_PYTHONPATH"] = usesBundledRuntime
            ? imageSource.path
            : (environment["MNEMOSYNE_MFLUX_PYTHONPATH"] ?? imageSource.path)
    }
    if fileManager.isExecutableFile(atPath: imagePython.path) {
        environment["MNEMOSYNE_MFLUX_PYTHON"] = usesBundledRuntime
            ? imagePython.path
            : (environment["MNEMOSYNE_MFLUX_PYTHON"] ?? imagePython.path)
    }

    var arguments = [
        python.executable.path,
        "-B",
        "-P",
        "-s",
        "-m",
        "mnemosyne_macos.cli",
        "serve",
        "--config",
        paths.config.path,
        "--env",
        paths.environment.path,
    ]
    arguments.append(contentsOf: CommandLine.arguments.dropFirst())
    let environmentEntries = environment
        .map { "\($0.key)=\($0.value)" }
        .sorted()

    let argumentPointers = arguments.map { strdup($0) } + [nil]
    let environmentPointers = environmentEntries.map { strdup($0) } + [nil]
    defer {
        argumentPointers.dropLast().forEach { free($0) }
        environmentPointers.dropLast().forEach { free($0) }
    }

    var mutableArguments = argumentPointers
    var mutableEnvironment = environmentPointers
    let result = python.executable.path.withCString { pythonPathPointer in
        mutableArguments.withUnsafeMutableBufferPointer { argv in
            mutableEnvironment.withUnsafeMutableBufferPointer { envp in
                execve(pythonPathPointer, argv.baseAddress, envp.baseAddress)
            }
        }
    }
    let errorNumber = errno
    throw NSError(
        domain: NSPOSIXErrorDomain,
        code: Int(errorNumber),
        userInfo: [
            NSLocalizedDescriptionKey: "execve returned \(result): \(String(cString: strerror(errorNumber)))",
        ]
    )
}

do {
    try execPython()
} catch {
    fputs("mnemosyne-service-bootstrap: \(error)\n", stderr)
    exit(78)
}
