import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api";
import { money } from "../format";
import { fieldName, humanFact, operandName, recordName, referenceLabels, tableName } from "../workspace/copy";
import { Icon } from "../workspace/Icon";
import { findingName } from "../workspace/model";
import { useResource } from "../workspace/resources";
import { research, type AttemptSummary, type Claim, type Evaluation, type Provenance, type Situation, type StageLinks, type StepView } from "./api";
import { researchError } from "./copy";

export const rlink = (...parts: string[]) => `#/research/${parts.map(encodeURIComponent).join("/")}`;
export const docLink = (base: string, key: string) => `${base}/doc/${key.split("/").map(encodeURIComponent).join("/")}`;

/** Loader with retry; keeps the previous value while refreshing and reports a failed refresh separately. */
export function useResearch<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>) {
  const [revision, setRevision] = useState(0);
  const [last, setLast] = useState<{ key: string; value: T; at: Date } | null>(null);
  const reply = useResource(key === null ? null : `${key}#${revision}`, load, 0, true);
  const fresh = reply?.value;
  useEffect(() => {
    if (key !== null && fresh !== undefined) setLast({ key, value: fresh, at: new Date() });
  }, [key, fresh]);
  const stale = reply?.error !== undefined && last?.key === key ? last : null;
  return {
    value: reply?.value ?? stale?.value, error: reply?.error, loading: reply === undefined && !stale,
    staleAt: stale?.at ?? null, retry: () => setRevision(value => value + 1),
  };
}

export function Origin({ provenance, compact = false }: { provenance?: Provenance; compact?: boolean }) {
  const kind = provenance?.kind ?? "unknown";
  const text = kind === "dev_fixture" ? "Фикстура разработки · не измерение" : kind === "measured" ? "Измерение"
    : kind === "test_double" ? "Тестовая замена · не измерение" : "Происхождение не указано";
  return <span className={`origin origin-${kind}`} title={compact ? text : undefined}>
    <Icon name={kind === "measured" ? "check" : "help"}/>{text}
  </span>;
}

export function FixtureNote({ provenance }: { provenance?: Provenance }) {
  if (provenance?.kind === "measured") return null;
  return <p className="fixture-note" role="note">
    {provenance?.kind === "dev_fixture"
      ? "Показаны данные фикстуры разработки: компании и документы настоящие, действия задал скрипт, оценка выполнена настоящим оценщиком. Это не ответы модели и не результаты эксперимента. Подробнее — «О данных»."
      : "Происхождение данных не указано в артефакте. Не используйте их как результат эксперимента."}
  </p>;
}

const STAGES = [
  ["experiment", "Эксперимент"], ["failure", "Слабость"], ["reduction", "Сокращённый случай"],
  ["curriculum", "Учебный набор"], ["comparison", "Сравнение версий"],
] as const;
export type Stage = typeof STAGES[number][0];

export function ResearchPath({ current, links }: { current: Stage; links: Partial<Record<Stage, string>> }) {
  return <nav className="research-path" aria-label="Путь исследования">
    <ol>{STAGES.map(([stage, label], index) => {
      const href = links[stage];
      const body = <><span className="path-index" aria-hidden="true">{index + 1}</span><span>{label}</span></>;
      return <li key={stage} className={stage === current ? "current" : href ? "" : "unavailable"}>
        {stage === current ? <span aria-current="step">{body}</span>
          : href ? <a href={href}>{body}</a>
          : <span aria-disabled="true" title="Нет связанного результата">{body}</span>}
      </li>;
    })}</ol>
  </nav>;
}

const ROUTES: Record<Stage, string> = {
  experiment: "experiments", failure: "failures", reduction: "reductions", curriculum: "curricula", comparison: "comparisons",
};
export function stageLinks(links?: StageLinks | null): Partial<Record<Stage, string>> {
  const out: Partial<Record<Stage, string>> = {};
  if (links) for (const stage of Object.keys(ROUTES) as Stage[]) {
    const target = links[stage];
    if (target) out[stage] = rlink(ROUTES[stage], target);
  }
  return out;
}

