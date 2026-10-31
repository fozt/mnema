// swift-tools-version:6.0
// Mnema for macOS: menu bar item, panel, windows. The logic lives in Python (mnema/bar), the
// wire format in mnema/bar/protocol.py. Built by `mnema-bar install` (Command Line Tools, no Xcode).
// mnema-asr: speech recognition on CoreML through FluidAudio, JSON lines over stdin and stdout
// (MnemaASRProtocol, tests/fixtures/asr-helper-v1.json); built with `--product mnema-asr`.
import PackageDescription

let package = Package(
    name: "Mnema",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Mnema", targets: ["Mnema"]),
        .executable(name: "mnema-asr", targets: ["MnemaASR"]),
    ],
    dependencies: [
        .package(url: "https://github.com/FluidInference/FluidAudio.git", exact: "0.18.0")
    ],
    targets: [
        .executableTarget(
            name: "Mnema",
            path: "Sources/Mnema",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        // The wire format and the model folder rules, without FluidAudio: its tests build fast.
        .target(
            name: "MnemaASRProtocol",
            path: "Sources/MnemaASRProtocol",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .executableTarget(
            name: "MnemaASR",
            dependencies: [
                "MnemaASRProtocol",
                .product(name: "FluidAudio", package: "FluidAudio"),
            ],
            path: "Sources/MnemaASR",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .testTarget(
            name: "MnemaTests",
            dependencies: ["Mnema"],
            path: "Tests/MnemaTests",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .testTarget(
            name: "MnemaASRProtocolTests",
            dependencies: ["MnemaASRProtocol"],
            path: "Tests/MnemaASRProtocolTests",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
