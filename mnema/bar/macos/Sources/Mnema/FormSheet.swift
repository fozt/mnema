import SwiftUI

/// A live form window (server, model) as a sheet over settings: secret and choice fields, a red
/// error line, and a save-anyway button. Buttons send submit/force events; only the engine (saved)
/// or Cancel closes it.
struct FormSheet: View {
    let form: LiveForm
    let store: Store

    var body: some View {
        let ask = form.ask
        let busy = !ask.busy.isEmpty
        VStack(alignment: .leading, spacing: 12) {
            Text(ask.title).font(.headline)
            if !ask.message.isEmpty {
                Text(Self.linked(ask.message)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Form {
                ForEach(ask.fields) { FormFieldRow(field: $0, form: form) }
            }
            .formStyle(.columns)
            .disabled(busy)
            if busy {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(ask.busy).foregroundStyle(.secondary)
                }
            }
            if !ask.error.isEmpty {
                Text(ask.error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if !ask.force.isEmpty {
                    Button(ask.force) { store.formEvent("force") }.disabled(busy)
                }
                Spacer()
                Button(ask.cancel, role: .cancel) { store.form = nil }.keyboardShortcut(.cancelAction)
                Button(ask.ok) { store.formEvent("submit") }.keyboardShortcut(.defaultAction).disabled(busy)
            }
            .padding(.top, 4)
        }
        .padding(20)
        .frame(width: 460)
    }

    /// A message with links: an address ("aistudio.google.com/apikey") opens on click; without a
    /// scheme it uses https.
    static func linked(_ text: String) -> AttributedString {
        var result = AttributedString(text)
        var links: [(Range<String.Index>, URL)] = []
        if let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) {
            for match in detector.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
                guard var url = match.url, let found = Range(match.range, in: text) else { continue }
                if url.scheme == "http", !text[found].lowercased().hasPrefix("http") {
                    url = URL(string: "https://\(text[found])") ?? url
                }
                links.append((found, url))
            }
        }
        // The detector misses scheme-less .ai domains ("openrouter.ai/settings/keys"), so a
        // "domain/path" word is an address too
        let address = try? Regex("[a-z0-9-]+(\\.[a-z0-9-]+)*\\.[a-z]{2,}/\\S*")
        for word in text.split(separator: " ") {
            let found = word.startIndex..<word.endIndex
            guard let address, (try? address.wholeMatch(in: word)) != nil,
                !links.contains(where: { $0.0.overlaps(found) }),
                let url = URL(string: "https://\(word)")
            else { continue }
            links.append((found, url))
        }
        for (found, url) in links {
            if let range = Range(found, in: result) { result[range].link = url }
        }
        return result
    }
}

private struct FormFieldRow: View {
    let field: FormField
    let form: LiveForm

    var body: some View {
        switch field.kind {
        case .secret:
            SecureField(field.label, text: value, prompt: Text(field.placeholder))
        case .choice:
            Picker(field.label, selection: value) {
                ForEach(field.choices) { choice in  // .disabled has no effect on Picker items
                    Text(title(choice)).tag(choice.key).selectionDisabled(!choice.enabled)
                }
            }
            .pickerStyle(.menu)
        }
    }

    /// Input stays in LiveForm and reaches the engine only on a button press.
    private var value: Binding<String> {
        Binding(get: { form.values[field.id] ?? "" }, set: { form.values[field.id] = $0 })
    }

    /// A choice with a detail: "qwen/qwen3-235b-a22b:free · free".
    private func title(_ c: Choice) -> String {
        c.detail.isEmpty ? c.title : "\(c.title) · \(c.detail)"
    }
}
