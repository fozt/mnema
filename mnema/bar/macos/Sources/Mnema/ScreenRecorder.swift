import AVFoundation
import AppKit
import CoreMedia
import ScreenCaptureKit

struct FrameSize: Equatable {
    var width: Int
    var height: Int
}

/// An on-screen app window, as the window list gives it front to back (global coordinates).
struct WindowInfo: Equatable {
    var pid: pid_t
    var bounds: CGRect
}

struct DisplayInfo: Equatable {
    var id: CGDirectDisplayID
    var frame: CGRect  // CGDisplayBounds: the same coordinates as the window bounds
}

/// The rules of the screen video: frame size, encoder, the display to capture, failure codes.
enum ScreenCapture {
    static let maxWidth = 1920
    static let frameRate: Int32 = 15
    static let bitRate = 2_000_000
    static let keyFrameInterval: Double = 10  // seconds
    static let fragmentInterval = CMTime(value: 10, timescale: 1)  // a broken-off file plays up to here

    /// At most 1920 pixels wide, the screen's proportions, even sides (HEVC 4:2:0).
    static func frameSize(pixelWidth: Int, pixelHeight: Int) -> FrameSize {
        let width = min(maxWidth, pixelWidth) / 2 * 2
        let height = Double(pixelHeight) * Double(width) / Double(max(pixelWidth, 1))
        return FrameSize(width: width, height: 2 * Int((height / 2).rounded()))
    }

    /// AVAssetWriterInput settings: hardware HEVC at an average bitrate, a key frame at least every
    /// 10 s, no frame reordering, BT.709 like the captured frames.
    static func outputSettings(_ size: FrameSize) -> [String: Any] {
        [
            AVVideoCodecKey: AVVideoCodecType.hevc,
            AVVideoWidthKey: size.width,
            AVVideoHeightKey: size.height,
            AVVideoColorPropertiesKey: [
                AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
            ],
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: bitRate,
                AVVideoExpectedSourceFrameRateKey: Int(frameRate),
                AVVideoMaxKeyFrameIntervalDurationKey: keyFrameInterval,
                AVVideoAllowFrameReorderingKey: false,
            ] as [String: Any],
        ]
    }

    /// The window that tells which display to capture: the active app's front window; when Mnema
    /// itself is active (its panel started the recording), the front window of another app.
    static func window(_ windows: [WindowInfo], front: pid_t?, own: pid_t) -> CGRect? {
        let others = windows.filter { $0.pid != own && !$0.bounds.isEmpty }
        guard let front, front != own else { return others.first?.bounds }
        return others.first { $0.pid == front }?.bounds
    }

    /// The display holding most of the window; no window or none on a display: the main one.
    static func display(for window: CGRect?, among displays: [DisplayInfo], main: CGDirectDisplayID)
        -> CGDirectDisplayID
    {
        guard let window else { return main }
        func area(_ display: DisplayInfo) -> CGFloat {
            let part = display.frame.intersection(window)
            return part.isNull ? 0 : part.width * part.height
        }
        guard let best = displays.max(by: { area($0) < area($1) }), area(best) > 0 else { return main }
        return best.id
    }

    /// The protocol code of an error: before the first frame a stream error is the writer's, while
    /// recording the captured display went away (unplugged, lid closed, stopped from the menu bar).
    static func failure(_ error: Error?, whileRecording: Bool) -> ScreenFailure {
        guard let error = error as NSError? else { return .writer }
        let stream = error.domain == SCStreamErrorDomain
        if stream && error.code == SCStreamError.Code.userDeclined.rawValue { return .permission }
        if outOfSpace(error) { return .noSpace }
        return stream && whileRecording ? .displayLost : .writer
    }

    private static func outOfSpace(_ error: NSError) -> Bool {
        switch (error.domain, error.code) {
        case (AVFoundationErrorDomain, AVError.Code.diskFull.rawValue), (NSPOSIXErrorDomain, Int(ENOSPC)),
            (NSCocoaErrorDomain, NSFileWriteOutOfSpaceError):
            return true
        default:
            return (error.userInfo[NSUnderlyingErrorKey] as? NSError).map(outOfSpace) ?? false
        }
    }
}

