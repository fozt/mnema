import AppKit
import Foundation
import Testing

@testable import Mnema

let fixtures = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("Fixtures")

func fixture(_ name: String) throws -> Data {
    try Data(contentsOf: fixtures.appendingPathComponent("\(name).json"))
}

@Test func everyOutgoingFixtureDecodes() throws {
    let names = try FileManager.default.contentsOfDirectory(atPath: fixtures.path)
        .filter { $0.hasPrefix("out-") }
    #expect(names.count >= 10)
    for name in names {
        _ = try Wire.decode(line: Data(contentsOf: fixtures.appendingPathComponent(name)))
    }
}

@Test func idleStateCarriesCardsAndSettingsTabs() throws {
    guard case .state(let panel) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    #expect(panel.screen == .idle)
    #expect(panel.version == Wire.version)  // an app built from the same sources is not out of date
    #expect(panel.cards.count == 4)
    #expect(panel.cards.allSatisfy { $0.openKey?.hasSuffix(":folder") == true })  // a click opens the folder
    #expect(panel.cards[0].menu.prefix(3).map(\.title) == ["Открыть пересказ", "Открыть расшифровку", "Открыть папку"])
    #expect(panel.cards[0].menu.filter(\.divider).map(\.title) == ["Переименовать…", "Удалить запись"])
    #expect(panel.cards[0].tooltip.hasSuffix("Язык: русский"))
    #expect(panel.cards[1].detail == "Без пересказа" && panel.cards[1].detailWarn)  // the reason is in the tooltip
    #expect(panel.cards[3].buttons.map(\.title) == ["Обработать…"])
    #expect(panel.footer.last?.divider == true && panel.cleanup != nil)
    #expect(panel.settings.tabs.map(\.id) == ["record", "server", "general"])
    #expect(panel.idle?.sources.map(\.label) == ["mic", "system"])
}

/// Processing shows in the recording card: the panel top stays Start recording; the running one
/// has a bar with the step fraction and a menu with language and stop; a waiting one is In queue
/// without a bar.
@Test func processingStateShowsProgressInTheCard() throws {
    guard case .state(let panel) = try Wire.decode(line: fixture("out-state-processing")) else {
        Issue.record("not a state")
        return
    }
    #expect(panel.screen == .idle && panel.idle != nil)
    let waiting = panel.cards[0], running = panel.cards[1]
    #expect(waiting.detail == "В очереди" && !waiting.busy)
    #expect(waiting.menu.contains { $0.title == "Убрать из очереди" })
    #expect(running.busy && running.progress == 0.4)
    #expect(running.detail.hasPrefix("Распознаю речь…"))
    let language = try #require(running.menu.first { $0.title == "Язык: русский" })
    #expect(language.children.map(\.checked) == [true, false])
    #expect(running.menu.last?.title == "Остановить обработку")
    guard case .state(let recording) = try Wire.decode(line: fixture("out-state-recording-queue")) else {
        Issue.record("not a state")
        return
    }
    #expect(recording.screen == .recording && recording.cards.first?.busy == true)
}

/// A running update download carries its share to the panel notice and to the settings row; an
/// offered update has no bar.
@Test func updateDownloadCarriesItsShare() throws {
    guard case .state(let panel) = try Wire.decode(line: fixture("out-state-updating")) else {
        Issue.record("not a state")
        return
    }
    #expect(panel.notices.map(\.progress) == [0.42])
    let general = try #require(panel.settings.tabs.first { $0.id == "general" })
    let status = try #require(general.sections.last?.fields.first)
    #expect(status.kind == .status && status.progress == 0.42)
    guard case .state(let idle) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    #expect(idle.notices.map(\.progress) == [nil])
}

/// Move to section is a submenu of the recording menu with the recording's section checked;
/// VoiceOver gets each option but the checked one as a separate action.
@Test func cardMenuCarriesMoveToSectionSubmenu() throws {
    guard case .state(let panel) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    let card = panel.cards[0]
    let move = try #require(card.menu.first { $0.title == "Переместить в раздел" })
    let children = move.children
    #expect(children.map(\.title) == ["Без раздела", "Работа", "Новый раздел…"])
    #expect(children.map(\.checked) == [false, true, nil] && children.last?.divider == true)
    let spoken = card.actions.map(\.title).filter { $0.hasPrefix("Переместить") }
    #expect(spoken == ["Переместить в раздел: Без раздела", "Переместить в раздел: Новый раздел…"])
    #expect(!card.actions.contains { $0.key == move.key })  // an item with a submenu has no action of its own
}

