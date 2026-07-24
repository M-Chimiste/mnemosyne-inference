import Testing
@testable import MnemosyneAppCore

@Test("LM Studio inventory requires saved and applied enablement")
func lmStudioInventoryRequiresAppliedEnablement() {
    var draft = LMStudioSettings()
    var saved = LMStudioSettings()
    let revision = String(repeating: "a", count: 64)

    draft.enabled = true
    #expect(
        LMStudioInventoryAvailability.evaluate(
            draft: draft,
            saved: saved,
            savedRevision: revision,
            appliedRevision: revision,
            restartRequired: false
        ) == .saveRequired
    )

    saved.enabled = true
    #expect(
        LMStudioInventoryAvailability.evaluate(
            draft: draft,
            saved: saved,
            savedRevision: revision,
            appliedRevision: String(repeating: "b", count: 64),
            restartRequired: true
        ) == .restartRequired
    )

    #expect(
        LMStudioInventoryAvailability.evaluate(
            draft: draft,
            saved: saved,
            savedRevision: revision,
            appliedRevision: revision,
            restartRequired: false
        ) == .available
    )
}

@Test("Unsaved LM Studio connection changes gate inventory discovery")
func lmStudioInventoryRequiresSavingEngineChanges() {
    var draft = LMStudioSettings()
    var saved = LMStudioSettings()
    draft.enabled = true
    saved.enabled = true
    draft.baseUrl = "http://127.0.0.1:2234"
    let revision = String(repeating: "c", count: 64)

    let availability = LMStudioInventoryAvailability.evaluate(
        draft: draft,
        saved: saved,
        savedRevision: revision,
        appliedRevision: revision,
        restartRequired: false
    )

    #expect(availability == .saveRequired)
    #expect(availability.guidance?.contains("Save") == true)
}
