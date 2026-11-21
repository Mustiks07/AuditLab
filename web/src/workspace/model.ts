import type { CompareView, FindingView, RunMeta } from "../api";

export const NAV = [
  ["/", "Рабочая область", "workspace"], ["/runs", "Запуски", "play"],
  ["/comparisons", "Сравнения", "compare"], ["/documents", "Документы", "document"],
  ["/saved", "Сохранённое", "bookmark"],
] as const;

const FIXTURES: Record<string, { source: string; title: string; side?: "A" | "B" }> = {
  S01: { source: "Компания S01 · зачёт до границы", title: "Полное распределение", side: "A" },
  S02: { source: "Компания S02 · зачёт после границы", title: "Частичное распределение", side: "B" },
  FX01: { source: "Компания FX01 · платёж 100 USD", title: "Проверка валютной оценки" },
  E6476455CA5F6F7CF4A34F78E68AD54C0: { source: "Сгенерированная компания · последний зачёт 30.11 23:59", title: "Компания A" },
  E651B6A63DDA2448A91DC825714D7732E: { source: "Сгенерированная компания · последний зачёт 01.12 00:01", title: "Компания B" },
  E04F4ED6D6765D816DF94C62477B6EC4C: { source: "Сгенерированная чистая компания", title: "Проверка расчётов" },
};
function fixture(run: RunMeta) {
  if (!Object.prototype.hasOwnProperty.call(FIXTURES, run.episode_id)) return undefined;
  const entry = FIXTURES[run.episode_id];
  return run.label === "training_sample" && entry?.source === run.company ? entry : undefined;
}

export function companyName(run: RunMeta): string {
  const entry = fixture(run);
  if (entry) return entry.title;
  const name = run.company?.trim();
  const label = run.derived_from ? "Проверка доказательств" :
    run.parent_run_id ? "Проверка с изменённым условием" : "Компания";
  return name && name.length <= 200 && !/[\\/{}]|\b[0-9a-f]{8,}\b|\b(?:S\d{2}|FX\d{2}|reconcile|auditlab\.\w+)\b/i.test(name)
    ? name : label;
}

export function runNames(runs: RunMeta[]): Map<string, string> {
  const groups = new Map<string, RunMeta[]>();
  for (const run of runs) {
    const title = companyName(run);
    groups.set(title, [...(groups.get(title) ?? []), run]);
  }
  const names = new Map<string, string>();
  for (const [title, group] of groups) {
    group.sort((a, b) => a.run_id.localeCompare(b.run_id));
    group.forEach((run, index) => names.set(run.run_id, group.length === 1 ? title : `${title} · проверка ${index + 1}`));
  }
  return names;
}

const months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
export function periodName(run: RunMeta): string {
  const match = /^(\d{4})-(0[1-9]|1[0-2])-\d{2}(?:T|$)/.exec(run.period?.start ?? "");
  return match ? `${months[Number(match[2]) - 1]} ${match[1]}` : "Период не указан";
}

const POLICIES: Record<string, string> = {
  reconcile: "Сверка платежей", random: "Случайная выборка", flag_all: "Сплошная проверка",
  oracle: "Контрольный ответ",
};
function label(dictionary: Record<string, string>, key: string, fallback: string) {
  const name = dictionary[key];
  return typeof name === "string" ? name : fallback;
}
export const auditorName = (policy: string) => label(POLICIES, policy, "Пользовательский аудитор");
export const FINDING_NAMES: Record<string, string> = {
  payment_allocation: "Нераспределённый платёж", duplicate_invoice: "Дублирующий счёт",
  fx_conversion: "Валютная оценка", period_cutoff: "Граница периода",
};
export const findingName = (type: string) => label(FINDING_NAMES, type, "Вывод проверки");

export const RUN_STATUS: Record<string, string> = {
  completed: "Проверка завершена", truncated: "Проверка не завершена",
  aborted: "Проверка прервана", policy_failure: "Проверка прервана",
  infrastructure_error: "Не удалось выполнить проверку", failed: "Не удалось выполнить проверку",
  running: "Проверка выполняется", pending: "Проверка ожидает запуска",
};
export const runStatus = (status: string) => label(RUN_STATUS, status, "Состояние недоступно");

export function runResult(run: RunMeta) {
  if (run.status !== "completed") return { text: runStatus(run.status), tone: "unknown" };
  if (!Number.isInteger(run.findings) || run.findings! < 0) return { text: "Результат недоступен", tone: "unknown" };
  return run.findings === 0 ? { text: "Без выводов", tone: "clear" } :
    { text: run.findings === 1 ? "Есть вывод" : `Выводы: ${run.findings}`, tone: "attention" };
}

