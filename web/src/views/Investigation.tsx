import { useEffect, useMemo, useState } from "react";
import { api, type FindingView, type RunDetail, type Step } from "../api";
import { DerivationView, DiagnosticDownload, Empty, ErrorBox, KeyChip, LabelBadge, Panel, StatusPill } from "../components";
import { money, reward, REASONS } from "../format";
import { compareLink, go, recordLink, runLink } from "../navigation";
import { WorkspaceHeader } from "../workspace/AppShell";
import { humanText, named, recordName, referenceLabels, stepName, tableName } from "../workspace/copy";
import { auditorName, companyName, documentConfirmed, findingName, independentConfirmed, periodName } from "../workspace/model";
import { useResource } from "../workspace/resources";
import { RecordPane } from "./RecordPane";
import { RescoreForm } from "./RescoreForm";

export function Investigation({ runId, initialKey, documents = false, title, names, saved, onSave }: {
  runId: string; initialKey?: string; documents?: boolean; title?: string; names: ReadonlyMap<string, string>;
  saved: boolean; onSave: () => void;
}) {
  const response = useResource(runId, signal => api.run(runId, signal));
  const detail = response?.value;
  const [stepNumber, chooseStep] = useState<number | null>(null);
  const [focus, setFocus] = useState<number | null>(0);
  const labels = useMemo(() => referenceLabels(detail?.keys ?? []), [detail?.keys]);
  const recordLabels = useMemo(() => new Map(Object.entries(labels)), [labels]);
  const openKey = (key: string) => go(recordLink(runId, key));
  const focused = focus === null ? undefined : detail?.findings.find(f => f.index === focus);
  const evidenceKeys = new Set(focused?.finding.evidence_ids ?? []);

  useEffect(() => {
    if (initialKey && window.matchMedia("(max-width: 767px)").matches) {
      document.getElementById("current-document")?.scrollIntoView({ block: "start" });
    }
  }, [initialKey, detail]);

  if (response?.error) return <>
    <WorkspaceHeader title="Проверка недоступна"/>
    <ErrorBox error={response.error}/><a className="text-link" href="#/runs">К запускам</a>
  </>;
  if (!detail) return <div className="loading" role="status">Загружаем проверку…</div>;
  const run = detail.run;
  const parent = run.parent_run_id ?? run.derived_from?.run_id;
  const openedAt = (key: string) => detail.steps.find(step => step.opened_key === key)?.step ?? null;
  const listedAt = (key: string) => detail.steps.find(step => step.listed_keys.includes(key))?.step ?? null;
  const selectedStep = !initialKey && detail.steps.find(step => step.step === stepNumber);
  const exponents = detail.snapshot.manifest.currency_exponents ?? {};
  const related = Object.fromEntries(detail.findings.flatMap(view =>
    view.affected.flatMap(item => item.key ? [[item.id, item.key]] : [])));
  const subtitle = `${periodName(run)} · ${auditorName(run.policy)}`;

  if (documents) return <>
    <WorkspaceHeader title="Документы проверки" subtitle={`${title ?? companyName(run)} · ${periodName(run)}`}
      action={<a className="btn" href={runLink(runId)}>К проверке</a>}/>
    <DocumentsList runId={runId} keys={detail.keys} labels={labels}/>
  </>;

  return <div className="investigation">
    <WorkspaceHeader title={title ?? companyName(run)} subtitle={subtitle}
      action={<button type="button" className="btn" aria-pressed={saved} onClick={onSave}>
        {saved ? "Убрать из сохранённого" : "Сохранить проверку"}
      </button>}/>
    <div className="run-overview">
      <div className="run-meta">
        <StatusPill status={run.status}/><LabelBadge run={run}/>
        {run.steps !== undefined && <span>Шагов: <b>{run.steps}</b></span>}
        {run.inspection_steps !== undefined && <span>Запросов к документам: <b>{run.inspection_steps}</b></span>}
        {run.reward != null && <span>Награда: <b>{reward(run.reward)}</b></span>}
      </div>
      <div className="detail-actions">
        <a className="btn" href={`${runLink(runId)}/documents`}>Документы проверки</a>
        {detail.journal.available && <a className="btn" href={`${runLink(runId)}/replay`}>Повторить проверку</a>}
        {parent && <a className="btn" href={compareLink(parent, runId)}>Сравнить с исходной</a>}
        {detail.children.map(child => <a className="btn" key={child} href={compareLink(runId, child)}>
          {names.get(child) ? `Сравнить: ${names.get(child)}` : "Сравнить с новой проверкой"}</a>)}
      </div>
    </div>
    {run.change && <p className="banner banner-live">Условие изменено в новой проверке.
      {parent && <> <a href={runLink(parent)}>Открыть исходную</a></>}</p>}
    {run.derived_from && <p className="banner banner-copy">Доказательство заменено в копии ответа.
      {" "}<a href={runLink(run.derived_from.run_id)}>Открыть исходную</a></p>}
    {run.reason && <p className="banner banner-error">Проверка прервана.
      {run.journal_available ? " Доступна часть журнала действий." : " Журнал действий недоступен."}</p>}

    <div className="three">
      <Panel title={`Действия аудитора · ${detail.steps.length}`} className="col-steps">
        {!detail.steps.length ? <Empty title="Журнал действий недоступен"/> : <ol className="steps">
          {detail.steps.map(step => {
            const active = initialKey ? initialKey === step.opened_key : stepNumber === step.step;
            const evidence = (step.opened_key && evidenceKeys.has(step.opened_key)) ||
              step.listed_keys.some(key => evidenceKeys.has(key));
            return <li key={step.step}>
              <button type="button" className={`step ${active ? "step-active" : ""} ${step.ok ? "" : "step-error"}`}
                aria-pressed={active} onClick={() => {
                  if (step.opened_key) openKey(step.opened_key);
                  else { chooseStep(step.step); if (initialKey) go(runLink(runId)); }
                }}>
                <span className="step-n">{step.step}</span><span className="step-text">{stepName(step)}</span>
                {evidence && <span className="step-tag" title="Шаг содержит доказательство">Док.</span>}
              </button>
            </li>;
          })}
        </ol>}
      </Panel>

      <Panel title={initialKey ? recordName(initialKey) : selectedStep ? `Шаг ${selectedStep.step}` : "Документ"}
        className="col-record">
        <div id="current-document">
          {initialKey ? <RecordPane runId={runId} recordKey={initialKey} onOpen={openKey}
            highlightFields={focused?.derivation?.operands.filter(op => op.key === initialKey).map(op => op.field) ?? []}
            openedAt={openedAt(initialKey)} listedAt={listedAt(initialKey)} cited={evidenceKeys.has(initialKey)}
            canChange={run.status === "completed"} generator={run.engine?.generator} periodEnd={run.period?.end}
            relatedKeys={related}/> : selectedStep ? <StepView step={selectedStep} onOpen={openKey} labels={labels}/> :
            <Empty title="Выберите документ">
              <p>Откройте доказательство или действие аудитора, чтобы увидеть исходную запись.</p>
              <a className="text-link" href={`${runLink(runId)}/documents`}>Все документы проверки</a>
            </Empty>}
        </div>
      </Panel>

      <Panel title="Выводы проверки" className="col-findings">
        {detail.submission === null && <Empty title="Ответ аудитора недоступен"/>}
        {detail.empty_submission && <div className="no-findings">
          <div className="no-findings-title">Ошибок не заявлено</div>
          <p>Аудитор завершил проверку без выводов.</p>
          <ConsoleScan detail={detail} onOpen={openKey} labels={labels}/>
        </div>}
        {detail.findings.map(view => <FindingCard key={view.index} runId={runId} view={view} focused={focus === view.index}
          selectedKey={initialKey} onFocus={() => setFocus(focus === view.index ? null : view.index)}
          onOpen={openKey} keys={detail.keys} labels={labels} recordLabels={recordLabels}
          canRescore={run.status === "completed"} exponents={exponents}/>)}
        <ScoreBlock detail={detail}/>
        <DiagnosticDownload data={{ run, score: detail.score, findings: detail.findings, journal: detail.journal }}/>
      </Panel>
    </div>
  </div>;
}

