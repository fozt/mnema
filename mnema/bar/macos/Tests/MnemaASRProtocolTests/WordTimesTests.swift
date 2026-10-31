import Foundation
import Testing

@testable import MnemaASRProtocol

/// tests/fixtures/word-timings-v1.json: the same file the Python side reads.
let wordTimings: [String: Any] = {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("tests/fixtures/word-timings-v1.json")
    let data = try! Data(contentsOf: url)
    return try! JSONSerialization.jsonObject(with: data) as! [String: Any]
}()

/// The fixture's token times; normalized: ▁ as FluidAudio's TokenTiming gives it, a space.
func fixtureTokens(normalized: Bool) -> [TokenTime] {
    (wordTimings["tokens"] as! [[String: Any]]).compactMap { token in
        guard let start = token["start"] as? Double, let end = token["end"] as? Double else {
            return nil  // <unk>: FluidAudio drops it before timing
        }
        let piece = token["piece"] as! String
        return TokenTime(
            piece: normalized ? piece.replacingOccurrences(of: "\u{2581}", with: " ") : piece,
            start: start, end: end)
    }
}

@Test(arguments: [true, false]) func tokenTimesBecomeTheFixtureWords(normalized: Bool) {
    let expected = (wordTimings["words"] as! [[String: Any]]).map {
        Word(text: $0["text"] as! String, start: $0["start"] as! Double, end: $0["end"] as! Double)
    }
    #expect(WordTimes.words(fixtureTokens(normalized: normalized)) == expected)
}

@Test func noTokensNoWords() {
    #expect(WordTimes.words([]).isEmpty)
    #expect(WordTimes.words([TokenTime(piece: "<|nospeech|>", start: 0, end: 0.08)]).isEmpty)
}
