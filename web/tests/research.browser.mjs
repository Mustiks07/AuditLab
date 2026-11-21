// Browser acceptance of the research path. Run against a console started with research fixtures:
//   python -m auditlab.demo --root artifacts/research-browser serve --port 8765 --research-fixtures
//   node web/tests/research.browser.mjs
// Negative states (network, empty data, long names, connected controller) are produced by intercepting
// the console API in this test only; they verify the interface, not the server.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.C3_PLAYWRIGHT || "playwright");
const base = process.env.C3_URL || "http://127.0.0.1:8765";
const output = process.env.C3_EVIDENCE || "artifacts/research/browser";
const repo = process.env.AUDITLAB_REPO || new URL("../../", import.meta.url).pathname;
const original = join(repo, "datasets/open/train/E651B6A63DDA2448A91DC825714D7732E/public");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.C3_CHROME || undefined, headless: true });
const checks = [], errors = [], external = [], screens = [];
const url = hash => `${base}/#${hash}`;
const test = async (name, action) => { console.log("RUN ", name); await action(); checks.push(name); console.log("PASS", name); };

async function treeHash(dir) {
  const hash = createHash("sha256");
  for (const name of (await readdir(dir)).sort()) { hash.update(name); hash.update(await readFile(join(dir, name))); }
  return hash.digest("hex");
}
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
  await p.waitForFunction(() => !document.querySelector("main [aria-busy='true']"));
  await p.waitForTimeout(150);
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
    copy.querySelectorAll("details, pre").forEach(node => node.remove());
    return [copy.innerText, ...[...main.querySelectorAll("[title],[aria-label]")].map(e => `${e.getAttribute("title") ?? ""} ${e.getAttribute("aria-label") ?? ""}`)].join("\n");
  });
  assert.doesNotMatch(text, /\b[A-Z]{0,2}[0-9A-F]{16,}\b|\b(?:exp|fail|att|red|cur|trn|cmp|val)-[a-z0-9-]+\b|snapshots\/|dev_fixture|payment_allocation|provider_error|budget_stop|demo\/|auditlab\.[a-z]|run_id|artifact_id/,
    `${name}: raw identifiers in the main text`);
}
const norm = text => text.replace(/[\u00a0\u202f]/g, " ");
const textOf = async locator => norm(await locator.innerText());
const textsOf = async locator => (await locator.allInnerTexts()).map(norm);
const heading = (p, name) => p.getByRole("heading", { level: 1, name }).waitFor();

const before = await treeHash(original);

