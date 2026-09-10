// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "UnifiedInferenceInstaller",
    platforms: [.macOS("15.0")],
    products: [.executable(name: "InstallUnifiedInference", targets: ["InstallApp"])],
    targets: [
        .target(name: "InstallCore"),
        .executableTarget(name: "InstallApp", dependencies: ["InstallCore"]),
        .testTarget(name: "InstallCoreTests", dependencies: ["InstallCore"]),
    ]
)
