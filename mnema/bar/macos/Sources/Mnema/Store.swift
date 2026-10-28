import AppKit
import Foundation
import Observation
import SwiftUI

/// App state on the main thread: the latest model from the engine and the way back to it.
/// Views read `panel` and call `send`/`answer`; the engine bridge (AppDelegate) sets `sink`.
/// View state that does not come from the engine lives here too: `@State` does not build without
/// Xcode (see tests/test_bar_protocol.py).
@MainActor @Observable
final class Store {
    var panel: PanelModel? {
        didSet {
            if let selected = recordingSection, !librarySections.contains(selected) {
                recordingSection = nil
            }
        }
    }
    var engineDown = false  // the engine is not running or crashed
    var outdated = false  // the engine protocol is newer than the app
    private(set) var inserted = true  // menu bar icon shown (MenuBarExtra isInserted)
    var settingsTab = "record"
    var settingsRequests = 0  // bumped on every SettingsMsg; the panel opens the window
    var recordingQuery = ""
    var recordingSection: String?  // nil is all; an empty string is the root
    var searching = false  // the list header turned into a search field
    var listHeightLock: CGFloat?  // list height frozen while searching so the window does not jump
    var form: LiveForm?  // the open form window (a sheet over settings)
    var formID: Int?  // the window id until onDismiss, when `form` is already nil

    @ObservationIgnored var listHeight: CGFloat = 0  // the last shown list height
    @ObservationIgnored let keys = PanelKeys()
    @ObservationIgnored var sink: (Incoming) -> Void = { _ in }
    @ObservationIgnored var onRestart: () -> Void = {}
    @ObservationIgnored var onOpenLog: () -> Void = {}
    @ObservationIgnored var onTerminate: () -> Void = { NSApp.terminate(nil) }

    init(panel: PanelModel? = nil) {
        self.panel = panel
    }

    /// isInserted for MenuBarExtra. SwiftUI writes the unchanged value back too and @Observable
    /// reports every write, so App.body re-evaluated in a loop and the app hung.
    var insertedBinding: Binding<Bool> {
        Binding(get: { self.inserted }, set: { self.showIcon($0) })
    }

    func showIcon(_ shown: Bool = true) {
        if inserted != shown { inserted = shown }
    }

    func send(_ key: String) { sink(.action(key: key)) }
    func answer(_ id: Int, _ value: JSONValue) { sink(.answer(id: id, value: value)) }
    /// The panel opened or closed: panel keys work only while it is visible; on close the search
    /// closes and the selected section stays.
    func panelVisible(_ open: Bool) {
        sink(.panel(open: open))
        if open {
            keys.install(store: self)
        } else {
            keys.remove()
            closeSearch()
        }
    }

    /// Opens settings through the panel (only a view has openSettings).
    func openSettings() { settingsRequests += 1 }

    var hasRecords: Bool { !(panel?.cards.isEmpty ?? true) }

    /// Search by title: the list header becomes a field and the list height freezes.
    func openSearch() {
        guard hasRecords, !searching else { return }
        listHeightLock = listHeight > 0 ? listHeight : nil
        searching = true
    }

    func closeSearch() {
        searching = false
        recordingQuery = ""
        listHeightLock = nil
    }

    /// Return in the search field opens the first match's folder.
    func openFirstMatch() {
        if let key = libraryCards.first?.openKey { send(key) }
    }

    /// A panel key (PanelKeys). Esc clears the search, then closes it, then closes the panel.
    func press(_ key: PanelKey) -> PanelKey.Result {
        switch key {
        case .escape:
            guard searching else { return .closePanel }
            if recordingQuery.isEmpty { closeSearch() } else { recordingQuery = "" }
        case .find:
            openSearch()
        case .settings:
            openSettings()
        case .quit:  // with no engine to answer quit, quit directly
            if engineDown || outdated { onTerminate() } else { send("quit") }
        case .type(let text):
            guard hasRecords else { return .pass }
            openSearch()
            recordingQuery += text
        case .pass:
            return .pass
        }
        return .handled
    }
    func restartEngine() { onRestart() }

    /// SettingsMsg: settings on `tab`; only a view has openSettings, so the panel calls it.
    func showSettings(_ tab: String) {
        settingsTab = tab
        settingsRequests += 1
    }
    func openLog() { onOpenLog() }

