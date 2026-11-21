import type { Operand, Step } from "../api";
import { when } from "../format";

const TABLES: Record<string, [string, string]> = {
  invoices: ["Счета", "Счёт"], payments: ["Платежи", "Платёж"],
  ledger: ["Проводки", "Проводка"], policy: ["Правила", "Правило"],
  fx: ["Валютные курсы", "Валютный курс"], invoice: ["Счета", "Счёт"],
  payment: ["Платежи", "Платёж"],
};
const RULES: Record<string, string> = {
  allocation: "Правило зачёта платежей", period: "Правило отчётного периода",
  fx_date: "Правило выбора курса", rounding: "Правило округления",
  accounts: "Назначение счетов учёта", rule_versions: "Версии правил",
};
const FIELDS: Record<string, string> = {
  timestamp: "Дата и время", issued_at: "Дата выставления", due_at: "Срок оплаты",
  amount_minor: "Сумма", total_minor: "Итого", currency: "Валюта",
  debit_minor: "Дебет", credit_minor: "Кредит", account: "Счёт учёта",
  rate: "Курс", date: "Дата курса", source_currency: "Исходная валюта", target_currency: "Валюта оценки",
  line_items: "Позиции", description: "Описание", invoice_refs: "Связанные счета",
  payment_id: "Связанный платёж", invoice_id: "Связанный счёт", settlement_index: "Движения до границы",
  boundary: "Граница периода", before: "До даты", version: "Версия правила",
};
const ACCOUNTS: Record<string, string> = {
  AR: "Дебиторская задолженность", CASH: "Денежные средства", REVENUE: "Выручка",
  UNAPPLIED: "Нераспределённые поступления", FX_VALUATION: "Валютная оценка",
  FX_OFFSET: "Балансирующая запись оценки", CASH_RESERVE: "Денежный резерв",
};

export function named<T>(dictionary: Record<string, T>, key: string): T | undefined {
  return Object.prototype.hasOwnProperty.call(dictionary, key) ? dictionary[key] : undefined;
}
export const tableName = (table: string) => named(TABLES, table)?.[0] ?? "Документы";
export const fieldName = (key: string) => named(FIELDS, key) ?? "Данные записи";
export function recordName(reference: string): string {
  const [table, key] = reference.split("/");
  return (table === "policy" && named(RULES, key)) || named(TABLES, table)?.[1] || "Документ";
}

export function referenceLabels(keys: string[]): Record<string, string> {
  const counts = new Map<string, number>();
  const keysToUse = [...new Set(keys)].sort();
  const totals = new Map<string, number>();
  for (const key of keysToUse) {
    const title = recordName(key);
    totals.set(title, (totals.get(title) ?? 0) + 1);
  }
  return Object.fromEntries(keysToUse.map(key => {
    const title = recordName(key);
    const count = (counts.get(title) ?? 0) + 1;
    counts.set(title, count);
    return [key, totals.get(title) === 1 ? title : `${title} · запись ${count}`];
  }));
}

export function humanText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!text || text.length > 1000 || /[{}\\]|\b[0-9a-f]{8,}\b|\b(?:invoices|payments|ledger|policy|public|private|demo|fx)\/|\b\w+_\w+\b|\bauditlab\.|\b(?:run_id|episode_id|reconcile|S\d{2}|FX\d{2})\b/i.test(text)) return null;
  return text;
}
export function humanFact(value: unknown, field?: string): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (typeof value === "number" && Number.isFinite(value)) return value.toLocaleString("ru-RU", { maximumFractionDigits: 12 });
  if (typeof value !== "string") return Array.isArray(value) ? `Записей: ${value.length}` : "Состав записи изменён";
  if (field === "account") return named(ACCOUNTS, value) ?? "Счёт учёта";
  if (value === "Commercial services") return "Коммерческие услуги";
  const offset = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (offset && Number.isFinite(Date.parse(value))) return `${when(value)} (UTC${offset[1] === "Z" ? "" : offset[1]})`;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value.split("-").reverse().join(".");
  return humanText(value) ?? "Значение доступно в исходной записи";
}

export function operandName(operand: Operand, operation: string, context = false): string {
  if (context) return "Зачёт после границы периода";
  if (operation === "payment_allocation") return operand.sign < 0 ? "Зачёт платежа" : "Поступление платежа";
  if (operation === "fx_conversion") {
    if (operand.field === "rate") return "Курс на дату операции";
    return operand.sign < 0 ? "Оценка в учёте" : "Исходная сумма";
  }
  return fieldName(operand.field);
}

export function stepName(step: Step): string {
  const table = step.action.args.table;
  switch (step.action.action) {
    case "list_records": return `Просмотр: ${tableName(typeof table === "string" ? table : "").toLocaleLowerCase("ru")}`;
    case "get_record": case "get_document": return step.opened_key ? `Открыт документ: ${recordName(step.opened_key).toLocaleLowerCase("ru")}` : "Открытие документа";
    case "calculator": return "Расчёт суммы";
    case "submit": return "Сданы выводы";
    default: return "Действие аудитора";
  }
}

const ERRORS: Record<string, string> = {
  offline: "Не удалось подключиться к сервису проверок.",
  invalid_value: "Укажите дату, время и смещение относительно UTC.",
  out_of_range: "Указанная дата выходит за поддерживаемый диапазон.",
  unsupported_field: "Для этой записи можно изменить только дату и время.",
  unsupported_counterfactual: "Это изменение не поддерживается для выбранной проверки.",
  generator_unsupported: "В этой проверке можно перенести только последний зачёт относительно границы периода.",
  generator_mismatch: "Не удалось воспроизвести исходную компанию. Новая проверка не создана.",
  unknown_target: "Эту запись нельзя изменить.",
  no_change: "Новое значение совпадает с текущим.",
  no_rate_for_date: "На выбранную дату нет опубликованного курса.",
  truth_unavailable: "Для этой проверки доступны документы. Изменение условий и пересчёт недоступны.",
  unknown_run: "Проверка недоступна среди открытых запусков.",
  no_submission: "В исходной проверке нет сданных выводов.",
  bad_finding: "Вывод недоступен.",
  bad_evidence: "Выберите действующую ссылку и другую запись для замены.",
  infrastructure_error: "Не удалось выполнить проверку.",
  timeout: "Проверка не завершилась за отведённое время.",
};
export function friendlyError(code?: string): string {
  return (code && named(ERRORS, code)) || "Действие не завершилось. Попробуйте снова или откройте другую проверку.";
}