function DocumentsList({ runId, keys, labels }: { runId: string; keys: string[]; labels: Record<string, string> }) {
  const [type, setType] = useState("");
  const [query, setQuery] = useState("");
  const tables = [...new Set(keys.map(key => key.split("/")[0]))];
  const current = type || (tables.includes("payments") ? "payments" : tables[0]);
  const found = keys.filter(key => key.split("/")[0] === current &&
    (!query.trim() || labels[key]?.toLocaleLowerCase("ru").includes(query.trim().toLocaleLowerCase("ru"))));
  return <Panel title="Записи проверки">
    {!tables.length ? <Empty title="Документы недоступны"/> : <>
      <div className="document-toolbar">
        <label>Раздел<select aria-label="Раздел" value={current} onChange={event => { setType(event.target.value); setQuery(""); }}>
          {tables.map(table => <option key={table} value={table}>{tableName(table)}</option>)}
        </select></label>
        <label>Поиск записи<input type="search" value={query} onChange={event => setQuery(event.target.value)}/></label>
      </div>
      {found.length ? <ul className="choice-list">
        {found.map(key => <li key={key}><a href={recordLink(runId, key)}>{labels[key]}</a></li>)}
      </ul> : <Empty title="Ничего не найдено"/>}
    </>}
  </Panel>;
}