export function NextStep({ href, label, missing }: { href?: string; label: string; missing?: string }) {
  return <div className="next-step">
    {href ? <a className="primary-action" href={href}>{label}<Icon name="arrow"/></a>
      : <p className="muted">{missing ?? "Следующий шаг пока недоступен."}</p>}
  </div>;
}

export function Loading({ label }: { label: string }) {
  return <div aria-busy="true" role="status" aria-label={label} className="research-loading">
    <div className="skeleton skeleton-title"/><div className="feature-grid skeleton-grid"><div className="skeleton"/><div className="skeleton"/></div>
  </div>;
}

export function Failed({ error, retry, back }: { error: unknown; retry?: () => void; back?: ReactNode }) {
  const code = error instanceof ApiError ? error.code : undefined;
  return <div className="workspace-empty" role="alert">
    <h2>{code === "not_found" ? "Результат недоступен" : "Не удалось загрузить данные"}</h2>
    <p>{researchError(code)}</p>
    <div className="empty-actions">
      {retry && code !== "not_found" && <button className="primary-action" type="button" onClick={retry}>Повторить</button>}
      {back}
    </div>
  </div>;
}

export function Stale({ at, retry }: { at: Date | null; retry: () => void }) {
  if (!at) return null;
  return <p className="stale-note" role="status">
    Не удалось обновить данные. Показан ответ от {at.toLocaleTimeString("ru-RU")}.
    <button className="text-link" type="button" onClick={retry}>Повторить</button>
  </p>;
}

export function claimText(claim: Claim, exponents: Record<string, number>): string {
  return `${findingName(claim.type)} · ${money(claim.claimed_amount_minor, claim.currency, exponents)}`;
}

export function Verdict({ attempt }: { attempt: Pick<AttemptSummary, "evaluation" | "evaluation_hidden" | "status"> }) {
  if (attempt.status && attempt.status !== "completed") return <span className="verdict verdict-unknown">Оценки нет: попытка не завершена</span>;
  if (attempt.evaluation_hidden) return <span className="verdict verdict-unknown">Оценка скрыта до окончания контроля</span>;
  const e: Evaluation | null = attempt.evaluation;
  if (!e) return <span className="verdict verdict-unknown">Оценка недоступна</span>;
  if (e.tp > 0 && e.fp === 0) return <span className="verdict verdict-pass"><Icon name="check"/>Вывод засчитан</span>;
  if (e.tp === 0 && e.fp > 0) return <span className="verdict verdict-fail">Вывод не засчитан</span>;
  if (e.tp === 0 && e.fp === 0) return <span className="verdict verdict-unknown">Выводов не сдано</span>;
  return <span className="verdict verdict-mixed">Засчитано частично</span>;
}

export function ScoreLine({ evaluation }: { evaluation: Evaluation | null }) {
  if (!evaluation) return null;
  return <span className="score-line">TP {evaluation.tp} · FP {evaluation.fp} · FN {evaluation.fn} · награда {
    evaluation.reward.toLocaleString("ru-RU", { maximumFractionDigits: 3 }).replace("-", "−")}</span>;
}

export function stepText(step: StepView, labels: Record<string, string>, opened?: string): string {
  const table = step.table ? tableName(step.table).toLocaleLowerCase("ru") : "записи";
  switch (step.action) {
    case "list_records": {
      const page = step.cursor ? "следующая страница" : "первая страница";
      const size = typeof step.records === "number" ? `, записей: ${step.records}` : "";
      return `Просмотр: ${table} (${page}${size}${step.more ? ", есть продолжение" : ""})`;
    }
    case "get_document": return opened ? `Открыт документ: ${opened}` : "Открыт документ";
    case "get_record": return opened ? `Открыта запись: ${opened}` : `Открыта запись: ${table}`;
    case "calculator": return "Расчёт на калькуляторе";
    case "submit": return `Сданы выводы: ${step.findings ?? 0}`;
    default: return labels[step.action] ?? "Действие агента";
  }
}

export function ActionList({ steps, labels = {} }: {
  steps: StepView[]; received?: { key: string; step: number }[]; labels?: Record<string, string>;
}) {
  if (!steps.length) return <p className="muted">Действий нет.</p>;
  return <ol className="action-list">{steps.map(step => {
    const key = step.opened_key;
    return <li key={step.step} className={step.ok === false ? "action-error" : ""}>
      <span className="action-number">{step.step}</span>
      <span>{stepText(step, {}, key && (labels[key] ?? recordName(key)).toLocaleLowerCase("ru"))}{step.ok === false && <b> · отказ среды</b>}</span>
      {typeof step.remaining_steps === "number" && <small>осталось действий: {step.remaining_steps}</small>}
    </li>;
  })}</ol>;
}

