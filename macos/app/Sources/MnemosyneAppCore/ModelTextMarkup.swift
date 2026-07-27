import Foundation

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
