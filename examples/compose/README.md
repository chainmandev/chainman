# Compose desktop example

This Gradle project builds a tiny Compose desktop application and tests its pure greeting function without opening a window. Kotlin and the Compose compiler share one version catalog entry; the Compose plugin has its own entry. Kotlin 2.4.10 supports the pinned Gradle 8.14.4, which can run on the pinned JDK 21.

From the asset root, run `just module compose verify`. Verification runs ktlint, compilation, and tests with strict dependency locking and SHA-256 artifact verification. Formatting uses ktlint. The build directory and Gradle project cache use the managed workspace.

The default Compose target follows the actual host OS and architecture. `-PcomposeTarget=linux-arm64`, `linux-x64`, `macos-arm64`, `macos-x64`, or `windows-x64` selects its matching desktop dependency and strict lockfile under `gradle/dependency-locks/`. Unknown targets fail. Dependency resolution refreshes all five lockfiles and verification metadata. Reviewing a metadata update is necessary before accepting newly trusted artifact checksums.

Dependency resolution for these five targets can run on Linux ARM64; it does not qualify their native execution. Native installers need the packaging tools for their target operating system and are outside the normal build and test lane.
