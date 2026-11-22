// Product language of the research screens. Raw codes and IDs stay in details and exports only.
import { named } from "../workspace/copy";

const pick = (dictionary: Record<string, string>, key: string | null | undefined, fallback: string) =>
  (key && named(dictionary, key)) || fallback;

export const EXPERIMENT_STATUS: Record<string, string> = {
  planned: "Запланирован", running: "Выполняется", completed: "Завершён", stopped: "Остановлен",
  failed: "Не удалось выполнить", interrupted: "Прерван",
};
export const experimentStatus = (status?: string) => pick(EXPERIMENT_STATUS, status, "Состояние недоступно");

export const ATTEMPT_STATUS: Record<string, string> = {
  completed: "Завершена", timeout: "Превышено время", provider_error: "Ошибка сервиса модели",
  invalid_response: "Некорректный ответ модели", budget_stop: "Остановлена по бюджету", running: "Выполняется",
};
export const attemptStatus = (status?: string) => pick(ATTEMPT_STATUS, status, "Состояние недоступно");

export const FAILURE_KINDS: Record<string, string> = {
  timeout: "превышено время", provider_error: "ошибка сервиса модели",
  invalid_response: "некорректный ответ", budget_stop: "остановлено по бюджету",
};

export const TRAINING_KIND: Record<string, string> = {
  weights: "Обучение весов", prompt: "Изменение инструкции", few_shot: "Примеры в запросе (few-shot)",
};
export const TRAINING_KIND_NOTE: Record<string, string> = {
  weights: "Новая версия модели после обучения на учебном наборе.",
  prompt: "Модель прежняя; изменён текст инструкции агента.",
  few_shot: "Модель прежняя; в запрос добавлены разобранные примеры.",
};
export const trainingKind = (kind?: string) => pick(TRAINING_KIND, kind, "Вид изменения не указан");

export const TRAINING_STATE: Record<string, string> = {
  not_available: "Нет доступа к обучению", queued: "В очереди", running: "Выполняется",
  failed: "Сбой обучения", interrupted: "Прервано", completed: "Готово", no_result: "Результата нет",
};
export const trainingState = (state?: string) => pick(TRAINING_STATE, state, "Состояние недоступно");

export const RELATIONS: Record<string, string> = {
  clean_analog: "Чистый аналог", defect_fixed: "Исправленный дефект", renamed: "Переименованные документы",
  reordered: "Изменённый порядок", distractor: "Законная отвлекающая операция",
};
export const relationName = (relation?: string) => pick(RELATIONS, relation, "Вариант семейства");

export const ORIGINS: Record<string, string> = {
  reduction: "Сокращённый случай", family_transform: "Вариант семейства", clean_control: "Чистый контроль",
  failure: "Найденная слабость", search: "Поиск генератора",
};
export const originName = (origin?: string) => pick(ORIGINS, origin, "Источник не указан");

export const SPLITS: Record<string, string> = {
  train: "Обучение", dev: "Отладка", holdout: "Новые компании (контроль)", search: "Поиск",
};
export const splitName = (split?: string) => pick(SPLITS, split, "Раздел не указан");

export const CHECK_STATUS: Record<string, string> = {
  passed: "Пройдена", failed: "Не пройдена", not_checked: "Не проверялась",
};
export const checkStatus = (status?: string) => pick(CHECK_STATUS, status, "Не проверялась");

export const METRICS: Record<string, string> = {
  micro_f1: "micro F1", mean_reward: "Средняя награда", clean_fp_rate: "Доля FP на чистых",
};
export const metricName = (metric: string) => pick(METRICS, metric, "Показатель");
/** Direction of a published metric, used only to word an interval that excludes zero. */
export const HIGHER_IS_BETTER: Record<string, boolean> = { micro_f1: true, mean_reward: true, clean_fp_rate: false };

const ERRORS: Record<string, string> = {
  offline: "Не удалось подключиться к сервису исследований.",
  timeout: "Сервис исследований не ответил вовремя.",
  not_found: "Этот результат недоступен: артефакт не найден или удалён.",
  bad_response: "Ответ сервиса не удалось прочитать.",
  controller_not_connected: "Запуск недоступен: контроллер экспериментов не подключён к этой среде.",
  controller_error: "Контроллер экспериментов сообщил об ошибке. Попробуйте позже.",
  budget_not_configured: "Запуск недоступен: на сервере не задан бюджет эксперимента.",
  api_key_missing: "Запуск недоступен: на сервере не настроен доступ к модели.",
  model_unavailable: "Выбранная модель сейчас недоступна у провайдера.",
  budget_exceeded: "Достигнут лимит расходов эксперимента.",
  incompatible_artifact: "Формат результата не поддерживается этой версией консоли.",
  training_unavailable: "Обучение весов недоступно для выбранной модели.",
  job_failed: "Задание обучения завершилось ошибкой.",
  interrupted: "Задание прервано до получения результата.",
};
export function researchError(code?: string): string {
  if (code && /^http_5\d\d$/.test(code)) return "Сервис исследований вернул ошибку. Попробуйте позже.";
  return pick(ERRORS, code, "Действие не завершилось. Попробуйте снова.");
}

export const FINDING_TYPE_FALLBACK = "Вывод проверки";

