# Nix execution

Chainman uses the installed host Nix in host mode and the pinned upstream image's
Nix in container mode. The launcher checks Nix >= 2.24 using the evaluator version;
platform qualification remains a separate release gate. The selected executable
family survives project shell refreshes. No replacement Nix package or source
patch is included in the Chainman runtime.

Project flake locks continue to pin language tools independently of this choice.
The container image is pinned by digest in the bootstrap and updated explicitly.
