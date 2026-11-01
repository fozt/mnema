import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit
import Testing

@testable import Mnema

private func t(_ seconds: Double) -> CMTime { CMTime(seconds: seconds, preferredTimescale: 1_000_000_000) }

@Test func frameIsAtMost1920WideWithTheScreenProportions() {
    #expect(ScreenCapture.frameSize(pixelWidth: 3024, pixelHeight: 1964) == FrameSize(width: 1920, height: 1246))
    #expect(ScreenCapture.frameSize(pixelWidth: 5120, pixelHeight: 2880) == FrameSize(width: 1920, height: 1080))
    #expect(ScreenCapture.frameSize(pixelWidth: 1440, pixelHeight: 900) == FrameSize(width: 1440, height: 900))
    // HEVC with 4:2:0 needs even sides
    #expect(ScreenCapture.frameSize(pixelWidth: 1367, pixelHeight: 769) == FrameSize(width: 1366, height: 768))
}

@Test func qualityPicksTheWidthAndTheBitrate() {
    let retina = (pixelWidth: 3024, pixelHeight: 1964)
    #expect(
        ScreenCapture.frameSize(pixelWidth: retina.pixelWidth, pixelHeight: retina.pixelHeight, quality: .hd)
            == FrameSize(width: 1280, height: 832))
    #expect(
        ScreenCapture.frameSize(pixelWidth: retina.pixelWidth, pixelHeight: retina.pixelHeight, quality: .fullHD)
            == FrameSize(width: 1920, height: 1246))
    // a screen narrower than the quality keeps its own pixels
    #expect(ScreenCapture.frameSize(pixelWidth: 1024, pixelHeight: 768, quality: .hd) == FrameSize(width: 1024, height: 768))
    // the encoder writes the frame it is given, and the lower quality spends fewer bits on it
    let size = FrameSize(width: 1280, height: 832)
    func encoder(_ quality: VideoQuality) -> (width: Int?, height: Int?, bitRate: Int?) {
        let settings = ScreenCapture.outputSettings(size, quality: quality)
        let compression = settings[AVVideoCompressionPropertiesKey] as? [String: Any]
        return (
            settings[AVVideoWidthKey] as? Int, settings[AVVideoHeightKey] as? Int,
            compression?[AVVideoAverageBitRateKey] as? Int
        )
    }
    let hd = encoder(.hd), fullHD = encoder(.fullHD)
    #expect(hd.width == 1280 && hd.height == 832 && fullHD.width == 1280 && fullHD.height == 832)
    #expect((hd.bitRate ?? .max) < (fullHD.bitRate ?? 0))
}

@Test func framesOfAPauseAreDroppedAndLaterOnesShiftByIt() {
    var timeline = PauseTimeline()
    #expect(timeline.place(t(100)) == t(100))
    timeline.pause(at: t(110))
    #expect(timeline.place(t(112)) == nil)  // paused
    timeline.resume(at: t(115))
    #expect(timeline.place(t(113)) == nil)  // a frame of the pause delivered late
    #expect(timeline.place(t(109)) == t(109))  // a frame from before the pause
    #expect(timeline.place(t(120)) == t(115))
    timeline.pause(at: t(130))
    timeline.pause(at: t(131))  // a second pause while paused changes nothing
    timeline.resume(at: t(132))
    #expect(timeline.place(t(140)) == t(133))
}

/// The wait for the first frame counts only capture time: a pause in the first seconds writes no
/// frames and is not a failure.
@Test func pausedTimeDoesNotCountTowardsTheFirstFrameWait() {
    var timeline = PauseTimeline()
    #expect(timeline.active(from: t(100), to: t(103)) == t(3))
    timeline.pause(at: t(101))
    #expect(timeline.active(from: t(100), to: t(110)) == t(1))  // still paused
    timeline.resume(at: t(111))
    #expect(timeline.active(from: t(100), to: t(115)) == t(5))
    // a pause before the capture started does not count against it
    #expect(timeline.active(from: t(112), to: t(115)) == t(3))
}

