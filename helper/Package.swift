// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "macwork-helper",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "macwork-helper",
            path: "Sources/macwork-helper",
            linkerSettings: [.linkedFramework("ApplicationServices"), .linkedFramework("AppKit"),
                             .linkedFramework("NaturalLanguage"), .linkedFramework("Carbon"),
                             .linkedFramework("ScreenCaptureKit"), .linkedFramework("Vision")]
        )
    ]
)
