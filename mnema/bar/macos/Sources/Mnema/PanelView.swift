import AppKit
import SwiftUI

/// The panel opened from the menu bar icon: header, phase screen, notices, recordings list. Grid
/// in Metrics: text from x 16 (40 after an icon), right edge at 344. Titles are ready-made
/// PanelModel strings from the engine.
struct PanelView: View {
    let store: Store
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Group {
            if store.outdated {
                OutdatedScreen()
            } else if store.engineDown {
                EngineDownScreen(store: store)
            } else if let panel = store.panel {
                PanelContent(panel: panel, store: store)
            } else {
                ProgressView()
                    .controlSize(.small)
                    .frame(maxWidth: .infinity)
                    .padding(24)
            }
        }
        .frame(width: Metrics.width)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { store.keys.fit(height: $0) }
        .background(WindowReader(keys: store.keys))
        .onAppear { store.panelVisible(true) }
        .onDisappear { store.panelVisible(false) }
        .onChange(of: store.settingsRequests) {  // Settings…, ⌘, or a card button
            store.keys.window?.orderOut(nil)  // otherwise the first click in settings only closes the panel
            SettingsWindow.bringForward { openSettings() }
        }
    }
}

private struct PanelContent: View {
    let panel: PanelModel
    let store: Store
    @StateObject private var heights = PanelHeights()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                Header(panel: panel, store: store)
                Spacer().frame(height: 12)
                switch panel.screen {
                case .idle:
                    if let idle = panel.idle { IdleScreen(idle: idle, hotkey: panel.hotkey, store: store) }
                case .recording:
                    if let recording = panel.recording { RecordingScreen(recording: recording, store: store) }
                }
                if !panel.notices.isEmpty {  // below the main block, so Start recording does not move
                    VStack(spacing: 8) {
                        ForEach(panel.notices, id: \.self) { NoticeRow(notice: $0, store: store) }
                    }
                    .padding(.top, 12)
                }
                Divider()
                    .padding(.horizontal, Metrics.inset)
                    .padding(.vertical, 12)
                if store.hasRecords { ListHeader(panel: panel, store: store) }
            }
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { heights.chrome = $0 + 12 + 8 }
            RecordList(panel: panel, store: store, heights: heights)
        }
        .padding(.top, 12)
        .padding(.bottom, 8)
    }
}

private struct Header: View {
    let panel: PanelModel
    let store: Store

    var body: some View {
        HStack(spacing: 4) {
            Text(panel.header)
                .font(.system(size: 13, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 8)
            if !panel.where.isEmpty { PlaceButton(place: panel.where, state: panel.whereState, store: store) }
            MoreMenu(panel: panel, store: store)
        }
        .frame(height: 28)
        .padding(.leading, Metrics.inset)
        .padding(.trailing, Metrics.iconInset)
    }
}

/// Where recordings are processed; a click chooses the server in settings.
private struct PlaceButton: View {
    let place: String
    let state: String
    let store: Store

    var body: some View {
        Button {
            store.send("choose_server")
        } label: {
            HStack(spacing: 4) {
                // server state; unavailable is also spelled out
                if state == "ok" {
                    Circle().fill(.green).frame(width: 6, height: 6)
                } else if state == "down" {
                    WarnMark()
                }
                Text(place)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: 180)
                    .fixedSize(horizontal: true, vertical: false)
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 6)
            .frame(height: 28)
        }
        .buttonStyle(HoverFillStyle())
        .help(L("Choose server"))
        .accessibilityLabel(L("Choose server"))
        .accessibilityValue(place)
    }
}

/// More: settings, library commands, quit. Items are the engine's footer; Delete audio… goes
/// before the first item with a divider.
private struct MoreMenu: View {
    let panel: PanelModel
    let store: Store

