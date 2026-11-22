// Financial laboratory: related situations, error dossier and results map over published artifacts.
// The browser never recomputes financial truth; amounts and outcomes come from the server projection.
import { useState, type ReactNode } from "react";
import "./research.css";
import "./lab.css";
import { money, when } from "../format";
import { WorkspaceHeader } from "../workspace/AppShell";
import { Icon } from "../workspace/Icon";
import { findingName } from "../workspace/model";
import { recordName } from "../workspace/copy";
import {
  research, type Claim, type Designated, type Dossier, type LabAttempt, type LabSituation, type MapCell, type Outcome,
  type Reference, type ResultsMap, type SituationCard,
} from "./api";
import {
  CHANGE, count, DIMENSION, facetName, OUTCOME_NOTE, OUTCOME_TONE, outcomeName, share, techError, TRANSFORMATION_NOTE,
  transformationName, splitName,
} from "./copy";
import { ActionList, DocButton, docLink, DocumentPanel, Failed, Origin, rlink, Stale, TechDetails, useResearch } from "./parts";

const SLOTS = ["id_order", "lawful_refund", "fixed", "currency", "boundary"] as const;
const OUTCOME_ORDER = ["correct", "miss", "false_accusation", "wrong_amount", "wrong_evidence", "unverified", "technical", "not_evaluated"] as const;
const back = <a className="text-link" href={rlink("lab")}>К лаборатории<Icon name="arrow"/></a>;

/** Loader with an explicit cancel: cancelling aborts the request and leaves a recoverable state. */
function useCancellable<T>(key: string, load: (signal: AbortSignal) => Promise<T>) {
  const [cancelled, setCancelled] = useState<string | null>(null);
  const view = useResearch(cancelled === key ? null : key, load);
  return { ...view, cancelled: cancelled === key, cancel: () => setCancelled(key), resume: () => setCancelled(null) };
}

function LabLoading({ label, cancel }: { label: string; cancel: () => void }) {
  return <div className="research-loading" aria-busy="true">
    <p role="status" className="loading-line"><span className="spinner" aria-hidden="true"/>{label}…</p>
    <button className="secondary-action" type="button" onClick={cancel}>Отменить загрузку</button>
    <div className="skeleton skeleton-title"/><div className="feature-grid skeleton-grid"><div className="skeleton"/><div className="skeleton"/></div>
  </div>;
}

function Cancelled({ resume }: { resume: () => void }) {
  return <div className="workspace-empty" role="status">
    <h2>Загрузка отменена</h2><p>Запрос к сервису исследований остановлен. Сохранённые данные не изменились.</p>
    <div className="empty-actions"><button className="primary-action" type="button" onClick={resume}>Загрузить снова</button>{back}</div>
  </div>;
}

function Screen<T>({ title, label, view, children }: {
  title: string; label: string; view: ReturnType<typeof useCancellable<T>>; children: (value: T) => ReactNode;
}) {
  if (view.cancelled) return <><WorkspaceHeader title={title}/><Cancelled resume={view.resume}/></>;
  if (view.loading) return <><WorkspaceHeader title={title}/><LabLoading label={label} cancel={view.cancel}/></>;
  if (view.value === undefined) return <><WorkspaceHeader title={title}/><Failed error={view.error} retry={view.retry} back={back}/></>;
  return <>{children(view.value)}</>;
}

function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  return <span className={`verdict verdict-${OUTCOME_TONE[outcome.primary] ?? "unknown"}`}>
    {outcome.primary === "correct" && <Icon name="check"/>}{outcomeName(outcome.primary)}
  </span>;
}

function amountText(items: { type: string; claimed_amount_minor: number; currency: string }[] | null | undefined,
  exponents: Record<string, number>, empty: string): string {
  if (!items?.length) return empty;
  return items.map(f => `${findingName(f.type)} · ${money(f.claimed_amount_minor, f.currency, exponents)}`).join("; ");
}

function referenceSource(reference: Reference): string {
  if (reference.source === "independent") return "Независимый финансовый расчёт (сделан до открытия эталона)";
  if (reference.source === "console") return reference.complete
    ? "Расчёт консоли по публичным документам" : "Расчёт консоли по публичным документам — покрывает не все типы нарушений";
  return "Независимой проверки нет";
}

function Arithmetic({ lines }: { lines: string[] }) {
  if (!lines.length) return null;
  return <details className="tech-details"><summary>Запись независимого расчёта</summary>
    <ul className="arith">{lines.map(line => <li key={line}>{line}</li>)}</ul></details>;
}

