// The models mnema-asr knows and the folder rules: the list of recognizers lives here, Python
// reads it with the models operation. Files are FluidAudio's layout; weights are pinned to a
// Hugging Face commit, so a fixed app version always loads the same files.
import Foundation

public struct ModelSpec: Sendable, Equatable {
    public let id: String
    public let title: String
    public let langs: [String]
    /// Hugging Face repository and the commit its files come from.
    public let repo: String
    public let revision: String
    /// Folder of the files, relative to the models folder.
    public let folder: String
    /// What a complete copy holds: compiled CoreML bundles (.mlmodelc) and plain files.
    public let files: [String]
    /// Bytes of these files on Hugging Face.
    public let bytes: Int64
    /// Other models the download brings along.
    public let parts: [ModelSpec]

    /// Bytes on disk with the parts.
    public var size: Int64 { bytes + parts.reduce(0) { $0 + $1.size } }
}

public enum Catalog {
    /// Speech segments for cloud recognition on a Mac without a server; comes with every
    /// recognizer and alone as "vad". VadManager(modelDirectory: models/vad) adds Models/<repo.folderName>.
    public static let vad = ModelSpec(
        id: "vad", title: "Silero VAD", langs: [],
        repo: "FluidInference/silero-vad-coreml", revision: "b419383c55c110e2c9271fa6ee0ea83d03c70d96",
        folder: "vad/Models/silero-vad",
        files: ["silero-vad-unified-256ms-v6.2.1.mlmodelc"],
        bytes: 1_063_427, parts: [])

    public static let parakeet = ModelSpec(
        id: "parakeet", title: "Parakeet Ultra",
        langs: "bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es sv ru uk"
            .split(separator: " ").map(String.init),
        repo: "FluidInference/parakeet-ultra-coreml", revision: "95eaa59a39d4394f047a4dc5cce480388a60d1b6",
        folder: "parakeet-ultra",
        files: [
            "Preprocessor.mlmodelc", "Encoder.mlmodelc", "Decoder.mlmodelc", "JointDecisionv3.mlmodelc",
            "parakeet_vocab.json",
        ],
        bytes: 632_314_500, parts: [vad])

    /// What the models operation lists, in the order Python shows them.
    public static let recognizers = [parakeet]

    public static func spec(_ id: String) -> ModelSpec? {
        (recognizers + [vad]).first { $0.id == id }
    }
}

public struct ModelStore: Sendable {
    public let root: URL

    public init(root: URL) {
        self.root = root
    }

    /// ~/Library/Application Support/Mnema/models from HOME: the app and the command share it.
    public static func standard(environment: [String: String] = ProcessInfo.processInfo.environment) -> ModelStore {
        let home = environment["HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser
        return ModelStore(root: home.appendingPathComponent("Library/Application Support/Mnema/models"))
    }

    public func folder(_ spec: ModelSpec) -> URL {
        root.appendingPathComponent(spec.folder, isDirectory: true)
    }

    /// Files of this model (not its parts) that are missing or not ready to load. A bundle needs
    /// its coremldata.bin and no *.partial of an interrupted download; the commit marker
    /// FluidAudio writes must name the pinned revision, or FluidAudio itself refuses the copy.
    public func incomplete(_ spec: ModelSpec) -> [String] {
        let folder = folder(spec)
        let fm = FileManager.default
        let marker = try? String(contentsOf: folder.appendingPathComponent(".fluidaudio-revision"), encoding: .utf8)
        guard marker?.trimmingCharacters(in: .whitespacesAndNewlines) == spec.revision else {
            return spec.files
        }
        return spec.files.filter { name in
            let path = folder.appendingPathComponent(name)
            guard name.hasSuffix(".mlmodelc") else { return !fm.fileExists(atPath: path.path) }
            return !fm.fileExists(atPath: path.appendingPathComponent("coremldata.bin").path)
                || Self.hasPartial(path)
        }
    }

    /// The model and its parts whose own files are not all there: what a download fetches.
    public func pending(_ spec: ModelSpec) -> [ModelSpec] {
        (incomplete(spec).isEmpty ? [] : [spec]) + spec.parts.flatMap(pending)
    }

    public func isInstalled(_ spec: ModelSpec) -> Bool {
        pending(spec).isEmpty
    }

    public func list() -> [ModelInfo] {
        Catalog.recognizers.map {
            ModelInfo(id: $0.id, title: $0.title, langs: $0.langs, size: $0.size, installed: isInstalled($0))
        }
    }

    /// Bytes still to download: what is already on disk (finished files and partial ones the
    /// download resumes) is not counted again.
    public func remaining(_ spec: ModelSpec) -> Int64 {
        pending(spec).reduce(0) { $0 + max(0, $1.bytes - Self.bytes(in: folder($1))) }
    }

    /// The download fits on the disk, with a margin for the rest of the system.
    public func checkSpace(for spec: ModelSpec, available: Int64) throws {
        let needed = remaining(spec)
        guard needed == 0 || available >= needed + Self.spaceMargin else {
            throw ASRFailure(.noSpace, "needs \(needed) bytes, \(available) free")
        }
    }

    static let spaceMargin: Int64 = 64 << 20

    private static func hasPartial(_ url: URL) -> Bool {
        let items = FileManager.default.enumerator(at: url, includingPropertiesForKeys: nil)
        while let item = items?.nextObject() as? URL {
            if item.pathExtension == "partial" { return true }
        }
        return false
    }

    private static func bytes(in url: URL) -> Int64 {
        let items = FileManager.default.enumerator(at: url, includingPropertiesForKeys: [.fileSizeKey])
        var total: Int64 = 0
        while let item = items?.nextObject() as? URL {
            total += Int64((try? item.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
        return total
    }
}

/// One download fraction for the models being fetched, weighted by their sizes; it never goes back.
public final class DownloadMeter: @unchecked Sendable {
    private let sizes: [String: Int64]
    private let total: Int64
    private var done: [String: Double] = [:]
    private var shown = -1.0
    private let lock = NSLock()

    public init(_ models: [ModelSpec]) {
        sizes = Dictionary(models.map { ($0.id, $0.bytes) }, uniquingKeysWith: { first, _ in first })
        total = max(1, sizes.values.reduce(0, +))
    }

    /// The fraction to show after part `id` reached `fraction`, or nil when it would not move.
    public func update(_ id: String, _ fraction: Double) -> Double? {
        lock.lock()
        defer { lock.unlock() }
        guard sizes[id] != nil else { return nil }
        done[id] = max(done[id] ?? 0, min(max(fraction, 0), 1))
        let overall = sizes.reduce(0.0) { $0 + Double($1.value) * (done[$1.key] ?? 0) } / Double(total)
        guard overall > shown + 0.001 || (overall >= 1 && shown < 1) else { return nil }
        shown = overall
        return overall
    }
}

/// An exclusive lock on a file: the app and the command never download into one folder at once.
public final class FileLock {
    private let fd: Int32

    /// Blocks until the lock is free.
    public init(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        fd = open(url.path, O_RDWR | O_CREAT | O_CLOEXEC, 0o644)
        guard fd >= 0 else { throw ASRFailure(.failed, "cannot open \(url.path): errno \(errno)") }
        while flock(fd, LOCK_EX) != 0 {
            guard errno == EINTR else {
                close(fd)
                throw ASRFailure(.failed, "cannot lock \(url.path): errno \(errno)")
            }
        }
    }

    deinit {
        close(fd)  // closing the descriptor drops the lock
    }
}