/// The video timeline without the pauses: frames of a pause are dropped, later ones move back by
/// the paused time, as the WAV tracks drop the paused audio. Times are host time.
struct PauseTimeline {
    private var pauses: [(start: CMTime, end: CMTime?)] = []

    mutating func pause(at time: CMTime) {
        if pauses.last.map({ $0.end != nil }) ?? true { pauses.append((time, nil)) }
    }

    mutating func resume(at time: CMTime) {
        guard let last = pauses.last, last.end == nil else { return }
        pauses[pauses.count - 1].end = max(time, last.start)
    }

    /// Where a frame goes; nil — it was taken during a pause.
    func place(_ time: CMTime) -> CMTime? {
        var shift = CMTime.zero
        for pause in pauses where pause.start <= time {
            guard let end = pause.end, end <= time else { return nil }
            shift = shift + (end - pause.start)
        }
        return time - shift
    }

    /// The end of the video at a stop; stopped while paused — where that pause began.
    func end(at time: CMTime) -> CMTime {
        let open = pauses.last.flatMap { $0.end == nil ? $0.start : nil }
        let stop = open.map { min($0, time) } ?? time
        let shift = pauses.reduce(CMTime.zero) { sum, pause in
            guard let end = pause.end, end <= stop else { return sum }
            return sum + (end - pause.start)
        }
        return stop - shift
    }
}

