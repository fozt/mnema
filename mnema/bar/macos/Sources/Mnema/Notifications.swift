import Foundation
import UserNotifications

/// Engine notifications (NotifyMsg). UNUserNotificationCenter needs a bundled app (Mnema.app);
/// without one they go to NSLog. The banner shows even while Mnema is active; a click sends the
/// notification's open_key back to the engine.
final class Notifications: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifications()
    private static let openKey = "open_key"
    private var onOpen: (String) -> Void = { _ in }

    private static var center: UNUserNotificationCenter? {
        Bundle.main.bundleIdentifier == nil ? nil : .current()
    }

    /// Asks for permission at launch, so the system prompt does not appear mid-processing.
    func start(onOpen: @escaping (String) -> Void) {
        self.onOpen = onOpen
        guard let center = Self.center else { return }
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            if !granted {
                NSLog("Mnema: notifications not allowed: %@", String(describing: error))
            }
        }
    }

    func post(_ note: NotifyMsg) {
        guard let center = Self.center else {
            return NSLog("Mnema: %@: %@", note.title, note.message)
        }
        let content = UNMutableNotificationContent()
        content.title = note.title
        content.body = note.message
        if let key = note.openKey {
            content.userInfo = [Self.openKey: key]
        }
        center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) { error in
            if let error {
                NSLog("Mnema: notification not delivered: %@", error.localizedDescription)
            }
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if let key = response.notification.request.content.userInfo[Self.openKey] as? String {
            DispatchQueue.main.async { self.onOpen(key) }
        }
        completionHandler()
    }

    /// `Mnema notify-test`: waits for the permission answer, posts a test notification and reports
    /// whether it was delivered. Returns 0 when notifications work.
    static func selfTest() -> Int32 {
        guard let center else {
            print(Strings.notifyNotApp)
            return 1
        }
        let asked = Reply<Bool>()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in asked.set(granted) }
        guard let granted = asked.wait(60) else {  // the first time, wait for the answer in the system prompt
            print(Strings.notifyNoAnswer)
            return 1
        }
        guard granted else {
            print(Strings.notifyDenied)
            return 1
        }
        let content = UNMutableNotificationContent()
        content.title = "Mnema"
        content.body = Strings.notifyBody
        let sent = Reply<String>()  // empty means delivered, otherwise the reason
        center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) {
            sent.set($0?.localizedDescription ?? "")
        }
        guard let failure = sent.wait(10) else {
            print(Strings.notifyTimeout)
            return 1
        }
        print(failure.isEmpty ? Strings.notifyOK : Strings.notifyFailed(failure))
        return failure.isEmpty ? 0 : 1
    }
}

/// A completion-handler result awaited with a timeout (the handler runs on another queue).
private final class Reply<Value>: @unchecked Sendable {
    private let done = DispatchSemaphore(value: 0)
    private var value: Value?

    func set(_ value: Value) {
        self.value = value
        done.signal()
    }

    func wait(_ seconds: Double) -> Value? {
        done.wait(timeout: .now() + seconds) == .success ? value : nil
    }
}