export function StepView({ step, onOpen, labels = {} }: {
  step: Step; onOpen: (key: string) => void; labels?: Record<string, string>;
}) {
  const result = step.observation.result;
  const calculation = result && typeof result === "object" && "value" in result ? result.value : result;
  const calc = step.ok && step.action.action === "calculator" && typeof calculation === "string" &&
    /^[-+]?\d+(?:\.\d+)?$/.test(calculation) ? calculation : null;
  return <div className="step-view">
    <div className="kv"><span>Действие</span><b>{stepName(step)}</b></div>
    <div className="kv"><span>Результат</span><b>{step.ok ? "Выполнено" : "Не удалось выполнить"}</b></div>
    <div className="kv"><span>Осталось шагов</span><b>{step.remaining_steps ?? "—"}</b></div>
    {calc && <div className="kv"><span>Расчёт</span><b>{calc}</b></div>}
    {step.listed_keys.length > 0 && <div className="listed">
      <p>Записи на этой странице</p>
      {step.listed_keys.map(key => <KeyChip key={key} k={key} label={labels[key]} onOpen={onOpen}/>)}
    </div>}
    {step.opened_key && <KeyChip k={step.opened_key} label={labels[step.opened_key]} onOpen={onOpen}/>}
    <DiagnosticDownload data={step} label="Скачать запись действия"/>
  </div>;
}

