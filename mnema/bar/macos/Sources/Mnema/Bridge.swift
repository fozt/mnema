import Foundation

/// How to start the engine: the venv interpreter, its PATH and the log.
struct Launch: Equatable {
    var python: String
    var path: String
    var log: String
    var libpython: String? = nil  // MnemaLibPython (written by `mnema-bar install`) or MNEMA_LIBPYTHON
    var configHome: String? = nil  // MnemaConfigHome: the installer's XDG_CONFIG_HOME
    var env: [String: String] = [:]  // more engine environment (the self-contained app)

    static let defaultLog = NSHomeDirectory() + "/.cache/mnema/bar.log"
}

enum BridgeError: Error, CustomStringConvertible {
    case noLibrary
    case pipe

    var description: String {
        switch self {
        case .noLibrary:
            return "no libpython: set MnemaLibPython in Info.plist (mnema-bar install) or MNEMA_LIBPYTHON"
        case .pipe:
            return "could not create pipes to the engine"
        }
    }
}

extension Launch {
    /// From the environment (MNEMA_PYTHON with MNEMA_LIBPYTHON, MNEMA_PATH, MNEMA_LOG: a terminal
    /// run) or Info.plist (MnemaPython, MnemaLibPython, MnemaPath, MnemaLog, written by
    /// `mnema-bar install`); the environment wins. Neither names a python: the self-contained
    /// app, everything from its bundle.
    /// The config directory comes only from Info.plist: the one the installer and CLI wrote to.
    /// nil when there is no python to run.
    init?(
        env: [String: String] = ProcessInfo.processInfo.environment,
        info: [String: Any] = Bundle.main.infoDictionary ?? [:],
        bundle: String = Bundle.main.bundlePath
    ) {
        func value(_ envKey: String, _ plistKey: String) -> String? {
            [env[envKey], info[plistKey] as? String].compactMap { $0 }.first { !$0.isEmpty }
        }
        let log = value("MNEMA_LOG", "MnemaLog") ?? Launch.defaultLog
        guard let python = value("MNEMA_PYTHON", "MnemaPython") else {
            self.init(bundle: bundle, log: log)
            return
        }
        self.init(
            python: python,
            path: value("MNEMA_PATH", "MnemaPath") ?? env["PATH"] ?? "/usr/bin:/bin",
            log: log,
            libpython: value("MNEMA_LIBPYTHON", "MnemaLibPython"),
            configHome: (info["MnemaConfigHome"] as? String).flatMap { $0.isEmpty ? nil : $0 })
    }

    /// The self-contained app (scripts/build_mac_app.py): Python, its packages, the mnema
    /// command and ffmpeg are in Contents/Resources, so the app runs from any folder.
    /// nil — no Python in the bundle.
    init?(bundle: String, log: String) {
        let res = bundle + "/Contents/Resources"
        let python = res + "/python/bin/python3.12"
        guard FileManager.default.isExecutableFile(atPath: python) else { return nil }
        self.init(
            python: python,
            path: [res + "/bin", res + "/python/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
                .joined(separator: ":"),
            log: log,
            libpython: res + "/python/lib/libpython3.12.dylib",
            env: [
                "SSL_CERT_FILE": res + "/python/lib/python3.12/site-packages/certifi/cacert.pem",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",  // the signed bundle is never written to
                "MNEMA_APP_BUNDLE": bundle,
            ])
    }
}

/// Splits engine output chunks into whole lines: a chunk may end mid-line; empty lines are skipped.
struct LineBuffer {
    private var tail = Data()

    mutating func feed(_ chunk: Data) -> [Data] {
        tail.append(chunk)
        guard let end = tail.lastIndex(of: 0x0A) else { return [] }
        let lines = tail[..<end].split(separator: 0x0A).map { Data($0) }
        tail = Data(tail[(end + 1)...])
        return lines
    }
}

/// The engine is the Python venv inside the app process (EmbeddedPython): its stdin and stdout
/// are pipes on descriptors 0 and 1, stderr is the log. Messages and exit arrive on the main
/// thread in order; call it from the main thread too.
final class Bridge {
    var onMessage: (Outgoing) -> Void = { _ in }
    var onExit: (_ expected: Bool) -> Void = { _ in }
    var onOutdated: () -> Void = {}  // a state did not decode: the protocol is newer than the app
    private var python: EmbeddedPython?
    private var input: FileHandle?
    private var log: FileHandle?
    private var expected = false
    private(set) var running = false

