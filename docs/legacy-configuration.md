# Legacy configuration compatibility

[Schema-3 reference](configuration.md) · [Adoption](adoption.md)

Schema 1 remains accepted for older consumers. Its command hooks below are a
compatibility interface; use named tasks in schema 3 for new integrations:

```toml
schema = 1
[project]
default_profile = "default"
[profiles.default]
flake = "nix#default"
[commands]
setup = [["sh", "scripts/setup-project.sh"]]
verify = [["just", "check"]] # Schema 1 hook; schema 2 uses verify_task = "check".
[setup]
inputs = ["nix/flake.nix", "nix/flake.lock", "package.json", "pnpm-lock.yaml"]
artifacts = ["node_modules/.pnpm/lock.yaml"]
```


Schema 2 uses named tasks and setup groups without schema-3 template composition.
These configuration forms remain supported. This does **not** preserve the old
archive-based installation contract: all consumers use the plain Git commit pin
and small justfile recipe. New integrations should use schema 3.