    var body: some View {
        Menu {
            Button(L("Settings…")) { store.openSettings() }
                .keyboardShortcut(",")
            Divider()
            ForEach(items) { item in
                if item.divider { Divider() }
                if item.key == "quit" {
                    Button(item.title) { store.send(item.key) }.keyboardShortcut("q")
                } else {
                    Button(item.title) { store.send(item.key) }.disabled(!item.enabled)
                }
            }
        } label: {
            HoverFill {  // like the search icon: secondary color, hover backdrop
                Image(systemName: "ellipsis.circle")
                    .font(.system(size: Metrics.icon))
                    .foregroundStyle(.secondary)
                    .frame(width: Metrics.target, height: Metrics.target)
            }
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .menuIndicator(.hidden)
        .fixedSize()
        .help(L("More"))
        .accessibilityLabel(L("More"))
    }

    private var items: [ButtonModel] {
        var items = panel.footer
        if let cleanup = panel.cleanup {
            items.insert(cleanup, at: items.firstIndex { $0.divider } ?? items.endIndex)
        }
        return items
    }
}

/// A warning or news item: icon at x 16, full text from x 40, buttons below it, Close at the top
/// right.
private struct NoticeRow: View {
    let notice: Notice
    let store: Store

    var body: some View {
        let info = notice.kind == "info"
        let close = notice.buttons.first(where: \.compact)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Group {
                if info {
                    Image(systemName: "arrow.down.circle.fill").foregroundStyle(.tint)
                } else {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
            }
            .font(.callout)
            .frame(width: Metrics.glyph)
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                Text(notice.text)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.trailing, close == nil ? 0 : 20)
                let buttons = notice.buttons.filter { !$0.compact }
                if !buttons.isEmpty {
                    HStack(spacing: 8) {
                        ForEach(buttons) { button in
                            Button(button.title) { store.send(button.key) }
                                .buttonStyle(SmallButtonStyle(destructive: button.role == .destructive))
                                .disabled(!button.enabled)
                        }
                    }
                }
            }
        }
        .padding(8)
        .overlay(alignment: .topTrailing) {
            if let close {
                Button {
                    store.send(close.key)
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .semibold))
                        .frame(width: 24, height: 24)
                        .contentShape(.rect)
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
                .help(close.title)
                .accessibilityLabel(close.title)
                .padding(4)
            }
        }
        // a warning gets a neutral backdrop and relies on the orange icon: an orange fill looks
        // brown in dark mode
        .background(
            info ? Color.accentColor.opacity(0.10) : Color.primary.opacity(0.06),
            in: .rect(cornerRadius: 8)
        )
        .padding(.horizontal, Metrics.rowInset)
    }
}

private struct IdleScreen: View {
    let idle: IdleView
    let hotkey: String?
    let store: Store

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Button {
                    store.send(idle.start.key)
                } label: {
                    FillLabel(title: idle.start.title, symbol: idle.start.symbol)
                }
                .buttonStyle(FillButtonStyle(kind: .red))
                .disabled(!idle.start.enabled)
                .help(startHelp)
                ForEach(idle.sources) { SourceToggle(source: $0, store: store) }
            }
            if let section = idle.section { FieldRow(field: section, symbol: "folder", store: store) }
        }
        .padding(.horizontal, Metrics.inset)
    }

    /// The keyboard shortcut goes in the tooltip, not on the button.
    private var startHelp: String {
        guard let spec = hotkey.flatMap(HotKeySpec.init) else { return idle.start.title }
        return "\(idle.start.title) \(spec.display)"
    }
}

private struct RecordingScreen: View {
    let recording: RecordingView
    let store: Store

    var body: some View {
        let main = recording.buttons.filter { !$0.compact && $0.role != .destructive }
        let discard = recording.buttons.filter { !$0.compact && $0.role == .destructive }
        let icons = recording.buttons.filter(\.compact)
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                RecordingDot(paused: recording.paused)
                Text(recording.heading)
                    .font(.system(size: 13, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 8)
                Text(recording.timer)
                    .font(.system(size: 22, weight: .medium, design: .rounded))
                    .monospacedDigit()
            }
            .frame(height: 32)
            .padding(.horizontal, Metrics.inset)
            VStack(spacing: 4) {
                ForEach(recording.sources) {
                    SourceRow(source: $0, paused: recording.paused, store: store)
                }
            }
            .padding(.horizontal, Metrics.inset)
            .padding(.top, 8)
            HStack(spacing: 8) {
                ForEach(main) { button in
                    Button {
                        store.send(button.key)
                    } label: {
                        FillLabel(title: button.title, symbol: button.symbol)
                    }
                    .buttonStyle(FillButtonStyle(kind: button.role == .primary ? .red : .neutral))
                    .disabled(!button.enabled)
                }
            }
            .padding(.horizontal, Metrics.inset)
            .padding(.top, 12)
            HStack(spacing: 8) {
                ForEach(discard) { button in
                    Button(button.title) { store.send(button.key) }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.red)
                        .disabled(!button.enabled)
                }
                Spacer(minLength: 8)
                ForEach(icons) { button in
                    IconButton(symbol: button.symbol, title: button.title, checked: button.checked == true) {
                        store.send(button.key)
                    }
                    .disabled(!button.enabled)
                }
            }
            .frame(height: 28)
            .padding(.leading, Metrics.inset)
            .padding(.trailing, Metrics.iconInset)
            .padding(.top, 8)
        }
    }
}

