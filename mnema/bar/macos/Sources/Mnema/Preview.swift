import AppKit
import SwiftUI

/// `Mnema --preview panel|settings|form <file.json> <out.png> [--dark] [--tab <id>]
/// [--search <text>] [--section <name>]` renders the panel or the settings window from a sample
/// state, or a form window from an AskMsg or FormMsg (Fixtures/ or engine output), without the menu
/// bar and without Python. --search opens the panel in search, --section selects a list section
/// ("" means no section). The app's own texts follow `MNEMA_UI_LANG`.
enum Preview {
    @MainActor static func run(_ args: [String]) -> Int32 {
        guard args.count >= 3 else {
            fail("Usage: Mnema --preview panel|settings|form <file.json> <out.png> [--dark] [--tab id] [--search text] [--section name]")
            return 2
        }
        let dark = args.contains("--dark")
        do {
            let input = URL(fileURLWithPath: args[1])
            _ = NSApplication.shared
            NSApp.setActivationPolicy(.accessory)
            let out = URL(fileURLWithPath: args[2])
            switch args[0] {
            case "form":
                let store = Store()
                try Snapshot.png(FormSheet(form: try loadForm(input), store: store), dark: dark, to: out)
            case "panel", "settings":
                let store = Store(panel: try loadPanel(input))
                if let i = args.firstIndex(of: "--tab"), i + 1 < args.count {
                    store.settingsTab = args[i + 1]
                }
                if let i = args.firstIndex(of: "--section"), i + 1 < args.count {
                    store.recordingSection = args[i + 1]
                }
                if let i = args.firstIndex(of: "--search"), i + 1 < args.count {
                    store.searching = true
                    store.recordingQuery = args[i + 1]
                }
                if args[0] == "panel" {
                    try Snapshot.png(PanelView(store: store), dark: dark, to: out)
                } else {
                    try Snapshot.png(SettingsView(store: store), dark: dark, to: out)
                }
            default: throw PreviewError.unknown(args[0])
            }
            return 0
        } catch {
            fail("preview: \(error)")
            return 1
        }
    }

    static func loadPanel(_ url: URL) throws -> PanelModel {
        let data = try Data(contentsOf: url)
        if case .state(let panel)? = try? Wire.decode(line: data) {
            return panel
        }
        return try Wire.decoder.decode(PanelModel.self, from: data)
    }

    @MainActor static func loadForm(_ url: URL) throws -> LiveForm {
        switch try Wire.decode(line: Data(contentsOf: url)) {
        case .ask(let id, .form(let ask)), .form(let id, let ask?): return LiveForm(id: id, ask: ask)
        default: throw PreviewError.notForm
        }
    }

    private static func fail(_ text: String) {
        FileHandle.standardError.write(Data((text + "\n").utf8))
    }
}

enum PreviewError: Error {
    case unknown(String)
    case notForm
}
