import Foundation

// Mirror of mnema/bar/protocol.py: same fields, snake_case in JSON ↔ camelCase here
// (Wire.decoder). Samples: Fixtures/*.json (`python -m mnema.bar.protocol`).

enum Role: String, Codable, Hashable {
    case normal, primary, destructive
}

struct ButtonModel: Codable, Hashable, Identifiable {
    var key: String
    var title: String
    var role: Role
    var enabled: Bool
    var symbol: String
    var checked: Bool?
    var compact: Bool
    var divider: Bool  // menu item: a divider before it
    var children: [ButtonModel]  // menu item: a submenu (Move to section)
    var id: String { key }
}

struct StatusItem: Codable, Hashable {
    enum Icon: String, Codable, Hashable {
        case idle, recording, paused, saving, warning
    }

    var icon: Icon
    var text: String
    var tooltip: String
}

struct Notice: Codable, Hashable {
    var text: String
    var buttons: [ButtonModel]
    var kind: String  // "warn" | "info"
    var progress: Double?  // a bar under the text 0..1 (the update download); nil: none
}

struct Source: Codable, Hashable, Identifiable {
    var label: String
    var title: String
    var symbol: String
    var symbolOff: String
    var on: Bool
    var key: String
    var enabled: Bool
    var levels: [Double]
    var status: String
    var id: String { label }
}

struct IdleView: Codable, Hashable {
    var start: ButtonModel
    var sources: [Source]
    var section: SettingsField?  // Section of a new recording; nil when there are no sections
}

struct RecordingView: Codable, Hashable {
    var heading: String
    var timer: String
    var paused: Bool
    var sources: [Source]
    var buttons: [ButtonModel]
}

struct Card: Codable, Hashable, Identifiable {
    var id: String
    var section: String
    var title: String
    var subtitle: String
    var gist: String
    var status: String
    var warn: Bool
    var remarks: [String]
    var openKey: String?  // click: the recording folder
    var buttons: [ButtonModel]  // a button in the detail line, at most one
    var menu: [ButtonModel]
    var detail: String  // third line: state, gist or a remark
    var detailWarn: Bool  // detail is a warning: shown with a triangle
    var tooltip: String  // gist, remarks, language, one per line
    var busy: Bool  // processing: a bar under detail (the step is in detail)
    var progress: Double?  // step fraction 0..1; nil while busy is an indeterminate bar
}

extension Card {
    /// VoiceOver actions: enabled menu items, a submenu option as "Move to section: Work"; the
    /// checked option (the recording is already there) does nothing and is left out.
    var actions: [ButtonModel] {
        menu.filter(\.enabled).flatMap { item -> [ButtonModel] in
            guard !item.children.isEmpty else { return [item] }
            return item.children.filter { $0.enabled && $0.checked != true }.map { child in
                var action = child
                action.title = "\(item.title): \(child.title)"
                return action
            }
        }
    }
}

enum FieldKind: String, Codable, Hashable {
    case toggle, choice, button, status, hotkey
}

enum Tone: String, Codable, Hashable {
    case none, ok, warn, error, busy
}

struct Choice: Codable, Hashable, Identifiable {
    var key: String
    var title: String
    var detail: String
    var checked: Bool
    var enabled: Bool
    var id: String { key }
}

struct SettingsField: Codable, Hashable, Identifiable {
    var kind: FieldKind
    var title: String
    var key: String?
    var checked: Bool
    var enabled: Bool
    var value: String
    var detail: String
    var placeholder: String
    var error: String
    var role: Role
    var choices: [Choice]
    var tone: Tone
    var progress: Double?  // status: a bar under the value 0..1; nil: none
    var id: String { "\(kind.rawValue):\(key ?? title)" }
}

struct SettingsSection: Codable, Hashable, Identifiable {
    var title: String
    var fields: [SettingsField]
    var id: String { title + fields.map(\.id).joined() }
}

struct SettingsTab: Codable, Hashable, Identifiable {
    var id: String
    var title: String
    var symbol: String
    var sections: [SettingsSection]
}

struct SettingsModel: Codable, Hashable {
    var title: String
    var tabs: [SettingsTab]
}

enum Screen: String, Codable, Hashable {
    case idle, recording  // processing shows in the recording cards
}

struct PanelModel: Codable, Hashable {
    var version: Int
    var screen: Screen
    var status: StatusItem
    var header: String
    var `where`: String
    var whereState: String
    var notices: [Notice]
    var idle: IdleView?
    var recording: RecordingView?
    var empty: String
    var cards: [Card]
    var sections: [String]
    var cleanup: ButtonModel?
    var footer: [ButtonModel]
    var settings: SettingsModel
    var hotkey: String?
}

// ── system windows ─────────────────────────────────────────────────────────────

struct TitleAsk: Codable, Hashable {
    var title: String
    var value: String
    var placeholder: String
    var ok: String
    var cancel: String
    var label: String  // field label (Name in New section)
    var required: Bool  // OK is disabled until something of the name survives as a folder name
}

struct ConfirmAsk: Codable, Hashable {
    var title: String
    var message: String
    var ok: String
    var cancel: String
}

struct FolderAsk: Codable, Hashable {
    var start: String
    var message: String
    var ok: String
}

