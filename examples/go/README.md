# Go workspace example

This Go workspace links a standard-library-only text module to a small command-line module. The library test checks whitespace and empty-value handling without network dependencies.

From the asset root, run `just module go verify`. The setup and verification commands use read-only module mode; the update command tidies each module with workspace resolution disabled, then synchronizes the workspace. Recursive module manifests, sums, and `go.work.sum` are tracked as update inputs and outputs.