/// New section: Create is enabled once something of the name survives as a folder name.
@Test func newSectionNeedsAUsableName() throws {
    guard case .ask(_, .title(let ask)) = try Wire.decode(line: fixture("out-ask-section")) else {
        Issue.record("not a title window")
        return
    }
    #expect(ask.title == "Новый раздел" && ask.label == "Название" && ask.required)
    #expect(!NameRequired.usable("") && !NameRequired.usable(" ... ") && !NameRequired.usable("?:/"))
    #expect(NameRequired.usable("Проекты") && NameRequired.usable(" a. "))
}

/// A button among the fields stays by its row (Choose model… under Model); buttons at the end of
/// a section go below the group.
@Test func settingsButtonsStayNextToTheirRows() throws {
    guard case .state(let panel) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    let tabs = Dictionary(uniqueKeysWithValues: panel.settings.tabs.map { ($0.id, $0) })
    let processing = try #require(tabs["record"]?.sections.first { $0.title == "Обработка" })
    let record = SectionLayout(processing.fields)
    #expect(record.lines.map { $0.fields.map(\.title) } == [
        ["Распознавание"], ["Движок"], ["Модель"], ["Выбрать модель…"],
        ["Составлять пересказ"], ["Язык пересказа"],
    ])
    #expect(record.footer.isEmpty)
    let server = SectionLayout(try #require(tabs["server"]?.sections.first).fields)
    #expect(server.footer.map(\.title) == ["Подключить другой…", "Отключить"])
    #expect(server.lines.allSatisfy { $0.fields.allSatisfy { $0.kind != .button } })
}

@MainActor @Test func libraryIsOneDatedListWithEachSectionNamed() throws {
    guard case .state(var panel) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    panel.sections = ["Работа", "Пустой"]
    panel.cards[0].section = "Работа"
    panel.cards[0].title = "План"
    panel.cards[0].gist = "Уникальное содержимое"
    UILanguage.override = "ru"
    let store = Store(panel: panel)
    // All sections: every recording in engine order (by date), the section in its subtitle
    #expect(store.libraryCards.map(\.id) == panel.cards.map(\.id))
    #expect(store.subtitle(panel.cards[0]).hasSuffix(" · Работа"))
    #expect(store.subtitle(panel.cards[1]) == panel.cards[1].subtitle)
    store.recordingSection = "Работа"  // inside the selected section its name is not repeated
    #expect(store.libraryCards.map(\.title) == ["План"])
    #expect(store.subtitle(panel.cards[0]) == panel.cards[0].subtitle)
    store.recordingSection = "Пустой"
    #expect(store.libraryCards.isEmpty)
    store.openSearch()  // search covers only the selected section and only titles
    #expect(store.searchPrompt == "Поиск в разделе «Пустой»")
    store.recordingQuery = "пЛаН"
    #expect(store.libraryCards.isEmpty)  // that recording is in another section
    store.recordingSection = "Работа"
    #expect(store.libraryCards.map(\.title) == ["План"])
    store.recordingQuery = "Уникальное содержимое"
    #expect(store.libraryCards.isEmpty)
    store.recordingSection = nil
    #expect(store.searchPrompt == "Поиск по названию")
    store.recordingQuery = "пЛаН"
    #expect(store.libraryCards.map(\.title) == ["План"])
    store.recordingSection = "Пустой"
    store.closeSearch()
    #expect(store.recordingSection == "Пустой" && store.recordingQuery.isEmpty)
}

@Test func listHeightFollowsContentWithinScreenAndLock() {
    // few recordings fit the content; many stop at 520 and the screen edge, but not below 180
    #expect(ListHeight.of(content: 120, chrome: 145, screen: 1000, lock: nil) == 120)
    #expect(ListHeight.of(content: 2000, chrome: 145, screen: 1000, lock: nil) == 520)
    #expect(ListHeight.of(content: 2000, chrome: 300, screen: 600, lock: nil) == 252)
    #expect(ListHeight.of(content: 2000, chrome: 300, screen: 400, lock: nil) == 180)
    #expect(ListHeight.of(content: 60, chrome: 145, screen: 1000, lock: 400) == 400)  // while searching
}