struct RangeAsk: Codable, Hashable {
    var title: String
    var subtitle: String
    var length: Double
    var name: String
    var replace: Bool
    var canReplace: Bool
    var startLabel: String
    var endLabel: String
    var nameLabel: String
    var placeholder: String
    var replaceLabel: String
    var wholeCaption: String
    var partCaption: String
    var langLabel: String
    var lang: String
    var langs: [Choice]
    var asrLabel: String
    var asr: String
    var asrs: [Choice]
    var sourcesLabel: String
    var sources: [Choice]  // the folder's tracks: key is mic | system, checked means checked initially
    var ok: String
    var cancel: String
}

enum FormFieldKind: String, Codable, Hashable {
    case secret  // input is hidden; the engine never sends the value
    case choice  // one of choices; the selected one is Choice.checked
}

/// A form field: LiveForm keeps the input; the engine gets it only with a window event.
struct FormField: Codable, Hashable, Identifiable {
    var id: String
    var label: String
    var kind: FormFieldKind
    var placeholder: String
    var choices: [Choice]  // choice: key is the value in values, detail explains the option
}

/// A window with fields and a check: actions are Incoming.formEvent, engine replies Outgoing.form.
struct FormAsk: Codable, Hashable {
    var title: String
    var message: String
    var fields: [FormField]
    var rev: Int
    var error: String
    var busy: String
    var ok: String
    var force: String
    var cancel: String
    var clear: [String]  // ids of fields whose input is cleared; only in a check reply, not while busy
}

enum Ask: Decodable, Hashable {
    case title(TitleAsk)
    case confirm(ConfirmAsk)
    case folder(FolderAsk)
    case range(RangeAsk)
    case form(FormAsk)

    private enum Keys: String, CodingKey { case kind }

    init(from decoder: Decoder) throws {
        let kind = try decoder.container(keyedBy: Keys.self).decode(String.self, forKey: .kind)
        switch kind {
        case "title": self = .title(try TitleAsk(from: decoder))
        case "confirm": self = .confirm(try ConfirmAsk(from: decoder))
        case "folder": self = .folder(try FolderAsk(from: decoder))
        case "range": self = .range(try RangeAsk(from: decoder))
        case "form": self = .form(try FormAsk(from: decoder))
        default:
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "unknown window \(kind)"))
        }
    }
}

// ── messages ───────────────────────────────────────────────────────────────────

struct NotifyMsg: Codable, Hashable {
    var title: String
    var message: String
    var openKey: String?
}

/// Engine → app.
enum Outgoing: Decodable {
    case state(PanelModel)
    case ask(id: Int, ask: Ask)
    case form(id: Int, ask: FormAsk?)  // nil closes the window (saved)
    case notify(NotifyMsg)
    case settings(tab: String)  // open the settings window on a tab
    case quit

    private enum Keys: String, CodingKey { case type, panel, id, ask, tab }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        let type = try c.decode(String.self, forKey: .type)
        switch type {
        case "state": self = .state(try c.decode(PanelModel.self, forKey: .panel))
        case "ask": self = .ask(id: try c.decode(Int.self, forKey: .id), ask: try c.decode(Ask.self, forKey: .ask))
        case "form":
            self = .form(id: try c.decode(Int.self, forKey: .id), ask: try c.decodeIfPresent(FormAsk.self, forKey: .ask))
        case "notify": self = .notify(try NotifyMsg(from: decoder))
        case "settings": self = .settings(tab: try c.decode(String.self, forKey: .tab))
        case "quit": self = .quit
        default:
            throw DecodingError.dataCorruptedError(
                forKey: .type, in: c, debugDescription: "unknown message \(type)")
        }
    }
}

/// A window answer (AnswerMsg.value): string, flag, list, object or null.
enum JSONValue: Codable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else if let a = try? c.decode([JSONValue].self) {
            self = .array(a)
        } else {
            self = .object(try c.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }
}

/// App → engine.
enum Incoming: Encodable, Hashable {
    case action(key: String)
    case answer(id: Int, value: JSONValue)
    case formEvent(id: Int, rev: Int, op: String, values: [String: String])
    case panel(open: Bool)

    private enum Keys: String, CodingKey { case type, key, id, value, open, rev, op, values }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Keys.self)
        switch self {
        case .action(let key):
            try c.encode("action", forKey: .type)
            try c.encode(key, forKey: .key)
        case .answer(let id, let value):
            try c.encode("answer", forKey: .type)
            try c.encode(id, forKey: .id)
            try c.encode(value, forKey: .value)
        case .formEvent(let id, let rev, let op, let values):
            try c.encode("form_event", forKey: .type)
            try c.encode(id, forKey: .id)
            try c.encode(rev, forKey: .rev)
            try c.encode(op, forKey: .op)
            try c.encode(values, forKey: .values)
        case .panel(let open):
            try c.encode("panel", forKey: .type)
            try c.encode(open, forKey: .open)
        }
    }
}

enum Wire {
    /// = protocol.VERSION; a newer engine (updated without `mnema-bar install`) shows App is out of date.
    static let version = 1

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    static func decode(line: Data) throws -> Outgoing {
        try decoder.decode(Outgoing.self, from: line)
    }

    /// A protocol line: JSON and a newline.
    static func encode(_ msg: Incoming) throws -> Data {
        var data = try JSONEncoder().encode(msg)
        data.append(0x0A)
        return data
    }
}
