import Foundation
import Testing

@testable import Mnema

/// tests/fixtures/offsets-v1.json: the typed-time table shared with the desktop and the phone.
private struct Offsets: Decodable {
    struct Typed: Decodable {
        var text: String
        var seconds: Double?
    }

    struct Written: Decodable {
        var seconds: Double
        var clock: String
    }

    struct Whole: Decodable {
        var start: Double
        var end: Double
        var length: Double
        var whole: Bool
    }

    var typed: [Typed]
    var clock: [Written]
    var whole: [Whole]
}

private let shared = fixtures
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().appendingPathComponent("tests/fixtures/offsets-v1.json")

@Test func clockReadsAndWritesTimeLikeTheSharedTable() throws {
    let table = try JSONDecoder().decode(Offsets.self, from: Data(contentsOf: shared))
    for row in table.typed {
        #expect(Clock.parse(row.text) == row.seconds, "«\(row.text)»")
    }
    for row in table.clock {
        #expect(Clock.format(row.seconds) == row.clock, "\(row.seconds)")
    }
    for row in table.whole {
        var pick = RangePick(length: row.length)
        pick.setStart(row.start)
        pick.setEnd(row.end)
        #expect(pick.whole == row.whole, "\(row.start)–\(row.end) of \(row.length)")
    }
}

/// Like test_range_pick_sliders_and_typed_time: the whole recording first; a start slider past the
/// end drags the end along; time still being typed ("0:5" on the way to "0:52") does not move the
/// other end; the caption says how much is selected.
@Test func rangeSlidersAndTypedTime() throws {
    guard case .ask(_, .range(let ask)) = try Wire.decode(line: fixture("out-ask-range")) else {
        Issue.record("not a range window")
        return
    }
    var pick = RangePick(length: 5700.8)
    #expect(pick.start == 0 && pick.end == 5700.8)
    #expect(pick.whole && pick.caption(ask) == "Выбрана вся запись")

    pick.setStart(600)
    pick.setEnd(3130)
    #expect(!pick.whole && pick.caption(ask) == "Выбрано 0:42:10")
    pick.setStart(4000)  // slider past the end: the end follows
    #expect(pick.start == 4000 && pick.end == 4000)
    pick.setStart(9999)
    #expect(pick.start == 5700.8)  // not past the end of the recording

    pick = RangePick(length: 5700.8)
    pick.setStart(600)
    pick.setEnd(Clock.parse("0:5") ?? 0, push: false)  // typing "0:52:10", so far "0:5"
    #expect(pick.start == 600 && pick.end == 5700.8)
    pick.setEnd(Clock.parse("0:52:10") ?? 0, push: false)
    #expect(pick.start == 600 && pick.end == 3130)
    #expect(Clock.parse("0:") == nil && Clock.parse("") == nil)
}

/// The answer carries the chosen language key (RangeAnswer.lang); cancel is null.
@MainActor @Test func rangeAnswerCarriesLanguageKey() throws {
    guard case .ask(_, .range(var ask)) = try Wire.decode(line: fixture("out-ask-range")) else {
        Issue.record("not a range window")
        return
    }
    ask.lang = "en"
    let window = Dialogs.range(ask)
    guard case .object(let answer) = window.read(true) else {
        Issue.record("the answer is not an object")
        return
    }
    #expect(answer["lang"] == .string("en"))
    #expect(answer["start"] == .string("0:00:00") && answer["title"] == .string(ask.name))
    #expect(answer["sources"] == .array([.string("mic")]))  // the silent track starts unchecked
    #expect(window.read(false) == .null)
}

/// At least one track: with none checked Process is disabled; a folder without mic and system
/// tracks has none in the answer.
@MainActor @Test func rangeNeedsOneSource() throws {
    guard case .ask(_, .range(var ask)) = try Wire.decode(line: fixture("out-ask-range")) else {
        Issue.record("not a range window")
        return
    }
    ask.sources = ask.sources.map { source in
        var off = source
        off.checked = false
        return off
    }
    #expect(!Dialogs.range(ask).alert.buttons[0].isEnabled)
    ask.sources = []
    let none = Dialogs.range(ask)
    #expect(none.alert.buttons[0].isEnabled)
    guard case .object(let answer) = none.read(true) else {
        Issue.record("the answer is not an object")
        return
    }
    #expect(answer["sources"] == nil)
}
