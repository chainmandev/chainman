// swift-tools-version: 5.10
import PackageDescription

let package = Package(
  name: "SwiftWorkspaceExample",
  platforms: [.macOS(.v13)],
  products: [
    .library(name: "GreetingCore", targets: ["GreetingCore"]),
    .executable(name: "greeting-app", targets: ["GreetingApp"]),
  ],
  targets: [
    .target(name: "GreetingCore"),
    .executableTarget(name: "GreetingApp", dependencies: ["GreetingCore"]),
    .testTarget(name: "GreetingCoreTests", dependencies: ["GreetingCore"]),
  ]
)
