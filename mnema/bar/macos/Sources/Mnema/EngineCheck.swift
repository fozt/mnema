import Foundation

/// `Mnema --engine-check`: start the engine the way the app does (Launch, the embedded Python),
/// wait for its first panel state, ask it to quit and wait for it to exit. No menu bar, no
/// windows; scripts/smoke_mac_app.py runs it under a temporary HOME. 0 — the engine answered.
/// Only the exit code reports: stdout and stderr become the engine's pipe and log.
enum EngineCheck {
    @MainActor static func run(timeout: TimeInterval = 120) -> Int32 {
        guard let launch = Launch() else {
            FileHandle.standardError.write(Data("engine-check: no python to run\n".utf8))
            return 2
        }
        let bridge = Bridge()
        var answered = false
        var finished = false
        bridge.onMessage = { msg in
            guard case .state = msg, !answered else { return }
            answered = true
            bridge.expectExit()
            bridge.send(.action(key: "quit_now"))
        }
        bridge.onExit = { _ in finished = true }
        do {
            try bridge.start(launch)
        } catch {
            FileHandle.standardError.write(Data("engine-check: \(error)\n".utf8))
            return 1
        }
        let deadline = Date().addingTimeInterval(timeout)
        while !finished && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        return answered && finished ? 0 : 1
    }
}
