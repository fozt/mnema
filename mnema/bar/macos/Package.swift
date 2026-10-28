// swift-tools-version:6.0
// Mnema for macOS: menu bar item, panel, windows. The logic lives in Python (mnema/bar), the
// wire format in mnema/bar/protocol.py. Built by `mnema-bar install` (Command Line Tools, no Xcode).
import PackageDescription

let package = Package(
    name: "Mnema",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "Mnema",
            path: "Sources/Mnema",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .testTarget(
            name: "MnemaTests",
            dependencies: ["Mnema"],
            path: "Tests/MnemaTests",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