export function documentConfirmed(view: FindingView): boolean {
  const calc = view.derivation;
  if (!Array.isArray(view.evidence) || !Array.isArray(view.uncited_operands)) return false;
  const cited = new Set(view.evidence.filter(row => row.exists === true).map(row => row.key));
  return view.console_agrees === true && calc != null && Number.isSafeInteger(calc.result_minor)
    && calc.currency === view.finding.currency && calc.result_minor === view.finding.claimed_amount_minor
    && view.evidence.length > 0 && view.evidence.every(row => row.exists === true)
    && view.uncited_operands.length === 0 && calc.operands.length > 0
    && calc.operands.every(row => cited.has(row.key))
    && calc.evidence.every(key => cited.has(key))
    && (calc.index_check == null || calc.index_check.complete === true);
}

export function independentConfirmed(view: FindingView): boolean {
  const result = view.verifier;
  return result?.connected === true && result.agrees === true && result.currency_matches === true
    && Number.isSafeInteger(result.amount_minor) && result.amount_minor === view.finding.claimed_amount_minor
    && Boolean(result.currency) && result.currency === view.finding.currency;
}

export interface Pair { left: RunMeta; right: RunMeta; directed: boolean }
export function realPairs(runs: RunMeta[]): Pair[] {
  const pairs: Pair[] = [];
  const completed = runs.filter(run => run.status === "completed");
  const byId = new Map(completed.map(run => [run.run_id, run]));
  const groups = new Map<string, RunMeta[]>();
  const added = new Set<string>();
  function add(left: RunMeta, right: RunMeta, directed: boolean) {
    const key = JSON.stringify([left.run_id, right.run_id].sort());
    if (left.run_id !== right.run_id && !added.has(key)) {
      added.add(key);
      pairs.push({ left, right, directed });
    }
  }
  for (const run of completed) if (run.pair_id && !run.parent_run_id && !run.derived_from) {
    groups.set(run.pair_id, [...(groups.get(run.pair_id) ?? []), run]);
  }
  const role = (run: RunMeta) => run.pair_role ?? fixture(run)?.side;
  for (const group of groups.values()) {
    const left = group.find(run => role(run) === "A");
    const right = group.find(run => role(run) === "B");
    if (left && right) add(left, right, true);
    else if (group.length === 2) add(group[0], group[1], false);
  }
  for (const run of completed) {
    const parent = byId.get(run.parent_run_id ?? run.derived_from?.run_id ?? "");
    if (parent) add(parent, run, true);
  }
  return pairs;
}

export function newestFirst(runs: RunMeta[]): RunMeta[] {
  const time = (run: RunMeta) => Date.parse(run.created_at ?? "") || 0;
  const priority = (run: RunMeta) => run.pair_role === "B" ? 2 : run.pair_role === "A" ? 1 : 0;
  return [...runs].sort((a, b) => time(b) - time(a) || priority(b) - priority(a) || b.run_id.localeCompare(a.run_id));
}

export function shortInstant(value: unknown): string | null {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return null;
  const parts = /^\d{4}-(\d{2})-(\d{2})T(\d{2}:\d{2}):\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
  return parts ? `${parts[2]}.${parts[1]} · ${parts[3]}` : null;
}

export function timeline(pair: Pair, details: CompareView | undefined) {
  if (!pair.directed || !details) return null;
  for (const item of details.calculations) {
    const left = item.left.rule.boundary;
    const right = item.right.rule.boundary;
    if (item.type !== "payment_allocation" || typeof left !== "string" || left !== right
        || item.left.rule.boundary_exclusive !== true || item.right.rule.boundary_exclusive !== true) continue;
    const keys = new Set([...item.left.operands, ...(item.left.context ?? []),
                          ...item.right.operands, ...(item.right.context ?? [])].map(row => row.key));
    for (const row of details.changes) {
      if (row.field !== "timestamp" || !keys.has(row.key)) continue;
      const a = shortInstant(row.before), b = shortInstant(row.after), boundary = shortInstant(left);
      const cutoff = Date.parse(left);
      if (a && b && boundary && typeof row.before === "string" && typeof row.after === "string"
          && (Date.parse(row.before) < cutoff) !== (Date.parse(row.after) < cutoff)) return { a, b, boundary };
    }
  }
  return null;
}