@Test func panelKeysAreReadByPhysicalKey() {
    let none: NSEvent.ModifierFlags = []
    #expect(PanelKey.of(keyCode: 53, characters: "\u{1b}", modifiers: none, editing: true) == .escape)
    #expect(PanelKey.of(keyCode: 3, characters: "а", modifiers: .command, editing: false) == .find)  // ⌘F in the Russian layout
    #expect(PanelKey.of(keyCode: 43, characters: "б", modifiers: .command, editing: false) == .settings)
    #expect(PanelKey.of(keyCode: 12, characters: "й", modifiers: .command, editing: false) == .quit)
    #expect(PanelKey.of(keyCode: 0, characters: "д", modifiers: none, editing: false) == .type("д"))
    #expect(PanelKey.of(keyCode: 0, characters: "Д", modifiers: .shift, editing: false) == .type("Д"))
    #expect(PanelKey.of(keyCode: 0, characters: "д", modifiers: none, editing: true) == .pass)  // in a text field
    #expect(PanelKey.of(keyCode: 0, characters: "a", modifiers: .control, editing: false) == .pass)
    #expect(PanelKey.of(keyCode: 49, characters: " ", modifiers: none, editing: false) == .pass)
    #expect(PanelKey.of(keyCode: 125, characters: "\u{F701}", modifiers: none, editing: false) == .pass)
}

@MainActor @Test func escapeClearsSearchThenClosesItThenThePanel() throws {
    guard case .state(let panel) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    let store = Store(panel: panel)
    var sent: [Incoming] = []
    store.sink = { sent.append($0) }
    store.listHeight = 300
    #expect(store.press(.type("д")) == .handled)  // a letter starts a search
    #expect(store.searching && store.recordingQuery == "д" && store.listHeightLock == 300)
    #expect(store.press(.escape) == .handled && store.searching && store.recordingQuery.isEmpty)
    #expect(store.press(.escape) == .handled && !store.searching && store.listHeightLock == nil)
    #expect(store.press(.escape) == .closePanel)
    #expect(store.press(.settings) == .handled && store.settingsRequests == 1)
    #expect(store.press(.quit) == .handled && sent == [.action(key: "quit")])
    store.press(.find)
    store.recordingQuery = "Release"
    store.openFirstMatch()  // Return: the first match's folder
    #expect(sent.last == .action(key: "record:00000000-0000-4000-8000-000000000003:folder"))
    let empty = Store(panel: { var p = panel; p.cards = []; return p }())
    #expect(empty.press(.type("д")) == .pass && !empty.searching)  // nothing to search
}

/// With the engine down or outdated nobody answers quit, so ⌘Q terminates the app itself.
@MainActor @Test func quitWithoutEngineTerminatesTheApp() {
    let store = Store()
    var sent: [Incoming] = []
    var terminated = 0
    store.sink = { sent.append($0) }
    store.onTerminate = { terminated += 1 }
    store.engineDown = true
    #expect(store.press(.quit) == .handled && terminated == 1 && sent.isEmpty)
    store.engineDown = false
    store.outdated = true
    #expect(store.press(.quit) == .handled && terminated == 2 && sent.isEmpty)
}

/// Writing the same isInserted value does not wake views: otherwise App.body wrote it again and hung.
@MainActor @Test func iconBindingWakesViewsOnlyOnRealChange() {
    final class Flag: @unchecked Sendable { var raised = false }
    let store = Store()
    let woke = Flag()
    withObservationTracking { _ = store.inserted } onChange: { woke.raised = true }
    store.insertedBinding.wrappedValue = true
    store.showIcon()
    #expect(!woke.raised)
    store.insertedBinding.wrappedValue = false
    #expect(woke.raised && !store.inserted)
}

@MainActor @Test func settingsFixtureSelectsTheTab() throws {
    guard case .settings(let tab) = try Wire.decode(line: fixture("out-settings")) else {
        Issue.record("not settings")
        return
    }
    #expect(tab == "server")
    let store = Store()
    store.showSettings(tab)
    #expect(store.settingsTab == "server" && store.settingsRequests == 1)
}