function AttemptLine({ attempt, exponents }: { attempt: LabAttempt; exponents: Record<string, number> }) {
  return <li className="attempt-line">
    <span className="attempt-agent">{attempt.agent}</span>
    <OutcomeBadge outcome={attempt.outcome}/>
    <span className="small">{attempt.outcome.primary === "technical" ? techError(attempt.error, attempt.status)
      : attempt.claims.length > 3 ? `Выводов: ${attempt.claims.length}, засчитано: ${attempt.evaluation?.tp ?? 0}`
      : attempt.claims.length ? attempt.claims.map((c: Claim) => money(c.claimed_amount_minor, c.currency, exponents)).join(", ")
      : "Выводов нет"}</span>
    <a className="text-link" href={rlink("dossiers", attempt.artifact_id)}>Досье<Icon name="arrow"/></a>
  </li>;
}

function Situation({ situation, docBase, labels, current }: {
  situation: LabSituation | null; docBase: string; labels: Record<string, string>; current?: string;
}) {
  const d: Designated | null | undefined = situation?.designated;
  const s = d?.summary;
  const exponents = situation?.currency_exponents ?? {};
  if (!situation) return <p className="muted">Снимок компании не опубликован: расчёт по документам недоступен.</p>;
  if (!s) return <p className="muted">В этой компании консоль не нашла платежа с остатком; для других типов нарушений смотрите независимый расчёт.</p>;
  return <>
    <dl className="calculation situation-calc">
      <div><dt>Поступило по платежу</dt><dd>{money(s.received_minor, s.currency, exponents)}</dd></div>
      <div><dt>Распределено до границы</dt><dd>{money(s.allocated_minor, s.currency, exponents)}</dd></div>
      <div><dt>Граница периода</dt><dd>{s.boundary ? `${when(s.boundary)}${s.boundary_exclusive ? " (не включительно)" : ""}` : "—"}</dd></div>
      {s.after_boundary.length > 0 && <div><dt>Зачтено после границы</dt><dd>{s.after_boundary.map(row => money(row.value_minor, s.currency, exponents)).join(", ")}</dd></div>}
      <div className="balance"><dt>Нераспределено на отчётную дату</dt><dd>{money(s.residual_minor, s.currency, exponents)}</dd></div>
    </dl>
    <div className="doc-row">{d!.evidence.map(key => <DocButton key={key} label={labels[key] ?? recordName(key)}
      href={docLink(docBase, key)} current={current === key}/>)}</div>
  </>;
}

function docLabels(card: { situation: LabSituation | null }): Record<string, string> {
  const d = card.situation?.designated;
  const labels: Record<string, string> = {};
  if (!d) return labels;
  labels[d.payment_key] = "Платёжное поручение";
  for (const op of d.operands) labels[op.key] = op.sign > 0 ? "Поступление на нераспределённый остаток" : "Зачёт до границы";
  for (const row of d.context) labels[row.key] = "Зачёт после границы периода";
  labels["policy/allocation"] = "Правило распределения платежей";
  return labels;
}

// ---------------------------------------------------------------------- home
function FamilyList({ items }: { items: { candidate_id: string; label: string | null; split?: string; transformations: string[]; attempts: number }[] }) {
  return <ul className="choice-list">{items.map(f => <li key={f.candidate_id}>
    <a href={rlink("families", f.candidate_id)}><Icon name="building"/>
      <span><strong>{f.label ?? "Компания"}</strong>
        <small>{f.transformations.length ? `Вариантов: ${f.transformations.length}` : "Без вариантов"} · попыток агентов: {count(f.attempts)} · раздел «{splitName(f.split)}»</small></span>
      <Icon name="arrow"/></a></li>)}</ul>;
}

export function LabHome() {
  const view = useCancellable("lab", signal => research.lab(signal));
  return <Screen title="Финансовая лаборатория" label="Загружаем лабораторию" view={view}>{data => <>
    <WorkspaceHeader title="Финансовая лаборатория"
      subtitle="Поймите финансовую задачу, измените допустимый факт и проверьте ответ агента по документам."
      action={<button className="secondary-action" type="button" onClick={view.retry}>Обновить</button>}/>
    <Stale at={view.staleAt} retry={view.retry}/>
    <ol className="lab-steps">
      <li><strong>Финансовая задача</strong><span>Платёж, распределение до границы периода и остаток по документам.</span></li>
      <li><strong>Допустимое изменение</strong><span>Сохранённые варианты генератора: переименование, возврат, исправление, валюта.</span></li>
      <li><strong>Проверка агента</strong><span>Ответ агента рядом с независимым расчётом, досье ошибки и документы.</span></li>
    </ol>
    <section className="research-panel" aria-labelledby="families-title">
      <h2 id="families-title">Компании и связанные ситуации</h2>
      {!data.families.length ? <div className="lab-empty">
        <p>Связанные ситуации не опубликованы. Подключите каталог, созданный экспортом семейств.</p>
        <details className="tech-details"><summary>Как получить данные</summary>
          <pre>python -m auditlab.research publish-families --output ПОИСК --target КАТАЛОГ</pre></details>
      </div> : <>
        <FamilyList items={data.families.filter(f => f.transformations.length)}/>
        {data.families.some(f => !f.transformations.length) && <details className="more-companies">
          <summary>Остальные компании без вариантов: {data.families.filter(f => !f.transformations.length).length}</summary>
          <FamilyList items={data.families.filter(f => !f.transformations.length)}/>
        </details>}
      </>}
    </section>
    <div className="research-grid equal">
      <section className="research-panel">
        <h2>Карта результатов</h2>
        <p>Типы нарушений, взаимодействия, чистые контрпримеры, валюты, граница периода и сложность. Пустые ячейки — «не проверено».</p>
        <a className="primary-action" href={rlink("map")}>Открыть карту<Icon name="arrow"/></a>
      </section>
      <section className="research-panel" aria-labelledby="experiments-title">
        <h2 id="experiments-title">Сохранённые прогоны</h2>
        {!data.experiments.length ? <p className="muted">Прогонов нет.</p> : <ul className="plain run-list">{data.experiments.map(e => {
          const failed = Object.values(e.progress?.failed ?? {}).reduce<number>((sum, n) => sum + (n ?? 0), 0);
          return <li key={e.artifact_id}><a href={rlink("experiments", e.artifact_id)}>{e.title ?? "Эксперимент"}</a>
            {e.agents?.length ? <span className="small">{e.agents.join(" · ")}</span> : null}
            <span className="small muted">Завершено {share(e.progress?.completed, e.progress?.planned)}{failed ? ` · сбоев: ${failed}` : ""}</span>
            <Origin provenance={e._provenance} compact/></li>;
        })}</ul>}
        <p className="muted small">Записи открываются без ключа и без сети. Живой запуск отмечается отдельно на экране эксперимента.</p>
      </section>
    </div>
  </>}</Screen>;
}