await test("full research path on desktop with real amounts and exact evidence", async () => {
  const p = await page(1440);
  await p.goto(url("/"));
  await p.getByRole("link", { name: "Исследование" }).first().click();
  await heading(p, "Исследование агентов"); await settle(p);
  await p.getByText("Фикстура разработки", { exact: false }).first().waitFor();
  await p.getByText("Несовместимые результаты: 1").waitFor();
  await cleanText(p, "home"); await layout(p, "home-1440"); await shot(p, "research-home-1440");
  await p.locator(".experiment-card").first().click();
  await heading(p, "Агент без памяти и агент с заметками"); await settle(p);
  for (const text of ["Модель", "Варианты агента", "Режим проверки", "Лимиты", "100 действий · 60 с на попытку", "10 из 10", "Неуспешных попыток", "Время", "Расходы"]) {
    await p.getByText(text, { exact: false }).first().waitFor();
  }
  assert.equal(await p.getByRole("button", { name: "Запустить" }).isDisabled(), true);
  await p.getByText("контроллер экспериментов не подключён", { exact: false }).first().waitFor();
  await p.getByText("Запись эксперимента", { exact: false }).waitFor();
  await cleanText(p, "experiment"); await layout(p, "experiment-1440"); await shot(p, "research-experiment-1440");
  await p.getByRole("link", { name: "Разобрать слабость" }).click();
  await heading(p, "Остаток платежа на границе периода"); await settle(p);
  const situation = await textOf(p.locator(".situation-calc"));
  for (const amount of ["50 000,00 ₸", "30 000,00 ₸", "20 000,00 ₸", "01.12.2026 00:00 (не включительно)"]) assert.ok(situation.includes(amount), amount);
  const check = await textOf(p.getByRole("heading", { name: "Проверка ответа" }).locator(".."));
  assert.ok(check.includes("20 000,00 ₸") && check.includes("50 000,00 ₸") && check.includes("Вывод не засчитан"), check);
  assert.equal(await p.locator(".needed-list .missing").count(), 2);
  await p.getByText("Агент не получил 2 из 4 документов", { exact: false }).waitFor();
  await p.getByText("Сохранённая траектория", { exact: false }).first().waitFor();
  await p.getByText("Открыт документ: платёжное поручение").waitFor();
  await p.locator(".needed-list .missing a").first().click();
  await p.waitForURL(/\/doc\/ledger\/RB74667B32C20460A9F0BCE8C$/);
  await p.locator(".research-document .document-fields").waitFor();
  const doc = await textOf(p.locator(".research-document"));
  assert.ok(doc.includes("Зачёт платежа") && doc.includes("30 000,00 ₸") && doc.includes("Нераспределённые поступления"), doc);
  await p.locator(".research-document summary").click();
  assert.ok((await p.locator(".research-document pre").innerText()).includes("RB74667B32C20460A9F0BCE8C"));
  await cleanText(p, "failure"); await layout(p, "failure-1440"); await shot(p, "research-1440");
  await p.goBack(); await p.waitForURL(url => !url.hash.includes("/doc/"));
  await p.locator(".attempt-tabs a").nth(2).click(); await settle(p);
  assert.equal(await p.locator(".needed-list .missing").count(), 0);
  await p.getByText("Агент получил все документы", { exact: false }).waitFor();
  await p.getByRole("link", { name: "Сокращённый случай" }).last().click();
  await heading(p, "Сокращённый случай"); await settle(p);
  const sides = await textsOf(p.locator(".reduction-side"));
  assert.ok(sides[0].includes("240") && sides[1].includes("8") && sides[1].includes("20 000,00 ₸") && sides[1].includes("2 из 2"), sides.join("|"));
  const families = await textsOf(p.locator(".research-table tbody tr"));
  assert.equal(families.length, 3);
  assert.ok(families[0].includes("Исправленный дефект") && families[0].includes("Ложное обвинение") && families[0].includes("Ответ изменился верно"));
  assert.ok(families[2].includes("Переименованные документы") && families[2].includes("Ошибка сохранилась"));
  await cleanText(p, "reduction"); await layout(p, "reduction-1440"); await shot(p, "research-reduction-1440");
  await p.getByRole("link", { name: "Учебный набор" }).last().click();
  await heading(p, "Учебный набор: остатки на границе периода"); await settle(p);
  for (const text of ["Обучение весов", "Изменение инструкции", "Примеры в запросе (few-shot)", "Нет доступа к обучению", "Сбой обучения", "Прервано", "Готово"]) {
    await p.getByText(text, { exact: true }).first().waitFor();
  }
  await cleanText(p, "curriculum"); await layout(p, "curriculum-1440"); await shot(p, "research-curriculum-1440");
  await p.getByRole("link", { name: "Сравнить версии" }).click();
  await heading(p, "Версии агента на новых компаниях"); await settle(p);
  for (const text of ["9 из 60", "117 из 120", "31,60 $", "Нет результата: Обучение весов недоступно"]) await p.getByText(text, { exact: false }).first().waitFor();
  assert.equal(await p.getByText("различие не установлено", { exact: false }).count(), 2);
  await p.getByText("Независимая проверка метрик: не проверялась", { exact: false }).waitFor();
  await cleanText(p, "comparison"); await layout(p, "comparison-1440"); await shot(p, "research-comparison-1440");
  await p.context().close();
});

