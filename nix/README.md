# Nix runtime patch

`capless-root-move-path.patch` modifies Nix 2.34.8's `movePath` helper to retain
ordinary filesystem behavior for owned directories when UID 0 lacks capabilities.
The change also restores the source mode if rename fails. The patch was added on
2026-09-08. It does not change Nix's fresh-copy or artifact-hash checks.

The patch includes code from [Nix 2.34.8](https://github.com/NixOS/nix/tree/2.34.8),
whose store library is licensed under LGPL-2.1-or-later. The patch and its
modifications use that same license; see [the complete license](NIX-LICENSE).
Chainman's MIT license applies to its own tooling, not to this upstream-derived
patch. The pinned package input supplies the complete corresponding Nix source;
`flake.nix` applies this exact patch to it without modifying the source in place.