private struct SourceRow: View {
    let source: Source
    let paused: Bool
    let store: Store

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: source.on ? source.symbol : source.symbolOff)
                .foregroundStyle(.secondary)
                .frame(width: Metrics.glyph)
                .accessibilityHidden(true)
            Text(source.title).lineLimit(1).layoutPriority(1)
            Spacer(minLength: 8)
            if !source.status.isEmpty {
                Text(source.status).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            LevelMeter(levels: source.levels, color: meterColor)
            Toggle(source.title, isOn: Binding(get: { source.on }, set: { _ in store.send(source.key) }))
                .toggleStyle(MiniSwitchStyle())
                .disabled(!source.enabled)
        }
        .frame(height: 28)
    }

    private var meterColor: Color {
        if !source.on || paused { return .secondary.opacity(0.5) }
        return .green
    }
}

/// List header: "Recordings", or the section filter in its place when there are sections; search
/// on the right; while searching, only the field.
private struct ListHeader: View {
    let panel: PanelModel
    let store: Store

    var body: some View {
        HStack(spacing: 4) {
            if store.searching {
                SearchField(store: store)
            } else {
                if store.librarySections.contains(where: { !$0.isEmpty }) {
                    ChoiceMenu(
                        value: shown, title: L("Section"), small: true, maxWidth: 220, choices: filterChoices)
                } else {
                    Text(L("Recordings"))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .accessibilityAddTraits(.isHeader)
                }
                Spacer(minLength: 8)
                IconButton(symbol: "magnifyingglass", title: L("Search by title")) { store.openSearch() }
            }
        }
        .frame(height: 24)  // the 28 pt icon overhangs by 2, keeping the title closer to its list than to the divider
        .padding(.leading, Metrics.inset)
        .padding(.trailing, store.searching ? Metrics.inset : Metrics.iconInset)
    }

    private var shown: String {
        store.recordingSection.map { $0.isEmpty ? L("No section") : $0 } ?? L("All sections")
    }

    private var filterChoices: [MenuChoice] {
        let all = MenuChoice(id: "*", title: L("All sections"), checked: store.recordingSection == nil) {
            store.recordingSection = nil
        }
        return [all]
            + store.librarySections.enumerated().map { i, section in
                MenuChoice(
                    id: "section:\(section)", title: section.isEmpty ? L("No section") : section,
                    checked: store.recordingSection == section, divider: i == 0
                ) { store.recordingSection = section }
            }
    }
}

/// AppKit search field: opens on the button, ⌘F or a first letter; Return opens the first match's
/// folder; empty and unfocused closes the search. Esc is handled in PanelKeys.
private struct SearchField: NSViewRepresentable {
    let store: Store

    func makeCoordinator() -> Coordinator { Coordinator(store: store) }

    func makeNSView(context: Context) -> NSSearchField {
        let field = NSSearchField()
        field.placeholderString = store.searchPrompt
        field.delegate = context.coordinator
        field.stringValue = store.recordingQuery
        field.setAccessibilityLabel(store.searchPrompt)
        DispatchQueue.main.async {  // after it is in the window; the typed letter stays unselected
            guard field.window?.makeFirstResponder(field) == true else { return }
            let end = (field.stringValue as NSString).length
            field.currentEditor()?.selectedRange = NSRange(location: end, length: 0)
        }
        return field
    }

    func updateNSView(_ field: NSSearchField, context: Context) {
        if field.stringValue != store.recordingQuery { field.stringValue = store.recordingQuery }
        if field.placeholderString != store.searchPrompt { field.placeholderString = store.searchPrompt }
    }

    @MainActor final class Coordinator: NSObject, NSSearchFieldDelegate {
        let store: Store
        init(store: Store) { self.store = store }