@Test func videoEndsAtTheStopOrAtThePauseItStoppedIn() {
    var timeline = PauseTimeline()
    timeline.pause(at: t(10))
    timeline.resume(at: t(12))
    #expect(timeline.end(at: t(20)) == t(18))
    timeline.pause(at: t(25))
    #expect(timeline.end(at: t(40)) == t(23))  // stopped while paused: the video ends where the pause began
}

private let mnema: pid_t = 100
private let zoom: pid_t = 200
private let editor: pid_t = 300

@Test func capturedWindowIsTheActiveAppsFrontWindowNotMnemas() {
    let windows = [
        WindowInfo(pid: mnema, bounds: CGRect(x: 0, y: 0, width: 400, height: 600)),
        WindowInfo(pid: editor, bounds: CGRect(x: 2000, y: 0, width: 800, height: 600)),
        WindowInfo(pid: zoom, bounds: CGRect(x: 100, y: 100, width: 800, height: 600)),
        WindowInfo(pid: zoom, bounds: CGRect(x: 3000, y: 100, width: 800, height: 600)),
    ]
    #expect(ScreenCapture.window(windows, front: zoom, own: mnema) == CGRect(x: 100, y: 100, width: 800, height: 600))
    // Mnema is active (its panel was clicked): the front window of another app
    #expect(ScreenCapture.window(windows, front: mnema, own: mnema) == CGRect(x: 2000, y: 0, width: 800, height: 600))
    #expect(ScreenCapture.window(windows, front: nil, own: mnema) == CGRect(x: 2000, y: 0, width: 800, height: 600))
    // the active app has no window on screen
    #expect(ScreenCapture.window(windows, front: 400, own: mnema) == nil)
}

@Test func capturedDisplayHoldsMostOfTheWindowOtherwiseTheMainOne() {
    let displays = [
        DisplayInfo(id: 1, frame: CGRect(x: 0, y: 0, width: 1512, height: 982)),
        DisplayInfo(id: 2, frame: CGRect(x: 1512, y: -200, width: 2560, height: 1440)),
    ]
    let across = CGRect(x: 1200, y: 100, width: 1000, height: 600)  // mostly on the external one
    #expect(ScreenCapture.display(for: across, among: displays, main: 1) == 2)
    #expect(ScreenCapture.display(for: CGRect(x: 10, y: 10, width: 300, height: 300), among: displays, main: 2) == 1)
    #expect(ScreenCapture.display(for: nil, among: displays, main: 2) == 2)
    #expect(ScreenCapture.display(for: CGRect(x: -5000, y: 0, width: 10, height: 10), among: displays, main: 1) == 1)
}

@Test func failureReasonNamesWhatWentWrong() {
    let declined = NSError(domain: SCStreamErrorDomain, code: SCStreamError.Code.userDeclined.rawValue)
    #expect(ScreenCapture.failure(declined, whileRecording: false) == .permission)
    #expect(ScreenCapture.failure(declined, whileRecording: true) == .permission)
    let diskFull = NSError(domain: AVFoundationErrorDomain, code: AVError.Code.diskFull.rawValue)
    #expect(ScreenCapture.failure(diskFull, whileRecording: true) == .noSpace)
    let posix = NSError(domain: NSPOSIXErrorDomain, code: Int(ENOSPC))
    let wrapped = NSError(domain: AVFoundationErrorDomain, code: -11800, userInfo: [NSUnderlyingErrorKey: posix])
    #expect(ScreenCapture.failure(wrapped, whileRecording: true) == .noSpace)
    // the display was disconnected or the lid closed while recording
    let lost = NSError(domain: SCStreamErrorDomain, code: SCStreamError.Code.noCaptureSource.rawValue)
    #expect(ScreenCapture.failure(lost, whileRecording: true) == .displayLost)
    #expect(ScreenCapture.failure(lost, whileRecording: false) == .writer)
    #expect(ScreenCapture.failure(NSError(domain: AVFoundationErrorDomain, code: -11800), whileRecording: true) == .writer)
    #expect(ScreenCapture.failure(nil, whileRecording: true) == .writer)
}

