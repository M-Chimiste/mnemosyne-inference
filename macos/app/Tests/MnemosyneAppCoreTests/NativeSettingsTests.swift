import MnemosyneAppCore
import Testing

@Test("Local network inference maps to the supported wildcard bind")
func localNetworkInferenceMapsToWildcardBind() {
    var server = ServerSettings()

    #expect(server.allowsLocalNetworkInference == false)
    #expect(server.inferenceBind == "127.0.0.1")

    server.allowsLocalNetworkInference = true
    #expect(server.inferenceBind == "0.0.0.0")
    #expect(server.allowsLocalNetworkInference == true)

    server.allowsLocalNetworkInference = false
    #expect(server.inferenceBind == "127.0.0.1")
    #expect(server.allowsLocalNetworkInference == false)
}
