// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "apple-summarize",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(
            name: "apple-summarize",
            path: "Sources"
        ),
    ]
)