// ---------------------------------------------------------------------- related situations
function expectedChange(original: SituationCard, variant: SituationCard): string {
  const before = original.reference.findings;
  const after = variant.expected ?? [];
  const exp = (c: SituationCard) => c.situation?.currency_exponents ?? {};
  if (!after.length) return before.length ? "Вывод должен исчезнуть: нарушения больше нет." : "Выводов не должно быть.";
  const same = after.length === before.length && after.every(a => before.some(b => b.type === a.type
    && b.claimed_amount_minor === a.claimed_amount_minor && b.currency === a.currency));
  if (same) return `Ответ прежний: ${amountText(after, exp(variant), "")}.`;
  return `Ответ меняется: ${amountText(before, exp(original), "выводов нет")} → ${amountText(after, exp(variant), "выводов нет")}.`;
}

function changeText(card: SituationCard): string {
  const c = card.change;
  if (!c) return "—";
  if (c.parameter === "settlement_timing") return `Последний зачёт: ${c.from === "after" ? "после границы" : c.from} → ${c.to === "before" ? "до границы" : c.to}`;
  if (c.parameter === "base_currency") return `Валюта договора: ${c.from} → ${c.to}`;
  if (c.parameter === "clean_distractors") return "Добавлена законная операция: возврат депозита";
  if (c.parameter === "ids_and_order") return "Номера документов и порядок строк";
  return "—";
}

function SideColumn({ card, title, docBase, current }: { card: SituationCard; title: string; docBase: string; current?: string }) {
  const exponents = card.situation?.currency_exponents ?? {};
  const labels = docLabels(card);
  return <section className="research-panel lab-side" aria-label={title}>
    <p className="side-kicker">{title}</p>
    <h2>{card.label ?? "Компания"}</h2>
    <h3>Финансовая ситуация</h3>
    <Situation situation={card.situation} docBase={docBase} labels={labels} current={current}/>
    <h3>Независимый расчёт</h3>
    <p className="reference-line"><strong>{amountText(card.reference.findings, exponents, "Нарушений не найдено")}</strong></p>
    <p className="muted small">{referenceSource(card.reference)}</p>
    <Arithmetic lines={card.reference.arithmetic}/>
    <h3>Ответы агентов</h3>
    {!card.attempts.length ? <p className="not-checked">Не проверено: агенты на этой компании не запускались.</p>
      : <ul className="plain attempt-lines">{card.attempts.map(a => <AttemptLine key={a.artifact_id} attempt={a} exponents={exponents}/>)}</ul>}
    <TechDetails data={card.raw}/>
  </section>;
}

