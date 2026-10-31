import Foundation
import Testing

@testable import MnemaASRProtocol

/// tests/fixtures/asr-helper-v1.json: the same file the Python side reads.
let contract: [String: Any] = {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("tests/fixtures/asr-helper-v1.json")
    let data = try! Data(contentsOf: url)
    return try! JSONSerialization.jsonObject(with: data) as! [String: Any]
}()

func section(_ name: String) -> [String: Any] {
    contract[name] as! [String: Any]
}

func line(_ object: Any) throws -> Data {
    try JSONSerialization.data(withJSONObject: object)
}

/// The reply as Python reads it: compared as parsed JSON, key order aside.
func parsed(_ reply: Reply) throws -> NSDictionary {
    let data = ASRWire.encode(reply)
    #expect(data.last == 0x0A && !data.dropLast().contains(0x0A))  // exactly one line
    return try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) as! NSDictionary
}

func expected(_ name: String) -> NSDictionary {
    section("replies")[name] as! NSDictionary
}

@Test func fixtureRequestsDecode() throws {
    let requests = section("requests")
    #expect(try ASRWire.decode(line: line(requests["models"]!)) == .models)
    #expect(try ASRWire.decode(line: line(requests["download"]!)) == .download(id: "parakeet"))
    #expect(try ASRWire.decode(line: line(requests["download_vad"]!)) == .download(id: "vad"))
    #expect(
        try ASRWire.decode(line: line(requests["transcribe"]!))
            == .transcribe(id: "parakeet", wavs: ["/tmp/rec/mic.wav", "/tmp/rec/system.wav"]))
    #expect(try ASRWire.decode(line: line(requests["speech"]!)) == .speech(wav: "/tmp/rec/system.wav"))
}

/// A request Python could not have meant gets the protocol code, never a guess.
@Test func badRequestsAnswerProtocol() {
    let bad = contract["bad_requests"] as! [String]
    #expect(bad.count >= 5)
    for text in bad {
        do {
            _ = try ASRWire.decode(line: Data(text.utf8))
            Issue.record("decoded: \(text)")
        } catch let failure as ASRFailure {
            #expect(failure.code == .protocol, "\(text)")
        } catch {
            Issue.record("\(error)")
        }
    }
}

@Test func repliesMatchTheContract() throws {
    #expect(try parsed(.progress(.download, 0.25)) == expected("progress"))
    #expect(try parsed(.progress(.load, nil)) == expected("progress_unknown"))
    #expect(try parsed(.downloaded) == expected("download"))
    let words = [Word(text: "Привет,", start: 0.32, end: 0.64), Word(text: "как", start: 0.72, end: 0.88)]
    #expect(try parsed(.transcribed([words, []])) == expected("transcribe"))
    let segments = [SpeechSegment(start: 4096, end: 20480), SpeechSegment(start: 32768, end: 61440)]
    #expect(try parsed(.speech(segments)) == expected("speech"))
    #expect(try parsed(.error(ASRFailure(.noModel, "parakeet is not downloaded"))) == expected("error"))
    #expect(ErrorCode.allCases.map(\.rawValue) == contract["error_codes"] as! [String])
}

/// Frame times computed in floating point (9 × 0.08) reach Python in milliseconds.
@Test func wordTimesAreMilliseconds() throws {
    let reply = try parsed(.transcribed([[Word(text: "да", start: 9 * 0.08, end: 0.1 + 0.2)]]))
    let word = (((reply["value"] as! NSDictionary)["tracks"] as! [NSDictionary])[0]["words"] as! [NSDictionary])[0]
    #expect(word["start"] as! Double == 0.72 && word["end"] as! Double == 0.3)
}

@Test func progressFractionStaysWithinBounds() throws {
    #expect(try parsed(.progress(.recognize, 1.2))["fraction"] as! Double == 1)
}