@Test func askFixturesDecodeToTheirKinds() throws {
    guard case .ask(let id, .range(let range)) = try Wire.decode(line: fixture("out-ask-range")) else {
        Issue.record("not a range window")
        return
    }
    #expect(id == 4)
    #expect(range.length > 5000 && range.canReplace)
    #expect(range.partCaption.contains("{}"))
    guard case .ask(7, .form(let form)) = try Wire.decode(line: fixture("out-ask-form-server")) else {
        Issue.record("not a form window")
        return
    }
    #expect(form.fields.map(\.id) == ["connection"])
    guard case .form(7, let error?) = try Wire.decode(line: fixture("out-form-error")),
        case .form(7, nil) = try Wire.decode(line: fixture("out-form-close"))
    else {
        Issue.record("not a form reply")
        return
    }
    #expect(error.rev == 3 && !error.force.isEmpty)
    guard case .ask(_, .form(let server)) = try Wire.decode(line: fixture("out-ask-form-server")),
        case .form(7, let busy?) = try Wire.decode(line: fixture("out-form-busy"))
    else {
        Issue.record("not the server window or its check")
        return
    }
    #expect(server.fields.map(\.id) == ["connection"])
    #expect(!busy.busy.isEmpty)
    let provider = try modelForm("out-ask-form-model-provider")
    #expect(provider.fields.map(\.kind) == [.choice])
    #expect(provider.fields[0].choices.first(where: \.checked)?.key == "gemini")
    let key = try modelForm("out-ask-form-model-key")
    #expect(key.fields.map(\.kind) == [.secret] && key.fields[0].choices.isEmpty)
    let model = try modelForm("out-form-model-model")
    #expect(model.fields[0].choices.filter { $0.detail == "бесплатная" }.count == 2)
}

/// A model window step from a sample: AskMsg or FormMsg of window 8.
func modelForm(_ name: String) throws -> FormAsk {
    switch try Wire.decode(line: fixture(name)) {
    case .ask(8, .form(let ask)), .form(8, let ask?): return ask
    default: throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "not a model window"))
    }
}

/// Model window steps: values hold only the current step's fields; a choice starts at the checked
/// option; clear erases a refused key; the Model step event is what the engine expects.
@MainActor @Test func modelWindowStepsKeepOnlyCurrentFields() throws {
    let form = LiveForm(id: 8, ask: try modelForm("out-ask-form-model-provider"))
    #expect(form.values == ["provider": "gemini"])
    form.values["provider"] = "openrouter"
    #expect(form.nextEvent("submit") == .formEvent(id: 8, rev: 1, op: "submit", values: ["provider": "openrouter"]))
    var key = try modelForm("out-ask-form-model-key")
    key.rev = 1
    form.apply(key)
    #expect(form.values == ["key": ""])
    form.values["key"] = "sk-or-v1-bad"
    _ = form.nextEvent("submit")  // rev 2
    var refused = key
    refused.rev = 2
    refused.error = "Ключ модели не подошёл"
    refused.clear = ["key"]
    form.apply(refused)
    #expect(form.values == ["key": ""] && form.ask.error == refused.error)
    form.values["key"] = "sk-or-v1-abc"
    _ = form.nextEvent("submit")  // rev 3
    var busy = refused  // the engine builds the busy state from the previous one, so clear stays in it
    busy.rev = 3
    busy.busy = "Проверяю…"
    busy.error = ""
    form.apply(busy)
    #expect(form.values == ["key": "sk-or-v1-abc"])  // the new key is checked and kept
    form.apply(try modelForm("out-form-model-model"))
    #expect(form.values == ["model": "qwen/qwen3-235b-a22b:free"])
    let sent = try JSONSerialization.jsonObject(with: Wire.encode(form.nextEvent("submit"))) as? NSDictionary
    #expect(sent == (try JSONSerialization.jsonObject(with: fixture("in-form-model")) as? NSDictionary))
    key.rev = 4  // the model refused the key: back to Key with a new empty field
    form.apply(key)
    #expect(form.values == ["key": ""])
}

/// A choice keeps the user's option while it is in the list; clear goes back to the engine's
/// checked one.
@MainActor @Test func choiceKeepsPickedVariantWhileItIsListed() throws {
    let model = try modelForm("out-form-model-model")
    let form = LiveForm(id: 8, ask: model)
    form.values["model"] = "openai/gpt-5.5"
    var reply = model
    reply.rev = 0
    reply.error = "Модель вернула пустой ответ: выберите другую модель"
    form.apply(reply)
    #expect(form.values["model"] == "openai/gpt-5.5")
    reply.fields[0].choices.removeAll { $0.key == "openai/gpt-5.5" }
    form.apply(reply)
    #expect(form.values["model"] == "qwen/qwen3-235b-a22b:free")
    form.values["model"] = "anthropic/claude-sonnet-5"
    reply.clear = ["model"]
    form.apply(reply)
    #expect(form.values["model"] == "qwen/qwen3-235b-a22b:free")
}