export function FamilyScreen({ id, variant, side: sideName, docKey }: { id: string; variant?: string; side?: string; docKey?: string }) {
  const view = useCancellable(`family:${id}`, signal => research.family(id, signal));
  return <Screen title="Связанные ситуации" label="Загружаем компанию и варианты" view={view}>{data => {
    const { original, variants } = data;
    const chosen = variants.find(v => v.candidate_id === variant) ?? variants[0];
    const base = rlink("families", id, "v", chosen?.candidate_id ?? "none");
    const side = sideName === "original" || !chosen ? original : chosen;
    const exponents = original.situation?.currency_exponents ?? {};
    const residual = original.situation?.designated?.summary;
    const byKind = new Map(variants.map(v => [v.transformation ?? "", v]));
    return <>
      <WorkspaceHeader title={original.label ?? "Компания"} subtitle="Исходная компания и допустимые изменения одного факта."
        action={<Origin provenance={original._provenance}/>}/>
      <Stale at={view.staleAt} retry={view.retry}/>
      <section className="research-panel lab-task" aria-labelledby="task-title">
        <h2 id="task-title">Финансовая задача</h2>
        {residual ? <p className="lead">Клиент перечислил {money(residual.received_minor, residual.currency, exponents)}. До границы периода
          ({when(residual.boundary)}, не включительно) распределено {money(residual.allocated_minor, residual.currency, exponents)}.
          Аудитор должен найти нераспределённый на отчётную дату остаток — {money(residual.residual_minor, residual.currency, exponents)} — и
          сослаться на платёж, проводки и правило распределения.</p>
          : <p className="lead">Аудитор проверяет книгу компании и сдаёт выводы с доказательствами. Правильный ответ: {amountText(original.reference.findings, exponents, "нарушений нет")}.</p>}
      </section>
      <section className="research-panel" aria-labelledby="change-title">
        <h2 id="change-title">Измените допустимый факт</h2>
        <p className="muted small">Варианты созданы генератором и проверены независимым расчётом. Выбор показывает сохранённый результат; новый запуск отсюда не выполняется.</p>
        <ul className="transform-list">{SLOTS.map(kind => {
          const card = byKind.get(kind);
          const selected = card && card.candidate_id === chosen?.candidate_id;
          return <li key={kind}>
            {card ? <a href={rlink("families", id, "v", card.candidate_id)} aria-current={selected ? "true" : undefined}
              className={selected ? "transform selected" : "transform"}>
              <strong>{transformationName(kind)}</strong><small>{TRANSFORMATION_NOTE[kind]}</small></a>
              : <div className="transform unavailable" aria-disabled="true">
                <strong>{transformationName(kind)}</strong>
                <small>{kind === "boundary"
                  ? "Здесь не подключено: контроллер исследований не меняет факты. Ближайший вариант — «Исправление нарушения»: транш переносится через границу (после → до). Перенос «ровно на границу» выполняет генератор в разделе «Сравнения»."
                  : "Этот вариант не опубликован для данной компании."}</small>
                {kind === "boundary" && <a className="text-link" href="#/comparisons">Открыть «Сравнения»<Icon name="arrow"/></a>}
              </div>}
          </li>;
        })}</ul>
      </section>
      {chosen ? <>
        <section className="research-panel" aria-labelledby="diff-title">
          <h2 id="diff-title">Что изменилось: {transformationName(chosen.transformation)}</h2>
          <table className="research-table compare-table">
            <thead><tr><th scope="col">Показатель</th><th scope="col">Исходная компания</th><th scope="col">После изменения</th></tr></thead>
            <tbody>
              <tr><th scope="row" data-label="Показатель">Допустимое изменение</th><td data-label="Исходная компания">—</td><td data-label="После изменения">{changeText(chosen)}</td></tr>
              {(["received_minor", "allocated_minor", "residual_minor"] as const).map(field => {
                const a = original.situation?.designated?.summary; const b = chosen.situation?.designated?.summary;
                const name = { received_minor: "Поступило по платежу", allocated_minor: "Распределено до границы", residual_minor: "Остаток на отчётную дату" }[field];
                return <tr key={field}><th scope="row" data-label="Показатель">{name}</th>
                  <td data-label="Исходная компания">{a ? money(a[field], a.currency, original.situation!.currency_exponents) : "—"}</td>
                  <td data-label="После изменения">{b ? money(b[field], b.currency, chosen.situation!.currency_exponents) : "—"}</td></tr>;
              })}
              <tr><th scope="row" data-label="Показатель">Ожидаемый правильный ответ</th>
                <td data-label="Исходная компания">{amountText(original.reference.findings, exponents, "Нарушений нет")}</td>
                <td data-label="После изменения">{expectedChange(original, chosen)}
                  <small className="muted block">{chosen.expected_check === "passed" ? "Независимый расчёт совпал с ожиданием генератора."
                    : chosen.expected_check === "failed" ? "Независимый расчёт НЕ совпал с ожиданием генератора." : "Сверка ожидания не выполнялась."}</small></td></tr>
            </tbody>
          </table>
          <p className="muted small">Все суммы — из независимого расчёта и расчёта консоли по публичным документам; браузер их не пересчитывает.</p>
        </section>
        <div className="research-grid equal">
          <SideColumn card={original} title="Исходная компания" docBase={`${base}/side/original`} current={side === original ? docKey : undefined}/>
          <SideColumn card={chosen} title={transformationName(chosen.transformation)} docBase={`${base}/side/variant`} current={side === chosen ? docKey : undefined}/>
        </div>
      </> : <p className="not-checked">Варианты этой компании не опубликованы: сравнивать не с чем.</p>}
      {docKey && <DocumentPanel snapshotId={side.snapshot_id} docKey={docKey} label={docLabels(side)[docKey]} closeHref={base}/>}
      <details className="tech-details"><summary>Как воспроизвести варианты</summary>
        <pre>python tools/research_rehearsal.py --output artifacts/research-rehearsal{"\n"}python tools/financial_lab_rehearsal.py --search artifacts/research-rehearsal/search --output artifacts/financial-lab</pre></details>
    </>;
  }}</Screen>;
}

