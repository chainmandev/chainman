# Swift package example

This Swift package keeps greeting behavior in a pure library with XCTest coverage. Its executable presents a SwiftUI window where SwiftUI is available and prints the same core result on other hosts.

From the asset root, run `just module swift verify`. Setup and dependency resolution keep SwiftPM build state under the managed scratch directory. A checked `Package.resolved` is enforced during setup; this dependency-free example does not produce one. Verification runs strict swift-format lint, compilation, and XCTest. Linux uses the command-line executable. The native SwiftUI lane requires macOS with Xcode and is not exercised on Linux.

Linux uses the standard explicit `Tests/LinuxMain.swift` XCTest entry point because the pinned Nix SwiftPM does not include the IndexStore library required for automatic test discovery. Keep `GreetingTests.allTests` synchronized when adding Linux tests.