@Test func messageAddressOpensOverHttps() throws {
    let text = try modelForm("out-ask-form-model-key").message
    let linked = FormSheet.linked(text)
    let links = linked.runs.compactMap { run in run.link.map { (String(linked[run.range].characters), $0) } }
    #expect(links.count == 1)
    #expect(links.first?.0 == "aistudio.google.com/apikey")
    #expect(links.first?.1 == URL(string: "https://aistudio.google.com/apikey"))
    #expect(FormSheet.linked("Сейчас: gemini/gemini-flash-latest, ключ …a1B2").runs.allSatisfy { $0.link == nil })
    // NSDataDetector misses a scheme-less .ai domain
    let openrouter = FormSheet.linked("Где взять ключ OpenRouter: openrouter.ai/settings/keys")
    #expect(openrouter.runs.compactMap(\.link) == [URL(string: "https://openrouter.ai/settings/keys")!])
}

func connectionForm() throws -> FormAsk {
    guard case .ask(7, .form(let ask)) = try Wire.decode(line: fixture("out-ask-form-server")) else {
        throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "not a connection window"))
    }
    return ask
}

/// An engine reply leaves typed input alone; a reply to an event followed by newer ones is ignored.
@MainActor @Test func formReplyKeepsWhatTheUserTyped() throws {
    let ask = try connectionForm()
    let form = LiveForm(id: 7, ask: ask)
    #expect(form.values["connection"] == "")
    form.values["connection"] = "http://srv:8000#invite=test-invite"
    _ = form.nextEvent("submit")  // rev 1
    form.values["connection"] = "http://other#invite=new"  // typed while the reply was on its way
    var reply = ask
    reply.rev = 1
    reply.error = "Сервер не отвечает"
    form.apply(reply)
    #expect(form.values["connection"] == "http://other#invite=new")
    #expect(form.ask.error == "Сервер не отвечает")
    _ = form.nextEvent("force")  // rev 2
    reply.error = "устарело"
    form.apply(reply)
    #expect(form.ask.error == "Сервер не отвечает")
    reply.rev = 2
    reply.error = ""
    form.apply(reply)
    #expect(form.ask.error.isEmpty && form.values["connection"] == "http://other#invite=new")
}

@MainActor @Test func closedSheetAnswersByFormID() throws {
    let ask = try connectionForm()
    let store = Store()
    var sent: [Incoming] = []
    store.sink = { sent.append($0) }
    store.formEvent("submit")  // no window, nothing to send
    store.openForm(7, ask)
    store.formEvent("submit")
    store.openForm(8, ask)  // a new window over an open one: the old one gets null
    store.formDismissed()  // onDismiss of the old sheet: 8 is open, so nothing is sent
    store.updateForm(7, nil)  // a reply to a closed window is ignored
    #expect(store.form?.id == 8)
    store.updateForm(8, nil)  // the engine saved and closed it
    store.formDismissed()
    store.formDismissed()
    store.openForm(9, ask)
    store.form = nil  // Cancel
    store.formDismissed()
    let values = LiveForm(id: 7, ask: ask).values
    #expect(
        sent == [
            .formEvent(id: 7, rev: 1, op: "submit", values: values),
            .answer(id: 7, value: .null),
            .answer(id: 8, value: .null),
            .answer(id: 9, value: .null),
        ])
    #expect(store.formID == nil)
}

@Test func incomingMatchesWhatPythonExpects() throws {
    let submitted = ["connection": "http://srv:8000#invite=test-invite"]
    let cases: [(Incoming, String)] = [
        (.action(key: "start"), "in-action"),
        (.formEvent(id: 7, rev: 3, op: "submit", values: submitted), "in-form-submit"),
        (.formEvent(id: 7, rev: 4, op: "force", values: submitted), "in-form-force"),
        (.answer(id: 2, value: .null), "in-answer-cancel"),
        (.panel(open: true), "in-panel"),
    ]
    for (msg, name) in cases {
        let data = try Wire.encode(msg)
        #expect(data.last == 0x0A)
        let ours = try JSONSerialization.jsonObject(with: data) as? NSDictionary
        let theirs = try JSONSerialization.jsonObject(with: fixture(name)) as? NSDictionary
        #expect(ours == theirs, "\(name)")
    }
}

private let sources = fixtures.deletingLastPathComponent().appendingPathComponent("Sources/Mnema")

private func hasCyrillic(_ text: String) -> Bool {
    text.unicodeScalars.contains { (0x0400...0x04FF).contains($0.value) }
}

