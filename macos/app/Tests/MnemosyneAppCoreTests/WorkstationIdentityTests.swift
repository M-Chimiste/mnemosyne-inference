import Testing
@testable import MnemosyneAppCore

@Test("The macOS Computer Name becomes the controller name")
func computerNameIdentity() {
    let name = WorkstationIdentity.resolve(
        environment: [:],
        computerName: "Theseus",
        localizedHostName: "Theseus.local"
    )
    #expect(name == "Theseus")
}

@Test("A process override wins and is trimmed")
func workstationNameOverride() {
    let name = WorkstationIdentity.resolve(
        environment: ["MNEMOSYNE_WORKSTATION_NAME": "  Athena  "],
        computerName: "Theseus",
        localizedHostName: nil
    )
    #expect(name == "Athena")
}

@Test("A local hostname is cleaned before display")
func localHostnameIdentity() {
    let name = WorkstationIdentity.resolve(
        environment: [:],
        computerName: "  ",
        localizedHostName: "Metis.local"
    )
    #expect(name == "Metis")
}

@Test("The portable product name remains the final fallback")
func workstationNameFallback() {
    let name = WorkstationIdentity.resolve(
        environment: ["MNEMOSYNE_WORKSTATION_NAME": ""],
        computerName: nil,
        localizedHostName: nil
    )
    #expect(name == "Mnemosyne")
}
