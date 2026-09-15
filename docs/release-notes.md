# Chainman v0.1.0

Chainman's first public release is an experimental alpha for project-owned
development environments, setup, services, caches, and verified updates.
Container Nix is the default; host Nix is supported explicitly.

Consumers commit a small justfile recipe, one full Git commit SHA in
`chainman.lock`, and their own configuration. The selected Git revision supplies
all Chainman implementation. There is no global installation, copied runtime,
custom archive distribution, or required GitHub authentication at launch.

Start with manual adoption in the README. For a new or empty directory, use
`just init DEST v0.1.0` from a disposable checkout. The starter uses schema 3 and
owns its flake and lock; initialization makes an initial Git commit unless
`--no-git` is selected. It does not run setup or application qualification.

Expect breaking changes during alpha. Explicit initial adoption can select this
release immediately. Automatic runtime updates retain the configurable 30-day age
policy and the project's verification gate. Successful updates commit by default;
preview and no-commit modes are available.

Platform SDK availability and application-specific acceptance remain the consuming
project's responsibility. See the runtime, update recovery, and qualification guides.
