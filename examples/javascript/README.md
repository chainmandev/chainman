# JavaScript workspace example

This pnpm workspace demonstrates default and named catalogs. The package compiles strict TypeScript, uses `camelcase` at runtime, and exercises both a named export and a default export with Node's test runner.

From the asset root, run `just module javascript verify`. Setup uses the frozen lockfile, ignores lifecycle scripts, and applies a 30-day minimum release age during resolution.

The pinned shell owns Node and Prettier; the project lock owns TypeScript. The exact pnpm version is enforced through `pmOnFail: error`, missing publication times do not bypass the age policy, and the Node type catalog matches runtime major 26. Formatting and verification use real Prettier.
