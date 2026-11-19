const SYMBOL: Record<string, string> = { KZT: "₸", USD: "$", EUR: "€" };

/** 2000000 KZT -> "20 000,00 ₸". Amounts are integers in minor units. */
export function money(minor: number | null | undefined, currency: string, exponents: Record<string, number>): string {
  if (minor === null || minor === undefined) return "—";
  const exp = exponents[currency];
  if (!Number.isInteger(exp) || exp < 0 || exp > 9) return `${minor} мин. ед. ${currency} (разрядность не указана)`;
  if (!Number.isSafeInteger(minor)) return `неточное число: см. исходную запись (${currency})`;
  const sign = minor < 0 ? "−" : "";
  const abs = Math.abs(minor);
  const div = 10 ** exp;
  const whole = Math.floor(abs / div).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  const frac = exp ? "," + (abs % div).toString().padStart(exp, "0") : "";
  return `${sign}${whole}${frac} ${SYMBOL[currency] ?? currency}`;
}

export function minor(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const digits = Math.abs(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");
  return (n < 0 ? "−" : "") + digits;  // same minus sign as money()
}

/** Russian explanation for trusted-lab operation errors; the original code and message stay visible. */
export const OP_ERRORS: Record<string, string> = {
  unsupported_counterfactual: "учебный адаптер не поддерживает такое изменение (например, перенос платежа в другой отчётный период или компания с ошибками вне распределения и FX)",
  invalid_value: "значение не распознано: нужна дата-время ISO 8601 со смещением, например 2026-11-15T00:01:00+05:00",
  out_of_range: "дата вне поддерживаемых синтетических лет",
  unsupported_field: "менять можно только дату (timestamp)",
  unknown_target: "запись нельзя изменить: нужна строка книги или документ платежа этого прогона",
  no_change: "новое значение совпадает с текущим",
  no_rate_for_date: "на новую местную дату нет опубликованного курса",
  truth_unavailable: "у доверенной стороны нет учебного эталона этого прогона: просмотр доступен, пересчёт и изменение — нет",
  unknown_run: "прогон не найден среди завершённых открытых экспортов",
  no_submission: "у исходной попытки нет сдачи",
  bad_finding: "нет вывода с таким номером",
  bad_evidence: "доказательство должно быть публичным ключом вида ledger/R6 и входить в выбранный вывод",
  bad_operation_key: "некорректный ключ операции",
  infrastructure_error: "сбой подключённого модуля; результат не засчитан и не подменён учебной заменой",
  timeout: "опыт не завершился за 60 с",
  offline: "сервер консоли недоступен",
};

export function opError(code: string | undefined, message: string | undefined): string {
  const text = (code && OP_ERRORS[code]) || "операция не выполнена";
  return `${text} [${code ?? "без кода"}${message ? `: ${message}` : ""}]`;
}

export function when(ts: string | undefined): string {
  if (!ts) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(ts);
  return m ? `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}` : ts;
}

export function reward(r: number | null | undefined): string {
  if (r === null || r === undefined) return "—";
  return r.toFixed(3).replace("-", "−");
}

export const REASONS: Record<string, string> = {
  matched: "засчитан",
  evidence_incomplete: "доказательства неполные: ни один допустимый набор не покрыт",
  amount_or_currency: "сумма или валюта не совпадает с эталоном",
  no_such_issue: "такой ошибки нет в эталоне",
  duplicate: "повтор уже засчитанного вывода",
  not_published: "причина не опубликована оценщиком (TP = 0)",
};

export const STATUS: Record<string, string> = {
  completed: "завершён",
  truncated: "усечён",
  aborted: "оборван",
  infrastructure_error: "сбой инфраструктуры",
  running: "выполняется",
};
