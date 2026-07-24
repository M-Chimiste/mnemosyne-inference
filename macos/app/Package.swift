// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "MnemosyneMac",
    platforms: [
        .macOS(.v15),
    ],
    products: [
        .executable(name: "MnemosyneMenu", targets: ["MnemosyneMenu"]),
        .executable(
            name: "mnemosyne-service-bootstrap",
            targets: ["MnemosyneServiceBootstrap"]
        ),
    ],
    targets: [
        .target(
            name: "MnemosyneAppCore",
            path: "Sources/MnemosyneAppCore"
        ),
        .executableTarget(
            name: "MnemosyneMenu",
            dependencies: ["MnemosyneAppCore"],
            path: "Sources/MnemosyneMenu"
        ),
        .executableTarget(
            name: "MnemosyneServiceBootstrap",
            path: "Sources/MnemosyneServiceBootstrap"
        ),
        .testTarget(
            name: "MnemosyneAppCoreTests",
            dependencies: ["MnemosyneAppCore"],
            path: "Tests/MnemosyneAppCoreTests"
        ),
    ]
)