        func controlTextDidChange(_ note: Notification) {
            if let field = note.object as? NSSearchField { store.recordingQuery = field.stringValue }
        }

        func controlTextDidEndEditing(_ note: Notification) {
            if store.recordingQuery.isEmpty { store.closeSearch() }
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
            store.openFirstMatch()
            return true
        }
    }
}

private struct RecordList: View {
    let panel: PanelModel
    let store: Store
    @ObservedObject var heights: PanelHeights

    var body: some View {
        let cards = store.libraryCards
        if panel.cards.isEmpty {
            VStack(spacing: 8) {
                Image(systemName: "waveform")
                    .font(.system(size: 22))
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
                Text(panel.empty).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 32)
        } else if cards.isEmpty {
            Text(store.searching && !store.recordingQuery.isEmpty ? L("Nothing found") : L("Section is empty"))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 24)
                .frame(height: store.listHeightLock, alignment: .top)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(cards) { CardRow(card: $0, store: store) }
                }
                .padding(.horizontal, Metrics.rowInset)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { heights.content = $0 }
                .padding(.bottom, scrolls ? 8 : 0)  // at the end of scrolling the last row is off the edge
            }
            .frame(height: height)
            .onChange(of: height, initial: true) { store.listHeight = height }
        }
    }

    private var height: CGFloat {
        let screen = NSScreen.main?.visibleFrame.height ?? 800
        return ListHeight.of(
            content: heights.content, chrome: heights.chrome, screen: screen, lock: store.listHeightLock)
    }

    private var scrolls: Bool { heights.content > height }
}

/// A recording; a click opens its folder. Title, date and length, then a detail line with the state,
/// gist or a remark, holding a button when the recording awaits an action; while processing, the
/// step is in detail with a bar below. Everything else is in the context menu.
private struct CardRow: View {
    let card: Card
    let store: Store
    @StateObject private var hover = Hover()
    @StateObject private var inline = Width()

    var body: some View {
        let button = card.buttons.first
        let detail = card.detail
        let detailWarn = card.detailWarn
        // VoiceOver reads remarks missing from the detail line after it
        let subtitle = store.subtitle(card)
        let spoken = [subtitle, detail] + card.remarks.filter { $0 != detail }
        Button {
            if let key = card.openKey { store.send(key) }
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .lastTextBaseline, spacing: 4) {
                    Text(card.title)
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                    // remarks without their own line show as a triangle after the title, even a long one
                    if card.warn, !detailWarn { WarnMark() }
                }
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if !detail.isEmpty || button != nil {
                    HStack(spacing: 4) {
                        if detailWarn { WarnMark() }
                        Text(detail)
                            .font(.callout)
                            .foregroundStyle(detailWarn ? .primary : .secondary)
                            .lineLimit(1)
                        if button != nil {
                            Spacer(minLength: 8)
                            Color.clear.frame(width: inline.value)  // room for the button
                        }
                    }
                    .frame(height: Metrics.small)  // rows with and without a button are the same height
                }
                if card.busy {  // step fraction; nil is an indeterminate bar
                    ProgressBar(value: card.progress)
                        .accessibilityHidden(true)  // step and fraction are in the card's value
                        .padding(.top, 4)
                        .padding(.bottom, 2)
                }
            }
            .padding(.horizontal, Metrics.rowInset)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(CardStyle(hovered: hover.on))
        .overlay(alignment: .bottomTrailing) {
            if let button {
                Button(button.title) { store.send(button.key) }
                    .buttonStyle(SmallButtonStyle())
                    .disabled(!button.enabled)
                    .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { inline.value = $0 }
                    .padding(.trailing, Metrics.rowInset)
                    .padding(.bottom, 6)
            }
        }
        .onHover { hover.on = $0 }
        .help(card.tooltip)
        .contextMenu {
            ForEach(card.menu) { item in
                if item.divider { Divider() }
                if !item.children.isEmpty {
                    Menu(item.title) {  // Move to section: the recording's section is checked
                        ForEach(item.children) { child in
                            if child.divider { Divider() }  // Language, Move to section
                            if let checked = child.checked {
                                Toggle(child.title, isOn: Binding(get: { checked }, set: { _ in store.send(child.key) }))
                                    .disabled(!child.enabled)
                            } else {
                                Button(child.title) { store.send(child.key) }.disabled(!child.enabled)
                            }
                        }
                    }
                    .disabled(!item.enabled)
                } else {
                    Button(item.title, role: item.role == .destructive ? .destructive : nil) {
                        store.send(item.key)
                    }
                    .disabled(!item.enabled)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(card.title)
        .accessibilityValue(spoken.filter { !$0.isEmpty }.joined(separator: ", "))
        .accessibilityHint(L("Opens the recording folder"))
        .accessibilityAction { if let key = card.openKey { store.send(key) } }
        .accessibilityActions {
            ForEach(card.actions) { item in
                Button(item.title) { store.send(item.key) }
            }
        }
    }
}

private struct CardStyle: ButtonStyle {
    let hovered: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .contentShape(.rect)
            .background(
                Color.primary.opacity(configuration.isPressed ? 0.12 : hovered ? 0.06 : 0),
                in: .rect(cornerRadius: 6))
    }
}

/// The engine is newer than the app and the panel cannot be built from its messages: say what to do.
private struct OutdatedScreen: View {
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "arrow.triangle.2.circlepath")
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(Strings.outdated).font(.headline).accessibilityAddTraits(.isHeader)
            Text(Strings.outdatedHint).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
            Button(Strings.quit) { NSApp.terminate(nil) }
                .buttonStyle(.borderless)
                .padding(.top, 4)
        }
        .frame(maxWidth: .infinity)
        .padding(Metrics.inset)
    }
}

