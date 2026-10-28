import Foundation

/// UI language of the app's own texts and of the engine (passed as `MNEMA_UI_LANG`).
/// Everything else the panel shows is rendered by Python in the same language.
enum UILanguage {
    /// Set by tests and previews; wins over the environment and the bundle.
    /// Tests run in parallel and share it, so tests only ever set it to "ru" (never back to nil);
    /// a test that needs English uses `$scoped`.
    static var override: String?

    /// A language for the current task only; wins over `override` without racing on it.
    @TaskLocal static var scoped: String?

    /// "ru" or "en": scoped ?? override ?? `MNEMA_UI_LANG` (terminal runs, previews) ??
    /// the bundle localization macOS chose from the user's preferred languages.
    static var current: String {
        if let tag = scoped ?? override { return normalize(tag) }
        return system
    }

    /// Read once: views call `L()` on every render.
    private static let system = normalize(
        ProcessInfo.processInfo.environment["MNEMA_UI_LANG"] ?? Bundle.main.preferredLocalizations.first)

    /// "ru", "ru-RU", "ru_RU" → "ru"; anything else, empty or missing → "en".
    static func normalize(_ tag: String?) -> String {
        guard let tag = tag?.trimmingCharacters(in: .whitespaces).lowercased() else { return "en" }
        return tag.hasPrefix("ru") ? "ru" : "en"
    }
}

/// `en` is the English source string; in Russian it is looked up in `ruStrings`
/// (a missing entry falls back to English).
func L(_ en: String) -> String {
    Localization.text(en, lang: UILanguage.current, catalog: ruStrings)
}

/// Template form: `L("Search in “{name}”", ["name": section])` translates first, then fills
/// each `{key}`, so the Russian entry keeps the same placeholders.
func L(_ en: String, _ args: [String: String]) -> String {
    Localization.fill(L(en), args)
}

enum Localization {
    static func text(_ en: String, lang: String, catalog: [String: String]) -> String {
        lang == "ru" ? catalog[en] ?? en : en
    }

    /// One pass over the template: a value that itself contains `{key}` is not expanded again;
    /// an unknown `{key}` stays as written.
    static func fill(_ template: String, _ args: [String: String]) -> String {
        var out = ""
        var rest = template[...]
        while let open = rest.firstIndex(of: "{") {
            out += rest[..<open]
            let after = rest.index(after: open)
            if let close = rest[after...].firstIndex(of: "}"),
                let value = args[String(rest[after..<close])]
            {
                out += value
                rest = rest[rest.index(after: close)...]
            } else {
                out.append("{")
                rest = rest[after...]
            }
        }
        return out + rest
    }
}
