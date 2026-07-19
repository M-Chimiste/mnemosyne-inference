import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("The menu app defaults to Mnemosyne's dedicated control port")
func defaultControlPort() {
    #expect(ControlAPIClient.defaultBaseURL.host == "127.0.0.1")
    #expect(ControlAPIClient.defaultBaseURL.port == 17_321)
}

@Test("Endpoint paths are resolved below the control origin")
func endpointResolution() {
    let client = ControlAPIClient(baseURL: URL(string: "http://localhost:17321")!)
    #expect(client.endpointURL("/manager/status").absoluteString == "http://localhost:17321/manager/status")
    #expect(client.endpointURL("/manager/models").absoluteString == "http://localhost:17321/manager/models")
}

@Test("Load requests use the control endpoint and exact JSON body")
func loadRequestEncoding() throws {
    let client = ControlAPIClient(
        baseURL: URL(string: "http://localhost:17321")!,
        adminPassword: "secret"
    )
    let request = try client.loadRequest(model: "glm-5-2")

    #expect(request.url?.absoluteString == "http://localhost:17321/manager/load")
    #expect(request.httpMethod == "POST")
    #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Basic YWRtaW46c2VjcmV0")
    let body = try #require(request.httpBody)
    let payload = try JSONDecoder().decode(LoadModelRequest.self, from: body)
    #expect(payload == LoadModelRequest(model: "glm-5-2"))
}

@Test("The status decoder ignores future fields")
func statusDecodeIsForwardCompatible() throws {
    let payload = #"""
    {
      "status": "running",
      "resident_alias": "glm-5-2",
      "resident_model": "glm-5-2",
      "resident_engine": "omlx",
      "in_flight_requests": 2,
      "token_sidecar": {
        "enabled": true,
        "outbox_depth": 4,
        "last_flush_at": 1784462400.5,
        "future_field": "ignored"
      },
      "another_future_field": true
    }
    """#.data(using: .utf8)!

    let snapshot = try JSONDecoder().decode(ServiceSnapshot.self, from: payload)
    #expect(snapshot.status == "running")
    #expect(snapshot.residentAlias == "glm-5-2")
    #expect(snapshot.residentModel == "glm-5-2")
    #expect(snapshot.residentEngine == "omlx")
    #expect(snapshot.inFlightRequests == 2)
    #expect(snapshot.tokenSidecar?.outboxDepth == 4)
    #expect(snapshot.tokenSidecar?.lastFlushAt == 1_784_462_400.5)
}

@Test("The catalog decoder preserves aliases and ignores future fields")
func modelCatalogDecodeIsForwardCompatible() throws {
    let payload = #"""
    {
      "models": [
        {
          "id": "deepseek-v4",
          "object": "model",
          "owned_by": "mnemosyne",
          "engine": "ds4",
          "upstream_model": "mlx-community/DeepSeek-V4",
          "capabilities": ["chat/completions"],
          "load_config_digest": "ignored"
        }
      ],
      "resident_alias": null,
      "future_field": true
    }
    """#.data(using: .utf8)!

    let catalog = try JSONDecoder().decode(ModelCatalogSnapshot.self, from: payload)
    #expect(catalog.models.map(\.id) == ["deepseek-v4"])
    #expect(catalog.models.first?.engine == "ds4")
    #expect(catalog.residentAlias == nil)
}
