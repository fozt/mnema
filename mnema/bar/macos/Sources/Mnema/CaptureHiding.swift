import AppKit

/// Mnema's own windows (panel, settings, dialogs) stay out of screen sharing, screen recordings
/// and screenshots: the user sees them, a call's screen share does not.
@MainActor
enum CaptureHiding {
    private static var observer: NSObjectProtocol?

    static func start() {
        hideAll()
        // every pass of the event loop: windows of SwiftUI and NSAlert appear without a hook of ours
        observer = NotificationCenter.default.addObserver(
            forName: NSApplication.didUpdateNotification, object: nil, queue: .main
        ) { _ in
            MainActor.assumeIsolated { hideAll() }
        }
    }

    private static func hideAll() {
        for window in NSApp.windows where window.sharingType != .none {
            window.sharingType = .none
        }
    }
}
