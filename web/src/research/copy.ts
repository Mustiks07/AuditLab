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
