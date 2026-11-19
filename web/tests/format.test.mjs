import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/format.ts', import.meta.url), 'utf8');
const emitted = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { money } = await import('data:text/javascript;base64,' + Buffer.from(emitted).toString('base64'));

test('manifest exponents govern all displayed currency units', () => {
  const exps = { JPY: 0, KZT: 2, KWD: 3 };
  assert.equal(money(12000, 'JPY', exps), '12 000 JPY');
  assert.equal(money(20000, 'KWD', exps), '20,000 KWD');
  assert.equal(money(-1, 'KWD', exps), '−0,001 KWD');
  assert.equal(money(2000000, 'KZT', exps), '20 000,00 ₸');
  assert.match(money(12000, 'JPY', {}), /разрядность не указана/);
  assert.match(money(9007199254740992, 'JPY', exps), /неточное число/);
});

test('minor units keep the same minus sign and grouping as money()', async () => {
  const { minor, opError } = await import('data:text/javascript;base64,' + Buffer.from(emitted).toString('base64'));
  assert.equal(minor(-2000000), '−2 000 000');
  assert.equal(minor(3000000), '3 000 000');
  assert.match(opError('unsupported_counterfactual', 'Payment-date changes must stay inside the same target period'), /другой отчётный период.*\[unsupported_counterfactual: Payment-date/);
  assert.match(opError('something_new', undefined), /операция не выполнена \[something_new\]/);
});
