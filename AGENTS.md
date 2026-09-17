# chainman development

Use host Nix and just for development; language tools come from the pinned shell.
Keep public commands behind just and the checked-in bootstrap. Consumers should
not need a global chainman installation. Treat the consumer root and immutable
runtime root as separate inputs; never update an installed runtime in place.

Keep changes in logical commits with their tests and documentation. Only the lead
mutates Git. Scope cooperating writers to disjoint files. Keep public examples
neutral and self-contained. Test dangerous filesystem and Git cases in disposable
fixtures. Never run workflows in external migration worktrees during integration
planning or static migrations. Do not publish, push, or change external services
without an explicit instruction.