    var librarySections: [String] {
        guard let panel else { return [] }
        return Set(panel.sections + panel.cards.map { $0.section }).sorted()
    }

    /// The section selected in the list filter; search looks only there.
    var shownSection: String? { recordingSection }

    /// Search field placeholder: the field covers the filter, so it names the section.
    var searchPrompt: String {
        guard let section = recordingSection else { return L("Search by title") }
        return L("Search in “{name}”", ["name": section.isEmpty ? L("No section") : section])
    }

    /// Recordings as one feed by date: search matches titles only; a section selected in the filter
    /// limits the list to it (panel.visible_cards).
    var libraryCards: [Card] {
        guard let panel else { return [] }
        let query = recordingQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        let selected = shownSection
        return panel.cards.filter {
            (selected == nil || $0.section == selected)
                && (query.isEmpty || $0.title.localizedCaseInsensitiveContains(query))
        }
    }

    /// Date and length; in the all-sections feed also the recording's section (panel.card_subtitle).
    func subtitle(_ card: Card) -> String {
        let section = shownSection == nil ? card.section : ""
        return [card.subtitle, section].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// AskMsg(form): a previous open form is answered with null.
    func openForm(_ id: Int, _ ask: FormAsk) {
        if let formID { answer(formID, .null) }
        form = LiveForm(id: id, ask: ask)
        formID = id
        // the form sheet lives over the settings window, which is not open yet when the form comes
        // from the panel (Connect… on a notice); without this it never appeared
        if !SettingsWindow.isOpen { openSettings() }
    }

    /// FormMsg: a new form state; nil means the engine closed it (saved).
    func updateForm(_ id: Int, _ ask: FormAsk?) {
        guard let form, form.id == id else { return }
        if let ask { form.apply(ask) } else { self.form = nil }
    }

    func formEvent(_ op: String) {
        if let form { sink(form.nextEvent(op)) }
    }

    /// The sheet closed: answer null. If a new form replaced it, openForm already answered.
    func formDismissed() {
        guard form == nil, let id = formID else { return }
        formID = nil
        answer(id, .null)
    }
}

/// A form window: what the user typed per field (secrets live only here) and the engine's latest
/// state. Engine replies keep typed values, except fields in `clear` of a check result.
@MainActor @Observable
final class LiveForm: Identifiable {
    let id: Int
    var ask: FormAsk
    var values: [String: String]
    private(set) var lastSent = 0

    init(id: Int, ask: FormAsk) {
        self.id = id
        self.ask = ask
        values = Self.values(ask, keeping: [:], clearing: [])
    }

    func nextEvent(_ op: String) -> Incoming {
        lastSent += 1
        return .formEvent(id: id, rev: lastSent, op: op, values: values)
    }

    func apply(_ new: FormAsk) {
        guard new.rev >= lastSent else { return }  // a reply to an event that newer ones superseded
        if !new.error.isEmpty, new.error != ask.error {  // VoiceOver announces the red error line
            AccessibilityNotification.Announcement(new.error).post()
        }
        // apply `clear` only to a check result: the engine builds the "Checking…" state from the
        // previous one, and its `clear` would erase a key typed after a refusal
        values = Self.values(new, keeping: values, clearing: new.busy.isEmpty ? new.clear : [])
        ask = new
    }

    /// Field values for a state: typed values stay; a missing field has no value; a new field, a
    /// cleared one, or a choice whose old option is gone gets empty or the checked option.
    private static func values(
        _ ask: FormAsk, keeping typed: [String: String], clearing: [String]
    ) -> [String: String] {
        var result: [String: String] = [:]
        for field in ask.fields where result[field.id] == nil {
            let kept = clearing.contains(field.id) ? nil : typed[field.id]
            result[field.id] = kept.flatMap { field.accepts($0) ? $0 : nil } ?? field.initial
        }
        return result
    }
}

extension FormField {
    /// A new field's value: the checked option for a choice, empty for a secret.
    var initial: String {
        kind == .choice ? choices.first(where: \.checked)?.key ?? "" : ""
    }

    /// Whether a previously typed value still fits: for a choice, only a listed option.
    func accepts(_ value: String) -> Bool {
        kind != .choice || choices.contains { $0.key == value }
    }
}