export function DocButton({ label, href, current }: { label: string; href: string; current?: boolean }) {
  return <a className={`doc-chip${current ? " doc-chip-current" : ""}`} href={href} aria-current={current ? "true" : undefined}>
    <Icon name="document"/>{label}<span className="sr-only"> — открыть документ</span>
  </a>;
}

/** Human names: the role of a record in the independent calculation first, then numbered table names. */
export function labelsFor(keys: string[], situation?: Situation | null): Record<string, string> {
  const labels: Record<string, string> = { ...referenceLabels(keys) };
  for (const calc of situation?.calculations ?? []) {
    for (const operand of calc.operands) labels[operand.key] = operandName(operand as never, calc.type);
    for (const row of calc.summary?.after_boundary ?? []) labels[row.key] = "Зачёт после границы периода";
    labels[calc.rule.key] = recordName(calc.rule.key);
    labels[calc.subject] = calc.type === "payment_allocation" ? "Платёжное поручение" : recordName(calc.subject);
  }
  return labels;
}

const MONEY = new Set(["debit_minor", "credit_minor", "amount_minor", "total_minor"]);
// Identifiers and nested structures stay in the source record (details), not in the main field list.
const HIDDEN_FIELDS = new Set(["row_id", "doc_id", "journal_id", "entity_id", "payment_id", "invoice_id", "canonical_key",
  "settlement_index", "invoice_refs", "line_items"]);

export function DocumentPanel({ snapshotId, docKey, label, closeHref }: {
  snapshotId: string | null; docKey: string; label?: string; closeHref: string;
}) {
  const record = useResource(snapshotId ? JSON.stringify([snapshotId, docKey]) : null,
    signal => research.record(snapshotId!, docKey, signal));
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    panel.current?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
    panel.current?.focus({ preventScroll: true });
  }, [docKey]);
  const title = label ?? recordName(docKey);
  return <section className="research-document" aria-labelledby="research-document-title" ref={panel} tabIndex={-1}>
    <div className="document-head">
      <h3 id="research-document-title"><Icon name="document"/>{title}</h3>
      <a className="text-link" href={closeHref}>Закрыть</a>
    </div>
    {!snapshotId ? <p className="muted">Документ недоступен: снимок компании не опубликован.</p>
      : !record ? <div className="skeleton skeleton-list" role="status" aria-label="Загружаем документ"/>
      : record.error ? <p className="muted" role="alert">Документ недоступен. {researchError(
          record.error instanceof ApiError ? record.error.code : undefined)}</p>
      : (() => {
        const view = record.value!;
        const currency = typeof view.fields.currency === "string" ? view.fields.currency : undefined;
        return <>
          <dl className="document-fields">{Object.entries(view.fields).filter(([name]) =>
            !HIDDEN_FIELDS.has(name)).map(([name, value]) =>
            <div key={name}><dt>{fieldName(name)}</dt><dd>{MONEY.has(name) && typeof value === "number" && currency
              ? money(value, currency, view.currency_exponents) : humanFact(value, name)}</dd></div>)}</dl>
          <details className="tech-details">
            <summary>Исходная запись</summary>
            <p>{view.file} · строка {view.line} из {view.total_lines}</p>
            <pre>{view.context.filter(row => row.line === view.line).map(row => row.text).join("\n")}</pre>
            <p className="mono">{docKey}</p>
          </details>
        </>;
      })()}
  </section>;
}

export function TechDetails({ title = "Технические сведения", data }: { title?: string; data: unknown }) {
  const text = JSON.stringify(data, null, 1);
  const href = `data:application/json;charset=utf-8,${encodeURIComponent(text)}`;
  return <details className="tech-details">
    <summary>{title}</summary>
    <pre>{text}</pre>
    <a className="text-link" href={href} download="auditlab-research.json">Скачать JSON</a>
  </details>;
}