    func start(_ launch: Launch) throws {
        guard !running else { return }
        openLog(launch.log)
        do {
            guard let library = launch.libpython else { throw BridgeError.noLibrary }
            if python == nil { python = try EmbeddedPython(library: library) }
        } catch {
            write("engine \(launch.python) failed to start: \(error)")
            throw error
        }
        var toEngine: [Int32] = [0, 0]
        var fromEngine: [Int32] = [0, 0]
        guard pipe(&toEngine) == 0, pipe(&fromEngine) == 0 else { throw BridgeError.pipe }
        dup2(toEngine[0], 0)
        close(toEngine[0])
        dup2(fromEngine[1], 1)
        close(fromEngine[1])
        if let log { dup2(log.fileDescriptor, 2) }
        input = FileHandle(fileDescriptor: toEngine[1], closeOnDealloc: true)
        setenv("PATH", launch.path, 1)
        setenv("PYTHONEXECUTABLE", launch.python, 1)  // the venv is found by the pyvenv.cfg next to it
        if let home = launch.configHome { setenv("XDG_CONFIG_HOME", home, 1) }
        for (name, value) in launch.env { setenv(name, value, 1) }
        setenv("MNEMA_UI_LANG", UILanguage.current, 1)  // the engine renders in the app language
        FileManager.default.changeCurrentDirectoryPath(NSHomeDirectory())
        expected = false
        running = true
        let ended = DispatchGroup()  // output fully read and the engine exited
        ended.enter()
        ended.enter()
        let out = FileHandle(fileDescriptor: fromEngine[0], closeOnDealloc: true)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var buffer = LineBuffer()
            while true {
                let chunk = out.availableData  // blocks for output; empty means the engine closed it
                if chunk.isEmpty { break }
                let lines = buffer.feed(chunk)
                DispatchQueue.main.async { self?.deliver(lines) }
            }
            ended.leave()
        }
        python?.run { code in
            // the engine never reached its output (failed on import): close the pipe for it
            let devnull = open("/dev/null", O_WRONLY)
            dup2(devnull, 1)
            close(devnull)
            ended.leave()
            if code != 0 { NSLog("Mnema: engine exited with error %d", code) }
        }
        ended.notify(queue: .main) { [weak self] in self?.exited() }
    }

    func send(_ msg: Incoming) {
        guard let input else { return }
        do {
            try input.write(contentsOf: Wire.encode(msg))
        } catch {
            write("could not send to the engine: \(error.localizedDescription)")
        }
    }

    /// The engine will exit on its own (it was asked to, or it sent quit): not a failure.
    func expectExit() { expected = true }

    private func deliver(_ lines: [Data]) {
        for line in lines {
            do {
                onMessage(try Wire.decode(line: line))
            } catch {
                if line.range(of: Data(#""type": "state""#.utf8)) != nil
                    || line.range(of: Data(#""type":"state""#.utf8)) != nil
                {
                    onOutdated()
                }
                write("skipping engine line \"\(String(decoding: line.prefix(300), as: UTF8.self))\": \(error)")
            }
        }
    }

    private func exited() {
        running = false
        input = nil  // the old engine's reader thread gets EOF and exits
        if !expected { write("engine stopped") }
        onExit(expected)
    }

    /// The log is appended to (O_APPEND): the engine writes there too.
    private func openLog(_ path: String) {
        try? FileManager.default.createDirectory(
            atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        log = fd < 0 ? nil : FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    }

    private static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss,SSS"
        return f
    }()

    private func write(_ text: String) {
        guard let log else { return NSLog("Mnema: %@", text) }
        try? log.write(contentsOf: Data("\(Self.stamp.string(from: Date())) WARNING Mnema: \(text)\n".utf8))
    }
}