await test("operational states: stopped by budget, failed attempts, unavailable model, not found", async () => {
  const p = await page(1280);
  await p.goto(url("/research/experiments/exp-budget-stop")); await settle(p);
  await p.getByText("Достигнут лимит расходов эксперимента.", { exact: false }).waitFor();
  for (const text of ["Превышено время", "Ошибка сервиса модели", "Некорректный ответ модели", "Остановлена по бюджету"]) await p.getByText(text).first().waitFor();
  assert.equal(await p.getByText("Оценки нет: попытка не завершена").count(), 4);
  await p.getByText("25,00 $ из 25,00 $").waitFor();
  await layout(p, "budget-1280"); await shot(p, "research-budget-stop-1280");
  await p.locator("a", { hasText: "Траектория" }).nth(1).click(); await settle(p);
  await p.getByText("Финансовой оценки у этой попытки нет", { exact: false }).waitFor();
  await p.goto(url("/research/experiments/exp-model-unavailable")); await settle(p);
  await p.getByText("Выбранная модель сейчас недоступна у провайдера.").waitFor();
  await shot(p, "research-model-unavailable-1280");
  await p.goto(url("/research/failures/no-such-failure")); await settle(p);
  await p.getByRole("heading", { name: "Результат недоступен" }).waitFor();
  await p.goto(url("/research/failures/fail-boundary-residual")); await settle(p);
  await layout(p, "failure-1280"); await shot(p, "research-1280");
  await p.context().close();
});

await test("network failure, server error, timeout and stale data", async () => {
  const p = await page(1440);
  await p.route("**/research/overview", route => route.abort());
  await p.goto(url("/research")); await settle(p);
  await p.getByRole("heading", { name: "Не удалось загрузить данные" }).waitFor();
  await p.getByText("Не удалось подключиться к сервису исследований.").waitFor();
  await shot(p, "research-state-offline");
  await p.unroute("**/research/overview");
  await p.route("**/research/overview", route => route.fulfill({ status: 500, body: "{}" }));
  await p.getByRole("button", { name: "Повторить" }).click();
  await p.getByText("Сервис исследований вернул ошибку. Попробуйте позже.").waitFor();
  await p.unroute("**/research/overview");
  await p.getByRole("button", { name: "Повторить" }).click();
  await heading(p, "Исследование агентов");
  await p.goto(url("/research/experiments/exp-memory-notes")); await settle(p);
  await p.route("**/research/experiments/exp-memory-notes", route => route.abort());
  await p.getByRole("button", { name: "Обновить" }).click();
  await p.getByText("Не удалось обновить данные. Показан ответ от", { exact: false }).waitFor();
  await p.getByText("10 из 10").waitFor();
  await shot(p, "research-state-stale");
  await p.unroute("**/research/experiments/exp-memory-notes");
  await p.route("**/research/comparisons/**", () => {});
  await p.goto(url("/research/comparisons/cmp-holdout-v1"));
  await p.getByText("Сервис исследований не ответил вовремя.").waitFor({ timeout: 20000 });
  await shot(p, "research-state-timeout");
  await p.context().close();
});

await test("empty research data and long names on a phone", async () => {
  const p = await page(390, 844);
  await p.route("**/research/status", route => route.fulfill({ json: { roots_configured: false, counts: {}, incompatible: 0, provenance: [],
    controller: { connected: false, reason: "controller_not_connected", budget_configured: null, api_key_configured: null } } }));
  await p.route("**/research/overview", route => route.fulfill({ json: { experiments: [], curricula: [], training: [], comparisons: [], incompatible: [] } }));
  await p.goto(url("/research")); await settle(p);
  await p.getByRole("heading", { name: "Пока нет результатов исследований" }).waitFor();
  await layout(p, "empty-390"); await shot(p, "research-state-empty-390");
  await p.unroute("**/research/status"); await p.unroute("**/research/overview");
  const long = "Очень длинное название эксперимента ".repeat(9) + "без пробелов_" + "x".repeat(80);
  await p.route("**/research/experiments/exp-memory-notes", async route => {
    const reply = await route.fetch(); const body = await reply.json();
    body.experiment.title = long; body.attempts[0].company_label = long;
    await route.fulfill({ response: reply, json: body });
  });
  await p.goto(url("/research/experiments/exp-memory-notes")); await settle(p);
  await layout(p, "long-390"); await shot(p, "research-long-names-390");
  await p.unroute("**/research/experiments/exp-memory-notes");
  for (const [hash, name] of [["/research/failures/fail-boundary-residual/attempt/att-b-plain-1/doc/ledger/RB74667B32C20460A9F0BCE8C", "research-390"],
    ["/research/reductions/red-boundary-residual", "research-reduction-390"], ["/research/comparisons/cmp-holdout-v1", "research-comparison-390"],
    ["/research/curricula/cur-boundary-v1", "research-curriculum-390"]]) {
    await p.goto(url(hash)); await settle(p); await layout(p, name); await shot(p, name);
  }
  const small = await page(320, 700);
  await small.goto(url("/research/failures/fail-boundary-residual")); await settle(small);
  await layout(small, "failure-320"); await shot(small, "research-320");
  await small.context().close();
  await p.context().close();
});

