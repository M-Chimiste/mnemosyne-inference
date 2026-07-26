import Foundation
import MnemosyneAppCore
import Testing

@Test("The approved oMLX Homebrew install is stable and argument-bounded")
func homebrewOMLXCommandsAreStable() {
    #expect(
        HomebrewOMLXInstaller.commands == [
            ["tap", "jundot/omlx", "https://github.com/jundot/omlx"],
            ["install", "omlx"],
        ]
    )
    #expect(
        HomebrewOMLXInstaller.commands
            .flatMap { $0 }
            .contains("--HEAD") == false
    )
}

@Test("The oMLX Homebrew runner rejects arbitrary executables and arguments")
func homebrewOMLXRunnerIsBounded() async {
    await #expect(throws: HomebrewOMLXInstallerError.self) {
        _ = try await HomebrewOMLXInstaller.run(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["install", "omlx"]
        )
    }
    await #expect(throws: HomebrewOMLXInstallerError.self) {
        _ = try await HomebrewOMLXInstaller.run(
            executableURL: URL(fileURLWithPath: "/opt/homebrew/bin/brew"),
            arguments: ["install", "--HEAD", "omlx"]
        )
    }
}