private func swiftSources() throws -> [(name: String, text: String)] {
    try FileManager.default.contentsOfDirectory(atPath: sources.path).filter { $0.hasSuffix(".swift") }.sorted()
        .map { ($0, try String(contentsOf: sources.appendingPathComponent($0), encoding: .utf8)) }
}

/// Every `L("…")` literal in Sources, unescaped.
private func localizedLiterals() throws -> Set<String> {
    let pattern = try NSRegularExpression(pattern: #"\bL\(\s*"((?:[^"\\]|\\.)*)""#)
    var found = Set<String>()
    for (_, text) in try swiftSources() {
        for match in pattern.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            guard let range = Range(match.range(at: 1), in: text) else { continue }
            found.insert(
                String(text[range]).replacingOccurrences(of: "\\n", with: "\n")
                    .replacingOccurrences(of: "\\\"", with: "\""))
        }
    }
    return found
}

private func placeholders(_ text: String) -> [String] {
    text.matches(of: #/\{[a-z_]+\}/#).map { String($0.output) }.sorted()
}

/// Every `L()` text has a Russian entry with the same placeholders, and every entry is used.
@Test func everyLocalizedTextHasARussianEntry() throws {
    let used = try localizedLiterals()
    #expect(used.count >= 20)
    for text in used {
        let ru = try #require(ruStrings[text], "no Russian entry for \(text)")
        #expect(placeholders(ru) == placeholders(text), "\(text)")
        #expect(!hasCyrillic(text), "the source string is not English: \(text)")
    }
    #expect(Set(ruStrings.keys).subtracting(used).isEmpty, "unused: \(Set(ruStrings.keys).subtracting(used))")
}

/// Russian lives only in the catalog: elsewhere only the time parser's unit words (a parse
/// pattern shared with the desktop and the phone) and their comments are Cyrillic.
@Test func swiftSourcesKeepRussianOnlyInTheCatalog() throws {
    for (name, text) in try swiftSources() where name != "RuStrings.swift" {
        for (index, line) in text.split(separator: "\n", omittingEmptySubsequences: false).enumerated()
        where hasCyrillic(String(line)) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let parser = name == "RangePick.swift" && (line.contains("#/") || trimmed.hasPrefix("//"))
            #expect(parser, "\(name):\(index + 1): \(trimmed)")
        }
    }
}

/// In English the app's own texts (no-engine screens, list header, search, notify-test) have no
/// Cyrillic, and the search prompt names the section.
@MainActor @Test func ownTextsRenderInEnglish() throws {
    guard case .state(var panel) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    panel.sections = ["Work"]
    let store = Store(panel: panel)
    UILanguage.$scoped.withValue("en") {
        var shown = [
            Strings.engineDown, Strings.restart, Strings.openLog, Strings.quit, Strings.outdated,
            Strings.outdatedHint, Strings.notifyBody, Strings.notifyOK, Strings.notifyNotApp,
            Strings.notifyNoAnswer, Strings.notifyDenied, Strings.notifyTimeout, Strings.notifyFailed("timeout"),
            store.searchPrompt,
        ]
        store.recordingSection = ""
        shown.append(store.searchPrompt)
        #expect(store.searchPrompt == "Search in “No section”")
        store.recordingSection = "Work"
        #expect(store.searchPrompt == "Search in “Work”")
        #expect(L("{n}%", ["n": "40"]) == "40%")
        for text in shown { #expect(!hasCyrillic(text), "\(text)") }
    }
    UILanguage.$scoped.withValue("ru") {
        #expect(store.searchPrompt == "Поиск в разделе «Work»")
        #expect(L("{n}%", ["n": "40"]) == "40 %")
    }
}

/// The SwiftUI settings window is titled after the selected tab, which the engine renders in the
/// UI language: it is recognised by the current tab titles, not by fixed words.
@Test func settingsWindowIsFoundByTheEngineTabTitles() {
    let english = ["Recording", "Server", "General"]
    #expect(SettingsWindow.isSettings(identifier: nil, title: "Server", tabs: english))
    #expect(SettingsWindow.isSettings(identifier: nil, title: "Запись", tabs: ["Запись", "Сервер", "Общие"]))
    #expect(!SettingsWindow.isSettings(identifier: nil, title: "Mnema", tabs: english))
    #expect(!SettingsWindow.isSettings(identifier: nil, title: "", tabs: [""]))
    #expect(SettingsWindow.isSettings(identifier: "com_apple_SwiftUI_Settings_window", title: "", tabs: []))
}
