import Foundation
import Testing
@testable import MnemosyneAppCore

@Test("HTML model blurbs become safe Markdown with useful formatting")
func htmlModelBlurbNormalization() {
    let source = """
    <div align="center">
      <img src="https://example.test/logo.png" alt="Liquid AI" style="width: 100%;">
      <br><br>
      <a href="https://playground.example.test"><strong>Try LFM</strong></a> •
      <a href="https://docs.example.test"><strong>Docs</strong></a>
      <script>stealCredentials()</script>
    </div>
    """

    let markdown = ModelTextMarkup.markdown(from: source)

    #expect(markdown.contains("*Liquid AI*"))
    #expect(
        markdown.contains(
            "[**Try LFM**](https://playground.example.test)"
        )
    )
    #expect(markdown.contains("[**Docs**](https://docs.example.test)"))
    #expect(!markdown.contains("<div"))
    #expect(!markdown.contains("<img"))
    #expect(!markdown.contains("stealCredentials"))
}

@Test("Unsafe HTML links retain their label without becoming clickable")
func unsafeModelBlurbLinksArePlainText() {
    let markdown = ModelTextMarkup.markdown(
        from: #"<a href="javascript:alert('x')"><b>Open</b></a>"#
    )

    #expect(markdown == "**Open**")
    #expect(!markdown.contains("javascript"))
}

@Test("Existing Markdown remains renderable and HTML entities are decoded")
func mixedModelCardMarkupNormalization() {
    let source = """
    ## Overview

    Use **vision** &amp; text.<br>
    <ul><li>128k context</li><li>GGUF</li></ul>
    """

    let markdown = ModelTextMarkup.markdown(from: source)
    let rendered = ModelTextMarkup.attributedString(from: source)

    #expect(markdown.contains("## Overview"))
    #expect(markdown.contains("Use **vision** & text."))
    #expect(markdown.contains("- 128k context"))
    #expect(markdown.contains("- GGUF"))
    #expect(String(rendered.characters).contains("Overview"))
    #expect(!String(rendered.characters).contains("<br>"))
}
