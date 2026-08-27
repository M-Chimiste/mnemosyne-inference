import Foundation

public enum ModelTextBlock: Equatable, Sendable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case unorderedItem(String)
    case orderedItem(number: Int, text: String)
    case quote(String)
    case code(String)
    case rule
}

public enum ModelTextMarkup {
    public static func attributedString(from source: String) -> AttributedString {
        let normalized = markdown(from: source)
        var options = AttributedString.MarkdownParsingOptions()
        options.interpretedSyntax = .full
        options.failurePolicy = .returnPartiallyParsedIfPossible
        return (try? AttributedString(markdown: normalized, options: options))
            ?? AttributedString(normalized)
    }

    public static func markdown(from source: String) -> String {
        var output = source.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")

        output = removingFrontMatter(from: output)

        output = replacing(
            #"(?is)<!--.*?-->"#,
            in: output,
            with: ""
        )
        output = replacing(
            #"(?is)<(script|style|noscript|template|svg|iframe|object|embed)\b[^>]*>.*?</\1\s*>"#,
            in: output,
            with: ""
        )

        output = replacing(
            #"(?is)<img\b[^>]*\balt\s*=\s*"([^"]*)"[^>]*>"#,
            in: output,
            with: "\n*$1*\n"
        )
        output = replacing(
            #"(?is)<img\b[^>]*\balt\s*=\s*'([^']*)'[^>]*>"#,
            in: output,
            with: "\n*$1*\n"
        )
        output = replacing(
            #"(?is)<img\b[^>]*>"#,
            in: output,
            with: ""
        )

        for level in 1...6 {
            output = replacing(
                #"(?is)<h\#(level)\b[^>]*>"#,
                in: output,
                with: "\n\(String(repeating: "#", count: level)) "
            )
            output = replacing(
                #"(?is)</h\#(level)\s*>"#,
                in: output,
                with: "\n"
            )
        }

        output = replacing(
            #"(?is)<(strong|b)\b[^>]*>"#,
            in: output,
            with: "**"
        )
        output = replacing(
            #"(?is)</(strong|b)\s*>"#,
            in: output,
            with: "**"
        )
        output = replacing(
            #"(?is)<(em|i)\b[^>]*>"#,
            in: output,
            with: "_"
        )
        output = replacing(
            #"(?is)</(em|i)\s*>"#,
            in: output,
            with: "_"
        )
        output = replacing(
            #"(?is)<code\b[^>]*>"#,
            in: output,
            with: "`"
        )
        output = replacing(
            #"(?is)</code\s*>"#,
            in: output,
            with: "`"
        )

        output = replacing(
            #"(?is)<a\b[^>]*\bhref\s*=\s*"(https?://[^"]+)"[^>]*>(.*?)</a\s*>"#,
            in: output,
            with: "[$2]($1)"
        )
        output = replacing(
            #"(?is)<a\b[^>]*\bhref\s*=\s*'(https?://[^']+)'[^>]*>(.*?)</a\s*>"#,
            in: output,
            with: "[$2]($1)"
        )

        output = replacing(
            #"(?is)<br\b[^>]*>"#,
            in: output,
            with: "\n"
        )
        output = replacing(
            #"(?is)<hr\b[^>]*>"#,
            in: output,
            with: "\n\n---\n\n"
        )
        output = replacing(
            #"(?is)<li\b[^>]*>"#,
            in: output,
            with: "\n- "
        )
        output = replacing(
            #"(?is)</li\s*>"#,
            in: output,
            with: "\n"
        )
        output = replacing(
            #"(?is)</?(ul|ol|p|div|section|article|header|footer|main|aside|blockquote|pre)\b[^>]*>"#,
            in: output,
            with: "\n"
        )
        output = replacing(
            #"(?is)</?(table|thead|tbody|tfoot|tr)\b[^>]*>"#,
            in: output,
            with: "\n"
        )
        output = replacing(
            #"(?is)</?(th|td)\b[^>]*>"#,
            in: output,
            with: " | "
        )

        // Any remaining HTML is deliberately reduced to its text content.
        // This keeps event handlers, unsafe link schemes, and unsupported
        // embeds out of the attributed Markdown surface.
        output = replacing(
            #"(?is)</?[a-z][^>]*>"#,
            in: output,
            with: ""
        )
        output = decodeEntities(output)
        output = replacing(#"[ \t]+\n"#, in: output, with: "\n")
        output = replacing(#"\n[ \t]+"#, in: output, with: "\n")
        output = replacing(#"\n{3,}"#, in: output, with: "\n\n")
        return output.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public static func blocks(from source: String) -> [ModelTextBlock] {
        let normalized = markdown(from: source)
        guard !normalized.isEmpty else { return [] }

        let lines = normalized.split(
            separator: "\n",
            omittingEmptySubsequences: false
        ).map(String.init)
        var blocks: [ModelTextBlock] = []
        var paragraph: [String] = []
        var code: [String] = []
        var codeFence: String?

        func flushParagraph() {
            guard !paragraph.isEmpty else { return }
            blocks.append(.paragraph(paragraph.joined(separator: " ")))
            paragraph.removeAll(keepingCapacity: true)
        }

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if let fence = codeFence {
                if trimmed.hasPrefix(fence) {
                    blocks.append(.code(code.joined(separator: "\n")))
                    code.removeAll(keepingCapacity: true)
                    codeFence = nil
                } else {
                    code.append(line)
                }
                continue
            }

            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                flushParagraph()
                codeFence = String(trimmed.prefix(3))
                continue
            }
            if trimmed.isEmpty {
                flushParagraph()
                continue
            }
            if let heading = heading(in: trimmed) {
                flushParagraph()
                blocks.append(.heading(level: heading.level, text: heading.text))
                continue
            }
            if isHorizontalRule(trimmed) {
                flushParagraph()
                blocks.append(.rule)
                continue
            }
            if let item = unorderedItem(in: trimmed) {
                flushParagraph()
                blocks.append(.unorderedItem(item))
                continue
            }
            if let item = orderedItem(in: trimmed) {
                flushParagraph()
                blocks.append(.orderedItem(number: item.number, text: item.text))
                continue
            }
            if trimmed.hasPrefix(">") {
                flushParagraph()
                let text = trimmed.dropFirst()
                    .trimmingCharacters(in: .whitespaces)
                blocks.append(.quote(text))
                continue
            }
            paragraph.append(trimmed)
        }

        flushParagraph()
        if !code.isEmpty {
            blocks.append(.code(code.joined(separator: "\n")))
        }
        return blocks
    }

    private static func removingFrontMatter(from source: String) -> String {
        let lines = source.split(
            separator: "\n",
            omittingEmptySubsequences: false
        ).map(String.init)
        guard lines.first?.trimmingCharacters(in: .whitespaces) == "---" else {
            return source
        }

        let searchLimit = min(lines.count, 200)
        guard let closingIndex = (1 ..< searchLimit).first(where: { index in
            let line = lines[index].trimmingCharacters(in: .whitespaces)
            return line == "---" || line == "..."
        }) else {
            return source
        }
        let yamlKey = try? NSRegularExpression(
            pattern: #"^[A-Za-z0-9_.-]+\s*:"#
        )
        let hasMetadata = lines[1 ..< closingIndex].contains { line in
            guard let yamlKey else { return false }
            return yamlKey.firstMatch(
                in: line,
                range: NSRange(line.startIndex..., in: line)
            ) != nil
        }
        guard hasMetadata else { return source }
        return lines.dropFirst(closingIndex + 1).joined(separator: "\n")
    }

    private static func heading(in line: String) -> (level: Int, text: String)? {
        let hashes = line.prefix(while: { $0 == "#" })
        guard (1 ... 6).contains(hashes.count) else { return nil }
        let remainder = line.dropFirst(hashes.count)
        guard remainder.first?.isWhitespace == true else { return nil }
        let text = remainder.trimmingCharacters(in: .whitespaces)
        return text.isEmpty ? nil : (hashes.count, text)
    }

    private static func unorderedItem(in line: String) -> String? {
        guard line.count >= 3 else { return nil }
        let marker = line.first
        guard marker == "-" || marker == "*" || marker == "+" else { return nil }
        let remainder = line.dropFirst()
        guard remainder.first?.isWhitespace == true else { return nil }
        let text = remainder.trimmingCharacters(in: .whitespaces)
        return text.isEmpty ? nil : text
    }

    private static func orderedItem(in line: String) -> (number: Int, text: String)? {
        let digits = line.prefix(while: { $0.isNumber })
        guard let number = Int(digits), !digits.isEmpty else { return nil }
        let markerIndex = line.index(line.startIndex, offsetBy: digits.count)
        guard markerIndex < line.endIndex,
              line[markerIndex] == "." || line[markerIndex] == ")"
        else { return nil }
        let remainderIndex = line.index(after: markerIndex)
        guard remainderIndex < line.endIndex,
              line[remainderIndex].isWhitespace
        else { return nil }
        let text = line[line.index(after: remainderIndex)...]
            .trimmingCharacters(in: .whitespaces)
        return text.isEmpty ? nil : (number, text)
    }

    private static func isHorizontalRule(_ line: String) -> Bool {
        let compact = line.replacingOccurrences(of: " ", with: "")
        guard compact.count >= 3, let marker = compact.first,
              marker == "-" || marker == "_" || marker == "*"
        else { return false }
        return compact.allSatisfy { $0 == marker }
    }

    private static func replacing(
        _ pattern: String,
        in source: String,
        with replacement: String
    ) -> String {
        guard let expression = try? NSRegularExpression(pattern: pattern) else {
            return source
        }
        return expression.stringByReplacingMatches(
            in: source,
            range: NSRange(source.startIndex..., in: source),
            withTemplate: replacement
        )
    }

    private static func decodeEntities(_ source: String) -> String {
        var output = source
        let namedEntities = [
            "&nbsp;": " ",
            "&amp;": "&",
            "&quot;": "\"",
            "&#39;": "'",
            "&apos;": "'",
            "&lt;": "<",
            "&gt;": ">",
        ]
        for (entity, value) in namedEntities {
            output = output.replacingOccurrences(of: entity, with: value)
        }

        guard let expression = try? NSRegularExpression(
            pattern: #"&#(x[0-9a-fA-F]+|[0-9]+);"#
        ) else {
            return output
        }
        let original = output as NSString
        let matches = expression.matches(
            in: output,
            range: NSRange(location: 0, length: original.length)
        )
        let mutable = NSMutableString(string: output)
        for match in matches.reversed() {
            let token = original.substring(with: match.range(at: 1))
            let radix = token.lowercased().hasPrefix("x") ? 16 : 10
            let digits = radix == 16 ? String(token.dropFirst()) : token
            guard
                let value = UInt32(digits, radix: radix),
                let scalar = UnicodeScalar(value)
            else {
                continue
            }
            mutable.replaceCharacters(
                in: match.range,
                with: String(Character(scalar))
            )
        }
        return mutable as String
    }
}
