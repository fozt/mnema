// Wire format of mnema-asr, the same envelope as mnema/core/worker.py: one JSON request per line on
// stdin ({"operation", "payload"}), JSON replies per line on stdout. Every reply carries the
// protocol version "v"; texts for people are Python's, the helper sends stages and error codes.
// The contract with Python is tests/fixtures/asr-helper-v1.json.
import Foundation

public enum ASRWire {
    /// Bumped on an incompatible change: Python then asks to update Mnema or the command.
    public static let version = 1

    public static func decode(line: Data) throws -> Request {
        guard let object = try? JSONSerialization.jsonObject(with: line),
            let request = object as? [String: Any]
        else { throw ASRFailure(.protocol, "the request is not a JSON object") }
        guard let operation = request["operation"] as? String else {
            throw ASRFailure(.protocol, "the request has no operation")
        }
        let payload = request["payload"] as? [String: Any] ?? [:]
        func text(_ key: String) throws -> String {
            guard let value = payload[key] as? String, !value.isEmpty else {
                throw ASRFailure(.protocol, "\(operation) needs a string \(key)")
            }
            return value
        }
        switch operation {
        case "models":
            return .models
        case "download":
            return .download(id: try text("id"))
        case "transcribe":
            guard let wavs = payload["wavs"] as? [String], !wavs.isEmpty, wavs.allSatisfy({ !$0.isEmpty }) else {
                throw ASRFailure(.protocol, "transcribe needs a list of wavs")
            }
            return .transcribe(id: try text("id"), wavs: wavs)
        case "speech":
            return .speech(wav: try text("wav"))
        default:
            throw ASRFailure(.protocol, "unknown operation \(operation)")
        }
    }

    /// One reply line, newline included.
    public static func encode(_ reply: Reply) -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        // Every field is a string, a number or a list of them: encoding cannot fail.
        var data = (try? encoder.encode(Envelope(reply: reply))) ?? Data()
        data.append(0x0A)
        return data
    }
}

public enum Request: Equatable, Sendable {
    case models
    case download(id: String)
    /// All tracks of a recording in one process, so the model loads once.
    case transcribe(id: String, wavs: [String])
    /// Raw speech segments in samples at 16 kHz; Python groups them.
    case speech(wav: String)
}

public enum Stage: String, Codable, Sendable {
    case download, load, recognize
}

public enum Reply: Sendable {
    /// fraction nil: the stage goes on, its share is unknown.
    case progress(Stage, Double?)
    case models([ModelInfo])
    case downloaded
    /// Words of each track, in the order of the request's wavs.
    case transcribed([[Word]])
    case speech([SpeechSegment])
    case error(ASRFailure)
}

public enum ErrorCode: String, Codable, CaseIterable, Sendable {
    case noModel = "no_model"
    case noSpace = "no_space"
    case downloadFailed = "download_failed"
    case badAudio = "bad_audio"
    case unsupportedMac = "unsupported_mac"
    case `protocol`
    /// Anything else (a CoreML failure, say); the detail goes to the log.
    case failed
}

/// What went wrong: the code for Python, the detail (English) for the log.
public struct ASRFailure: Error, Equatable, Sendable {
    public let code: ErrorCode
    public let detail: String

    public init(_ code: ErrorCode, _ detail: String) {
        self.code = code
        self.detail = detail
    }
}

public struct ModelInfo: Codable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let langs: [String]
    /// Bytes the download takes on disk.
    public let size: Int64
    public let installed: Bool
}

public struct Word: Codable, Equatable, Sendable {
    /// The word with its punctuation, as the model gave it.
    public let text: String
    /// Seconds from the start of the track.
    public let start: Double
    public let end: Double

    public init(text: String, start: Double, end: Double) {
        self.text = text
        // Times are encoder frames (80 ms): milliseconds keep them exact and the JSON short.
        self.start = (start * 1000).rounded() / 1000
        self.end = (end * 1000).rounded() / 1000
    }
}

/// Start and end sample of a stretch of speech, at 16 kHz.
public struct SpeechSegment: Codable, Equatable, Sendable {
    public let start: Int
    public let end: Int

    public init(start: Int, end: Int) {
        self.start = start
        self.end = end
    }

    public init(from decoder: Decoder) throws {
        var pair = try decoder.unkeyedContainer()
        start = try pair.decode(Int.self)
        end = try pair.decode(Int.self)
    }

    public func encode(to encoder: Encoder) throws {
        var pair = encoder.unkeyedContainer()
        try pair.encode(start)
        try pair.encode(end)
    }
}

private struct Envelope: Encodable {
    let reply: Reply

    private enum Key: String, CodingKey {
        case v, type, stage, fraction, value, code, detail
        case ok, tracks, words, segments
    }

    private struct Track: Encodable {
        let words: [Word]
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        try c.encode(ASRWire.version, forKey: .v)
        switch reply {
        case .progress(let stage, let fraction):
            try c.encode("progress", forKey: .type)
            try c.encode(stage, forKey: .stage)
            try c.encode(fraction.map { min(max($0, 0), 1) }, forKey: .fraction)  // null when unknown
        case .error(let failure):
            try c.encode("error", forKey: .type)
            try c.encode(failure.code, forKey: .code)
            try c.encode(failure.detail, forKey: .detail)
        case .models(let models):
            try c.encode("result", forKey: .type)
            try c.encode(models, forKey: .value)
        case .downloaded:
            try c.encode("result", forKey: .type)
            var value = c.nestedContainer(keyedBy: Key.self, forKey: .value)
            try value.encode(true, forKey: .ok)
        case .transcribed(let tracks):
            try c.encode("result", forKey: .type)
            var value = c.nestedContainer(keyedBy: Key.self, forKey: .value)
            try value.encode(tracks.map(Track.init(words:)), forKey: .tracks)
        case .speech(let segments):
            try c.encode("result", forKey: .type)
            var value = c.nestedContainer(keyedBy: Key.self, forKey: .value)
            try value.encode(segments, forKey: .segments)
        }
    }
}
