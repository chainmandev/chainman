# Getting started

[Documentation index](README.md)

For an existing project, begin with the [README's complete manual installation](../README.md#adopt-an-existing-project),
then follow [progressive adoption](adoption.md). Keep your existing flake and just
workflows; route one underlying command before integrating setup and updates.

For a new or empty directory, follow the [greenfield initializer](../README.md#start-a-new-project).
The initializer generates a starter and makes its initial Git commit by default.
It does not run setup or certify the application. `--no-git` generates files only.

Both paths use the same small recipe and Git commit pin. Neither requires a global
Chainman installation or a persistent checkout at any particular path.
