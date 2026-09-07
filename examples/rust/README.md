# Rust workspace example

This Cargo workspace keeps label summarization in a dependency-free library and exposes it through a small command-line program. Its integration test fixes the trimming and blank-label behavior.

From the asset root, run `just module rust verify`. Cargo fetches from the frozen lockfile before formatting, linting, and testing the entire workspace.
