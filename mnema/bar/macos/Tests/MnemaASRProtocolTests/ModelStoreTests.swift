import Foundation
import Testing

@testable import MnemaASRProtocol

func temporaryStore() throws -> ModelStore {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("mnema-asr-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return ModelStore(root: root)
}

/// The layout a finished FluidAudio download leaves: bundles with coremldata.bin, plain files, the
/// commit marker.
func place(_ spec: ModelSpec, in store: ModelStore, revision: String? = nil, weights: Int = 0) throws {
    let fm = FileManager.default
    let folder = store.folder(spec)
    try fm.createDirectory(at: folder, withIntermediateDirectories: true)
    try Data(((revision ?? spec.revision) + "\n").utf8).write(to: folder.appendingPathComponent(".fluidaudio-revision"))
    for name in spec.files {
        let path = folder.appendingPathComponent(name)
        if name.hasSuffix(".mlmodelc") {
            try fm.createDirectory(at: path.appendingPathComponent("weights"), withIntermediateDirectories: true)
            try Data("x".utf8).write(to: path.appendingPathComponent("coremldata.bin"))
            try Data(count: weights).write(to: path.appendingPathComponent("weights/weight.bin"))
        } else {
            try Data("{}".utf8).write(to: path)
        }
    }
}

/// An empty models folder lists exactly what the Python side expects.
@Test func emptyStoreListsTheContractModels() throws {
    let store = try temporaryStore()
    let reply = try parsed(.models(store.list()))
    #expect(reply == expected("models"))
}

@Test func completeDownloadIsInstalled() throws {
    let store = try temporaryStore()
    try place(Catalog.parakeet, in: store)
    try place(Catalog.vad, in: store)
    #expect(store.isInstalled(Catalog.parakeet))
    #expect(store.list().map(\.installed) == [true])
}

/// The recognizer brings the speech finder along: without it the download is not finished.
@Test func recognizerWithoutItsPartsIsNotInstalled() throws {
    let store = try temporaryStore()
    try place(Catalog.parakeet, in: store)
    #expect(!store.isInstalled(Catalog.parakeet))
    #expect(store.pending(Catalog.parakeet) == [Catalog.vad])  // the download fetches only it
    #expect(store.remaining(Catalog.parakeet) <= Catalog.vad.bytes)
}

/// An interrupted download leaves the bundle folders: a staging file or a missing coremldata.bin
/// means not installed, so transcribe answers no_model and download resumes.
@Test func interruptedDownloadIsNotInstalled() throws {
    let store = try temporaryStore()
    try place(Catalog.parakeet, in: store)
    try place(Catalog.vad, in: store)
    let encoder = store.folder(Catalog.parakeet).appendingPathComponent("Encoder.mlmodelc")
    try Data(count: 10).write(to: encoder.appendingPathComponent("weights/weight.bin.partial"))
    #expect(store.incomplete(Catalog.parakeet) == ["Encoder.mlmodelc"])
    try FileManager.default.removeItem(at: encoder.appendingPathComponent("weights/weight.bin.partial"))
    try FileManager.default.removeItem(at: encoder.appendingPathComponent("coremldata.bin"))
    #expect(store.incomplete(Catalog.parakeet) == ["Encoder.mlmodelc"])
    #expect(store.list().map(\.installed) == [false])
}

/// Files from another commit are what FluidAudio would replace: not installed.
@Test func otherRevisionIsNotInstalled() throws {
    let store = try temporaryStore()
    try place(Catalog.parakeet, in: store, revision: "main")
    try place(Catalog.vad, in: store)
    #expect(!store.isInstalled(Catalog.parakeet))
}

/// The app and the command find one copy: FluidAudio's folder names (repo.folderName drops
/// "-coreml") under HOME.
@Test func modelsLiveUnderTheHomeApplicationSupport() {
    let store = ModelStore.standard(environment: ["HOME": "/Users/someone"])
    let models = "/Users/someone/Library/Application Support/Mnema/models"
    #expect(store.folder(Catalog.parakeet).path == "\(models)/parakeet-ultra")
    #expect(store.folder(Catalog.vad).path == "\(models)/vad/Models/silero-vad")
}

/// The free space check counts only what is still to download.
@Test func spaceCheckCountsWhatIsLeft() throws {
    let store = try temporaryStore()
    let full = Catalog.parakeet.size
    #expect(store.remaining(Catalog.parakeet) == full)
    #expect(throws: ASRFailure.self) { try store.checkSpace(for: Catalog.parakeet, available: full) }
    do {
        try store.checkSpace(for: Catalog.parakeet, available: 1000)
    } catch let failure as ASRFailure {
        #expect(failure.code == .noSpace)
    }
    try store.checkSpace(for: Catalog.parakeet, available: 2 * full)

    try place(Catalog.vad, in: store)
    let partial = store.folder(Catalog.parakeet).appendingPathComponent("Encoder.mlmodelc/weights")
    try FileManager.default.createDirectory(at: partial, withIntermediateDirectories: true)
    try Data(count: 1_000_000).write(to: partial.appendingPathComponent("weight.bin.partial"))
    #expect(store.remaining(Catalog.parakeet) == Catalog.parakeet.bytes - 1_000_000)

    try place(Catalog.parakeet, in: store)
    try FileManager.default.removeItem(at: partial.appendingPathComponent("weight.bin.partial"))
    #expect(store.remaining(Catalog.parakeet) == 0)
    try store.checkSpace(for: Catalog.parakeet, available: 0)  // nothing to download
}

/// The recognizer and its speech finder make one fraction, weighted by size, that never goes back.
@Test func downloadFractionWeighsPartsBySize() throws {
    let meter = DownloadMeter([Catalog.parakeet, Catalog.vad])
    let half = try #require(meter.update("parakeet", 0.5))
    #expect(abs(half - 0.5 * Double(Catalog.parakeet.bytes) / Double(Catalog.parakeet.size)) < 1e-9)
    #expect(meter.update("parakeet", 0.4) == nil)  // a second listing restarts at zero: not shown
    #expect(meter.update("other", 1) == nil)
    let speech = try #require(meter.update("vad", 1))
    #expect(speech > half)
    #expect(meter.update("parakeet", 1) == 1)
    #expect(meter.update("parakeet", 1) == nil)
}