/// A stream that ends by itself: stopped from the menu bar indicator is no failure; before the first
/// frame a stream error is the writer's, after it the display went away.
@Test func streamEndNamesWhoStoppedIt() {
    let userStopped = NSError(domain: SCStreamErrorDomain, code: SCStreamError.Code.userStopped.rawValue)
    #expect(ScreenCapture.end(of: userStopped, afterFirstFrame: true) == .byUser)
    #expect(ScreenCapture.end(of: userStopped, afterFirstFrame: false) == .byUser)
    let lost = NSError(domain: SCStreamErrorDomain, code: SCStreamError.Code.noCaptureSource.rawValue)
    #expect(ScreenCapture.end(of: lost, afterFirstFrame: true) == .failed(.displayLost))
    #expect(ScreenCapture.end(of: lost, afterFirstFrame: false) == .failed(.writer))
}

@Test func screenFixturesDecodeAsPythonWritesThem() throws {
    guard case .screen(.start, let path, let quality) = try Wire.decode(line: fixture("out-screen-start")) else {
        Issue.record("not a screen start")
        return
    }
    #expect(path == "/Users/me/mnema/2026-09-30_10-05/screen.mov" && quality == .fullHD)
    guard case .screen(.start, _, .hd) = try Wire.decode(line: fixture("out-screen-start-720p")) else {
        Issue.record("not a 720p start")
        return
    }
    guard case .screen(.pause, "", _) = try Wire.decode(line: fixture("out-screen-pause")) else {
        Issue.record("not a screen pause")
        return
    }
    guard case .screen(.stop, "", _) = try Wire.decode(line: fixture("out-screen-stop")) else {
        Issue.record("not a screen stop")
        return
    }
}

@MainActor private func until(_ done: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(10)
    while !done() {
        guard Date() < deadline else { throw Timeout() }
        try await Task.sleep(nanoseconds: 10_000_000)
    }
}

private struct Timeout: Error {}

/// The engine waits for stopped after its stop, even when no video was running (it did not start);
/// a stop for a gone engine sends nothing and only reports that the file is closed.
@MainActor @Test func stopIsAnsweredWithStoppedOnlyForTheEngine() async throws {
    let recorder = ScreenRecorder()
    var events: [ScreenEvent] = []
    recorder.onEvent = { events.append($0) }
    recorder.stop()
    try await until { !events.isEmpty }
    #expect(events == [.stopped])
    var closed = false
    recorder.stop(report: false) { closed = true }
    try await until { closed }
    #expect(events == [.stopped] && !recorder.active)
}

/// macOS applies a granted Screen Recording permission only after a restart: Mnema asks when
/// "Screen" is turned on, so it can be granted before the first recording.
@Test func turningTheScreenOnWithoutPermissionAsksForIt() throws {
    guard case .state(let panel) = try Wire.decode(line: fixture("out-state-idle")) else {
        Issue.record("not a state")
        return
    }
    #expect(panel.idle?.sources.first { $0.label == "screen" }?.on == false)
    #expect(ScreenAccess.shouldAsk(key: "toggle_screen", panel: panel, granted: false))
    #expect(!ScreenAccess.shouldAsk(key: "toggle_screen", panel: panel, granted: true))
    #expect(!ScreenAccess.shouldAsk(key: "toggle_mic", panel: panel, granted: false))
    var on = panel  // turning it off asks nothing
    on.idle?.sources = panel.idle?.sources.map { var s = $0; if s.label == "screen" { s.on = true }; return s } ?? []
    #expect(!ScreenAccess.shouldAsk(key: "toggle_screen", panel: on, granted: false))
    var recording = panel  // the toggle does not change while recording
    recording.idle = nil
    #expect(!ScreenAccess.shouldAsk(key: "toggle_screen", panel: recording, granted: false))
}