function FindingCard({ runId, view, focused, selectedKey, onFocus, onOpen, keys, labels, recordLabels, canRescore, exponents }: {
  runId: string; view: FindingView; focused: boolean; selectedKey?: string; onFocus: () => void;
  onOpen: (key: string) => void; keys: string[]; labels: Record<string, string>; recordLabels: ReadonlyMap<string, string>;
  canRescore: boolean; exponents: Record<string, number>;
}) {
  const f = view.finding;
  const calc = view.derivation;
  const explanation = humanText(f.explanation);
  const [rescoring, setRescoring] = useState(false);
  const verifier = view.verifier;
  const independentlyVerified = independentConfirmed(view);
  return <article className={`finding ${focused ? "finding-focused" : ""}`}>
    <header className="finding-head"><h3 className="finding-type">{findingName(f.type)}</h3></header>
    <div className="line line-agent">
      <span className="line-label">Заявлено</span>
      <button type="button" className="amount" aria-expanded={focused} onClick={onFocus}>
        {money(f.claimed_amount_minor, f.currency, exponents)}
      </button>
      <span className="small muted">{focused ? "Скрыть расчёт" : "Показать расчёт"}</span>
    </div>
    <div className="evidence">
      <div className="small muted">Доказательства · {view.evidence.length}</div>
      {view.evidence.map(item => <div key={item.key} className={`evidence-row ${selectedKey === item.key ? "evidence-active" : ""}`}>
        <KeyChip k={item.key} label={labels[item.key]} onOpen={item.exists ? onOpen : undefined} active={selectedKey === item.key} missing={!item.exists}/>
        <small>{!item.exists ? "Запись недоступна" : item.opened_at_step ? `Открыто на шаге ${item.opened_at_step}` :
          item.listed_at_step ? `Получено на шаге ${item.listed_at_step}` : "В журнале нет открытия"}</small>
      </div>)}
    </div>
    {explanation && <p className="explanation">{explanation}</p>}
    {focused && calc && <DerivationView calc={calc} exponents={exponents} onOpen={onOpen} cited={f.evidence_ids} activeKey={selectedKey}/>}
    {focused && view.uncited_operands.length > 0 && <div className="warn">
      В ответе не хватает доказательств.{" "}
      {view.uncited_operands.map(key => <KeyChip key={key} k={key} label={labels[key]} onOpen={onOpen} missing/>)}
    </div>}
    <div className="verdicts">
      <div className="line"><span className="line-label">Оценщик</span>
        {view.verdict ? <span className={view.verdict.matched ? "ok-text" : "bad-text"}>
          {view.verdict.matched ? "Вывод засчитан" : `Вывод не засчитан. ${named(REASONS, view.verdict.reason) ?? "Причина не опубликована."}`}
          {view.verdict.source === "aggregate" && <small> По общей оценке проверки.</small>}
        </span> : <span className="muted">Отдельный вердикт по выводу недоступен</span>}
      </div>
      <div className="line"><span className="line-label">По документам</span>
        {calc ? <span className={documentConfirmed(view) ? "ok-text" : "muted"}>
          {money(calc.result_minor, calc.currency, exponents)} · {documentConfirmed(view) ? "Расчёт подтверждён" : "Расчёт требует проверки"}
        </span> : <span className="muted">Расчёт для этого вывода недоступен</span>}
      </div>
      <div className="line"><span className="line-label">Независимая сверка</span>
        {independentlyVerified && verifier.currency
          ? <span className="ok-text">Независимо подтверждено · {money(verifier.amount_minor, verifier.currency, exponents)}</span>
          : <span className="muted">{verifier?.connected !== true ? "Независимая сверка не выполнена" :
            verifier.currency_matches === false ? "Валюта не совпадает" :
            verifier.agrees === false ? "Вывод не подтверждён сверкой" : "Результат сверки недоступен"}</span>}
      </div>
    </div>
    {canRescore && <div className="finding-actions">
      {rescoring ? <RescoreForm runId={runId} findingIndex={view.index} evidence={f.evidence_ids} keys={keys} recordLabels={recordLabels}
        onCancel={() => setRescoring(false)}/> : <button type="button" className="btn" onClick={() => setRescoring(true)}>
        Заменить доказательство в копии
      </button>}
    </div>}
  </article>;
}

function ScoreBlock({ detail }: { detail: RunDetail }) {
  const s = detail.score;
  if (!s) return null;
  return <div className="score">
    <div className="score-title">Итог оценщика</div>
    <div className="score-grid">
      <div><span>Подтверждено</span><b>{s.tp}</b></div>
      <div><span>Ложные выводы</span><b>{s.fp}</b></div>
      <div><span>Пропущено</span><b>{s.fn}</b></div>
      <div><span>F1 проверки</span><b>{reward(s.f1)}</b></div>
      <div><span>Запросов</span><b>{s.inspection_steps ?? "—"}</b></div>
      <div><span>Награда</span><b>{reward(s.reward)}</b></div>
    </div>
    {s.tp === 0 && s.fp === 0 && s.fn === 0 && (
      <p className="small muted">F1 равна 1: в эталоне нет ошибок и аудитор не заявил ни одной.</p>
    )}
  </div>;
}

function ConsoleScan({ detail, onOpen, labels }: {
  detail: RunDetail; onOpen: (key: string) => void; labels: Record<string, string>;
}) {
  const exponents = detail.snapshot.manifest.currency_exponents ?? {};
  return <div className="console-scan">
    <p>{detail.console_scan.length ? "Расчёт по документам выявил расхождения, которых нет в ответе." :
      "Расчёт зачёта и валютных оценок не выявил расхождений в пределах этих проверок."}</p>
    {detail.console_scan.map(row => <div key={row.type + row.subject}>
      <KeyChip k={row.subject} label={labels[row.subject]} onOpen={onOpen}/>{" "}
      {findingName(row.type)} · {money(Math.abs(row.result_minor), row.currency, exponents)}
    </div>)}
    <p>{detail.verifier.connected ? `Независимая сверка · Расхождений: ${detail.verifier.findings.length}` :
      "Независимая сверка не выполнена"}</p>
    {detail.verifier.findings.map((row, index) => <div key={index}>
      {findingName(row.type)} · {money(row.claimed_amount_minor, row.currency, exponents)}
      {row.evidence_ids.map(key => <KeyChip key={key} k={key} label={labels[key]} onOpen={onOpen}/>)}
    </div>)}
  </div>;
}