private struct EngineDownScreen: View {
    let store: Store

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 28))
                .symbolRenderingMode(.multicolor)
                .accessibilityHidden(true)
            Text(Strings.engineDown).font(.headline).accessibilityAddTraits(.isHeader)
            HStack(spacing: 8) {
                Button(Strings.quit) { NSApp.terminate(nil) }
                    .buttonStyle(.borderless)
                Spacer(minLength: 0)
                Button(Strings.openLog) { store.openLog() }
                Button(Strings.restart) { store.restartEngine() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(Metrics.inset)
    }
}

/// Keeps the settings window in front and active. A menu bar app (LSUIElement) opens the SwiftUI
/// settings window behind other apps: NSApp.activate() is only a request the active app may keep
/// refusing, so clicks go there and the first one just closes the panel. While settings are open,
/// Mnema is a regular app with a Dock icon, which lets macOS activate it; once closed it is
/// menu-bar-only again.
@MainActor
enum SettingsWindow {
    private static var closing: NSObjectProtocol?

    static func bringForward(open: () -> Void) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        open()
        DispatchQueue.main.async { front(attempts: 10) }
    }

    /// The window does not appear at once, so look for it over a few run loop turns.
    private static func front(attempts: Int) {
        guard let window = NSApp.windows.first(where: isSettings) else {
            if attempts > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { front(attempts: attempts - 1) }
            }
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        if closing == nil {
            closing = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification, object: window, queue: .main
            ) { _ in
                MainActor.assumeIsolated {
                    if let closing { NotificationCenter.default.removeObserver(closing) }
                    closing = nil
                    NSApp.setActivationPolicy(.accessory)
                }
            }
        }
    }

    /// NSApp is nil in unit tests (Store.openForm asks this without an application).
    static var isOpen: Bool { NSApp?.windows.contains { isSettings($0) && $0.isVisible } ?? false }

    static func isSettings(_ window: NSWindow) -> Bool {
        isSettings(
            identifier: window.identifier?.rawValue, title: window.title,
            tabs: AppDelegate.store.panel?.settings.tabs.map(\.title) ?? [])
    }

    /// The SwiftUI settings window takes the selected tab's title, which the engine renders in the
    /// UI language, so it is matched against the current tab titles rather than fixed words.
    nonisolated static func isSettings(identifier: String?, title: String, tabs: [String]) -> Bool {
        identifier == "com_apple_SwiftUI_Settings_window" || (!title.isEmpty && tabs.contains(title))
    }
}

/// Runs a modal window (NSAlert, folder picker) with Mnema in front: otherwise a menu bar app opened
/// it behind other apps and Mnema stopped responding while it was open. Afterwards Mnema is
/// menu-bar-only again unless settings are open.
@MainActor
enum Foreground {
    static func modal<T>(_ body: () -> T) -> T {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        defer { if !SettingsWindow.isOpen { NSApp.setActivationPolicy(.accessory) } }
        return body()
    }
}
