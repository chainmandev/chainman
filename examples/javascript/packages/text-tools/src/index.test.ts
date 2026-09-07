import assert from "node:assert/strict";
import test from "node:test";

import labelSummary, { normalizeLabel } from "./index.js";

test("normalizes a label", () => {
  assert.equal(normalizeLabel("  release NOTES "), "releaseNotes");
});

test("summarizes nonempty labels", () => {
  assert.equal(
    labelSummary(["First item", " ", "SECOND_item"]),
    "firstItem, secondItem",
  );
});
