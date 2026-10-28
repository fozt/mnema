import SwiftUI

@main
enum MnemaMain {
    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--preview") {
            let rest = Array(args[(i + 1)...])
            exit(MainActor.assumeIsolated { Preview.run(rest) })
        }
        if args.dropFirst().first == "--engine-check" {
            exit(MainActor.assumeIsolated { EngineCheck.run() })
        }
        if args.dropFirst().first == "notify-test" {
            exit(Notifications.selfTest())
        }
        MnemaApp.main()
    }
}

struct MnemaApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    private var store: Store { AppDelegate.store }

    var body: some Scene {
        MenuBarExtra(isInserted: store.insertedBinding) {
            PanelView(store: store)
        } label: {
            StatusLabel(store: store)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(store: store)
        }
        .commands {
            // ⌘Q in the middle of a call would cut the recording: quit from the panel or settings instead
            CommandGroup(replacing: .appTermination) {}
        }
    }
}
