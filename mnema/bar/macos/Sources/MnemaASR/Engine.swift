import AVFoundation
@preconcurrency import FluidAudio
import Foundation
import MnemaASRProtocol

/// The operations of the protocol on FluidAudio. Only download touches the network: every other
/// operation runs with ModelHub.offlineMode, so a missing model is no_model, not a silent 600 MB
/// download.
struct Engine: Sendable {
    let store: ModelStore
    let send: @Sendable (Reply) -> Void

    init(store: ModelStore, send: @escaping @Sendable (Reply) -> Void) {
        self.store = store
        self.send = send
        AppLogger.minimumLevel = .info  // debug lines carry transcript text
        ModelHub.offlineMode = true
        // The pinned commits, the default registry: REGISTRY_URL and the like are not honoured.
        ModelRegistry.baseURL = "https://huggingface.co"
        ModelRegistry.revisionOverrides = Dictionary(
            uniqueKeysWithValues: (Catalog.recognizers + [Catalog.vad]).map { ($0.repo, $0.revision) })
    }

    func run(_ request: Request) async throws -> Reply {
        #if !arch(arm64)
        throw ASRFailure(.unsupportedMac, "CoreML recognition needs Apple Silicon")
        #else
        switch request {
        case .models:
            return .models(store.list())
        case .download(let id):
            try await download(try spec(id))
            return .downloaded
        case .transcribe(let id, let wavs):
            return .transcribed(try await transcribe(try spec(id), wavs))
        case .speech(let wav):
            return .speech(try await speech(URL(fileURLWithPath: wav)))
        }
        #endif
    }

    private func spec(_ id: String) throws -> ModelSpec {
        guard let spec = Catalog.spec(id) else { throw ASRFailure(.protocol, "unknown model \(id)") }
        return spec
    }

    private func requireInstalled(_ spec: ModelSpec) throws {
        guard store.isInstalled(spec) else { throw ASRFailure(.noModel, "\(spec.id) is not downloaded") }
    }

    // MARK: download

    private func download(_ spec: ModelSpec) async throws {
        let lock = try FileLock(store.root.appendingPathComponent(".download.lock"))
        defer { withExtendedLifetime(lock) {} }
        guard !store.isInstalled(spec) else { return }  // the other process has just finished it
        try store.checkSpace(for: spec, available: freeSpace())
        Log.info("downloading \(spec.id) into \(store.root.path)")
        let pending = store.pending(spec)
        let meter = DownloadMeter(pending)
        send(.progress(.download, 0))
        ModelHub.offlineMode = false
        defer { ModelHub.offlineMode = true }
        do {
            for part in pending {
                try await ModelHub.download(
                    repo(part), to: store.folder(part).deletingLastPathComponent(),
                    progressHandler: progress(meter, part.id))
            }
        } catch {
            throw Self.downloadFailure(error)
        }
        ModelHub.offlineMode = true  // the check load below must not fetch anything
        guard store.isInstalled(spec) else {
            throw ASRFailure(.downloadFailed, "incomplete after download: \(store.incomplete(spec))")
        }
        // The first load compiles the model for the Neural Engine: done now, the first recording
        // does not wait for it, and a model that cannot load shows up here.
        guard Catalog.recognizers.contains(spec) else { return }
        send(.progress(.load, nil))
        do {
            _ = try await loadRecognizer()
        } catch {
            throw ASRFailure(.downloadFailed, "the downloaded model does not load: \(error)")
        }
    }

    /// FluidAudio's repository of a model; it downloads into <folder>/<repo.folderName>.
    private func repo(_ spec: ModelSpec) throws -> Repo {
        let repos: [String: Repo] = [Catalog.parakeet.id: .parakeetUltra, Catalog.vad.id: .vad]
        guard let repo = repos[spec.id], repo.remotePath == spec.repo,
            repo.folderName == store.folder(spec).lastPathComponent
        else { throw ASRFailure(.failed, "FluidAudio has no repository for \(spec.id)") }
        return repo
    }

    /// Bytes of the download phase: FluidAudio reports them as 0…0.5 of a repository load.
    private func progress(_ meter: DownloadMeter, _ id: String) -> ProgressHandler {
        { [send] progress in
            guard case .downloading = progress.phase,
                let fraction = meter.update(id, min(1, progress.fractionCompleted * 2))
            else { return }
            send(.progress(.download, fraction))
        }
    }

    private func freeSpace() throws -> Int64 {
        try FileManager.default.createDirectory(at: store.root, withIntermediateDirectories: true)
        let values = try store.root.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values.volumeAvailableCapacityForImportantUsage ?? Int64.max
    }

    private static func downloadFailure(_ error: Error) -> ASRFailure {
        if let failure = error as? ASRFailure { return failure }
        if outOfSpace(error) { return ASRFailure(.noSpace, "\(error)") }
        return ASRFailure(.downloadFailed, "\(error)")
    }

    private static func outOfSpace(_ error: Error) -> Bool {
        let ns = error as NSError
        if ns.domain == NSCocoaErrorDomain && ns.code == NSFileWriteOutOfSpaceError { return true }
        if ns.domain == NSPOSIXErrorDomain && ns.code == Int(ENOSPC) { return true }
        if case DownloadError.downloadFailed(_, let underlying) = error { return outOfSpace(underlying) }
        return (ns.userInfo[NSUnderlyingErrorKey] as? Error).map(outOfSpace) ?? false
    }

    // MARK: transcribe

