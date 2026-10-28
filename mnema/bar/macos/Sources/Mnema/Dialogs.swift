import AppKit

/// System windows the engine asks for (AskMsg). The answer is the AnswerMsg value; null means the
/// window was closed. All texts come with the request.
@MainActor
enum Dialogs {
    /// The window and how to read its answer; `ok` means the first button was pressed.
    typealias Prepared = (alert: NSAlert, read: (_ ok: Bool) -> JSONValue)

    static func answer(_ ask: Ask) -> JSONValue {
        switch ask {
        case .title(let a): return run(title(a))
        case .confirm(let a): return run(confirm(a), escape: true)
        case .form: return .null  // handled elsewhere: a live form is a FormSheet (AppDelegate → Store.openForm)
        case .range(let a): return run(range(a))
        case .folder(let a): return folder(a)
        }
    }

    static func title(_ a: TitleAsk) -> Prepared {
        let alert = alert(a.title, ok: a.ok, cancel: a.cancel)
        let field = NSTextField(string: a.value)
        field.placeholderString = a.placeholder
        field.widthAnchor.constraint(equalToConstant: 300).isActive = true
        if !a.label.isEmpty {  // New section: the label sits left of the field
            field.setAccessibilityLabel(a.label)
            let row = NSStackView(views: [Self.label(a.label), field])
            row.orientation = .horizontal
            row.alignment = .firstBaseline
            row.spacing = 8
            alert.accessoryView = fitted(row)
        } else {
            alert.accessoryView = fitted(field)
        }
        alert.window.initialFirstResponder = field
        let guarded = a.required ? NameRequired(field, ok: alert.buttons[0]) : nil
        return (alert, { ok in
            _ = guarded  // keeps OK disabled while the name is empty, for as long as the window is open
            return ok ? .string(field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) : .null
        })
    }

    static func confirm(_ a: ConfirmAsk) -> Prepared {
        let alert = alert(a.title, message: a.message, ok: a.ok, cancel: a.cancel)
        let (ok, cancel) = (alert.buttons[0], alert.buttons[1])
        ok.hasDestructiveAction = true
        ok.keyEquivalent = ""
        cancel.keyEquivalent = "\r"  // Return picks the safe choice, not the deletion
        return (alert, { .bool($0) })
    }

    /// Process recording: which part (sliders and time fields), title, language, tracks, and
    /// whether the full recording goes to the Trash.
    static func range(_ a: RangeAsk) -> Prepared {
        let alert = alert(a.title, message: a.subtitle, ok: a.ok, cancel: a.cancel)
        let controls = RangeControls(a)
        let checks = SourceChecks(a.sources, ok: alert.buttons[0])
        let (start, end) = (controls.rows[0], controls.rows[1])
        let name = NSTextField(string: a.name)
        name.placeholderString = a.placeholder
        let lang = popup(a.langs, selected: a.lang)
        let asr = popup(a.asrs, selected: a.asr)
        let grid = NSGridView(views: [
            [label(a.startLabel), start.slider, start.field],
            [label(a.endLabel), end.slider, end.field],
            [NSGridCell.emptyContentView, controls.caption],
            [label(a.nameLabel), name],
            [label(a.langLabel), lang],
            [label(a.asrLabel), asr],
            [label(a.sourcesLabel), checks.row],
            [NSGridCell.emptyContentView, controls.replace],
        ])
        grid.rowSpacing = 10
        grid.columnSpacing = 8
        grid.rowAlignment = .firstBaseline
        grid.column(at: 0).xPlacement = .trailing
        for row in 2...7 {
            grid.row(at: row).mergeCells(in: NSRange(location: 1, length: 2))
        }
        grid.row(at: 6).isHidden = a.sources.count < 2  // one track: nothing to choose
        grid.row(at: 7).isHidden = !a.canReplace  // not a Mnema recording folder: not offered
        for ((slider, field), title) in zip(controls.rows, [a.startLabel, a.endLabel]) {
            slider.widthAnchor.constraint(equalToConstant: 260).isActive = true
            field.widthAnchor.constraint(equalToConstant: 84).isActive = true
            slider.setAccessibilityLabel(title)  // VoiceOver: the title is a separate label
            field.setAccessibilityLabel(title)
        }
        name.setAccessibilityLabel(a.nameLabel)
        lang.setAccessibilityLabel(a.langLabel)
        asr.setAccessibilityLabel(a.asrLabel)
        alert.accessoryView = fitted(grid)
        alert.window.initialFirstResponder = start.field
        return (alert, { ok in
            guard ok else { return .null }
            var answer: [String: JSONValue] = [
                "start": .string(start.field.stringValue),
                "end": .string(end.field.stringValue),
                "title": .string(name.stringValue),
                "replace": .bool(a.canReplace && controls.replace.state == .on),
                "lang": .string(lang.selectedItem?.representedObject as? String ?? a.lang),
                "asr": .string(asr.selectedItem?.representedObject as? String ?? ""),
            ]
            if !a.sources.isEmpty {
                answer["sources"] = .array(checks.chosen.map { .string($0) })
            }
            return .object(answer)
        })
    }

