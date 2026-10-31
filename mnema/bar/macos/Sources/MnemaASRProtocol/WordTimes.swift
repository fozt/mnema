// Words of a track from FluidAudio's token times, without FluidAudio: one rule with the server's
// ONNX recognizer (mnema/core/parakeet.py timed_words), pinned by tests/fixtures/word-timings-v1.json.
import Foundation

/// A recognized SentencePiece token and its time in seconds, as FluidAudio's TokenTiming gives it
/// (the word boundary ▁ already a leading space there).
public struct TokenTime: Equatable, Sendable {
    public let piece: String
    public let start: Double
    public let end: Double

    public init(piece: String, start: Double, end: Double) {
        self.piece = piece
        self.start = start
        self.end = end
    }
}

public enum WordTimes {
    /// A word starts at a piece with ▁ or a leading space and spans its pieces; service tokens
    /// (<unk>, <|nospeech|>, <blank>) are neither words nor parts of one.
    public static func words(_ tokens: [TokenTime]) -> [Word] {
        var words: [Word] = []
        var text = ""
        var start = 0.0
        var end = 0.0

        func flush() {
            let word = text.trimmingCharacters(in: .whitespaces)
            if !word.isEmpty { words.append(Word(text: word, start: start, end: end)) }
        }

        for token in tokens {
            if token.piece.hasPrefix("<") && token.piece.hasSuffix(">") { continue }
            let piece = token.piece.replacingOccurrences(of: "\u{2581}", with: " ")
            if piece.isEmpty { continue }
            let new = piece.hasPrefix(" ") || text.isEmpty
            if new && !text.isEmpty {
                flush()
                text = ""
            }
            if new {
                text = piece.hasPrefix(" ") ? String(piece.dropFirst()) : piece
                start = token.start
            } else {
                text += piece
            }
            end = token.end
        }
        flush()
        return words
    }
}
