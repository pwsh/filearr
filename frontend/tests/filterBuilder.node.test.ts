// Pure-codec tests for the visual filter builder (FIX-14).
//
// Runs on Node's built-in test runner with native TypeScript type-stripping
// (Node >=22.18): `node --test frontend/tests/`. No bundler / DOM needed — this
// exercises the DOM-free rows<->DSL codec in ../src/lib/filterBuilder.ts.
//
// Focus: the codec is TOTAL and compiles the VALID SUBSET of rows —
//   * incomplete rows (empty value/key) are excluded, never throw;
//   * a `<input type="number">` binding gives a NUMBER (or null), which must not
//     crash the codec (the FIX-14 root cause: `.trim()` on a number);
//   * malformed values (invalid) are excluded too, but reported distinctly from
//     incomplete ones;
//   * a mix of valid + incomplete/invalid compiles exactly the valid rows.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  newCondition,
  conditionToDsl,
  conditionsToDsl,
  validateCondition,
  type Condition,
} from "../src/lib/filterBuilder.ts";

function row(field: Condition["field"], patch: Partial<Condition> = {}): Condition {
  return Object.assign(newCondition(field), patch);
}

test("empty value => incomplete, excluded, no throw", () => {
  const c = row("size", { op: ">", unit: "M", value: "" });
  assert.equal(validateCondition(c).state, "incomplete");
  assert.equal(conditionToDsl(c), null);
});

test("number-typed value (from <input type=number>) does not throw and compiles", () => {
  // Svelte binds a NUMBER, not a string — the exact FIX-14 crash input.
  const c = row("size", { op: ">", unit: "G", value: 1 as unknown as string });
  assert.equal(validateCondition(c).state, "ok");
  assert.equal(conditionToDsl(c), "size:>1G");
});

test("null value (empty number input) is incomplete, not a crash", () => {
  const c = row("size", { op: ">", unit: "M", value: null as unknown as string });
  assert.equal(validateCondition(c).state, "incomplete");
  assert.equal(conditionToDsl(c), null);
});

test("malformed size (non-integer) => invalid, excluded", () => {
  const c = row("size", { op: ">", unit: "M", value: "1.5" });
  assert.equal(validateCondition(c).state, "invalid");
  assert.equal(conditionToDsl(c), null);
});

test("modified relative number compiles; empty is incomplete", () => {
  const ok = row("modified", { op: ">", unit: "d", dateMode: "relative", value: 7 as unknown as string });
  assert.equal(conditionToDsl(ok), "modified:>7d");
  const empty = row("modified", { op: ">", unit: "d", dateMode: "relative", value: "" });
  assert.equal(validateCondition(empty).state, "incomplete");
  assert.equal(conditionToDsl(empty), null);
});

test("modified absolute requires YYYY-MM-DD", () => {
  const good = row("modified", { op: ">=", dateMode: "absolute", value: "2025-01-01" });
  assert.equal(conditionToDsl(good), "modified:>=2025-01-01");
  const bad = row("modified", { op: ">=", dateMode: "absolute", value: "2025" });
  assert.equal(validateCondition(bad).state, "invalid");
});

test("range needs both bounds; partial is incomplete", () => {
  const partial = row("size", { op: "range", unit: "M", value: 100 as unknown as string, value2: "" });
  assert.equal(validateCondition(partial).state, "incomplete");
  assert.equal(conditionToDsl(partial), null);
  const full = row("size", { op: "range", unit: "M", value: 100 as unknown as string, value2: 4000 as unknown as string });
  assert.equal(conditionToDsl(full), "size:100M..4000M");
});

test("mixed valid + incomplete/invalid compiles ONLY the valid subset", () => {
  const rows: Condition[] = [
    row("kind", { value: "video" }),                                   // valid
    row("size", { op: ">", unit: "G", value: 1 as unknown as string }),// valid (number)
    row("modified", { op: ">", unit: "d", value: "" }),                // incomplete
    row("size", { op: ">", unit: "M", value: "abc" }),                 // invalid
    row("tag", { value: "archived", op: "is_not" }),                   // valid (negated)
  ];
  assert.equal(conditionsToDsl(rows), "kind:video size:>1G -tag:archived");
});

test("zero valid rows compiles to empty string, never throws", () => {
  const rows: Condition[] = [
    row("size", { op: ">", unit: "M", value: "" }),
    row("modified", { op: ">", unit: "d", value: null as unknown as string }),
  ];
  assert.equal(conditionsToDsl(rows), "");
});

test("meta requires key AND value; distinct hints", () => {
  const noKey = row("meta", { op: ">=", key: "", value: "1080" });
  assert.equal(validateCondition(noKey).state, "incomplete");
  const noVal = row("meta", { op: ">=", key: "height", value: "" });
  assert.equal(validateCondition(noVal).state, "incomplete");
  const good = row("meta", { op: ">=", key: "height", value: "1080" });
  assert.equal(conditionToDsl(good), "meta.height:>=1080");
});