    private static func popup(_ choices: [Choice], selected: String) -> NSPopUpButton {
        let button = NSPopUpButton()
        for choice in choices {
            button.addItem(withTitle: choice.title)
            button.lastItem?.representedObject = choice.key
            if choice.key == selected { button.select(button.lastItem) }
        }
        return button
    }

    private static func folder(_ a: FolderAsk) -> JSONValue {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = a.message
        panel.prompt = a.ok
        panel.directoryURL = URL(fileURLWithPath: a.start)
        panel.level = .floating
        guard panel.runModal() == .OK, let url = panel.url else { return .null }
        return .string(url.path)
    }

    /// `escape`: Esc cancels the window itself, since Cancel's key equivalent is Return (confirm).
    private static func run(_ prepared: Prepared, escape: Bool = false) -> JSONValue {
        let monitor = escape ? NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.keyCode == 53 else { return event }  // Esc
            NSApp.stopModal(withCode: .alertSecondButtonReturn)
            return nil
        } : nil
        defer { if let monitor { NSEvent.removeMonitor(monitor) } }
        // above other apps' windows: a modal window behind them locked up all of Mnema
        prepared.alert.window.level = .floating
        return prepared.read(prepared.alert.runModal() == .alertFirstButtonReturn)
    }

    private static func alert(_ title: String, message: String = "", ok: String, cancel: String) -> NSAlert {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: ok)
        alert.addButton(withTitle: cancel).keyEquivalent = "\u{1b}"  // Esc cancels
        alert.window.autorecalculatesKeyViewLoop = true  // Tab moves between the fields
        return alert
    }

    private static func label(_ text: String) -> NSTextField {
        NSTextField(labelWithString: text)
    }

    /// NSAlert sizes the accessory view from its frame, so compute it with Auto Layout.
    private static func fitted(_ view: NSView) -> NSView {
        view.frame = NSRect(origin: .zero, size: view.fittingSize)
        return view
    }
}

/// A window with a required name (New section): OK is enabled once something of the name survives
/// as a folder name, like the engine's naming.folder_title: forbidden and control characters,
/// spaces and dots do not count.
@MainActor
final class NameRequired: NSObject, NSTextFieldDelegate {
    private let ok: NSButton

    init(_ field: NSTextField, ok: NSButton) {
        self.ok = ok
        super.init()
        field.delegate = self
        ok.isEnabled = Self.usable(field.stringValue)
    }

    nonisolated static func usable(_ name: String) -> Bool {
        let ignored = CharacterSet(charactersIn: "/\\:*?\"<>|.")
            .union(.whitespacesAndNewlines).union(.controlCharacters)
        return name.unicodeScalars.contains { !ignored.contains($0) }
    }

    func controlTextDidChange(_ note: Notification) {
        guard let field = note.object as? NSTextField else { return }
        ok.isEnabled = Self.usable(field.stringValue)
    }
}