    private func loadRecognizer() async throws -> AsrManager {
        let models = try await AsrModels.load(from: store.folder(Catalog.parakeet), version: .ultra)
        // Each worker decodes a window on the shared models; 2 keeps the peak memory down.
        let manager = AsrManager(config: ASRConfig(parallelChunkConcurrency: 2))
        try await manager.loadModels(models)
        return manager
    }

    private func transcribe(_ spec: ModelSpec, _ wavs: [String]) async throws -> [[Word]] {
        guard Catalog.recognizers.contains(spec) else { throw ASRFailure(.protocol, "\(spec.id) does not recognize") }
        try requireInstalled(spec)
        let tracks = try wavs.map { try Track(URL(fileURLWithPath: $0)) }
        send(.progress(.load, nil))
        let manager: AsrManager
        do {
            manager = try await loadRecognizer()
        } catch {
            throw Self.loadFailure(error)
        }
        var result: [[Word]] = []
        for (index, track) in tracks.enumerated() {
            send(.progress(.recognize, Double(index) / Double(tracks.count)))
            guard track.duration >= ASRConstants.minimumAudioDurationSeconds else {
                result.append([])  // too short to hold a word
                continue
            }
            let windows = WindowCount()
            await manager.setWindowHandler { [send] window in
                let done = windows.finished()
                let share = Double(done) / Double(max(window.windowCount, done))
                send(.progress(.recognize, (Double(index) + share) / Double(tracks.count)))
            }
            var state = TdtDecoderState.make(decoderLayers: await manager.decoderLayerCount)
            let started = Date()
            let asr: ASRResult
            do {
                asr = try await manager.transcribe(track.url, decoderState: &state)
            } catch ASRError.invalidAudioData {
                throw ASRFailure(.badAudio, "\(track.url.lastPathComponent): no audio samples")
            }
            let words = buildWordTimings(from: asr.tokenTimings ?? []).map {
                Word(text: $0.word, start: $0.startTime, end: $0.endTime)
            }
            Log.info(
                "\(track.url.lastPathComponent): \(words.count) words, \(String(format: "%.1f", track.duration)) s "
                    + "in \(String(format: "%.1f", Date().timeIntervalSince(started))) s")
            result.append(words)
        }
        await manager.setWindowHandler(nil)
        return result
    }

    private static func loadFailure(_ error: Error) -> ASRFailure {
        switch error {
        case DownloadError.modelMissing, DownloadError.networkDisabled, AsrModelsError.modelNotFound:
            return ASRFailure(.noModel, "\(error)")
        default:
            return ASRFailure(.failed, "the model does not load: \(error)")
        }
    }

    // MARK: speech

    /// Raw segments of FluidAudio's silero VAD, reading the WAV in its 4096-sample chunks: a long
    /// track never sits in memory whole.
    private func speech(_ url: URL) async throws -> [SpeechSegment] {
        try requireInstalled(Catalog.vad)
        let track = try Track(url)
        guard track.file.processingFormat.sampleRate == Double(VadManager.sampleRate),
            track.file.processingFormat.channelCount == 1
        else { throw ASRFailure(.badAudio, "\(url.lastPathComponent): not 16 kHz mono") }
        let vad: VadManager
        let directory = store.folder(Catalog.vad).deletingLastPathComponent().deletingLastPathComponent()  // models/vad
        do {
            vad = try await VadManager(modelDirectory: directory)
        } catch {
            throw Self.loadFailure(error)
        }
        let total = Int(track.file.length)
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: track.file.processingFormat, frameCapacity: AVAudioFrameCount(VadManager.chunkSize))
        else { throw ASRFailure(.failed, "no audio buffer") }
        var state = await vad.makeStreamState()
        var segments: [SpeechSegment] = []
        var start: Int?
        var read = 0
        while read < total {
            do {
                try track.file.read(into: buffer, frameCount: AVAudioFrameCount(VadManager.chunkSize))
            } catch {
                throw ASRFailure(.badAudio, "\(url.lastPathComponent): \(error)")
            }
            let count = Int(buffer.frameLength)
            guard count > 0, let channel = buffer.floatChannelData?[0] else { break }
            read += count
            let result = try await vad.processStreamingChunk(
                Array(UnsafeBufferPointer(start: channel, count: count)), state: state)
            state = result.state
            if let event = result.event {
                if event.isStart {
                    start = event.sampleIndex
                } else if let begin = start {
                    segments.append(SpeechSegment(start: begin, end: min(event.sampleIndex, total)))
                    start = nil
                }
            }
            send(.progress(.recognize, (Double(read) / Double(max(total, 1)) * 100).rounded() / 100))
        }
        if let begin = start {
            segments.append(SpeechSegment(start: begin, end: total))
        }
        return segments
    }
}

/// A WAV Python prepared; one that does not open is bad_audio.
struct Track {
    let url: URL
    let file: AVAudioFile

    init(_ url: URL) throws {
        self.url = url
        do {
            file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
        } catch {
            throw ASRFailure(.badAudio, "\(url.lastPathComponent): \(error.localizedDescription)")
        }
    }

    var duration: Double {
        Double(file.length) / file.processingFormat.sampleRate
    }
}

/// Windows decoded so far; their handler runs on several workers.
final class WindowCount: @unchecked Sendable {
    private var count = 0
    private let lock = NSLock()

    func finished() -> Int {
        lock.lock()
        defer { lock.unlock() }
        count += 1
        return count
    }
}
