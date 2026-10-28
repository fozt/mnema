import AppKit
import Carbon.HIToolbox

/// A key in the panel and what to do with it. Shortcuts match the physical key (keyCode): in the
/// Russian layout ⌘F arrives as a Cyrillic letter.
enum PanelKey: Equatable {
    case escape  // clear search, close it, close the panel
    case find  // ⌘F
    case settings  // ⌘,
    case quit  // ⌘Q
    case type(String)  // a letter without ⌘⌃⌥ outside a text field: starts a search with it
    case pass

    enum Result: Equatable {
        case handled, closePanel, pass
    }

    /// editing: focus is in a text field, so letters go there, not to a new search.
    static func of(
        keyCode: UInt16, characters: String?, modifiers: NSEvent.ModifierFlags, editing: Bool
    ) -> PanelKey {
        let mods = modifiers.intersection([.command, .control, .option, .shift])
        if Int(keyCode) == kVK_Escape { return .escape }
        if mods == .command {
            switch Int(keyCode) {
            case kVK_ANSI_F: return .find
            case kVK_ANSI_Comma: return .settings
            case kVK_ANSI_Q: return .quit
            default: return .pass
            }
        }
        guard !editing, mods.isDisjoint(with: [.command, .control, .option]),
            let text = characters, text.count == 1, let scalar = text.unicodeScalars.first,
            !CharacterSet.controlCharacters.contains(scalar),
            !CharacterSet.whitespacesAndNewlines.contains(scalar),
            !(0xF700...0xF8FF).contains(scalar.value)  // arrows and function keys
        else { return .pass }
        return .type(text)
    }
}

/// Keys of the open panel through one local NSEvent monitor: onExitCommand needs focus, menu item
/// shortcuts work only while the menu is closed, and a menu bar app has no main menu.
@MainActor
final class PanelKeys {
    weak var window: NSWindow?  // the panel window (WindowReader); other windows are left alone
    private var monitor: Any?

    /// A MenuBarExtra window grows by itself but never shrinks: when a filter leaves fewer
    /// recordings, the window is fitted to the panel with its top kept at the menu bar.
    func fit(height: CGFloat) {
        DispatchQueue.main.async { [weak self] in
            guard let window = self?.window, height > 0 else { return }
            let content = window.contentRect(forFrameRect: window.frame)
            guard abs(content.height - height) > 0.5 else { return }
            var frame = window.frameRect(forContentRect: NSRect(
                x: content.minX, y: content.maxY - height, width: content.width, height: height))
            frame.origin.y = window.frame.maxY - frame.height
            window.setFrame(frame, display: true)
        }
    }

    func install(store: Store) {
        remove()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self, weak store] event in
            let (code, characters, modifiers, number) = (
                event.keyCode, event.characters, event.modifierFlags, event.windowNumber
            )
            let result = MainActor.assumeIsolated { () -> PanelKey.Result in
                guard let self, let store, let window = self.window, window.windowNumber == number else {
                    return .pass
                }
                let key = PanelKey.of(
                    keyCode: code, characters: characters, modifiers: modifiers,
                    editing: window.firstResponder is NSText)
                let result = store.press(key)
                if result == .closePanel { window.close() }
                return result
            }
            return result == .pass ? event : nil
        }
    }

    func remove() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }
}
