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

@Test("Hugging Face front matter is removed before model-card rendering")
func huggingFaceFrontMatterRemoval() {
    let source = """
    ---
    tags:
    - unsloth
    - qwen3
    base_model: Qwen/Qwen3-Coder-30B-A3B-Instruct
    license: apache-2.0
    ---
    # Qwen3 Coder

    A **local coding model** with [documentation](https://example.test/docs).
    """

    let markdown = ModelTextMarkup.markdown(from: source)
    let rendered = String(ModelTextMarkup.attributedString(from: source).characters)

    #expect(markdown.hasPrefix("# Qwen3 Coder"))
    #expect(!markdown.contains("base_model:"))
    #expect(!rendered.contains("license:"))
    #expect(rendered.contains("local coding model"))
}

@Test("Model cards are split into readable Markdown blocks")
func modelCardBlockParsing() {
    let source = """
    ## Overview

    Use **vision** and text.

    - 128k context
    2. Exact GGUF selection

    > Verify the runtime first.

    ```shell
    llama-server --model model.gguf
    ```
    """

    let blocks = ModelTextMarkup.blocks(from: source)

    #expect(blocks[0] == .heading(level: 2, text: "Overview"))
    #expect(blocks[1] == .paragraph("Use **vision** and text."))
    #expect(blocks[2] == .unorderedItem("128k context"))
    #expect(blocks[3] == .orderedItem(number: 2, text: "Exact GGUF selection"))
    #expect(blocks[4] == .quote("Verify the runtime first."))
    #expect(blocks[5] == .code("llama-server --model model.gguf"))
}
