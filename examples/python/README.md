# Python workspace example

A uv workspace installs a typed library and tests its public import. Python and uv
come from the selected Nix profile. Run `just module python verify` from the asset
root; no host Python is required.

Member `build-system.requires` entries own build-backend requirements. The updater
derives the `toolchain-build` group into the workspace manifest and ordinary
`uv.lock`. Setup installs that frozen group without the workspace first, then
synchronizes the workspace without isolated build downloads. Verification forces
PEP 517 against the installed backend, builds both sdist and wheel into the managed
work directory, and runs Ruff and unit tests. The qualified example backend baseline
is uv_build 0.12.3; its compatibility interval has an explicit reason in the root
policy. Runtime and Ruff targets follow the root SDK coordination configuration.
