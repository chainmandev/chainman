# Flutter application example

This package contains a minimal Material application and a widget test. The managed build produces a release asset bundle, which does not package a native executable.

From the asset root, run `just module flutter verify`. Building or running an Android target additionally requires an Android SDK with operator-accepted licenses. Apple targets require macOS and Xcode. Neither native SDK is installed or configured by this example. On Linux ARM64, the pinned Flutter wrapper is enabled for analysis, widget tests, and asset bundles. Its bundled `aapt2` is an x86_64 binary, so Android builds on Linux ARM64 are unsupported.
