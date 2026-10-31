import Foundation
import MnemaASRProtocol

/// Replies go to a copy of stdout; fd 1 itself points to stderr, so a print from a library never
/// lands in the JSON pipe (as isolate_stdout in mnema/core/worker.py).
final class Output: @unchecked Sendable {
    private let fd: Int32
    private let lock = NSLock()
    private var lastProgress: Data?

    private init(fd: Int32) {
        self.fd = fd
    }

    static func takeStdout() -> Output {
        let copy = dup(STDOUT_FILENO)
        dup2(STDERR_FILENO, STDOUT_FILENO)
        return Output(fd: copy)
    }

    /// Progress equal to the previous line (the same stage again) is not repeated.
    func send(_ reply: Reply) {
        let data = ASRWire.encode(reply)
        lock.lock()
        defer { lock.unlock() }
        if case .progress = reply {
            guard data != lastProgress else { return }
            lastProgress = data
        } else {
            lastProgress = nil
        }
        data.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                let written = write(fd, buffer.baseAddress! + offset, buffer.count - offset)
                if written < 0 {
                    if errno == EINTR { continue }
                    exit(1)  // nobody reads the replies any more
                }
                offset += written
            }
        }
    }
}

/// Log lines on stderr as "LEVEL name: text": Python moves them into its log at that level.
enum Log {
    static func info(_ text: String) {
        line("INFO", text)
    }

    static func warning(_ text: String) {
        line("WARNING", text)
    }

    static func error(_ text: String) {
        line("ERROR", text)
    }

    private static func line(_ level: String, _ text: String) {
        FileHandle.standardError.write(Data("\(level) mnema-asr: \(text)\n".utf8))
    }
}
