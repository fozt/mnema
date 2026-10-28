import AppKit
import Carbon.HIToolbox
import SwiftUI

/// Shortcut field: click to record a new one (Esc cancels), the cross clears it.
struct HotKeyField: View {
    let field: SettingsField
    let store: Store
    @StateObject private var recorder = HotKeyRecorder()

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            HStack(spacing: 4) {
                recordButton
                if !field.value.isEmpty {
                    Button {
                        recorder.stop()
                        send("")
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                            .frame(width: 28, height: 28)  // HIG hit area
                            .contentShape(.rect)
                    }
                    .buttonStyle(.borderless)
                    .help(L("Remove shortcut"))
                    .accessibilityLabel(L("Remove shortcut"))
                }
            }
            if !field.error.isEmpty {
                Text(field.error).font(.caption).foregroundStyle(.red)
            }
        }
        .onDisappear { recorder.stop() }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didResignKeyNotification)) { _ in
            recorder.stop()
        }
    }

    @ViewBuilder private var recordButton: some View {
        let button = Button {
            if recorder.on { recorder.stop() } else { recorder.start { send($0.description) } }
        } label: {
            ZStack {
                Text(field.placeholder).hidden()  // keeps the width while recording
                Text(label)
            }
            .foregroundStyle(field.value.isEmpty && !recorder.on ? .secondary : .primary)
        }
        if recorder.on {
            button.buttonStyle(.borderedProminent)
        } else {
            button.buttonStyle(.bordered)
        }
    }

    private var label: String {
        if recorder.on || field.value.isEmpty { return field.placeholder }
        return HotKeySpec(field.value)?.display ?? field.value
    }

    private func send(_ spec: String) {
        if let key = field.key { store.send("\(key):\(spec)") }
    }
}

/// Recording a shortcut: while on, the field takes key presses in the app and the global shortcut is off.
@MainActor
final class HotKeyRecorder: ObservableObject {
    @Published private(set) var on = false
    private var monitor: Any?

    func start(_ done: @escaping (HotKeySpec) -> Void) {
        on = true
        HotKeyCenter.shared.paused = true
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            if event.keyCode == UInt16(kVK_Escape) {
                stop()
            } else if let spec = HotKeySpec(event: event) {
                stop()
                done(spec)
            } else {
                NSSound.beep()
            }
            return nil
        }
    }

    func stop() {
        guard on else { return }
        on = false
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        HotKeyCenter.shared.paused = false
    }
}
