// Browser walk of the financial laboratory against REAL published artifacts (no fixtures):
//   python tools/research_rehearsal.py --output artifacts/research-rehearsal
//   python tools/financial_lab_rehearsal.py --search artifacts/research-rehearsal/search --output artifacts/financial-lab
//   python -m auditlab.demo --root artifacts/lab-console serve --port 8765 \
//     --research artifacts/financial-lab/console-families --research artifacts/financial-lab/console-runs \
//     --research docs/research/gemini/evidence/console --research artifacts/research-rehearsal/console-search
//   node web/tests/lab.browser.mjs
// Negative states (cancel, service error, offline, stale, empty, budget stop, live run) are produced by
// intercepting the console API in this test only; they check the interface, not the server.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.C3_PLAYWRIGHT || "playwright");
const base = process.env.C3_URL || "http://127.0.0.1:8765";
const output = process.env.C3_EVIDENCE || "artifacts/research/lab-browser";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.C3_CHROME || undefined, headless: true });
const checks = [], errors = [], external = [], screens = [], timings = [];
const url = hash => `${base}/#${hash}`;
const test = async (name, action) => { console.log("RUN ", name); await action(); checks.push(name); console.log("PASS", name); };
const GEMINI = "gemini-live-official-v2";
const GEMINI_MISS = "gemini-tools--82be64bda11527df";

const watch = page => {
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", m => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource:")) errors.push(m.text()); });
  page.on("request", r => { if (/^https?:/.test(r.url()) && new URL(r.url()).origin !== new URL(base).origin) external.push(r.url()); });
};
async function page(width, height = 900, options = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, ...options });
  const p = await context.newPage();
  watch(p);
  return p;
}
async function settle(p) {
  await p.waitForTimeout(120); // let the hash route render its loading state first
  await p.waitForFunction(() => !document.querySelector("main [aria-busy='true']"), null, { timeout: 20000 });
  await p.waitForTimeout(150);
}
async function go(p, hash) {
  const start = Date.now();
  await p.goto(url(hash)); await settle(p);
  timings.push({ hash, ms: Date.now() - start });
}
async function open(p, locator, pattern) {
  await locator.click();
  await p.waitForURL(pattern);
  await p.waitForTimeout(50);
  await settle(p);
}
async function layout(p, name) {
  const size = await p.evaluate(() => ({ window: innerWidth, body: document.documentElement.scrollWidth }));
  assert.ok(size.body <= size.window, `${name}: horizontal overflow ${JSON.stringify(size)}`);
  screens.push({ name, ...size });
}
async function shot(p, name) { await p.screenshot({ path: join(output, `${name}.png`), fullPage: true }); }
async function cleanText(p, name) {
  const text = await p.locator("main").evaluate(main => {
    const copy = main.cloneNode(true);
    copy.querySelectorAll("details, pre, option").forEach(node => node.remove());
    return copy.innerText;
  });
  assert.doesNotMatch(text, /\b[A-Z]{0,2}[0-9A-F]{16,}\b|\b(?:train|dev|test)-[a-z]+-[a-z0-9_-]+\b|gemini-(?:tools|memory)--|snapshots\/|payment_allocation|provider_error|rate_limit|flag_all|artifact_id|candidate_id/,
    `${name}: raw identifiers in the main text`);
}
const norm = text => text.replace(/[  ]/g, " ");
const textOf = async locator => norm(await locator.innerText());

