import SwiftUI

/// The settings window built from the engine model (panel.settings); changing a field sends its key.
struct SettingsView: View {
    let store: Store

    var body: some View {
        Group {
            if store.engineDown {
                Text(Strings.engineDown).foregroundStyle(.secondary).padding().frame(width: 520)
            } else if let tabs = store.panel?.settings.tabs {
                TabView(selection: Bindable(store).settingsTab) {
                    ForEach(tabs) { tab in
                        SettingsTabView(tab: tab, store: store)
                            .tabItem { Label(tab.title, systemImage: tab.symbol) }
                            .tag(tab.id)
                    }
                }
                .frame(width: 520)
            } else {  // the engine is starting
                ProgressView().controlSize(.small).padding().frame(width: 520)
            }
        }
        .sheet(item: Bindable(store).form, onDismiss: store.formDismissed) { FormSheet(form: $0, store: store) }
    }
}

/// A settings tab built from VStack groups, not Form: a grouped Form on macOS is a scrolling table,
/// and after the window height adjusts between tabs its clicks landed about 90 points above what
/// was drawn, so buttons did not respond.
private struct SettingsTabView: View {
    let tab: SettingsTab
    let store: Store

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            ForEach(tab.sections) { section in
                let layout = SectionLayout(section.fields)
                VStack(alignment: .leading, spacing: 8) {
                    if !section.title.isEmpty {
                        Text(section.title).font(.headline).accessibilityAddTraits(.isHeader)
                            .padding(.leading, 4)
                    }
                    SettingsGroup {
                        ForEach(Array(layout.lines.enumerated()), id: \.element.id) { index, line in
                            if index > 0 { Divider() }
                            if line.fields.first?.kind == .button {  // a button next to the row it belongs to
                                ButtonRow(fields: line.fields, store: store)
                            } else {
                                SettingsRow(field: line.fields[0], store: store)
                            }
                        }
                        if !layout.footer.isEmpty {
                            if !layout.lines.isEmpty { Divider() }
                            ButtonRow(fields: layout.footer, store: store)
                        }
                    }
                }
            }
        }
        .padding(20)
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// A row group like System Settings: light backdrop, thin border, dividers between rows.
private struct SettingsGroup<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) { content }
            .padding(.horizontal, 12)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.primary.opacity(0.08)))
    }
}

/// A group row: title on the left, value on the right.
private struct Row<Trailing: View>: View {
    let title: String
    var detail: String = ""
    @ViewBuilder let trailing: Trailing

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if !detail.isEmpty { Text(detail).font(.caption).foregroundStyle(.secondary) }
            }
            Spacer(minLength: 8)
            trailing
        }
        .padding(.vertical, 8)
        .frame(minHeight: 36)
    }
}

/// Lays out a settings section in lines: consecutive buttons share one line in place (Choose
/// model… right under Model); buttons at the end of the section go below the group.
struct SectionLayout {
    struct Line: Identifiable {
        let fields: [SettingsField]
        var id: String { fields.map(\.id).joined(separator: "|") }
    }

    var lines: [Line] = []
    var footer: [SettingsField] = []

    init(_ fields: [SettingsField]) {
        let last = fields.lastIndex { $0.kind != .button }.map { $0 + 1 } ?? 0
        footer = Array(fields[last...])
        for field in fields[..<last] {
            if field.kind == .button, lines.last?.fields.last?.kind == .button {
                lines[lines.count - 1] = Line(fields: lines[lines.count - 1].fields + [field])
            } else {
                lines.append(Line(fields: [field]))
            }
        }
    }
}

/// Buttons in one right-aligned line.
private struct ButtonRow: View {
    let fields: [SettingsField]
    let store: Store

    var body: some View {
        HStack {
            Spacer()
            ForEach(fields) { SettingsRow(field: $0, store: store) }
        }
        .padding(.vertical, 8)
    }
}

private struct SettingsRow: View {
    let field: SettingsField
    let store: Store

    var body: some View {
        Group {
            switch field.kind {
            case .toggle:
                // a disabled toggle's explanation is a gray line under it, as on Linux
                Row(title: field.title, detail: field.detail) {
                    Toggle(field.title, isOn: Binding(get: { field.checked }, set: { _ in send(field.key) }))
                        .toggleStyle(.switch)
                        .labelsHidden()
                }
            case .choice:
                Row(title: field.title) { ChoicePicker(field: field, store: store) }
            case .button:
                Button(field.title, role: field.role == .destructive ? .destructive : nil) {
                    send(field.key)
                }
                // AppKit does not dim the red title of a disabled button (Disconnect while recording)
                .opacity(field.role == .destructive && !field.enabled ? 0.5 : 1)
            case .status:
                Row(title: field.title) {
                    VStack(alignment: .trailing, spacing: 2) {
                        HStack(spacing: 6) {
                            if field.tone != .none { ToneDot(tone: field.tone) }
                            Text(field.value).foregroundStyle(.secondary)
                        }
                        if !field.detail.isEmpty {
                            Text(field.detail).font(.caption).foregroundStyle(.secondary)
                        }
                        if let progress = field.progress {
                            ProgressBar(value: progress)
                                .frame(width: 160)
                                .accessibilityHidden(true)  // the share is in the value
                                .padding(.top, 2)
                        }
                    }
                }
            case .hotkey:
                Row(title: field.title) { HotKeyField(field: field, store: store) }
            }
        }
        .disabled(!field.enabled)
    }

    private func send(_ key: String?) {
        if let key { store.send(key) }
    }
}

/// Status dot: green works, yellow wait, red needs action, gray unchecked; a spinner while
/// checking.
private struct ToneDot: View {
    let tone: Tone

    var body: some View {
        if tone == .busy {
            ProgressView().controlSize(.mini)
        } else {
            Circle().fill(color).frame(width: 8, height: 8).accessibilityHidden(true)
        }
    }

    private var color: Color {
        switch tone {
        case .ok: .green
        case .warn: .yellow
        case .error: .red
        case .none, .busy: .secondary
        }
    }
}

/// A single choice as a pop-up button, with the selected option's detail below it.
private struct ChoicePicker: View {
    let field: SettingsField
    let store: Store

    var body: some View {
        let selected = field.choices.first(where: \.checked)
        VStack(alignment: .trailing, spacing: 2) {
            Picker(field.title, selection: Binding(get: { selected?.key ?? "" }, set: { store.send($0) })) {
                ForEach(field.choices) { choice in  // .disabled has no effect on Picker items
                    Text(title(choice)).tag(choice.key).selectionDisabled(!choice.enabled)
                }
            }
            .pickerStyle(.menu)
            .labelsHidden()
            .fixedSize()
            if let detail = selected?.detail, !detail.isEmpty {
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    /// An option with its detail, "Home · unavailable" (the selected one shows it under the button).
    private func title(_ c: Choice) -> String {
        guard !c.checked, let first = c.detail.first else { return c.title }
        return "\(c.title) · \(first.lowercased())\(c.detail.dropFirst())"
    }
}
