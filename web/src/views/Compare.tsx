import { useMemo, useState } from "react";
import { api, type Change, type CompareSide, type CompareView, type Derivation } from "../api";
import { DiagnosticDownload, Empty, ErrorBox, Panel } from "../components";
import { money, reward, TIMINGS } from "../format";
import { recordLink, runLink } from "../navigation";
import { WorkspaceHeader } from "../workspace/AppShell";
import { fieldName, humanFact, humanText, operandName, recordName, referenceLabels } from "../workspace/copy";
import { auditorName, companyName, findingName, periodName, realPairs, runStatus } from "../workspace/model";
import { useResource } from "../workspace/resources";

type PairMember = "left" | "right";
type Relationship = { kind: "change" | "copy"; original: PairMember; updated: PairMember };

function relationship(view: CompareView): Relationship | undefined {
  for (const updated of ["right", "left"] as const) {
    const original = updated === "right" ? "left" : "right";
    const child = view[updated].run;
    const parent = view[original].run.run_id;
    if (child.derived_from?.run_id === parent) return { kind: "copy", original, updated };
    if (child.parent_run_id === parent && child.change) return { kind: "change", original, updated };
  }
}

function sideTitles(view: CompareView, relation: Relationship | undefined) {
  if (relation) {
    const labels = { left: "", right: "" };
    labels[relation.original] = "Исходная проверка";
    labels[relation.updated] = relation.kind === "copy" ? "Копия ответа" : "Проверка с новым условием";
    return labels;
  }
  const [pair] = realPairs([view.left.run, view.right.run]);
  if (pair?.directed) {
    const sameOrder = pair.left.run_id === view.left.run.run_id;
    return { left: sameOrder ? "Компания A" : "Компания B", right: sameOrder ? "Компания B" : "Компания A" };
  }
  return { left: "Первая проверка", right: "Вторая проверка" };
}

function userPrediction(value: string | undefined) {
  const text = humanText(value);
  return text && !/[\/<>]|\b(?:python|demo|[RIDP]\d+)\b|\b(?:TRAIN|OPEN|PAIR)-[A-Za-z0-9_-]+\b/i.test(text) ? text : null;
}

const currencyCode = (value: string) => /^[A-Z]{3}$/.test(value);

function amount(minor: number | null, currency: string, exponents: Record<string, number>) {
  return minor === null || currencyCode(currency) ? money(minor, currency, exponents) : "Валюта не указана";
}

function difference(left: Derivation, right: Derivation, leftExponents: Record<string, number>,
  rightExponents: Record<string, number>) {
  const currency = right.currency;
  const exponent = rightExponents[currency];
  const a = left.result_minor;
  const b = right.result_minor;
  if (left.type !== right.type || left.subject !== right.subject ||
      left.index_check?.complete === false || right.index_check?.complete === false ||
      !currencyCode(currency) || left.currency !== currency || !Number.isInteger(exponent) ||
      exponent < 0 || exponent > 9 || leftExponents[currency] !== exponent ||
      typeof a !== "number" || typeof b !== "number" ||
      !Number.isSafeInteger(a) || !Number.isSafeInteger(b) || !Number.isSafeInteger(b - a)) return null;
  return { value: b - a, currency };
}

function runAlias(run: CompareSide["run"], names: ReadonlyMap<string, string> | undefined) {
  return names?.get(run.run_id) ?? companyName(run);
}

function DocumentLink({ runId, record, labels }: {
  runId: string; record: string; labels: Record<string, string>;
}) {
  return <a href={recordLink(runId, record)}>{labels[record] ?? recordName(record)}</a>;
}

function compareRecordLabels(view: CompareView | undefined) {
  if (!view) return {};
  const claims = [...view.left.claims, ...view.right.claims];
  const calculations = view.calculations.flatMap(row => [row.left, row.right]);
  return referenceLabels([
    ...view.changes.map(row => row.key),
    ...claims.flatMap(row => [...row.evidence_ids, ...row.unconfirmed.map(item => item.key)]),
    ...calculations.flatMap(calc => [...calc.operands, ...(calc.context ?? [])].map(item => item.key)),
  ]);
}