export function count(value: number | null | undefined): string {
  return typeof value === "number" && Number.isInteger(value) ? value.toLocaleString("ru-RU") : "—";
}
export function share(part: number | null | undefined, total: number | null | undefined): string {
  if (!Number.isInteger(part) || !Number.isInteger(total)) return "нет данных";
  return `${count(part)} из ${count(total)}`;
}
export function decimal(value: number | null | undefined, digits = 2): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits }).replace("-", "−");
}
export function signed(value: number | null | undefined, digits = 2): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const text = decimal(Math.abs(value), digits);
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}
export function usd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${value.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} $`;
}
export function duration(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${decimal(seconds, 1)} с`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин ${Math.round(seconds % 60)} с`;
  return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}

// ------------------------------------------------------------------ financial laboratory
export const TRANSFORMATION: Record<string, string> = {
  id_order: "Переименование документов", lawful_refund: "Законная отвлекающая операция",
  fixed: "Исправление нарушения", currency: "Изменение суммы и валюты", boundary: "Граница периода",
  amount: "Изменение суммы", interaction: "Исправление одной из двух ошибок",
};
export const TRANSFORMATION_NOTE: Record<string, string> = {
  id_order: "Новые номера документов и другой порядок строк. Деньги не меняются: правильный ответ прежний.",
  lawful_refund: "Добавлен настоящий независимый возврат депозита. Он законен и не должен попасть в выводы.",
  fixed: "Нарушение исправлено согласованным изменением документов. Соответствующий вывод должен исчезнуть.",
  amount: "Изменена денежная величина. Правильный ответ пересчитан по связанным документам.",
  interaction: "Исправлено одно нарушение. Независимая проверка подтверждает, какая ошибка осталась.",
  currency: "Договор в иенах (без копеек). Тот же остаток 20 000 единиц меняет валюту и сумму в минимальных единицах.",
  boundary: "Существенное событие перенесено через границу периода. Сравните правильные ответы по документам.",
};
export const transformationName = (kind?: string | null) => pick(TRANSFORMATION, kind, "Исходная компания");

export const OUTCOME: Record<string, string> = {
  correct: "Верно", miss: "Пропуск нарушения", false_accusation: "Ложное обвинение", wrong_amount: "Неверная сумма",
  wrong_evidence: "Неверные доказательства", unverified: "Причина не установлена",
  technical: "Технический сбой", not_evaluated: "Без оценки",
};
export const OUTCOME_NOTE: Record<string, string> = {
  correct: "Оценщик засчитал ответ: лишних и пропущенных выводов нет.",
  miss: "Нарушение есть в правильном выводе, а в ответе агента его нет.",
  false_accusation: "Агент заявил нарушение там, где правильный вывод его не содержит.",
  wrong_amount: "Нарушение названо верно, но сумма или валюта не совпадает с правильным выводом.",
  wrong_evidence: "Тип и сумма совпадают с правильным выводом, но оценщик не принял вывод: ссылки не подтверждают нарушение.",
  unverified: "Сохранённых данных недостаточно, чтобы установить причину оценки конкретного вывода.",
  technical: "Попытка не завершилась: финансовая оценка не выставляется.",
  not_evaluated: "Оценка недоступна или скрыта.",
};
export const outcomeName = (kind?: string) => pick(OUTCOME, kind, "Без оценки");
export const OUTCOME_TONE: Record<string, string> = {
  correct: "pass", miss: "fail", false_accusation: "fail", wrong_amount: "fail", wrong_evidence: "mixed",
  unverified: "mixed", technical: "unknown", not_evaluated: "unknown",
};

// Provider-specific reasons; attempt statuses keep their established wording (ATTEMPT_STATUS).
export const TECH_ERRORS: Record<string, string> = {
  rate_limit: "Провайдер отклонил запрос: превышен лимит запросов",
};
export function techError(error?: { code?: string; message?: string } | null, status?: string): string {
  const base = pick(TECH_ERRORS, error?.code, attemptStatus(status ?? error?.code));
  return error?.message && /^HTTP \d{3}$/.test(error.message) ? `${base} (${error.message})` : base;
}

export const DIMENSION: Record<string, string> = {
  violation: "Тип нарушения", interaction: "Взаимодействие", clean: "Чистые контрпримеры", currency: "Валюты",
  boundary: "Граница периода", difficulty: "Сложность случая", transformation: "Преобразование",
};
const FACET_VALUES: Record<string, Record<string, string>> = {
  violation: { clean: "Чистая книга (нарушений нет)", unknown: "Состав не опубликован" },
  interaction: { none: "Без взаимодействия", allocation_fx_same_payment: "Остаток и курс в одном платеже",
    cutoff_fx_same_invoice: "Граница и курс в одном счёте", unknown: "Состав не опубликован" },
  clean: { clean: "Чистые контрпримеры", defect: "С нарушением", unknown: "Состав не опубликован" },
  currency: { unknown: "Не указано" },
  boundary: { before: "Зачёт до границы", at: "Зачёт ровно на границе", after: "Зачёт после границы",
    none: "Без пограничного зачёта", unknown: "Состав не опубликован" },
  difficulty: { clean: "Чистая книга", single: "Одно нарушение", multiple: "Несколько нарушений",
    interaction: "Взаимодействие нарушений", unknown: "Состав не опубликован" },
  transformation: { original: "Исходная компания", unknown: "Вне семейств" },
};
export function facetName(dimension: string, value: string): string {
  if (dimension === "violation" && FACET_VALUES.violation[value] === undefined) return named({ payment_allocation: "Нераспределённый платёж",
    duplicate_invoice: "Дублирующий счёт", fx_conversion: "Валютная оценка", period_cutoff: "Граница периода" }, value) ?? "Другой тип";
  if (dimension === "transformation" && FACET_VALUES.transformation[value] === undefined) return transformationName(value);
  if (dimension === "currency" && value !== "unknown") return value.split("+").join(" + ");
  return FACET_VALUES[dimension]?.[value] ?? "Другое значение";
}
export const CHANGE: Record<string, string> = {
  regression: "Регрессия", improvement: "Улучшение", unchanged_correct: "Верно до и после",
  unchanged_wrong: "Ошибка до и после", not_comparable: "Нельзя сравнить",
};
