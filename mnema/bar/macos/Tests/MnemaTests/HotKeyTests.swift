import AppKit
import Carbon.HIToolbox
import Testing

@testable import Mnema

private func keyDown(_ code: Int, _ flags: NSEvent.ModifierFlags) throws -> NSEvent {
    try #require(
        NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0,
            context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false,
            keyCode: UInt16(code)))
}

@Test func specIsWrittenInFixedModifierOrder() {
    let spec = HotKeySpec("cmd+shift+opt+ctrl+R")
    #expect(spec?.description == "ctrl+opt+shift+cmd+R")
    #expect(spec?.display == "⌃⌥⇧⌘R")
    #expect(spec?.modifiers == UInt32(controlKey | optionKey | shiftKey | cmdKey))
    #expect(HotKeySpec("ctrl+opt+R")?.description == "ctrl+opt+R")
    #expect(HotKeySpec("ctrl+opt+R")?.display == "⌃⌥R")
}

@Test func functionKeysAndSpaceMapToTheirKeyCodes() {
    #expect(HotKeySpec("cmd+F5")?.code == UInt32(kVK_F5))
    #expect(HotKeySpec("cmd+F5")?.display == "⌘F5")
    #expect(HotKeySpec("ctrl+opt+Space")?.code == UInt32(kVK_Space))
    #expect(HotKeySpec("ctrl+opt+Space")?.display == "⌃⌥␣")
    #expect(HotKeySpec("opt+7")?.code == UInt32(kVK_ANSI_7))
}

@Test func specParsesOnlyKnownModifiers() {
    for text in ["", "ctrl+", "+R", "alt+R", "ctrl+opt"] {
        #expect(HotKeySpec(text) == nil, "\(text)")
    }
}

/// tests/fixtures/hotkeys-v1.json: keys and shortcuts the engine accepts
/// (bar.state.valid_hotkey; its pytest reads the same file).
private struct EngineHotkeys: Decodable {
    let keys: [String]
    let unknown: [String]
    let valid: [String]
}

private func engineHotkeys() throws -> EngineHotkeys {
    let repo = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let file = repo.appendingPathComponent("tests/fixtures/hotkeys-v1.json")
    return try JSONDecoder().decode(EngineHotkeys.self, from: Data(contentsOf: file))
}

@Test func everyKeyOfTheEngineHasItsOwnCodeAndComesBackFromAKeyPress() throws {
    let table = try engineHotkeys()
    var codes = Set<UInt32>()
    for key in table.keys {
        let spec = try #require(HotKeySpec("ctrl+opt+\(key)"), "\(key)")
        codes.insert(spec.code)
        let pressed = HotKeySpec(event: try keyDown(Int(spec.code), [.control, .option]))
        #expect(pressed == spec, "\(key)")
    }
    #expect(codes.count == table.keys.count)
    for key in table.unknown {
        #expect(HotKeySpec("ctrl+opt+\(key)") == nil, "\(key)")
    }
    for text in table.valid {
        #expect(HotKeySpec(text) != nil, "\(text)")
    }
}

@Test func keyPressGivesTheSpecTheEngineChecks() throws {
    #expect(HotKeySpec(event: try keyDown(kVK_ANSI_R, [.control, .option]))?.description == "ctrl+opt+R")
    #expect(HotKeySpec(event: try keyDown(kVK_F5, [.command, .function]))?.description == "cmd+F5")
    #expect(HotKeySpec(event: try keyDown(kVK_ANSI_R, [.shift, .capsLock]))?.description == "shift+R")
    #expect(HotKeySpec(event: try keyDown(kVK_LeftArrow, [.command])) == nil)
}