// ---------------------------------------------------------------------- dossier
function EvidenceList({ rows, base, labels, current, empty }: {
  rows: { key: string; received: boolean; step: number | null; in_reference?: boolean }[]; base: string;
  labels: Record<string, string>; current?: string; empty: string;
}) {
  if (!rows.length) return <p className="muted">{empty}</p>;
  return <ul className="needed-list">{rows.map(row => <li key={row.key} className={row.received ? "got" : "missing"}>
    <span className="needed-mark">{row.received ? <><Icon name="check"/><span className="sr-only">получен</span></> : <span aria-hidden="true">✕</span>}</span>
    <DocButton label={labels[row.key] ?? recordName(row.key)} href={docLink(base, row.key)} current={current === row.key}/>
    <small>{row.received ? `получен на шаге ${row.step}` : "агент не получал"}{row.in_reference === false ? " · нет в независимом расчёте" : ""}</small>
  </li>)}</ul>;
}

export function DossierScreen({ id, docKey }: { id: string; docKey?: string }) {
  const view = useCancellable(`dossier:${id}`, signal => research.dossier(id, signal));
  const [showAll, setShowAll] = useState(false);
  return <Screen title="Досье ошибки" label="Собираем досье" view={view}>{(d: Dossier) => {
    const base = rlink("dossiers", id);
    const exponents = d.situation?.currency_exponents ?? d.attempt.currency_exponents ?? {};
    const labels = docLabels({ situation: d.situation });
    const o = d.outcome;
    const technical = o.primary === "technical";
    const experiment = typeof d.attempt.raw.experiment_id === "string" ? d.attempt.raw.experiment_id : undefined;
    return <>
      <WorkspaceHeader title={`Досье: ${outcomeName(o.primary).toLocaleLowerCase("ru")}`}
        subtitle={[d.company ?? "Компания", d.agent, (d.attempt.repetition ?? 0) > 0 ? `повтор ${count(d.attempt.repetition)}` : null].filter(Boolean).join(" · ")}
        action={<Origin provenance={d.attempt._provenance}/>}/>
      <Stale at={view.staleAt} retry={view.retry}/>
      <section className={`research-panel verdict-panel tone-${OUTCOME_TONE[o.primary]}`} aria-labelledby="verdict-title">
        <h2 id="verdict-title"><OutcomeBadge outcome={o}/></h2>
        <p>{technical ? `${techError(o.error, o.status)}. ${OUTCOME_NOTE.technical}` : OUTCOME_NOTE[o.primary]}</p>
        {o.kinds.length > 1 && <p className="small">Также в этой попытке: {o.kinds.slice(1).map(k => outcomeName(k).toLocaleLowerCase("ru")).join(", ")}.</p>}
        {o.evaluation && <p className="score-line">TP {o.evaluation.tp} · FP {o.evaluation.fp} · FN {o.evaluation.fn} · награда {o.evaluation.reward.toLocaleString("ru-RU", { maximumFractionDigits: 3 }).replace("-", "−")}</p>}
        <p className="muted small">Объяснение построено только по действиям агента, ответам среды и оценщику. Рассуждения модели не записываются и здесь не предполагаются.</p>
      </section>
      <div className="research-grid equal">
        <section className="research-panel" aria-labelledby="correct-title">
          <h2 id="correct-title">Правильный вывод</h2>
          <p className="reference-line"><strong>{amountText(d.reference.findings, exponents, "Нарушений не найдено")}</strong></p>
          <p className="muted small">{referenceSource(d.reference)}{d.reference.note ? `. ${d.reference.note}` : ""}</p>
          <Arithmetic lines={d.reference.arithmetic}/>
          <h3>Арифметика по документам</h3>
          <Situation situation={d.situation} docBase={base} labels={labels} current={docKey}/>
        </section>
        <section className="research-panel" aria-labelledby="answer-title">
          <h2 id="answer-title">Ответ агента</h2>
          {technical ? <p className="inline-error">{techError(o.error, o.status)}. Ответа нет; финансовая оценка не выставляется.</p>
            : !d.claimed.length ? <p><strong>Пустой ответ: выводов нет.</strong></p>
            : <ul className="submitted">{d.claimed.slice(0, showAll ? undefined : 5).map((c, i) => <li key={i}>
              <strong>{findingName(c.type)} · {money(c.claimed_amount_minor, c.currency, exponents)}</strong>
              <span className={`verdict verdict-${c.kind === "accepted" ? "pass" : OUTCOME_TONE[c.kind] ?? "fail"}`}>{c.kind === "accepted" ? "Засчитан" : outcomeName(c.kind)}</span>
              {c.reference && c.kind === "wrong_amount" && <small className="muted block">По расчёту: {money(c.reference.claimed_amount_minor, c.reference.currency, exponents)}</small>}
              <EvidenceList rows={c.evidence} base={base} labels={labels} current={docKey} empty="Ссылок на документы нет."/>
            </li>)}</ul>}
          {d.claimed.length > 5 && <button className="text-link" type="button" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Свернуть выводы" : `Показать все выводы: ${d.claimed.length}`}</button>}
          <h3>Документы, нужные для правильного вывода</h3>
          <EvidenceList rows={d.evidence} base={base} labels={labels} current={docKey} empty="Список документов недоступен: нет расчёта для этой компании."/>
        </section>
      </div>
      {docKey && <DocumentPanel snapshotId={d.attempt.snapshot_id} docKey={docKey} label={labels[docKey]} closeHref={base}/>}
      <section className="research-panel" aria-labelledby="actions-title">
        <h2 id="actions-title">Наблюдаемые действия: {d.attempt.steps_detail.length}</h2>
        <p className="run-kind"><Icon name="document"/>Сохранённая траектория</p>
        <ActionList steps={d.attempt.steps_detail} labels={labels}/>
      </section>
      <div className="research-grid equal">
        <section className="research-panel" aria-labelledby="repeats-title">
          <h2 id="repeats-title">Повторы этого агента</h2>
          {d.repeats.length <= 1 ? <p className="not-checked">Не проверено: на этой компании у агента одна попытка; повторяемость не измерена.</p>
            : <ul className="plain attempt-lines">{d.repeats.map(a => <AttemptLine key={a.artifact_id} attempt={a} exponents={exponents}/>)}</ul>}
          <h3>Другие агенты на этой компании</h3>
          {!d.others.length ? <p className="muted">Других попыток нет.</p>
            : <ul className="plain attempt-lines">{d.others.map(a => <AttemptLine key={a.artifact_id} attempt={a} exponents={exponents}/>)}</ul>}
        </section>
        <section className="research-panel" aria-labelledby="control-title">
          <h2 id="control-title">Сокращённый случай и исправленный контроль</h2>
          <h3>Сокращённый случай</h3>
          {d.reduced ? <p><a href={rlink("reductions", d.reduced.reduction_id)}>Открыть сокращённый случай</a> · проводок {count(d.reduced.ledger_rows[0])} → {count(d.reduced.ledger_rows[1])}</p>
            : <p className="not-checked">Не проверено: сокращение для этой ошибки не выполнялось.</p>}
          <h3>Исправленный контроль</h3>
          {!d.control ? <p className="not-checked">Не проверено: компания не входит в опубликованное семейство.</p>
            : !d.control.available ? <p className="not-checked">Не проверено. {d.control.reason}</p>
            : <>
              <p><a href={rlink("families", d.control.family_root, "v", d.control.candidate_id)}>{d.control.label ?? "Исправленный вариант"}</a>.
                Правильный ответ: {amountText(d.control.reference.findings, exponents, "нарушений нет")}.</p>
              {!d.control.attempts.length ? <p className="not-checked">Не проверено: этот агент на контроле не запускался.</p>
                : <ul className="plain attempt-lines">{d.control.attempts.map(a => <AttemptLine key={a.artifact_id} attempt={a} exponents={exponents}/>)}</ul>}
            </>}
        </section>
      </div>
      <div className="next-step">
        {d.family_root && <a className="secondary-action" href={rlink("families", d.family_root)}>Связанные ситуации</a>}
        {experiment && <a className="primary-action" href={rlink("experiments", experiment)}>К эксперименту<Icon name="arrow"/></a>}
      </div>
      <TechDetails data={{ ...d.attempt.raw, candidate_id: d.candidate_id, reference: { source: d.reference.source, validation_id: d.reference.validation_id } }}/>
    </>;
  }}</Screen>;
}

// ---------------------------------------------------------------------- results map
export interface MapState { dimension?: string; experiment?: string; before?: string; after?: string; column?: string; value?: string }
export function mapLink(state: MapState): string {
  const parts: string[] = ["map"];
  for (const [short, key] of [["d", "dimension"], ["e", "experiment"], ["b", "before"], ["a", "after"], ["c", "column"], ["r", "value"]] as const) {
    if (state[key]) parts.push(short, state[key]!);
  }
  return rlink(...parts);
}

function groups(columns: ResultsMap["columns"]) {
  const out: { key: string; title?: string; span: number }[] = [];
  for (const c of columns) {
    const key = c.id.split("/")[0];
    const last = out[out.length - 1];
    if (last && last.key === key) last.span += 1; else out.push({ key, title: c.experiment, span: 1 });
  }
  return out;
}

function CellView({ cell, href, selected, label }: { cell: MapCell; href: string; selected: boolean; label: string }) {
  if (cell.state === "not_checked") return <span className="map-cell not-checked-cell">не проверено</span>;
  const shown = OUTCOME_ORDER.filter(k => cell.outcomes[k]);
  return <a className={`map-cell${selected ? " selected" : ""}${cell.state === "partial" ? " partial" : ""}`} href={href}
    aria-current={selected ? "true" : undefined} aria-label={`${label}: семейств ${cell.families}, попыток ${cell.attempts}`}>
    <span className="cell-counts">{cell.families} сем. · {cell.attempts} поп.</span>
    <span className="cell-outcomes">{shown.map(k => <span key={k} className={`chip tone-${OUTCOME_TONE[k]}`}>{outcomeName(k).toLocaleLowerCase("ru")}: {cell.outcomes[k]}</span>)}</span>
    {cell.state === "partial" && <span className="cell-flag">частичный результат</span>}
  </a>;
}

export function MapScreen({ state }: { state: MapState }) {
  const dimension = state.dimension && DIMENSION[state.dimension] ? state.dimension : "violation";
  const view = useCancellable(`map:${state.experiment ?? ""}:${state.before ?? ""}:${state.after ?? ""}`,
    signal => research.map({ experiment: state.experiment, before: state.before, after: state.after }, signal));
  return <Screen title="Карта результатов" label="Строим карту результатов" view={view}>{(m: ResultsMap) => {
    const table = m.dimensions[dimension] ?? [];
    const go = (patch: MapState) => mapLink({ ...state, dimension, ...patch });
    const selectedRow = table.find(r => r.value === state.value);
    const cell = selectedRow && state.column ? selectedRow.cells[state.column] : undefined;
    const drill = cell ? m.rows.filter(r => cell.attempt_ids.includes(r.artifact_id)) : [];
    const columnLabel = (id?: string) => m.columns.find(c => c.id === id)?.label ?? "Агент";
    const measured = m.experiments.some(e => e._provenance.kind === "measured");
    return <>
      <WorkspaceHeader title="Карта результатов" subtitle="Где агенты ошибаются: по типам нарушений и условиям. Каждая ячейка открывает конкретные попытки."
        action={<div className="heading-tools">{measured && <Origin provenance={m.experiments.find(e => e._provenance.kind === "measured")!._provenance}/>}
          <button className="secondary-action" type="button" onClick={view.retry}>Обновить</button></div>}/>
      <Stale at={view.staleAt} retry={view.retry}/>
      {m.experiments.some(e => e._provenance.kind !== "measured") && <p className="fixture-note" role="note">Часть данных — не измерение (фикстура или тестовая замена). Такие прогоны помечены на экранах эксперимента.</p>}
      {m.incomplete.length > 0 && <section className="research-panel incomplete" aria-labelledby="incomplete-title">
        <h2 id="incomplete-title">Незавершённые прогоны и ошибки сервиса</h2>
        <ul className="plain">{m.incomplete.map(e => <li key={e.experiment_id}>
          <a href={rlink("experiments", e.experiment_id)}>{e.title ?? "Эксперимент"}</a>: завершено {share(e.completed, e.planned)}
          {Object.entries(e.failed).filter(([, n]) => n).map(([kind, n]) => ` · ${techError({ code: kind })}: ${n}`).join("")}.
          {" "}Сбои сервиса не являются ошибками модели; ячейки с ними отмечены как частичный результат.
        </li>)}</ul>
      </section>}
      <section className="research-panel" aria-labelledby="map-title">
        <h2 id="map-title">{DIMENSION[dimension]}</h2>
        <div className="map-controls">
          <div className="segmented" role="group" aria-label="Измерение карты">{Object.keys(DIMENSION).map(key =>
            <a key={key} href={go({ dimension: key, column: undefined, value: undefined })} aria-current={key === dimension ? "true" : undefined}>{DIMENSION[key]}</a>)}</div>
          <label className="map-select">Прогон
            <select value={state.experiment ?? ""} onChange={event => { location.hash = go({ experiment: event.target.value || undefined, before: undefined, after: undefined, column: undefined, value: undefined }); }}>
              <option value="">Все прогоны</option>
              {m.experiments.map(e => <option key={e.artifact_id} value={e.artifact_id}>{e.title ?? "Эксперимент"}</option>)}
            </select></label>
        </div>
        {!m.columns.length ? <p className="not-checked">Попыток нет: подключите каталог с результатами прогонов.</p> :
          <><p className="scroll-hint">Таблица прокручивается вправо: в каждом столбце — один агент.</p>
          <div className="map-scroll" tabIndex={0} aria-label="Таблица карты, прокручивается по горизонтали"><table className="map-table">
            <thead>
              <tr><td className="map-corner"/>{groups(m.columns).map(g => <th scope="colgroup" colSpan={g.span} key={g.key} className="map-group">{g.title ?? "Прогон"}</th>)}</tr>
              <tr><th scope="col">{DIMENSION[dimension]}</th>{m.columns.map(c => <th scope="col" key={c.id}>{c.label}</th>)}</tr>
            </thead>
            <tbody>{table.map(row => <tr key={row.value}>
              <th scope="row">{facetName(dimension, row.value)}</th>
              {m.columns.map(c => <td key={c.id}><CellView cell={row.cells[c.id]} label={`${facetName(dimension, row.value)} — ${c.label}`}
                href={go({ column: c.id, value: row.value })} selected={state.column === c.id && state.value === row.value}/></td>)}
            </tr>)}</tbody>
          </table></div></>}
        <p className="muted small">«не проверено» — в этой ячейке нет ни одной попытки; это не ноль ошибок. Семейство — одна экономическая компания со всеми её вариантами.</p>
      </section>
      {cell && <section className="research-panel" aria-labelledby="drill-title">
        <h2 id="drill-title">{facetName(dimension, state.value!)} · {columnLabel(state.column)}: {drill.length} попыток</h2>
        <table className="research-table">
          <thead><tr><th scope="col">Компания</th><th scope="col">Исход</th><th scope="col">Подробности</th><th scope="col"><span className="sr-only">Досье</span></th></tr></thead>
          <tbody>{drill.map(r => <tr key={r.artifact_id}>
            <td data-label="Компания">{r.company}</td>
            <td data-label="Исход"><OutcomeBadge outcome={r.outcome}/></td>
            <td data-label="Подробности">{r.outcome.primary === "technical" ? techError(r.error, r.status) : r.outcome.kinds.length > 1 ? `также: ${r.outcome.kinds.slice(1).map(k => outcomeName(k).toLocaleLowerCase("ru")).join(", ")}` : "—"}</td>
            <td><a className="text-link" href={rlink("dossiers", r.artifact_id)}>Досье<Icon name="arrow"/></a></td>
          </tr>)}</tbody>
        </table>
        <a className="text-link" href={go({ column: undefined, value: undefined })}>Скрыть попытки</a>
      </section>}
      <Regression m={m} state={state} go={go}/>
    </>;
  }}</Screen>;
}

function Regression({ m, state, go }: { m: ResultsMap; state: MapState; go: (patch: MapState) => string }) {
  const r = m.regression;
  const label = (id?: string) => m.columns.find(c => c.id === id)?.label ?? "Агент";
  const select = (key: "before" | "after", value: string | undefined) =>
    <select value={value ?? ""} onChange={event => { location.hash = go({ [key]: event.target.value }); }}>
      {m.columns.map(c => <option key={c.id} value={c.id}>{c.label}</option>)}
    </select>;
  return <section className="research-panel" aria-labelledby="regression-title">
    <h2 id="regression-title">До и после: регрессии</h2>
    {m.columns.length < 2 || !r ? <p className="not-checked">Не проверено: для сравнения нужны два агента на одних и тех же компаниях.</p> : <>
      <div className="map-controls">
        <label className="map-select">До {select("before", r.before)}</label>
        <label className="map-select">После {select("after", r.after)}</label>
        <a className="secondary-action" href={go({ before: r.after, after: r.before })}>Поменять местами</a>
      </div>
      <dl className="facts">{(["regression", "improvement", "unchanged_correct", "unchanged_wrong", "not_comparable"] as const).map(k =>
        <div key={k} className={k === "regression" && r.counts[k] ? "regression-count" : ""}><dt>{CHANGE[k]}</dt><dd>{count(r.counts[k])}</dd></div>)}</dl>
      <p className="muted small">Компании сопоставлены по содержимому публичных файлов. «Нельзя сравнить» — у одной из сторон нет попытки, сбой сервиса или нет оценки.</p>
      {r.items.filter(i => i.change === "regression" || i.change === "not_comparable").length > 0 && <table className="research-table">
        <thead><tr><th scope="col">Компания</th><th scope="col">Изменение</th><th scope="col">{label(r.before)}</th><th scope="col">{label(r.after)}</th></tr></thead>
        <tbody>{r.items.filter(i => i.change === "regression" || i.change === "not_comparable").map(i => <tr key={i.company_key}>
          <td data-label="Компания">{i.company}</td>
          <td data-label="Изменение"><span className={`verdict verdict-${i.change === "regression" ? "fail" : "unknown"}`}>{CHANGE[i.change]}</span></td>
          <td data-label={label(r.before)}>{i.before_attempts.length ? i.before_attempts.map((id, n) => <a key={id} href={rlink("dossiers", id)}>{outcomeName(i.before_outcome[n])}</a>) : "нет попытки"}</td>
          <td data-label={label(r.after)}>{i.after_attempts.length ? i.after_attempts.map((id, n) => <a key={id} href={rlink("dossiers", id)}>{outcomeName(i.after_outcome[n])}</a>) : "нет попытки"}</td>
        </tr>)}</tbody></table>}
      {state.before === undefined && <p className="muted small">По умолчанию выбрана пара из опубликованного сравнения.</p>}
    </>}
  </section>;
}
