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
        .executable(
            name: "mnemosyne-lifecycle-helper",
            targets: ["MnemosyneLifecycleHelper"]
        ),
        .executable(
            name: "mnemosyne-lifecycle-runner",
            targets: ["MnemosyneLifecycleRunner"]
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
        .executableTarget(
            name: "MnemosyneLifecycleHelper",
            dependencies: ["MnemosyneAppCore"],
            path: "Sources/MnemosyneLifecycleHelper",
            linkerSettings: [
                .linkedFramework("LocalAuthentication"),
                .linkedFramework("Security"),
            ]
        ),
        .executableTarget(
            name: "MnemosyneLifecycleRunner",
            dependencies: ["MnemosyneAppCore"],
            path: "Sources/MnemosyneLifecycleRunner",
            linkerSettings: [
                .linkedFramework("Security"),
            ]
        ),
        .testTarget(
            name: "MnemosyneAppCoreTests",
            dependencies: ["MnemosyneAppCore"],
            path: "Tests/MnemosyneAppCoreTests"
        ),
    ]
)
