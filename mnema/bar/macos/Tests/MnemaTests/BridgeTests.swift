import Foundation
import Testing

@testable import Mnema

private func text(_ lines: [Data]) -> [String] {
    lines.map { String(decoding: $0, as: UTF8.self) }
}

@Test func lineBufferJoinsBrokenLinesAndSkipsEmptyOnes() {
    var buffer = LineBuffer()
    #expect(buffer.feed(Data(#"{"type":"#.utf8)).isEmpty)  // the line is cut
    let lines = buffer.feed(Data("\"quit\"}\n\n{\"a\":1}\n{\"b\"".utf8))
    #expect(text(lines) == [#"{"type":"quit"}"#, #"{"a":1}"#])
    #expect(text(buffer.feed(Data(":2}\n\n".utf8))) == [#"{"b":2}"#])
    #expect(buffer.feed(Data("\n".utf8)).isEmpty)
}

private func footprint() -> Int {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    _ = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
        }
    }
    return Int(info.phys_footprint)
}

@Test func readingTheEngineKeepsNothingOfWhatItRead() {
    let pipe = Pipe()
    let lineCount = 5000
    let line = Data(repeating: 0x41, count: 40_000) + Data("\n".utf8)  // 200 MB in all
    DispatchQueue.global().async {
        for _ in 0..<lineCount { pipe.fileHandleForWriting.write(line) }
        pipe.fileHandleForWriting.closeFile()
    }
    let done = DispatchSemaphore(value: 0)
    var lines = 0
    var grown = 0
    DispatchQueue.global().async {  // like Bridge: measured before this block ends and frees its pool
        let before = footprint()
        Bridge.readLines(from: pipe.fileHandleForReading) { lines += $0.count }
        grown = footprint() - before
        done.signal()
    }
    done.wait()
    #expect(lines == lineCount)
    #expect(grown < 50_000_000)
}

@Test func launchOfATerminalRunTakesTheVenvFromTheEnvironment() {
    let env = [
        "MNEMA_PYTHON": "/dev/python", "MNEMA_LIBPYTHON": "/dev/libpython.dylib",
        "MNEMA_PATH": "/dev/bin", "MNEMA_LOG": "/tmp/bar.log", "PATH": "/usr/bin",
    ]
    #expect(
        Launch(env: env, bundle: "/nonexistent/Mnema.app")
            == Launch(python: "/dev/python", path: "/dev/bin", log: "/tmp/bar.log", libpython: "/dev/libpython.dylib"))
    #expect(Launch(env: ["MNEMA_PYTHON": "/dev/python", "PATH": "/usr/bin"], bundle: "/nonexistent/Mnema.app")?.path == "/usr/bin")
    #expect(Launch(env: ["PATH": "/usr/bin", "MNEMA_PYTHON": ""], bundle: "/nonexistent/Mnema.app") == nil)
}

@Test func launchRunsTheBundledPythonWhenNothingNamesOne() throws {
    let bundle = FileManager.default.temporaryDirectory.appendingPathComponent("Mnema-\(UUID()).app").path
    defer { try? FileManager.default.removeItem(atPath: bundle) }
    #expect(Launch(env: ["PATH": "/usr/bin"], bundle: bundle) == nil)  // no Python inside
    let res = bundle + "/Contents/Resources"
    try FileManager.default.createDirectory(atPath: res + "/python/bin", withIntermediateDirectories: true)
    FileManager.default.createFile(
        atPath: res + "/python/bin/python3.12", contents: Data(), attributes: [.posixPermissions: 0o755])
    let launch = try #require(Launch(env: ["PATH": "/opt/homebrew/bin:/usr/bin"], bundle: bundle))
    #expect(launch.python == res + "/python/bin/python3.12")
    #expect(launch.libpython == res + "/python/lib/libpython3.12.dylib")
    #expect(launch.path == "\(res)/bin:\(res)/python/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    #expect(launch.env["MNEMA_APP_BUNDLE"] == bundle)
    #expect(launch.env["MNEMA_ASR"] == bundle + "/Contents/MacOS/mnema-asr")  // recognition on CoreML
    #expect(launch.env["SSL_CERT_FILE"] == res + "/python/lib/python3.12/site-packages/certifi/cacert.pem")
    #expect(launch.log == Launch.defaultLog)
    // a terminal run of a development build still names its venv
    let dev = Launch(env: ["MNEMA_PYTHON": "/dev/python", "PATH": "/usr/bin"], bundle: bundle)
    #expect(dev == Launch(python: "/dev/python", path: "/usr/bin", log: Launch.defaultLog))
}

@MainActor @Test func bridgeReportsEngineThatCannotStart() throws {
    let bridge = Bridge()
    let log = FileManager.default.temporaryDirectory.appendingPathComponent("mnema-bridge-\(UUID()).log")
    defer { try? FileManager.default.removeItem(at: log) }
    #expect(throws: (any Error).self) {
        try bridge.start(Launch(python: "/nonexistent/python", path: "/usr/bin", log: log.path))
    }
    #expect(!bridge.running)
    #expect(try String(contentsOf: log, encoding: .utf8).contains("/nonexistent/python"))
}

@MainActor private func until(_ done: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(10)
    while !done() {
        guard Date() < deadline else { throw Timeout() }
        try await Task.sleep(nanoseconds: 10_000_000)
    }
}

private struct Timeout: Error {}