/// Tracks in Process recording: at least one must be checked, or Process is disabled.
@MainActor
private final class SourceChecks: NSObject {
    private let sources: [Choice]
    private let boxes: [NSButton]
    private let ok: NSButton
    let row: NSStackView

    init(_ sources: [Choice], ok: NSButton) {
        let boxes = sources.map { NSButton(checkboxWithTitle: $0.title, target: nil, action: nil) }
        let stack = NSStackView(views: boxes)
        stack.orientation = .horizontal
        stack.spacing = 12
        self.sources = sources
        self.ok = ok
        self.boxes = boxes
        row = stack
        super.init()
        for (box, source) in zip(boxes, sources) {
            box.state = source.checked ? .on : .off
            box.target = self
            box.action = #selector(toggled(_:))
        }
        update()
    }

    /// The checked tracks; with a single track (no choice shown), that track.
    var chosen: [String] {
        if sources.count < 2 { return sources.map(\.key) }
        return zip(sources, boxes).filter { $0.1.state == .on }.map { $0.0.key }
    }

    @objc private func toggled(_ sender: NSButton) {
        update()
    }

    private func update() {
        ok.isEnabled = sources.isEmpty || !chosen.isEmpty
    }
}

/// Links the sliders and fields of the range window (state in RangePick): a slider updates its
/// field, typed time moves the slider; the field being typed in is never rewritten.
@MainActor
private final class RangeControls: NSObject, NSTextFieldDelegate {
    private var pick: RangePick
    private let ask: RangeAsk
    let rows: [(slider: NSSlider, field: NSTextField)]
    let caption = NSTextField(labelWithString: "")
    let replace: NSButton

    init(_ ask: RangeAsk) {
        self.ask = ask
        pick = RangePick(length: ask.length)
        rows = [0, ask.length].map { value in
            let field = NSTextField(string: Clock.format(value))
            field.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
            field.alignment = .right
            return (NSSlider(value: value, minValue: 0, maxValue: max(ask.length, 1), target: nil, action: nil), field)
        }
        replace = NSButton(checkboxWithTitle: ask.replaceLabel, target: nil, action: nil)
        super.init()
        replace.state = ask.replace && ask.canReplace ? .on : .off
        caption.textColor = .secondaryLabelColor
        for (slider, field) in rows {
            slider.target = self
            slider.action = #selector(moved(_:))
            slider.isContinuous = true
            field.delegate = self
        }
        sync()
    }

    private func sync(editing: NSTextField? = nil) {
        for ((slider, field), value) in zip(rows, [pick.start, pick.end]) {
            slider.doubleValue = value
            slider.setAccessibilityValueDescription(Clock.format(value))  // VoiceOver reads a time, not a number
            if field !== editing, Clock.parse(field.stringValue) != value.rounded(.towardZero) {
                field.stringValue = Clock.format(value)
            }
        }
        caption.stringValue = pick.caption(ask)
        replace.isEnabled = !pick.whole  // whole recording: nothing to remove
    }

    private func set(_ index: Int, _ seconds: Double, push: Bool, editing: NSTextField? = nil) {
        if index == 0 {
            pick.setStart(seconds, push: push)
        } else {
            pick.setEnd(seconds, push: push)
        }
        sync(editing: editing)
    }

    @objc private func moved(_ sender: NSSlider) {
        guard let index = rows.firstIndex(where: { $0.slider === sender }) else { return }
        set(index, sender.doubleValue, push: true)
    }

    func controlTextDidChange(_ note: Notification) {
        guard case let (index, field, seconds)? = typed(note) else { return }
        set(index, seconds, push: false, editing: field)
    }

    /// Editing ended (Return or focus left): the time applies and the other end follows.
    func controlTextDidEndEditing(_ note: Notification) {
        guard case let (index, _, seconds)? = typed(note) else { return }
        set(index, seconds, push: true)
    }

    private func typed(_ note: Notification) -> (Int, NSTextField, Double)? {
        guard let field = note.object as? NSTextField,
            let index = rows.firstIndex(where: { $0.field === field }),
            let seconds = Clock.parse(field.stringValue)
        else { return nil }
        return (index, field, seconds)
    }
}
