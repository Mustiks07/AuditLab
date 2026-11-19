const SYMBOL: Record<string, string> = { KZT: "₸", USD: "$", EUR: "€" };

/** 2000000 KZT -> "20 000,00 ₸". Amounts are integers in minor units. */
export function money(minor: number | null | undefined, currency = "KZT", exp = 2): string {
  if (minor === null || minor === undefined) return "—";
  const sign = minor < 0 ? "−" : "";
  const abs = Math.abs(minor);
  const div = 10 ** exp;
  const whole = Math.floor(abs / div).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  const frac = exp ? "," + (abs % div).toString().padStart(exp, "0") : "";
  return `${sign}${whole}${frac} ${SYMBOL[currency] ?? currency}`;
}

export function minor(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
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
