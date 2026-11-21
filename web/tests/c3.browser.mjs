// Run against a fresh local AuditLab sample store with Playwright available outside the app lockfile.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.C3_PLAYWRIGHT || "playwright");
const base = process.env.C3_URL || "http://127.0.0.1:8765";
const output = process.env.C3_EVIDENCE || "artifacts/c3/browser";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  executablePath: process.env.C3_CHROME || undefined, headless: true,
});
const checks = [];
const errors = [];
const external = [];
const missing = [];
const warnings = [];
const screens = [];
const url = (hash) => `${base}/#${hash}`;
const test = async (name, action) => {
  console.log("RUN ", name);
  await action();
  checks.push(name);
  console.log("PASS", name);
};
async function layout(page, width, name) {
  const sizes = await page.evaluate(() => ({
    window: innerWidth, body: document.documentElement.scrollWidth,
    font: [...document.fonts].some(font => font.family === "Inter" && font.status === "loaded"),
  }));
  assert.equal(sizes.window, width, name);
  assert.ok(sizes.body <= width, `${name}: unexpected horizontal layout ${JSON.stringify(sizes)}`);
  assert.equal(sizes.font, true, name);
  screens.push({ name, ...sizes });
}
async function heading(page, name) {
  await page.getByRole("heading", { name, exact: true, level: 1 }).waitFor();
}
async function cleanText(page) {
  await page.waitForFunction(() => !document.querySelector("main .loading, main [aria-busy='true']"));
  const content = await page.locator("main").evaluate(main =>
    [
      main.innerText,
      ...[...main.querySelectorAll("[title],[aria-label]")].flatMap(element =>
        [element.getAttribute("title"), element.getAttribute("aria-label")]),
    ].join("\n"));
  assert.doesNotMatch(content, /\b(?:run_id|episode_id|infrastructure_error|reconcile|S0[12]|FX01|demo\/|auditlab\.|[0-9a-f]{12,})\b/i);
}
const watch = page => {
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", message => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource:")) warnings.push(message.text());
  });
  page.on("request", request => {
    if (/^https?:/.test(request.url()) && new URL(request.url()).origin !== new URL(base).origin) external.push(request.url());
  });
  page.on("response", response => {
    if (response.status() >= 400 && /\.(?:js|css|ttf)(?:\?|$)/.test(response.url())) missing.push(response.url());
  });
};
const shot = (page, name) => page.screenshot({ path: join(output, name + ".png"), fullPage: true });
const account = async (page, path) => {
  const response = await page.request.get(base + path);
  assert.ok(response.ok(), `${path}: ${response.status()}`);
  return response.json();
};

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, reducedMotion: "reduce" });
  context.setDefaultTimeout(12_000);
  const page = await context.newPage();
  watch(page);
  const runs = (await account(page, "/demo/runs")).runs;
  const a = runs.find(run => run.pair_id === "train-boundary" && run.pair_role === "A");
  const b = runs.find(run => run.pair_id === "train-boundary" && run.pair_role === "B");
  const s01 = runs.find(run => run.episode_id === "S01" && run.label === "training_sample");
  const s02 = runs.find(run => run.episode_id === "S02" && run.label === "training_sample");
  assert.ok(a && b && s01 && s02, "Use samples in a fresh demo store");
  const findingB = (await account(page, `/demo/runs/${b.run_id}`)).findings[0];
  await test("Real home, local font, amount, dates, modal and keyboard", async () => {
    await page.goto(url("/"));
    await heading(page, "Рабочая область");
    await page.getByRole("heading", { name: "Компания B", exact: true }).waitFor();
    await page.getByText("20\u00a0000,00\u00a0₸", { exact: true }).first().waitFor();
    await page.getByText("30.11 · 23:59", { exact: true }).waitFor();
    await page.getByText("01.12 · 00:01", { exact: true }).waitFor();
    await page.getByText("Подтверждено по документам", { exact: true }).waitFor();
    assert.equal(await page.getByRole("table", { name: "Запуски проверок" }).locator("tbody tr").count(), 5);
    const open = page.getByRole("button", { name: "Открыть запуск" });
    await open.click();
    const chooser = page.getByRole("dialog", { name: "Открыть запуск" });
    await chooser.getByRole("link", { name: /Компания B/ }).waitFor();
    await page.keyboard.press("Escape");
    assert.equal(await chooser.count(), 0);
    assert.equal(await open.evaluate(el => el === document.activeElement), true);
    await page.keyboard.press("Control+k");
    const search = page.getByRole("searchbox", { name: "Поиск запусков" });
    assert.equal(await search.evaluate(el => el === document.activeElement), true);
    await search.fill("компания b");
    await page.getByRole("button", { name: "С выводами", exact: true }).click();
    assert.equal(await page.locator("tbody tr").count(), 1);
    await page.locator(".run-filters summary").click();
    await page.getByLabel("Аудитор", { exact: true }).selectOption("Сверка платежей");
    await page.getByLabel("Период", { exact: true }).selectOption("Ноябрь 2026");
    await page.getByLabel("Ход проверки", { exact: true }).selectOption("completed");
    assert.equal(await page.locator("tbody tr").count(), 1);
    await search.fill("несуществующая проверка");
    await page.getByRole("heading", { name: "Ничего не найдено", exact: true }).waitFor();
    await page.locator(".workspace-empty").getByRole("button", { name: "Очистить фильтры" }).click();
    assert.equal(await page.locator("tbody tr").count(), 5);
    await page.locator(".run-filters summary").click();
    await cleanText(page);
  });

  await test("Open a real run, save, return and restore after reload", async () => {
    await page.getByRole("button", { name: "Открыть запуск" }).click();
    await page.getByRole("dialog").getByRole("link", { name: /Компания B/ }).click();
    await heading(page, "Компания B");
    await page.getByText("Независимо подтверждено", { exact: false }).first().waitFor();
    await page.getByRole("button", { name: "Сохранить проверку", exact: true }).click();
    await page.getByRole("navigation").getByRole("link", { name: "Сохранённое" }).click();
    await heading(page, "Сохранённое");
    await page.getByRole("table").getByRole("link", { name: /Компания B/ }).waitFor();
    await page.getByRole("navigation").getByRole("link", { name: "Рабочая область" }).click();
    await page.reload();
    await page.getByText("Продолжить проверку", { exact: true }).waitFor();
    await page.getByText("20\u00a0000,00\u00a0₸", { exact: true }).first().waitFor();
  });

  await test("Workspace fits desktop, tablet, mobile and effective 200% width", async () => {
    for (const [width, height] of [[1440, 960], [1280, 800], [390, 844], [320, 844]]) {
      await page.setViewportSize({ width, height });
      await page.evaluate(() => document.fonts.ready);
      await layout(page, width, `${width}x${height}`);
      await shot(page, `workspace-${width}`);
    }
    const toggler = page.getByRole("button", { name: "Открыть меню" });
    await toggler.click();
    const mobile = page.getByRole("dialog", { name: "Меню" });
    await mobile.getByRole("navigation").getByRole("link", { name: "Рабочая область" }).waitFor();
    await page.keyboard.press("Tab");
    assert.equal(await mobile.evaluate(element => element.contains(document.activeElement)), true);
    await page.keyboard.press("Escape");
    assert.equal(await toggler.evaluate(element => element === document.activeElement), true);
    const zoom = await browser.newContext({ viewport: { width: 720, height: 480 }, deviceScaleFactor: 2 });
    const zoomPage = await zoom.newPage();
    watch(zoomPage);
    await zoomPage.goto(url("/"));
    await zoomPage.getByRole("link", { name: "Открыть проверку", exact: true }).waitFor();
    await zoomPage.evaluate(() => document.fonts.ready);
    await layout(zoomPage, 720, "Effective CSS viewport at 200%");
    await zoom.close();
    await page.setViewportSize({ width: 1440, height: 960 });
  });

  await test("Live pair, exact document links, Back and the document library", async () => {
    await page.getByRole("link", { name: "Открыть сравнение", exact: true }).click();
    await heading(page, "Сравнение проверок");
    await page.getByText("20\u00a0000,00\u00a0₸", { exact: false }).first().waitFor();
    await cleanText(page);
    await page.goto(url(`/runs/${b.run_id}`));
    await heading(page, "Компания B");
    const doc = findingB.evidence.find(item => item.key.startsWith("payments/"));
    const evidence = page.locator(".evidence").getByRole("button", { name: /^Платёж/ }).first();
    await evidence.click();
    await page.waitForURL("**/record/**");
    assert.ok(decodeURIComponent(new URL(page.url()).hash).endsWith(doc.key));
    await cleanText(page);
    await page.goBack();
    await page.waitForURL(url(`/runs/${b.run_id}`));
    await heading(page, "Компания B");
    await page.getByRole("navigation").getByRole("link", { name: "Документы", exact: true }).click();
    await heading(page, "Документы проверки");
    await page.getByLabel("Раздел", { exact: true }).selectOption("invoices");
    await page.locator(".choice-list").getByRole("link", { name: /^Счёт/ }).first().click();
    await page.waitForURL("**/record/invoices/**");
    await cleanText(page);
  });

  await test("Recorded replay and live verification", async () => {
    await page.goto(url(`/runs/${s02.run_id}/replay`));
    await heading(page, "Повтор проверки");
    await page.getByRole("button", { name: "Вперёд", exact: true }).click();
    await page.getByRole("button", { name: "Выполнить повтор заново", exact: true }).click();
    await page.getByText(/наблюдения совпали/i).first().waitFor({ timeout: 30_000 });
    await cleanText(page);
  });

  await test("Change a real posting and rescore a copy without touching originals", async () => {
    const old = await account(page, `/demo/runs/${s01.run_id}`);
    await page.goto(url(`/runs/${s01.run_id}/record/ledger/R7`));
    await page.getByRole("button", { name: "Изменить дату и время" }).click();
    await page.getByRole("button", { name: "+2 минуты", exact: true }).click();
    await page.getByRole("button", { name: "Запустить проверку", exact: true }).click();
    await page.waitForURL(url(`/compare/${s01.run_id}/**`), { timeout: 60_000 });
    await heading(page, "Сравнение проверок");
    const child = new URL(page.url()).hash.split("/").at(-1);
    assert.notEqual(child, s01.run_id);
    const changed = await account(page, `/demo/runs/${child}`);
    assert.ok(changed.findings.some(item => item.finding.claimed_amount_minor === 2_000_000 && item.finding.currency === "KZT"));
    assert.deepEqual((await account(page, `/demo/runs/${s01.run_id}`)).score, old.score);
    await cleanText(page);
    await page.goto(url(`/runs/${s02.run_id}`));
    await heading(page, "Частичное распределение");
    await page.getByRole("button", { name: "Заменить доказательство в копии" }).first().click();
    await page.getByLabel("Доказательство для замены").selectOption("ledger/R5");
    await page.getByLabel("Новая запись").selectOption("ledger/R6");
    await page.getByRole("button", { name: "Пересчитать копию", exact: true }).click();
    await page.waitForURL(url(`/compare/${s02.run_id}/**`), { timeout: 60_000 });
    await heading(page, "Сравнение проверок");
    const copy = await account(page, `/demo/runs/${new URL(page.url()).hash.split("/").at(-1)}`);
    assert.deepEqual([copy.score.tp, copy.score.fp, copy.score.fn], [0, 1, 1]);
    assert.equal((await account(page, `/demo/runs/${s02.run_id}`)).score.tp, 1);
    await cleanText(page);
  });

  await test("Move the real generated settlement across the published boundary", async () => {
    const source = await account(page, `/demo/runs/${b.run_id}`);
    const target = findingB.derivation.context[0].key;
    await page.goto(url(`/runs/${b.run_id}/record/${target}`));
    await page.getByRole("button", { name: "Изменить дату и время" }).click();
    await page.getByRole("radio", { name: /^До границы периода/ }).check();
    await page.getByRole("button", { name: "Запустить проверку", exact: true }).click();
    await page.waitForURL(url(`/compare/${b.run_id}/**`), { timeout: 60_000 });
    await heading(page, "Сравнение проверок");
    const rerun = await account(page, `/demo/runs/${new URL(page.url()).hash.split("/").at(-1)}`);
    assert.equal(rerun.run.change.timing.to, "before");
    assert.ok(rerun.findings.every(item => item.finding.type !== "payment_allocation"));
    assert.ok(rerun.findings.some(item => item.finding.type === "duplicate_invoice"));
    assert.deepEqual((await account(page, `/demo/runs/${b.run_id}`)).score, source.score);
    await cleanText(page);
  });

  await test("Real missing run and document states", async () => {
    await page.goto(url(`/runs/${b.run_id}/record/payments/unavailable`));
    await page.getByText("Документ недоступен", { exact: true }).waitFor();
    await cleanText(page);
    await page.goto(url("/runs/absent-local-export"));
    await heading(page, "Проверка недоступна");
    await cleanText(page);
  });

  await test("Controlled API states show empty, offline and no verification without a false success", async () => {
    const fixture = await browser.newContext();
    try {
      const sample = await fixture.newPage();
      watch(sample);
      await sample.route("**/demo/runs", route => route.fulfill({ json: { runs: [] } }));
      await sample.goto(url("/"));
      await sample.getByRole("heading", { name: "Пока нет проверок", exact: true }).waitFor();
      await sample.unroute("**/demo/runs");
      await sample.route("**/demo/runs", route => route.abort());
      await sample.reload();
      await sample.getByRole("heading", { name: "Не удалось подключиться к сервису проверок", exact: true }).waitFor();
      await sample.unroute("**/demo/runs");
      const unknown = await account(page, `/demo/runs/${b.run_id}`);
      unknown.findings.forEach(item => { item.verifier = { connected: true }; item.derivation = null; });
      delete unknown.findings[1].verifier;
      await sample.route(`**/demo/runs/${b.run_id}`, route => route.fulfill({ json: unknown }));
      await sample.goto(url(`/runs/${b.run_id}`));
      await sample.getByText("Результат сверки недоступен", { exact: true }).first().waitFor();
      assert.equal(await sample.getByText("Независимо подтверждено", { exact: false }).count(), 0);
      await cleanText(sample);
    } finally { await fixture.close(); }
  });

  await test("Slow response, removed saved item, loading and long names are handled", async () => {
    const fixture = await browser.newContext({viewport: {width: 320, height: 844}});
    try {
      const sample = await fixture.newPage();
      watch(sample);
      const first = await account(page, `/demo/runs/${a.run_id}`);
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      let settle;
      const settled = new Promise(resolve => { settle = resolve; });
      await sample.route(`**/demo/runs/${a.run_id}`, async route => {
        await gate;
        try { await route.fulfill({json: first}); } catch { /* The request can be cancelled by navigation. */ }
        finally { settle(); }
      });
      await sample.goto(url(`/runs/${a.run_id}`));
      await sample.getByText("Загружаем проверку…", {exact: true}).waitFor();
      await sample.goto(url(`/runs/${b.run_id}`));
      await heading(sample, "Компания B");
      release();
      await settled;
      await heading(sample, "Компания B");
      const long = "Научно производственная компания по разработке и поставкам вычислительных систем для промышленного учёта и автоматизации";
      await sample.route("**/demo/runs", route => route.fulfill({ json: {runs: [
        {...a, company: long, findings: null},
        {...s02, status: "infrastructure_error", findings: 0, reward: null},
      ]} }));
      await sample.evaluate(id => {
        localStorage.setItem("auditlab:workspace:last:v1", JSON.stringify(id));
        localStorage.setItem("auditlab:workspace:saved:v1", JSON.stringify([id]));
      }, b.run_id);
      await sample.goto(url("/saved"));
      await sample.reload();
      await sample.getByRole("heading", {name: "Пока ничего не сохранено", exact: true}).waitFor();
      await sample.evaluate(() => { window.location.hash = "/"; });
      await sample.getByRole("heading", { name: long, exact: true }).waitFor();
      await sample.getByText("Результат недоступен", {exact: true}).first().waitFor();
      await sample.getByRole("table").getByText("Не удалось выполнить проверку", {exact: true}).waitFor();
      assert.equal(await sample.getByText("Продолжить проверку", {exact: true}).count(), 0);
      await sample.evaluate(() => document.fonts.ready);
      await layout(sample, 320, "320 px, long company and unavailable result");
      await cleanText(sample);
    } finally { await fixture.close(); }
  });

  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  assert.deepEqual(missing, []);
  assert.deepEqual(warnings, []);
  const report = { checks, screens, errors, external, missing, warnings };
  await writeFile(join(output, "browser-results.json"), JSON.stringify(report, null, 2) + "\n");
} catch (error) {
  await writeFile(join(output, "browser-failure.json"), JSON.stringify({ checks, screens, errors, external, missing, warnings, error: String(error) }, null, 2));
  throw error;
} finally {
  await browser.close();
}