export function Compare({ left, right, names }: { left: string; right: string; names?: ReadonlyMap<string, string> }) {
  const [revision, setRevision] = useState(0);
  const result = useResource(JSON.stringify([left, right]), signal => api.compare(left, right, signal), revision);
  const view = result?.value;
  const labels = useMemo(() => compareRecordLabels(view), [view]);
  const linked = view ? relationship(view) : undefined;
  const original = linked && view ? view[linked.original] : undefined;
  const updated = linked && view ? view[linked.updated] : undefined;
  const change = linked?.kind === "change" ? updated?.run.change : undefined;
  const copy = linked?.kind === "copy" ? updated?.run.derived_from : undefined;
  const prediction = userPrediction(updated?.run.prediction);
  const titles = view ? sideTitles(view, linked) : null;
  const changed = change && view?.changes.find(row => row.key === change.target && row.field === change.field);
  return <div className="compare">
    <WorkspaceHeader title="Сравнение проверок" action={<a className="btn" href="#/comparisons">Все сравнения</a>}/>
    {result?.error ? <>
      <ErrorBox error={result.error}/>
      <button className="btn" type="button" onClick={() => setRevision(value => value + 1)}>Повторить загрузку</button>
    </> : !view || !titles ? <div className="loading" role="status">Загружаем сравнение…</div> : <>
      {(change || copy || prediction) && <div className="change-summary">
        {change && updated && <div className="cs-block">
          <div className="cs-label">Изменённое условие</div>
          <div className="cs-main"><DocumentLink runId={updated.run.run_id} record={change.target} labels={labels}/></div>
          {changed && linked && <div className="cs-arrow">
            <span>{humanFact(linked.original === "left" ? changed.before : changed.after, change.field)}</span>
            <span aria-hidden="true"> → </span>
            <span className="strong">{humanFact(change.value, change.field)}</span>
          </div>}
          {change.timing && TIMINGS[change.timing.from] && TIMINGS[change.timing.to] &&
            <div className="small muted">Время зачёта: {TIMINGS[change.timing.from]} → {TIMINGS[change.timing.to]}.</div>}
        </div>}
        {copy && original && updated && <div className="cs-block">
          <div className="cs-label">Замена доказательства</div>
          <div className="cs-main">Вывод {copy.finding_index + 1}</div>
          <div className="cs-arrow">
            <DocumentLink runId={original.run.run_id} record={copy.replaced.from} labels={labels}/>
            <span aria-hidden="true"> → </span>
            <DocumentLink runId={updated.run.run_id} record={copy.replaced.to} labels={labels}/>
          </div>
          {original.score && updated.score && <div className="small">
            Засчитано: {original.score.tp} → {updated.score.tp}. Награда: {reward(original.score.reward)} → {reward(updated.score.reward)}.
          </div>}
        </div>}
        {prediction && <div className="cs-block"><div className="cs-label">Прогноз перед проверкой</div>
          <div className="cs-main">«{prediction}»</div></div>}
      </div>}
      <div className="two">
        <Side side={view.left} title={titles.left} name={runAlias(view.left.run, names)} labels={labels}/>
        <Side side={view.right} title={titles.right} name={runAlias(view.right.run, names)} labels={labels}/>
      </div>
      {view.calculations.length > 0 && <Panel title="Расчёты по документам">
        {view.calculations.map(row => {
          const delta = difference(row.left, row.right, view.left.currency_exponents, view.right.currency_exponents);
          return <div className="calc-pair" key={row.type + row.subject}>
            <CalcCell calc={row.left} side={view.left} title={titles.left} labels={labels}/>
            <div className="calc-diff">
              <div className="small muted">Разница: вторая сумма минус первая</div>
              <div className="strong">{delta === null ? "Суммы несопоставимы"
                : `${delta.value > 0 ? "+" : ""}${money(delta.value, delta.currency, view.right.currency_exponents)}`}</div>
            </div>
            <CalcCell calc={row.right} side={view.right} title={titles.right} labels={labels}/>
          </div>;
        })}
      </Panel>}
      <Panel title="Различия в документах" extra={<DiagnosticDownload data={view}/>}>
        {view.changes.length === 0 ? <Empty title="Изменений в документах нет"/>
          : <table className="diff">
            <thead><tr><th scope="col">Документ</th><th scope="col">Что изменилось</th>
              <th scope="col">{titles.left}</th><th scope="col">{titles.right}</th></tr></thead>
            <tbody>{view.changes.map(row => <tr key={`${row.key}/${row.field ?? ""}`}>
              <th scope="row">{row.key === "manifest" ? "Сведения о проверке"
                : <DocumentLink runId={row.field === null && row.after == null ? view.left.run.run_id : view.right.run.run_id}
                    record={row.key} labels={labels}/>}</th>
              <td>{row.field ? fieldName(row.field) : "Наличие записи"}</td>
              <td>{changeValue(row, row.before)}</td><td>{changeValue(row, row.after)}</td>
            </tr>)}</tbody>
          </table>}
      </Panel>
    </>}
  </div>;
}

