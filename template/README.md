# Your project

This starter pins Chainman in `chainman.lock`. The small `chainman` recipe in the
justfile fetches and verifies that Git revision. Chainman is not installed globally;
any checkout used to generate this project can be deleted.

Install **Git + just + Docker or Podman**. Container Nix is the default. Alternatively,
install Nix 2.24 or newer and select host-Nix mode:

```sh
export CHAINMAN_MODE=host-nix
```

The initializer generated files and, unless `--no-git` was selected, made the initial
Git commit. It did not run setup or verification.

```sh
just chainman setup
just verify
just chainman exec -- python3 scripts/demo.py
just chainman shell
```

Your `flake.nix` and `flake.lock` own the development environment. Your
`chainman.toml` routes the example verification command through it. Replace the
example with your application commands, keeping the acceptance gate in
`recipes.verify` and `updates.verify_task` consistent.

Inspect dependency changes before applying them:

```sh
just chainman deps-update --skip-chainman mode=dry-run
just chainman deps-update --skip-chainman commit=off
```

The first command verifies an isolated candidate and leaves the original unchanged.
The second applies verified changes without a commit. Without either option, updates
apply and commit after verification. Runtime updates select the current public default-branch SHA immediately and
freeze it through verification and resume. Ordinary launches stay pinned. The
30-day maturity policy applies to project dependencies.

Read the [adoption guide](https://github.com/chainmandev/chainman/blob/HEAD/docs/adoption.md),
[configuration reference](https://github.com/chainmandev/chainman/blob/HEAD/docs/configuration.md),
and [update and recovery guide](https://github.com/chainmandev/chainman/blob/HEAD/docs/updates.md).