/// The screen video of a recording: SCStream frames of one display, without Mnema's windows, into
/// a fragmented HEVC screen.mov. The engine drives it (ScreenMsg), events go back through onEvent
/// on the main thread. Frames and commands share one serial queue, so the state needs no lock.
final class ScreenRecorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    var onEvent: (ScreenEvent) -> Void = { _ in }

    private enum Phase { case idle, starting, running, closing }

    private let queue = DispatchQueue(label: "mnema.screen", qos: .userInitiated)
    private var phase = Phase.idle
    private var generation = 0  // a start; late answers of an earlier one are ignored
    private var silent = false  // the engine is gone: no events
    private var answerStop = false  // the engine sent stop and waits for stopped
    private var stream: SCStream?
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var timeline = PauseTimeline()
    private var startedAt = CMTime.invalid  // the start message
    private var sessionStart = CMTime.invalid  // the first frame
    private var lastFrame = CMTime.invalid  // the last written frame
    private var waiters: [() -> Void] = []  // stop callers, on the main thread
    private var asked = false  // the system permission request is shown once per run

    static func now() -> CMTime { CMClockGetTime(CMClockGetHostTimeClock()) }

    /// Capture is open or closing; read from the main thread.
    var active: Bool { queue.sync { phase != .idle } }

    /// Call on the main thread: the front window is read here, at the start.
    func start(path: String) {
        let at = Self.now()
        let window = Self.frontWindow()
        queue.async { [self] in
            silent = false
            guard phase == .idle else { return emit(.failed(.writer)) }
            guard CGPreflightScreenCaptureAccess() else {
                if !asked {
                    asked = true
                    DispatchQueue.main.async { _ = CGRequestScreenCaptureAccess() }
                }
                return emit(.failed(.permission))
            }
            generation += 1
            phase = .starting
            timeline = PauseTimeline()
            startedAt = at
            (sessionStart, lastFrame) = (.invalid, .invalid)
            let started = generation
            Task { await open(URL(fileURLWithPath: path), window: window, generation: started) }
        }
    }

    func pause() {
        let at = Self.now()
        queue.async { [self] in if phase != .idle { timeline.pause(at: at) } }
    }

    func resume() {
        let at = Self.now()
        queue.async { [self] in if phase != .idle { timeline.resume(at: at) } }
    }

    /// Finishes the file; `then` runs on the main thread once it is closed. report: false — the
    /// engine is gone or quitting, it gets no stopped.
    func stop(report: Bool = true, then: (() -> Void)? = nil) {
        let at = Self.now()
        queue.async { [self] in
            if report { answerStop = true } else { silent = true }
            if let then { waiters.append(then) }
            switch phase {
            case .idle: finished(nil)
            case .starting, .running: close(at: at, failure: nil)
            case .closing: break
            }
        }
    }

    // ── on the queue ──────────────────────────────────────────────────────────

    private func open(_ url: URL, window: CGRect?, generation started: Int) async {
        var opened: AVAssetWriter?  // until the queue takes it, cancelled here
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
            let displays = content.displays.map { DisplayInfo(id: $0.displayID, frame: CGDisplayBounds($0.displayID)) }
            let id = ScreenCapture.display(for: window, among: displays, main: CGMainDisplayID())
            guard let display = content.displays.first(where: { $0.displayID == id }) ?? content.displays.first
            else { throw NSError(domain: SCStreamErrorDomain, code: SCStreamError.Code.noDisplayList.rawValue) }
            let own = content.applications.filter { $0.processID == getpid() }
            let filter = SCContentFilter(display: display, excludingApplications: own, exceptingWindows: [])
            let mode = CGDisplayCopyDisplayMode(display.displayID)
            let size = ScreenCapture.frameSize(
                pixelWidth: mode?.pixelWidth ?? display.width, pixelHeight: mode?.pixelHeight ?? display.height)

            let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
            writer.movieFragmentInterval = ScreenCapture.fragmentInterval
            writer.initialMovieFragmentInterval = CMTime(value: 2, timescale: 1)
            let input = AVAssetWriterInput(mediaType: .video, outputSettings: ScreenCapture.outputSettings(size))
            input.expectsMediaDataInRealTime = true
            guard writer.canAdd(input) else { throw writer.error ?? CocoaError(.fileWriteUnknown) }
            writer.add(input)
            guard writer.startWriting() else { throw writer.error ?? CocoaError(.fileWriteUnknown) }
            opened = writer

            let stream = SCStream(filter: filter, configuration: Self.configuration(size), delegate: self)
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            let current = queue.sync { () -> Bool in
                guard generation == started, phase == .starting else { return false }
                (self.stream, self.writer, self.input) = (stream, writer, input)
                phase = .running
                return true
            }
            opened = nil
            guard current else { return writer.cancelWriting() }
            try await stream.startCapture()
            queue.asyncAfter(deadline: .now() + 5) { [self] in
                // no frame at all (a sleeping display): the engine is not left waiting
                if generation == started, phase == .running, !lastFrame.isValid {
                    close(at: Self.now(), failure: .writer)
                }
            }
        } catch {
            NSLog("Mnema: screen capture did not start: %@", "\(error)")
            opened?.cancelWriting()  // also removes the file
            queue.async { [self] in
                if generation == started, phase == .starting || phase == .running {
                    close(at: Self.now(), failure: ScreenCapture.failure(error, whileRecording: false))
                }
            }
        }
    }

    private static func configuration(_ size: FrameSize) -> SCStreamConfiguration {
        let config = SCStreamConfiguration()
        config.width = size.width
        config.height = size.height
        config.captureResolution = .best
        config.preservesAspectRatio = true
        config.minimumFrameInterval = CMTime(value: 1, timescale: ScreenCapture.frameRate)
        config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        config.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2
        config.colorSpaceName = CGColorSpace.sRGB
        config.showsCursor = true
        config.queueDepth = 6
        return config
    }

    /// The front app's window on screen, or another app's when Mnema is active.
    private static func frontWindow() -> CGRect? {
        let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        let windows = (list as? [[String: Any]] ?? []).compactMap { info -> WindowInfo? in
            guard info[kCGWindowLayer as String] as? Int == 0,
                (info[kCGWindowAlpha as String] as? Double ?? 1) > 0,
                let pid = info[kCGWindowOwnerPID as String] as? pid_t,
                let bounds = info[kCGWindowBounds as String] as? NSDictionary,
                let rect = CGRect(dictionaryRepresentation: bounds)
            else { return nil }
            return WindowInfo(pid: pid, bounds: rect)
        }
        let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
        return ScreenCapture.window(windows, front: front, own: getpid())
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, phase == .running, stream === self.stream, buffer.isValid, Self.complete(buffer),
            let writer, let input
        else { return }
        guard writer.status == .writing else {
            return close(at: Self.now(), failure: ScreenCapture.failure(writer.error, whileRecording: true))
        }
        guard let at = timeline.place(buffer.presentationTimeStamp), !lastFrame.isValid || at > lastFrame else { return }
        if !sessionStart.isValid {
            writer.startSession(atSourceTime: at)
            sessionStart = at
            emit(.started(offset: (at - startedAt).seconds))
        }
        guard input.isReadyForMoreMediaData else { return }  // the encoder is behind: skip a frame
        guard let frame = Self.retimed(buffer, at), input.append(frame) else {
            return close(at: Self.now(), failure: ScreenCapture.failure(writer.error, whileRecording: true))
        }
        lastFrame = at
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        NSLog("Mnema: screen capture stopped: %@", "\(error)")
        queue.async { [self] in
            if stream === self.stream, phase == .running {
                close(at: Self.now(), failure: ScreenCapture.failure(error, whileRecording: true))
            }
        }
    }

    /// Stops capture and closes the file: up to the stop (or the pause it is in), so a still screen
    /// at the end is not cut off. No frame was written — no file.
    private func close(at time: CMTime, failure: ScreenFailure?) {
        phase = .closing
        generation += 1
        stream?.stopCapture { _ in }
        guard let writer, writer.status == .writing, lastFrame.isValid else {
            if let writer, writer.status == .writing { writer.cancelWriting() }  // also removes the file
            return finished(failure)
        }
        input?.markAsFinished()
        writer.endSession(atSourceTime: max(timeline.end(at: time), lastFrame))
        writer.finishWriting { [self] in
            if writer.status != .completed {
                NSLog("Mnema: screen video not finished: %@", "\(writer.error.map { "\($0)" } ?? "?")")
            }
            queue.async { [self] in finished(failure) }
        }
    }

    private func finished(_ failure: ScreenFailure?) {
        phase = .idle
        (stream, writer, input) = (nil, nil, nil)
        if let failure { emit(.failed(failure)) }
        if answerStop { emit(.stopped) }
        answerStop = false
        let done = waiters
        waiters = []
        DispatchQueue.main.async { done.forEach { $0() } }
    }

    private func emit(_ event: ScreenEvent) {
        guard !silent else { return }
        DispatchQueue.main.async { [self] in onEvent(event) }
    }

    private static func complete(_ buffer: CMSampleBuffer) -> Bool {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false)
            as? [[SCStreamFrameInfo: Any]],
            let raw = attachments.first?[.status] as? Int
        else { return false }
        return SCFrameStatus(rawValue: raw) == .complete
    }

    private static func retimed(_ buffer: CMSampleBuffer, _ time: CMTime) -> CMSampleBuffer? {
        guard time != buffer.presentationTimeStamp else { return buffer }
        var timing = CMSampleTimingInfo(duration: buffer.duration, presentationTimeStamp: time, decodeTimeStamp: .invalid)
        var copy: CMSampleBuffer?
        let status = CMSampleBufferCreateCopyWithNewTiming(
            allocator: nil, sampleBuffer: buffer, sampleTimingEntryCount: 1, sampleTimingArray: &timing,
            sampleBufferOut: &copy)
        return status == noErr ? copy : nil
    }
}
