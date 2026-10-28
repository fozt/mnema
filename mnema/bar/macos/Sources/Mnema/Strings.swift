/// Texts needed when there is no engine (it failed to start or crashed); every other string
/// comes from Python in the PanelModel.
enum Strings {
    static var engineDown: String { L("Mnema stopped") }
    static var restart: String { L("Restart") }
    static var openLog: String { L("Open log") }
    static var quit: String { L("Quit Mnema") }
    static var outdated: String { L("App is out of date") }
    static var outdatedHint: String { L("Quit Mnema and open it again. If that doesn't help, reinstall Mnema") }

    // `Mnema notify-test`
    static var notifyBody: String { L("Notifications work") }
    static var notifyOK: String { L("Notifications are allowed. A test notification was shown.") }
    static var notifyNotApp: String {
        L(
            "Mnema is not running as an app, so there are no notifications. Check it like this:\n~/Applications/Mnema.app/Contents/MacOS/Mnema notify-test"
        )
    }
    static var notifyNoAnswer: String {
        L("Notification permission was not granted. Allow notifications for Mnema in System Settings > Notifications.")
    }
    static var notifyDenied: String {
        L("Notifications are turned off. Allow notifications for Mnema in System Settings > Notifications.")
    }
    static var notifyTimeout: String { L("The notification was not confirmed within 10 seconds.") }
    static func notifyFailed(_ reason: String) -> String { L("Notification not delivered: {reason}", ["reason": reason]) }
}
