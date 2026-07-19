import Foundation
import Testing
@testable import MnemosyneAppCore

private struct TemporaryApplicationSupport {
    let root: URL
    let applicationSupport: URL
    let mnemosyne: URL

    init() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "mnemosyne-menu-config-\(UUID().uuidString)", directoryHint: .isDirectory)
        applicationSupport = root.appending(path: "Application Support", directoryHint: .isDirectory)
        mnemosyne = applicationSupport.appending(path: "Mnemosyne", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(
            at: mnemosyne,
            withIntermediateDirectories: true
        )
    }

    func remove() {
        try? FileManager.default.removeItem(at: root)
    }

    func writeConfig(_ contents: String) throws {
        try contents.write(
            to: mnemosyne.appending(path: "config.yaml"),
            atomically: true,
            encoding: .utf8
        )
    }

    func writeEnvironment(_ contents: String) throws {
        try contents.write(
            to: mnemosyne.appending(path: ".env"),
            atomically: true,
            encoding: .utf8
        )
    }
}

@Test("Finder launches use Application Support and safe loopback defaults")
func finderDefaults() throws {
    let temporary = try TemporaryApplicationSupport()
    defer { temporary.remove() }

    let configuration = ControlConnectionConfiguration.load(
        processEnvironment: [:],
        applicationSupportDirectory: temporary.applicationSupport
    )

    #expect(configuration.baseURL.absoluteString == "http://127.0.0.1:17321")
    #expect(configuration.passwordEnvironmentKey == "ADMIN_PASSWORD")
    #expect(configuration.adminPassword == nil)
    #expect(configuration.configURL == temporary.mnemosyne.appending(path: "config.yaml"))
    #expect(configuration.environmentURL == temporary.mnemosyne.appending(path: ".env"))
}

@Test("The menu reads custom server and password-key settings")
func configuredConnectionAndPasswordKey() throws {
    let temporary = try TemporaryApplicationSupport()
    defer { temporary.remove() }
    try temporary.writeConfig(
        #"""
        server:
          inference_port: 9999
          control_bind: "127.0.0.2" # local control interface
          control_port: 18421
          control_password_env: 'MNEMOSYNE_MENU_SECRET'
          nested:
            control_port: 65534
        engines:
          example:
            control_port: 65533
        """#
    )
    try temporary.writeEnvironment(
        #"""
        # The configured key, not a hard-coded ADMIN_PASSWORD, is authoritative.
        ADMIN_PASSWORD=wrong-secret
        MNEMOSYNE_MENU_SECRET='from-private-file'
        MNEMOSYNE_MENU_SECRET=duplicate-is-ignored
        """#
    )

    let configuration = ControlConnectionConfiguration.load(
        processEnvironment: [:],
        applicationSupportDirectory: temporary.applicationSupport
    )

    #expect(configuration.baseURL.absoluteString == "http://127.0.0.2:18421")
    #expect(configuration.passwordEnvironmentKey == "MNEMOSYNE_MENU_SECRET")
    #expect(configuration.adminPassword == "from-private-file")
}

@Test("Launch environment values take precedence over the private env file")
func processEnvironmentPrecedence() throws {
    let temporary = try TemporaryApplicationSupport()
    defer { temporary.remove() }
    try temporary.writeConfig(
        #"""
        server:
          control_password_env: CUSTOM_CONTROL_PASSWORD
        """#
    )
    try temporary.writeEnvironment("CUSTOM_CONTROL_PASSWORD=file-value\n")

    let configuration = ControlConnectionConfiguration.load(
        processEnvironment: ["CUSTOM_CONTROL_PASSWORD": " process-value "],
        applicationSupportDirectory: temporary.applicationSupport
    )

    #expect(configuration.adminPassword == "process-value")
}

@Test("Development paths and control URL can be overridden explicitly")
func developmentOverrides() throws {
    let temporary = try TemporaryApplicationSupport()
    defer { temporary.remove() }
    let alternate = temporary.root.appending(path: "fixtures", directoryHint: .isDirectory)
    try FileManager.default.createDirectory(at: alternate, withIntermediateDirectories: true)
    let configURL = alternate.appending(path: "native.yaml")
    let environmentURL = alternate.appending(path: "native.env")
    try #"server: { ignored_by_scoped_parser: true }"#.write(
        to: configURL,
        atomically: true,
        encoding: .utf8
    )
    try "ADMIN_PASSWORD=fixture-secret\n".write(
        to: environmentURL,
        atomically: true,
        encoding: .utf8
    )

    let configuration = ControlConnectionConfiguration.load(
        processEnvironment: [
            "MNEMOSYNE_MACOS_CONFIG_PATH": configURL.path,
            "MNEMOSYNE_MACOS_ENV_PATH": environmentURL.path,
            "MNEMOSYNE_CONTROL_URL": "https://localhost:19421/",
        ],
        applicationSupportDirectory: temporary.applicationSupport
    )

    #expect(configuration.baseURL.absoluteString == "https://localhost:19421")
    #expect(configuration.adminPassword == "fixture-secret")
    #expect(configuration.configURL == configURL.standardizedFileURL)
    #expect(configuration.environmentURL == environmentURL.standardizedFileURL)
}

@Test("Wildcard binds become local connect addresses and invalid overrides fail safe")
func wildcardAndInvalidOverrideHandling() throws {
    let temporary = try TemporaryApplicationSupport()
    defer { temporary.remove() }
    try temporary.writeConfig(
        #"""
        server:
          control_bind: 0.0.0.0
          control_port: 18321
        """#
    )

    let configuration = ControlConnectionConfiguration.load(
        processEnvironment: [
            "MNEMOSYNE_CONTROL_URL": "https://admin:secret@example.test/control?unsafe=true",
        ],
        applicationSupportDirectory: temporary.applicationSupport
    )

    #expect(configuration.baseURL.absoluteString == "http://127.0.0.1:18321")
}

@Test("IPv6 wildcard binds use IPv6 loopback")
func ipv6WildcardHandling() throws {
    let temporary = try TemporaryApplicationSupport()
    defer { temporary.remove() }
    try temporary.writeConfig(
        #"""
        server:
          control_bind: "::"
          control_port: 18322
        """#
    )

    let configuration = ControlConnectionConfiguration.load(
        processEnvironment: [:],
        applicationSupportDirectory: temporary.applicationSupport
    )

    #expect(configuration.baseURL.absoluteString == "http://[::1]:18322")
}