await test("full path 1440: task → allowed change → agent answer → documents → dossier", async () => {
  const p = await page(1440);
  await go(p, "/research");
  await open(p, p.getByRole("link", { name: "Открыть лабораторию" }), /research\/lab/);
  assert.equal(await p.locator("h1").innerText(), "Финансовая лаборатория");
  await shot(p, "lab-home-1440"); await layout(p, "lab home 1440"); await cleanText(p, "lab home");
  await open(p, p.locator(".choice-list a", { hasText: "Граница периода · компания 2" }).first(), /families/);
  const task = await textOf(p.locator(".lab-task"));
  assert.match(task, /50 000,00 ₸/); assert.match(task, /30 000,00 ₸/); assert.match(task, /20 000,00 ₸/); assert.match(task, /01\.07\.2032 00:00/);
  // Every backend transformation is a link; the boundary slot is visibly unavailable, never a fake launch.
  const slots = p.locator(".transform-list > li");
  assert.equal(await slots.count(), 5);
  assert.equal(await p.locator(".transform.unavailable").count(), 1);
  assert.match(await textOf(p.locator(".transform.unavailable")), /не подключено/);
  assert.equal(await p.locator(".transform-list button").count(), 0, "no launch buttons");
  const expectations = {
    "Переименование документов": { after: /20 000,00 ₸/, expected: /Ответ прежний/ },
    "Законная отвлекающая операция": { after: /20 000,00 ₸/, expected: /Ответ прежний/ },
    "Исправление нарушения": { after: /0,00 ₸/, expected: /Вывод должен исчезнуть/ },
    "Изменение суммы и валюты": { after: /20 000 JPY/, expected: /Ответ меняется: .*20 000,00 ₸ → .*20 000 JPY/ },
  };
  for (const [name, want] of Object.entries(expectations)) {
    await p.locator("a.transform", { hasText: name }).click();
    await p.waitForFunction(n => document.querySelector("a.transform[aria-current='true'] strong")?.textContent === n, name);
    await settle(p);
    assert.equal(await p.locator("a.transform[aria-current='true'] strong").innerText(), name);
    const rows = p.locator(".compare-table tbody tr");
    assert.match(await textOf(rows.nth(3)), want.after, `${name}: residual after change`);
    assert.match(await textOf(rows.nth(4)), want.expected, `${name}: expected answer`);
    assert.match(await textOf(rows.nth(4)), /Независимый расчёт совпал с ожиданием генератора/);
    const side = p.locator(".lab-side").nth(1);
    assert.ok(await side.locator(".attempt-line").count() >= 4, `${name}: agent answers on the variant`);
    await cleanText(p, `family ${name}`);
  }
  await shot(p, "lab-family-currency-1440"); await layout(p, "family 1440");
  // Documents of the changed company open by exact key on the variant side.
  await p.locator(".lab-side").nth(1).getByRole("link", { name: /Платёжное поручение/ }).click();
  await p.waitForURL(/\/side\/variant\/doc\//);
  await p.locator(".research-document .document-fields").waitFor();
  assert.match(await textOf(p.locator(".research-document")), /JPY|¥|50 000/);
  await shot(p, "lab-family-document-1440");
  await p.locator(".research-document").getByRole("link", { name: "Закрыть" }).click();
  await p.waitForFunction(() => !location.hash.includes("/doc/"));
  // Open the dossier of a miss from the original company.
  await p.locator(".lab-side").first().locator(".attempt-line", { hasText: "Пустой ответ" }).getByRole("link", { name: /Досье/ }).click(); await p.waitForURL(/dossiers/); await settle(p);
  assert.match(await p.locator("h1").innerText(), /пропуск нарушения/);
  const dossier = await textOf(p.locator("main"));
  assert.match(dossier, /Пустой ответ: выводов нет/);
  assert.match(dossier, /20 000,00 ₸/);
  assert.match(dossier, /Рассуждения модели не записываются/);
  assert.ok(await p.locator(".needed-list .missing").count() > 0, "missing documents are shown");
  assert.match(dossier, /Исправленный контроль/);
  assert.ok(await p.locator("#control-title ~ ul .attempt-line").count() >= 1, "corrected control has the same agent's answer");
  await cleanText(p, "dossier miss"); await shot(p, "lab-dossier-miss-1440"); await layout(p, "dossier 1440");
  await p.context().close();
});

await test("outcome kinds on real attempts: wrong amount and false accusation", async () => {
  const p = await page(1440);
  await go(p, "/research/families/train-boundary-a/v/train-boundary-a-id_order");
  await p.locator(".lab-side").nth(1).locator(".attempt-line", { hasText: "Обвинить всё" }).getByRole("link", { name: /Досье/ }).click(); await p.waitForURL(/dossiers/); await settle(p);
  const text = await textOf(p.locator("main"));
  assert.match(text, /Неверная сумма/);
  assert.match(text, /По расчёту: 20 000,00 ₸/);
  assert.match(text, /50 000,00 ₸/);
  await shot(p, "lab-dossier-wrong-amount-1440");
  await go(p, "/research/families/train-boundary-a/v/train-boundary-a-fixed");
  await p.locator(".lab-side").nth(1).locator(".attempt-line", { hasText: "Случайные выводы" }).getByRole("link", { name: /Досье/ }).click(); await p.waitForURL(/dossiers/); await settle(p);
  assert.match(await p.locator("h1").innerText(), /ложное обвинение/);
  await p.context().close();
});

await test("Gemini scenario: one miss, three HTTP 429, no comparison claim, saved record", async () => {
  const p = await page(1440);
  await go(p, `/research/experiments/${GEMINI}`);
  const text = await textOf(p.locator("main"));
  assert.match(text, /gemini-3\.1-flash-lite/);
  assert.match(text, /Запись эксперимента: состояние на момент сохранения/);
  assert.doesNotMatch(text, /Живой запуск/);
  assert.equal((text.match(/HTTP 429/g) ?? []).length, 3, "three quota failures named");
  assert.match(text, /1 из 4/);
  await shot(p, "lab-gemini-experiment-1440");
  await p.locator("tr", { hasText: "Выводов не сдано" }).getByRole("link", { name: /Досье/ }).click(); await p.waitForURL(/dossiers/); await settle(p);
  const dossier = await textOf(p.locator("main"));
  assert.match(dossier, /пропуск нарушения/i);
  assert.match(dossier, /30 240,00 ₸/);
  assert.match(dossier, /Пустой ответ: выводов нет/);
  assert.match(await textOf(p.locator("#answer-title ~ .needed-list li.missing")), /Правило распределения платежей[\s\S]*агент не получал/);
  assert.match(dossier, /Технический сбой/);
  await cleanText(p, "gemini dossier"); await shot(p, "lab-gemini-dossier-1440");
  await go(p, `/research/comparisons/comparison-${GEMINI}`);
  const comparison = await textOf(p.locator("main"));
  assert.match(comparison, /Завершено 0 из 2 попыток; сбои сервиса не доказывают качество модели/);
  assert.match(comparison, /Парные различия не опубликованы/);
  assert.doesNotMatch(comparison, /улучшил/);
  await go(p, `/research/map/e/${GEMINI}/b/${encodeURIComponent(`${GEMINI}/gemini-memory`)}/a/${encodeURIComponent(`${GEMINI}/gemini-tools`)}`);
  const map = await textOf(p.locator("main"));
  assert.match(map, /Незавершённые прогоны и ошибки сервиса/);
  assert.match(map, /частичный результат/);
  const counts = await textOf(p.locator("#regression-title ~ dl"));
  assert.match(counts, /Регрессия\s*0/); assert.match(counts, /Улучшение\s*0/); assert.match(counts, /Нельзя сравнить\s*2/);
  await shot(p, "lab-gemini-map-1440");
  // The live state appears only for a running experiment with a connected controller (intercepted).
  await p.route(`**/research/experiments/${GEMINI}`, async route => {
    const reply = await route.fetch(); const json = await reply.json();
    json.experiment.status = "running"; json.controller = { connected: true, budget_configured: true, api_key_configured: true };
    await route.fulfill({ json });
  });
  await go(p, `/research/experiments/${GEMINI}`);
  assert.match(await textOf(p.locator("main")), /Живой запуск/);
  await p.context().close();
});

await test("results map: dimensions, not checked, drill-down, regressions", async () => {
  const p = await page(1440);
  await go(p, "/research/map");
  for (const name of ["Тип нарушения", "Взаимодействие", "Чистые контрпримеры", "Валюты", "Граница периода", "Сложность случая", "Преобразование"]) {
    await p.locator(".segmented a", { hasText: name }).click(); await settle(p);
    assert.equal(await p.locator("#map-title").innerText(), name);
    assert.ok(await p.locator(".map-table tbody tr").count() > 0, name);
  }
  await p.locator(".segmented a", { hasText: "Тип нарушения" }).click(); await settle(p);
  assert.ok(await p.locator(".not-checked-cell").count() > 0, "missing measurements are «не проверено»");
  assert.equal(await p.locator(".map-cell", { hasText: /^0 сем/ }).count(), 0, "no zero cells");
  await shot(p, "lab-map-1440"); await layout(p, "map 1440"); await cleanText(p, "map");
  const cell = p.locator("tr", { hasText: "Нераспределённый платёж" }).locator("a.map-cell").first();
  const label = await cell.getAttribute("aria-label");
  const attempts = Number(/попыток (\d+)/.exec(label)[1]);
  await cell.click(); await settle(p);
  assert.equal(await p.locator("#drill-title ~ table tbody tr").count(), attempts);
  await p.locator("#drill-title ~ table").getByRole("link", { name: /Досье/ }).first().click(); await p.waitForURL(/dossiers/); await settle(p);
  assert.match(await p.locator("h1").innerText(), /^Досье/);
  await p.goBack(); await settle(p);
  // Default pair comes from the published comparison; swapping reveals regressions.
  const before = await textOf(p.locator("#regression-title ~ dl"));
  assert.match(before, /Улучшение\s*25/);
  await p.getByRole("link", { name: "Поменять местами" }).click(); await settle(p);
  assert.match(await textOf(p.locator("#regression-title ~ dl")), /Регрессия\s*25/);
  assert.ok(await p.locator("#regression-title ~ table tbody tr").count() >= 25);
  await shot(p, "lab-map-regression-1440");
  await p.context().close();
});

await test("states: cancel, service error, offline, stale, empty, budget stop", async () => {
  const p = await page(1280);
  let release;
  await p.route("**/research/lab", route => new Promise(resolve => { release = () => resolve(route.continue()); }));
  await p.goto(url("/research/lab"));
  await p.getByRole("button", { name: "Отменить загрузку" }).click();
  await p.getByRole("heading", { name: "Загрузка отменена" }).waitFor();
  await shot(p, "lab-state-cancelled-1280");
  release?.(); await p.unroute("**/research/lab");
  await p.getByRole("button", { name: "Загрузить снова" }).click(); await settle(p);
  assert.equal(await p.locator("h1").innerText(), "Финансовая лаборатория");
  await p.route("**/research/lab/map**", route => route.fulfill({ status: 500, body: "{}" }));
  await go(p, "/research/map");
  assert.match(await textOf(p.locator("main")), /Сервис исследований вернул ошибку/);
  await shot(p, "lab-state-error-1280");
  await p.unroute("**/research/lab/map**");
  await p.route("**/research/lab/families/**", route => route.abort());
  await go(p, "/research/families/train-boundary-a");
  assert.match(await textOf(p.locator("main")), /Не удалось подключиться к сервису исследований/);
  await p.unroute("**/research/lab/families/**");
  await go(p, "/research/lab");
  await p.route("**/research/lab", route => route.fulfill({ status: 503, body: "{}" }));
  await p.getByRole("button", { name: "Обновить" }).click();
  await p.locator(".stale-note").waitFor();
  assert.match(await textOf(p.locator(".stale-note")), /Не удалось обновить данные. Показан ответ от/);
  await shot(p, "lab-state-stale-1280");
  await p.unroute("**/research/lab");
  await p.route("**/research/lab", route => route.fulfill({ json: { families: [], experiments: [] } }));
  await p.reload(); await settle(p);
  assert.match(await textOf(p.locator("main")), /Связанные ситуации не опубликованы/);
  assert.match(await textOf(p.locator("main")), /Прогонов нет/);
  await shot(p, "lab-state-empty-1280");
  await p.unroute("**/research/lab");
  await p.route(`**/research/experiments/${GEMINI}`, async route => {
    const reply = await route.fetch(); const json = await reply.json();
    json.experiment.status = "stopped"; json.experiment.stop_reason = { code: "budget_exceeded" };
    await route.fulfill({ json });
  });
  await go(p, `/research/experiments/${GEMINI}`);
  assert.match(await textOf(p.locator("main")), /Достигнут лимит расходов эксперимента/);
  await shot(p, "lab-state-budget-1280");
  await p.context().close();
});

await test("phone 390 and 320: no horizontal overflow", async () => {
  for (const width of [390, 320]) {
    const p = await page(width, 844);
    for (const [hash, name] of [["/research/lab", "home"], ["/research/families/train-boundary-a/v/train-boundary-a-currency", "family"],
      [`/research/dossiers/${GEMINI_MISS}`, "dossier"], ["/research/map", "map"]]) {
      await go(p, hash);
      await layout(p, `${name} ${width}`);
      await shot(p, `lab-${name}-${width}`);
    }
    await p.context().close();
  }
});

await test("keyboard, focus and reduced motion", async () => {
  const p = await page(1440, 900, { reducedMotion: "reduce" });
  await go(p, "/research/families/train-boundary-a");
  let reached = false;
  for (let i = 0; i < 60 && !reached; i++) {
    await p.keyboard.press("Tab");
    reached = await p.evaluate(() => document.activeElement?.matches("a.transform") && document.activeElement.textContent.includes("Исправление нарушения"));
  }
  assert.ok(reached, "transformation links reachable with Tab");
  assert.notEqual(await p.evaluate(() => getComputedStyle(document.activeElement).outlineStyle), "none");
  await p.keyboard.press("Enter"); await settle(p);
  assert.match(await p.evaluate(() => location.hash), /train-boundary-a-fixed/);
  reached = false;
  for (let i = 0; i < 80 && !reached; i++) {
    await p.keyboard.press("Tab");
    reached = await p.evaluate(() => document.activeElement?.classList.contains("doc-chip"));
  }
  assert.ok(reached, "document chips reachable with Tab");
  await p.keyboard.press("Enter");
  await p.locator(".research-document").waitFor();
  assert.ok(await p.evaluate(() => document.activeElement?.classList.contains("research-document")), "focus moves to the document");
  let release;
  await p.route("**/research/lab/map", route => new Promise(resolve => { release = () => resolve(route.continue()); }));
  await p.goto(url("/research/map"));
  await p.locator(".spinner").waitFor();
  assert.equal(await p.locator(".spinner").evaluate(e => getComputedStyle(e).animationName), "none");
  release?.();
  await p.context().close();
});

await test("no external requests and no page errors", async () => {
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
});
await writeFile(join(output, "browser-results.json"), JSON.stringify({ base, checks, screens, timings, external, errors }, null, 1) + "\n");
await browser.close();
console.log(`OK ${checks.length} checks`);