function changeValue(row: Change, value: unknown) {
  if (row.field === null) return value == null ? "Записи нет" : "Запись есть";
  if (row.field.endsWith("_id")) return value == null ? "—" : "Связь указана";
  return humanFact(value, row.field);
}

function Side({ side, title, name, labels }: {
  side: CompareSide; title: string; name: string; labels: Record<string, string>;
}) {
  const run = side.run;
  const complete = run.status === "completed";
  return <Panel title={title}>
    <h3><a href={runLink(run.run_id)}>{name}</a></h3>
    <p className="small muted">{periodName(run)} · {auditorName(run.policy)}</p>
    <p className="small">{runStatus(run.status)}</p>
    <div className="side-claims">
      <div className="line-label">Ответ аудитора</div>
      {!complete ? <p className="muted">Итог проверки недоступен.</p>
        : side.empty ? <p className="muted">Без выводов</p>
        : side.claims.length === 0 ? <p className="muted">Ответ недоступен.</p>
        : side.claims.map((claim, index) => <div className="claim" key={index}>
          <div><b>{amount(claim.amount_minor, claim.currency, side.currency_exponents)}</b> · {findingName(claim.type)}</div>
          {claim.evidence_ids.length > 0 && <ul className="small">{claim.evidence_ids.map(key =>
            <li key={key}><DocumentLink runId={run.run_id} record={key} labels={labels}/></li>)}</ul>}
          {claim.verdict && <div className={claim.verdict.matched ? "ok-text" : "bad-text"}>
            {claim.verdict.matched ? "Засчитан оценщиком" : "Не засчитан оценщиком"}</div>}
          {claim.unconfirmed.length > 0 && <div className="warn">
            <b>Каких доказательств не хватает</b>
            <ul>{claim.unconfirmed.map(item => <li key={item.key}>
              <DocumentLink runId={run.run_id} record={item.key} labels={labels}/>
              {item.value_minor !== undefined && item.currency && <> · {amount(item.value_minor, item.currency, side.currency_exponents)}</>}
            </li>)}</ul>
          </div>}
        </div>)}
    </div>
    {complete && side.score && <div className="score-inline">
      Засчитано: {side.score.tp} · Не засчитано: {side.score.fp} · Пропущено: {side.score.fn} · Награда: <b>{reward(side.score.reward)}</b>
      {Number.isFinite(side.score.f1) && <div className="small">Мера точности и полноты: {reward(side.score.f1)}.</div>}
    </div>}
    <a className="btn btn-small" href={runLink(run.run_id)}>Открыть проверку</a>
  </Panel>;
}

function CalcCell({ calc, side, title, labels }: {
  calc: Derivation; side: CompareSide; title: string; labels: Record<string, string>;
}) {
  return <div className="calc-cell">
    <h3>{title}</h3>
    <p className="small muted">{findingName(calc.type)}</p>
    <ul className="small">
      {calc.operands.map(operand => <li key={operand.key + operand.field}>
        <DocumentLink runId={side.run.run_id} record={operand.key} labels={labels}/> · {operandName(operand, calc.type)}
        {" · "}{operand.value_minor !== undefined && operand.currency
          ? amount(operand.value_minor, operand.currency, side.currency_exponents)
          : typeof operand.value === "string" && /^-?\d+(?:\.\d+)?$/.test(operand.value) ? operand.value.replace(".", ",") : "—"}
      </li>)}
      {(calc.context ?? []).map(operand => <li key={operand.key}>
        <DocumentLink runId={side.run.run_id} record={operand.key} labels={labels}/>
        {" · "}После границы: {humanFact(operand.timestamp, "timestamp")}
      </li>)}
    </ul>
    {calc.rule.boundary_exclusive === true && typeof calc.rule.boundary === "string" &&
      <div className="small">Граница периода: {humanFact(calc.rule.boundary, "timestamp")}, не включается.</div>}
    {typeof calc.rule.local_date === "string" &&
      <div className="small">Дата валютного курса: {humanFact(calc.rule.local_date, "date")}.</div>}
    {calc.type === "fx_conversion" && calc.expected_minor != null && calc.recorded_minor != null &&
      <div className="small">По курсу: {amount(calc.expected_minor, calc.currency, side.currency_exponents)} · В учёте: {amount(calc.recorded_minor, calc.currency, side.currency_exponents)}</div>}
    {calc.index_check?.complete === false && <p className="warn">Перечень движений платежа расходится с проводками.</p>}
    <div className="strong">Итог: {amount(calc.result_minor, calc.currency, side.currency_exponents)}</div>
  </div>;
}
