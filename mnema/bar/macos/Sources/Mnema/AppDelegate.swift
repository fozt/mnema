import AppKit

/// App lifecycle: the engine is Python inside this process (Bridge), plus its windows and
/// notifications. Quitting goes through the engine: ⌘Q and "Quit Mnema" ask it ("quit"), logging
/// out does not ask ("quit_now"): the recording is saved, then the engine sends quit. The screen
/// video is closed before the app exits and when the engine stops: the file stays readable.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let store = Store()
    private let bridge = Bridge()
    private let screen = ScreenRecorder()
    private var launch: Launch?
    private var panelOpen = false  // a restarted engine learns the panel is open
    private var engineQuit = false  // the engine itself sent quit
    private var terminating = false  // quitting waits for the engine (terminateLater)

    /// Mnema is already running: a second instance exits before showing its icon.
    func applicationWillFinishLaunching(_ note: Notification) {
        if let id = Bundle.main.bundleIdentifier,
            NSRunningApplication.runningApplications(withBundleIdentifier: id)
                .contains(where: { $0.processIdentifier != getpid() })
        {
            exit(0)
        }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        signal(SIGPIPE, SIG_IGN)  // writing to a dead engine's stdin gives an error, not a crash
        CaptureHiding.start()
        let store = Self.store
        store.sink = { [weak self] msg in
            if case .panel(let open) = msg { self?.panelOpen = open }
            if case .action(let key) = msg,
                ScreenAccess.shouldAsk(key: key, panel: store.panel, granted: ScreenAccess.granted)
            {
                ScreenAccess.ask()  // turned on: granting now leaves time to restart before a recording
            }
            self?.bridge.send(msg)
            if case .panel(true) = msg { self?.reportScreenAccess() }  // granted in System Settings
            if case .action("toggle_screen") = msg { self?.reportScreenAccess() }
        }
        store.onRestart = { [weak self] in self?.startEngine() }
        store.onOpenLog = { [weak self] in
            NSWorkspace.shared.open(URL(fileURLWithPath: self?.launch?.log ?? Launch.defaultLog))
        }
        bridge.onMessage = { [weak self] in self?.handle($0) }
        bridge.onOutdated = { Self.store.outdated = true }
        bridge.onExit = { [weak self] in self?.engineExited(expected: $0) }
        screen.onEvent = { [weak self] in self?.bridge.send(.screen($0)) }
        Notifications.shared.start { store.send($0) }
        launch = Launch()
        startEngine()
    }

    /// The icon was removed from the menu bar: launching Mnema again brings it back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        Self.store.showIcon()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard bridge.running, !engineQuit else {
            guard screen.active else { return .terminateNow }
            screen.stop(report: false) { NSApp.reply(toApplicationShouldTerminate: true) }
            return .terminateLater
        }
        terminating = true
        bridge.expectExit()
        // Log out, restart, or Quit from outside: no engine question, since a refusal would leave
        // terminateLater unanswered; the reply comes on quit or when the engine exits.
        Self.store.send("quit_now")
        return .terminateLater
    }

    private func startEngine() {
        guard let launch else {
            NSLog("Mnema: no python in the bundle and no MNEMA_PYTHON")
            Self.store.engineDown = true
            return
        }
        engineQuit = false
        do {
            try bridge.start(launch)
            reportScreenAccess()
            if panelOpen { bridge.send(.panel(open: true)) }
        } catch {
            Self.store.engineDown = true
        }
    }

    /// The engine shows "Permission needed" by a "Screen" that is on until macOS lets Mnema record.
    private func reportScreenAccess() {
        bridge.send(.screenAccess(granted: ScreenAccess.granted))
    }

    private func handle(_ msg: Outgoing) {
        let store = Self.store
        switch msg {
        case .state(let panel):
            store.outdated = panel.version != Wire.version
            store.panel = panel
            store.engineDown = false
            let spec = panel.hotkey
            if !HotKeyCenter.shared.update(spec: spec, onPress: { store.send("toggle_record") }), let spec {
                store.send("hotkey_busy:\(spec)")
            }
        case .ask(let id, .form(let ask)):
            store.openForm(id, ask)  // a live form is a sheet over settings, not a modal NSAlert
        case .ask(let id, .unknown):
            store.answer(id, .null)  // a window this app cannot draw: closed, nothing happens
        case .ask(let id, let ask):
            // Not from a main-queue block: otherwise engine messages stall while the window is open.
            RunLoop.main.perform(inModes: [.common]) {
                MainActor.assumeIsolated {
                    store.answer(id, Foreground.modal { Dialogs.answer(ask) })
                }
            }
        case .form(let id, let ask):
            store.updateForm(id, ask)
        case .notify(let note):
            Notifications.shared.post(note)
        case .settings(let tab):
            store.showSettings(tab)  // click on the server in the panel header
        case .screen(.start, let path, let quality):
            // no file to write: refused at once, the engine does not wait for the first frame
            guard !path.isEmpty else { return bridge.send(.screen(.failed(.writer))) }
            screen.start(path: path, quality: quality)
        case .screen(.pause, _, _):
            screen.pause()
        case .screen(.resume, _, _):
            screen.resume()
        case .screen(.stop, _, _):
            screen.stop()
        case .quit:
            engineQuit = true
            bridge.expectExit()
            screen.stop(report: false) { [weak self] in
                guard let self else { return }
                if terminating {
                    finishTerminating()
                } else {
                    NSApp.terminate(nil)
                }
            }
        }
    }

    private func engineExited(expected: Bool) {
        if terminating { return screen.stop(report: false) { [weak self] in self?.finishTerminating() } }
        screen.stop(report: false)  // nobody to stop it: the file is closed now
        guard !expected else { return }
        Self.store.panel = nil  // the icon and panel must not show a frozen recording
        Self.store.formID = nil  // the old engine's window closes without answering the new one
        Self.store.form = nil
        Self.store.engineDown = true
    }

    private func finishTerminating() {
        guard terminating else { return }
        terminating = false
        NSApp.reply(toApplicationShouldTerminate: true)
    }
}
