import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/workspace/model.ts", import.meta.url), "utf8");
const emitted = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
}).outputText;
const { companyName, documentConfirmed, independentConfirmed, realPairs, runResult, timeline } =
  await import("data:text/javascript;base64," + Buffer.from(emitted).toString("base64"));

const run = (run_id, extra = {}) => ({
  run_id, episode_id: `episode-${run_id}`, status: "completed", ...extra,
});

test("completed checks distinguish known empty, positive findings, unknown, and failure", () => {
  assert.deepEqual(runResult(run("empty", { findings: 0 })), { text: "Без выводов", tone: "clear" });
  assert.deepEqual(runResult(run("one", { findings: 1 })), { text: "Есть вывод", tone: "attention" });
  assert.deepEqual(runResult(run("unknown", { findings: null })), { text: "Результат недоступен", tone: "unknown" });
  assert.equal(runResult(run("omitted")).tone, "unknown");
  assert.deepEqual(runResult(run("failed", { status: "infrastructure_error", findings: 0 })), {
    text: "Не удалось выполнить проверку", tone: "unknown",
  });
});

function finding() {
  return {
    finding: { currency: "KZT", claimed_amount_minor: 2000000 },
    console_agrees: true,
    uncited_operands: [],
    evidence: [{ key: "ledger/1", exists: true }, { key: "policy/allocation", exists: true }],
    derivation: {
      currency: "KZT", result_minor: 2000000,
      operands: [{ key: "ledger/1" }],
      evidence: ["ledger/1", "policy/allocation"],
      index_check: { complete: true },
    },
    verifier: { connected: true, agrees: true, currency_matches: true, amount_minor: 2000000, currency: "KZT" },
  };
}

test("document verification requires the calculation and accessible cited operands", () => {
  assert.equal(documentConfirmed(finding()), true);
  for (const change of [
    view => { view.derivation = null; },
    view => { view.derivation.result_minor = null; },
    view => { delete view.derivation.currency; },
    view => { view.uncited_operands.push("ledger/2"); },
    view => { view.evidence[0].exists = false; },
    view => { view.evidence.pop(); },
    view => { view.derivation.index_check.complete = false; },
    view => { delete view.console_agrees; },
  ]) {
    const view = finding();
    change(view);
    assert.equal(documentConfirmed(view), false, String(change));
  }
});

test("independent verification needs explicit agreement and the published matching amount and currency", () => {
  assert.equal(independentConfirmed(finding()), true);
  for (const key of ["connected", "agrees", "currency_matches", "amount_minor", "currency"]) {
    const view = finding();
    const value = view.verifier[key];
    delete view.verifier[key];
    assert.equal(independentConfirmed(view), false, key);
    view.verifier[key] = typeof value === "boolean" ? false : null;
    assert.equal(independentConfirmed(view), false, key);
  }
  const contradictory = finding();
  contradictory.verifier.currency = "USD";
  assert.equal(independentConfirmed(contradictory), false);
  delete contradictory.verifier;
  assert.equal(independentConfirmed(contradictory), false);
  const zero = finding();
  zero.finding.claimed_amount_minor = zero.verifier.amount_minor = 0;
  assert.equal(independentConfirmed(zero), true);
});

test("a known sample title requires exact episode metadata", () => {
  const sample = {
    episode_id: "S02", label: "training_sample", company: "Компания S02 · зачёт после границы",
  };
  assert.equal(companyName(run("any-id", sample)), "Частичное распределение");
  assert.equal(companyName(run("other", { ...sample, episode_id: "other-S02" })), "Компания");
  assert.equal(companyName(run("other-S02", { ...sample, company: "Северный торговый дом" })), "Северный торговый дом");
  assert.equal(companyName(run("other", { ...sample, label: "export" })), "Компания");
});

test("role and parent metadata determine pair direction regardless of lexical ID order", () => {
  const a = run("z-first", { pair_id: "pair", pair_role: "A" });
  const b = run("a-second", { pair_id: "pair", pair_role: "B" });
  const child = run("b-child", { parent_run_id: b.run_id, pair_id: "pair" });
  const incomplete = run("incomplete", { status: "truncated", parent_run_id: a.run_id });
  assert.deepEqual(realPairs([child, b, incomplete, a]), [
    { left: a, right: b, directed: true },
    { left: b, right: child, directed: true },
  ]);
  const left = run("x", { pair_id: "unoriented" });
  const right = run("y", { pair_id: "unoriented" });
  assert.deepEqual(realPairs([left, right]), [{ left, right, directed: false }]);
});

test("a period timeline needs directed pairing, an exclusive shared rule and a changing operand", () => {
  const pair = { left: run("a"), right: run("b"), directed: true };
  const boundary = "2026-12-01T00:00:00+05:00";
  const before = "2026-11-30T23:59:00+05:00";
  const after = "2026-12-01T00:01:00+05:00";
  const calc = { rule: { boundary, boundary_exclusive: true }, operands: [{ key: "ledger/1" }] };
  const data = {
    calculations: [{ type: "payment_allocation", left: calc, right: structuredClone(calc) }],
    changes: [{ key: "ledger/1", field: "timestamp", before, after }],
  };
  assert.deepEqual(timeline(pair, data), { a: "30.11 · 23:59", b: "01.12 · 00:01", boundary: "01.12 · 00:00" });
  assert.equal(timeline({ ...pair, directed: false }, data), null);
  assert.equal(timeline(pair, undefined), null);
  assert.equal(timeline(pair, { ...data, changes: [{ ...data.changes[0], key: "ledger/unrelated" }] }), null);
  assert.equal(timeline(pair, { ...data, changes: [{ ...data.changes[0], after: "2026-11-30T23:59:59+05:00" }] }), null);
  data.calculations[0].right.rule.boundary_exclusive = false;
  assert.equal(timeline(pair, data), null);
});
