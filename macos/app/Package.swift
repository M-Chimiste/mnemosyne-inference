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
        .executable(
            name: "mnemosyne-file-trash",
            targets: ["MnemosyneFileTrash"]
        ),
    ],
    dependencies: [
        .package(
            url: "https://github.com/sparkle-project/Sparkle",
            exact: "2.9.2"
        ),
    ],
    targets: [
        .target(
            name: "MnemosyneAppCore",
            path: "Sources/MnemosyneAppCore"
        ),
        .executableTarget(
            name: "MnemosyneMenu",
            dependencies: [
                "MnemosyneAppCore",
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            path: "Sources/MnemosyneMenu"
        ),
        .executableTarget(
            name: "MnemosyneServiceBootstrap",
            path: "Sources/MnemosyneServiceBootstrap"
        ),
        .executableTarget(
            name: "MnemosyneFileTrash",
            path: "Sources/MnemosyneFileTrash"
        ),
        .testTarget(
            name: "MnemosyneAppCoreTests",
            dependencies: ["MnemosyneAppCore"],
            path: "Tests/MnemosyneAppCoreTests"
        ),
    ]
)
