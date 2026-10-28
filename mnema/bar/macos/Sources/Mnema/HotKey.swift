import AppKit
import Carbon.HIToolbox

/// A shortcut from bar.json, like "ctrl+opt+shift+cmd+R". The engine validates it
/// (bar.state.valid_hotkey); this only parses known modifiers and keys.
struct HotKeySpec: Hashable, CustomStringConvertible {
    enum Modifier: String, CaseIterable {
        case ctrl, opt, shift, cmd  // written order

        fileprivate var parts: (symbol: String, carbon: Int, flag: NSEvent.ModifierFlags) {
            switch self {
            case .ctrl: ("⌃", controlKey, .control)
            case .opt: ("⌥", optionKey, .option)
            case .shift: ("⇧", shiftKey, .shift)
            case .cmd: ("⌘", cmdKey, .command)
            }
        }
    }

    let mods: [Modifier]  // in written order
    let key: String  // "R", "7", "F5", "Space"
    let code: UInt32  // kVK_*: the physical key

    init?(_ text: String) {
        var parts = text.components(separatedBy: "+")
        let key = parts.removeLast()
        let mods = parts.compactMap(Modifier.init(rawValue:))
        guard mods.count == parts.count else { return nil }
        self.init(mods, key)
    }

    init?(event: NSEvent) {
        guard let key = Self.codes.first(where: { $0.value == Int(event.keyCode) })?.key else {
            return nil
        }
        self.init(Modifier.allCases.filter { event.modifierFlags.contains($0.parts.flag) }, key)
    }

    private init?(_ mods: [Modifier], _ key: String) {
        guard let code = Self.codes[key] else { return nil }
        self.mods = Modifier.allCases.filter(mods.contains)
        self.key = key
        self.code = UInt32(code)
    }

    var description: String { (mods.map(\.rawValue) + [key]).joined(separator: "+") }
    var display: String { mods.map(\.parts.symbol).joined() + (key == "Space" ? "␣" : key) }
    var modifiers: UInt32 { UInt32(mods.reduce(0) { $0 | $1.parts.carbon }) }  // Carbon

    /// Taken by the system (screenshots, Spotlight…): RegisterEventHotKey accepts these anyway.
    var takenBySystem: Bool {
        var list: Unmanaged<CFArray>?
        guard CopySymbolicHotKeys(&list) == noErr,
            let system = list?.takeRetainedValue() as? [[String: Any]]
        else { return false }
        let fn = UInt32(kEventKeyModifierFnMask)  // system F-key shortcuts also carry fn
        return system.contains {
            $0[kHISymbolicHotKeyEnabled as String] as? Bool == true
                && $0[kHISymbolicHotKeyCode as String] as? Int == Int(code)
                && ($0[kHISymbolicHotKeyModifiers as String] as? UInt32).map { $0 & ~fn } == modifiers
        }
    }

    private static let codes: [String: Int] = [
        "A": kVK_ANSI_A, "B": kVK_ANSI_B, "C": kVK_ANSI_C, "D": kVK_ANSI_D, "E": kVK_ANSI_E,
        "F": kVK_ANSI_F, "G": kVK_ANSI_G, "H": kVK_ANSI_H, "I": kVK_ANSI_I, "J": kVK_ANSI_J,
        "K": kVK_ANSI_K, "L": kVK_ANSI_L, "M": kVK_ANSI_M, "N": kVK_ANSI_N, "O": kVK_ANSI_O,
        "P": kVK_ANSI_P, "Q": kVK_ANSI_Q, "R": kVK_ANSI_R, "S": kVK_ANSI_S, "T": kVK_ANSI_T,
        "U": kVK_ANSI_U, "V": kVK_ANSI_V, "W": kVK_ANSI_W, "X": kVK_ANSI_X, "Y": kVK_ANSI_Y,
        "Z": kVK_ANSI_Z, "0": kVK_ANSI_0, "1": kVK_ANSI_1, "2": kVK_ANSI_2, "3": kVK_ANSI_3,
        "4": kVK_ANSI_4, "5": kVK_ANSI_5, "6": kVK_ANSI_6, "7": kVK_ANSI_7, "8": kVK_ANSI_8,
        "9": kVK_ANSI_9, "F1": kVK_F1, "F2": kVK_F2, "F3": kVK_F3, "F4": kVK_F4, "F5": kVK_F5,
        "F6": kVK_F6, "F7": kVK_F7, "F8": kVK_F8, "F9": kVK_F9, "F10": kVK_F10, "F11": kVK_F11,
        "F12": kVK_F12, "Space": kVK_Space,
    ]
}

/// The global start/stop recording shortcut: Carbon, so no Accessibility permission is needed.
@MainActor
final class HotKeyCenter {
    static let shared = HotKeyCenter()

    /// The shortcut is unregistered while the settings field records a new one.
    var paused = false {
        didSet {
            if paused != oldValue, !register() { spec = nil }
        }
    }

    private var spec: String?
    private var ref: EventHotKeyRef?
    private var onPress: () -> Void = {}

    private init() {
        var pressed = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, _ in
                Task { @MainActor in HotKeyCenter.shared.onPress() }
                return noErr
            }, 1, &pressed, nil, nil)
    }

    /// Registers a shortcut ("ctrl+opt+R"; nil unregisters). false when taken or unparsable.
    @discardableResult
    func update(spec: String?, onPress: @escaping () -> Void) -> Bool {
        self.onPress = onPress
        guard spec != self.spec else { return true }
        self.spec = spec
        if register() { return true }
        self.spec = nil
        return false
    }

    /// Drops the previous registration and registers `spec` (only drops it while paused).
    private func register() -> Bool {
        if let ref { UnregisterEventHotKey(ref) }
        ref = nil
        guard let spec, !paused else { return true }
        guard let key = HotKeySpec(spec), !key.takenBySystem else { return false }
        // exclusive: fails if another app holds the shortcut the same way
        return RegisterEventHotKey(
            key.code, key.modifiers, EventHotKeyID(signature: 0x4D4E_4D41, id: 1),  // 'MNMA'
            GetApplicationEventTarget(), OptionBits(kEventHotKeyExclusive), &ref) == noErr
    }
}
