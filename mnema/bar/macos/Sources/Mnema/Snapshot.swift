import AppKit
import SwiftUI

/// Renders a SwiftUI view to PNG through a real off-screen window: ImageRenderer draws system
/// controls (buttons, switches, lists) as placeholders, a window draws them as on screen.
/// Missing: background blur (nothing is behind the window) and the inactive-window accent color.
enum Snapshot {
    private final class KeyWindow: NSWindow {
        override var canBecomeKey: Bool { true }
        override var canBecomeMain: Bool { true }
    }

    @MainActor static func png<V: View>(_ view: V, dark: Bool, to url: URL) throws {
        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let host = NSHostingView(rootView: view)
        let size = host.fittingSize
        let frame = NSRect(origin: .zero, size: size)
        let background = NSVisualEffectView(frame: frame)
        background.material = .popover
        background.state = .active
        background.blendingMode = .behindWindow
        host.frame = frame
        host.autoresizingMask = [.width, .height]
        background.addSubview(host)
        let window = KeyWindow(
            contentRect: NSRect(x: -10_000, y: -10_000, width: size.width, height: size.height),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = background
        window.isOpaque = false
        window.backgroundColor = .windowBackgroundColor
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))  // layout and appear animations
        window.setContentSize(host.fittingSize)  // system controls learn their size only inside a window
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        background.layoutSubtreeIfNeeded()
        background.display()
        guard let rep = background.bitmapImageRepForCachingDisplay(in: background.bounds) else {
            throw SnapshotError.noBitmap
        }
        background.cacheDisplay(in: background.bounds, to: rep)
        window.orderOut(nil)
        guard let data = rep.representation(using: .png, properties: [:]) else {
            throw SnapshotError.noBitmap
        }
        try data.write(to: url)
    }
}

enum SnapshotError: Error {
    case noBitmap
}