await test("connected controller: refusals, accepted start and live label (intercepted)", async () => {
  const p = await page(1440);
  let controller = { connected: true, reason: null, budget_configured: false, api_key_configured: true };
  let status = "completed";
  await p.route("**/research/experiments/exp-memory-notes", async route => {
    if (route.request().method() !== "GET") return route.fallback();
    const reply = await route.fetch(); const body = await reply.json();
    body.controller = controller; body.experiment.status = status;
    await route.fulfill({ response: reply, json: body });
  });
  await p.goto(url("/research/experiments/exp-memory-notes")); await settle(p);
  await p.getByText("на сервере не задан бюджет эксперимента", { exact: false }).waitFor();
  assert.equal(await p.getByRole("button", { name: "Запустить" }).isDisabled(), true);
  controller = { ...controller, budget_configured: true };
  await p.route("**/research/experiments/exp-memory-notes/start", route => route.fulfill({ status: 409, json: { detail: { code: "model_unavailable" } } }));
  await p.getByRole("button", { name: "Обновить" }).click();
  await p.getByRole("button", { name: "Запустить" }).click();
  await p.getByText("Выбранная модель сейчас недоступна у провайдера.").waitFor();
  await p.unroute("**/research/experiments/exp-memory-notes/start");
  await p.route("**/research/experiments/exp-memory-notes/start", route => route.fulfill({ json: { ok: true } }));
  await p.getByRole("button", { name: "Запустить" }).click();
  await p.getByText("Контроллер принял запуск.").waitFor();
  status = "running";
  await p.getByRole("button", { name: "Обновить" }).click();
  await p.getByText("Живой запуск").waitFor();
  assert.equal(await p.getByRole("button", { name: "Остановить" }).isDisabled(), false);
  await shot(p, "research-state-live-controller");
  await p.context().close();
});

await test("keyboard, focus and reduced motion", async () => {
  const p = await page(1440, 900, { reducedMotion: "reduce" });
  await p.goto(url("/research/failures/fail-boundary-residual")); await settle(p);
  const duration = await p.locator(".research-path a").first().evaluate(a => getComputedStyle(a).transitionDuration);
  assert.ok(duration.split(",").every(value => parseFloat(value) === 0), duration);
  await p.keyboard.press("Tab");
  assert.equal(await p.evaluate(() => document.activeElement?.textContent), "К содержимому");
  let reached = false;
  for (let i = 0; i < 40 && !reached; i++) {
    await p.keyboard.press("Tab");
    reached = await p.evaluate(() => document.activeElement?.closest(".needed-list") !== null);
  }
  assert.ok(reached, "evidence links reachable with Tab");
  const outline = await p.evaluate(() => getComputedStyle(document.activeElement).outlineStyle);
  assert.notEqual(outline, "none");
  await p.keyboard.press("Enter");
  await p.waitForURL(/\/doc\//);
  await p.locator(".research-document").waitFor();
  assert.ok(await p.evaluate(() => document.activeElement?.classList.contains("research-document")), "focus moves to the document");
  await p.context().close();
});

const after = await treeHash(original);
await test("originals unchanged and no external requests", async () => {
  assert.equal(after, before, "datasets/open public files changed");
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
});
await writeFile(join(output, "browser-results.json"), JSON.stringify({ base, checks, screens, external, errors,
  original_public_sha256: { before, after } }, null, 1) + "\n");
await browser.close();
console.log(`OK ${checks.length} checks`);
