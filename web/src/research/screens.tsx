import { useEffect, useState } from "react";
import "./research.css";
import { ApiError } from "../api";
import { money, when } from "../format";
import { WorkspaceHeader } from "../workspace/AppShell";
import { Icon } from "../workspace/Icon";
import { findingName } from "../workspace/model";
import { recordName } from "../workspace/copy";
import {
  research, type AttemptDetail, type AttemptSummary, type ControllerStatus, type Experiment, type Family,
  type ReductionSide, type Training, type VariantMetrics,
} from "./api";
import {
  attemptStatus, checkStatus, count, decimal, duration, experimentStatus, FAILURE_KINDS, HIGHER_IS_BETTER, metricName, originName,
  relationName, researchError, share, signed, splitName, TRAINING_KIND_NOTE, trainingKind, trainingState, usd,
} from "./copy";
import {
  ActionList, claimText, DocButton, docLink, DocumentPanel, Failed, FixtureNote, labelsFor, Loading, NextStep,
  Origin, ResearchPath, rlink, ScoreLine, Stale, stageLinks, TechDetails, useResearch, Verdict,
} from "./parts";

const backToResearch = <a className="text-link" href="#/research">К экспериментам<Icon name="arrow"/></a>;

function modelText(experiment: Experiment): string {
  const models = (experiment.agents ?? []).map(agent => agent.kind === "scripted_fixture" || !agent.model?.model_id
    ? "Скриптовая политика фикстуры (модель не вызывается)" : [agent.model.provider, agent.model.model_id].filter(Boolean).join(" · "));
  return [...new Set(models)].join("; ") || "Модель не указана";
}

function ProgressBlock({ experiment }: { experiment: Experiment }) {
  const p = experiment.progress ?? {};
  const failed = p.failed ?? {};
  const failedTotal = Object.values(failed).reduce<number>((sum, value) => sum + (Number.isInteger(value) ? value! : 0), 0);
  const planned = Number.isInteger(p.planned) ? p.planned! : null;
  const done = Number.isInteger(p.completed) ? p.completed! : null;
  const percent = planned && done !== null ? Math.min(100, Math.round(done / planned * 100)) : null;
  return <section className="research-panel" aria-labelledby="progress-title">
    <h2 id="progress-title">Ход эксперимента</h2>
    {percent !== null ? <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={planned!} aria-valuenow={done!}
      aria-label={`Выполнено попыток: ${done} из ${planned}`}><span style={{ width: `${percent}%` }}/></div> : null}
    <dl className="facts">
      <div><dt>Выполнено попыток</dt><dd>{share(done, planned)}</dd></div>
      <div><dt>Выполняется сейчас</dt><dd>{count(p.running)}</dd></div>
      <div><dt>Неуспешных попыток</dt><dd>{count(failedTotal)}</dd></div>
      <div><dt>Время</dt><dd>{duration(experiment.timing?.elapsed_s)}</dd></div>
      <div><dt>Расходы</dt><dd>{experiment.cost?.usd != null ? usd(experiment.cost.usd) : "нет данных"}</dd></div>
      <div><dt>Токены (вход / выход)</dt><dd>{count(experiment.cost?.input_tokens)} / {count(experiment.cost?.output_tokens)}</dd></div>
    </dl>
    {failedTotal > 0 && <ul className="failure-kinds">{Object.entries(failed).filter(([, value]) => (value ?? 0) > 0).map(([kind, value]) =>
      <li key={kind}>{FAILURE_KINDS[kind] ?? "другая ошибка"}: {count(value)}</li>)}</ul>}
    {experiment.cost?.note && <p className="muted small">{experiment.cost.note}</p>}
  </section>;
}

function ControllerPanel({ experiment, controller }: { experiment: Experiment; controller: ControllerStatus }) {
  const [pending, setPending] = useState<"start" | "stop" | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const reason = !controller.connected ? "controller_not_connected"
    : controller.budget_configured !== true ? "budget_not_configured"
    : controller.api_key_configured !== true ? "api_key_missing" : null;
  const running = experiment.status === "running";
  async function act(action: "start" | "stop") {
    setPending(action);
    setMessage(null);
    try {
      await research.control(experiment.artifact_id, action);
      setMessage({ tone: "ok", text: action === "start" ? "Контроллер принял запуск." : "Контроллер принял остановку." });
    } catch (error) {
      setMessage({ tone: "error", text: researchError(error instanceof ApiError ? error.code : undefined) });
    } finally {
      setPending(null);
    }
  }
  return <section className="research-panel" aria-labelledby="control-title">
    <h2 id="control-title">Запуск</h2>
    <p className="run-kind">{controller.connected && running ? <><span className="live-dot" aria-hidden="true"/>Живой запуск</>
      : <><Icon name="document"/>Запись эксперимента: состояние на момент сохранения</>}</p>
    <div className="control-actions">
      <button className="primary-action" type="button" disabled={reason !== null || running || pending !== null}
        onClick={() => act("start")}>{pending === "start" ? "Запускаем…" : "Запустить"}</button>
      <button className="secondary-action" type="button" disabled={!controller.connected || !running || pending !== null}
        onClick={() => act("stop")}>{pending === "stop" ? "Останавливаем…" : "Остановить"}</button>
    </div>
    {reason && <p className="muted small">{researchError(reason)}</p>}
    {message && <p className={message.tone === "error" ? "inline-error" : "inline-ok"} role="status">{message.text}</p>}
    <p className="muted small">Ключ доступа к модели хранится только на сервере и в браузер не передаётся.</p>
  </section>;
}

export function ResearchHome() {
  const overview = useResearch("overview", signal => research.overview(signal));
  const status = useResearch("status", signal => research.status(signal));
  if (overview.loading) return <><WorkspaceHeader title="Исследование агентов"/><Loading label="Загружаем эксперименты"/></>;
  if (!overview.value) return <><WorkspaceHeader title="Исследование агентов"/><Failed error={overview.error} retry={overview.retry}/></>;
  const data = overview.value;
  const fixtures = data.experiments.some(e => e._provenance.kind !== "measured");
  return <>
    <WorkspaceHeader title="Исследование агентов" subtitle="Эксперименты, найденные слабости, учебные наборы и сравнение версий."/>
    <Stale at={overview.staleAt} retry={overview.retry}/>
    {status.value && !status.value.roots_configured && <div className="workspace-empty">
      <h2>Пока нет результатов исследований</h2>
      <p>Подключите каталог результатов эксперимента. Пока каталог не подключён, здесь ничего не показывается.</p>
      <details className="tech-details"><summary>Техническая помощь</summary>
        <pre>python -m auditlab.demo serve --research КАТАЛОГ{"\n"}python -m auditlab.demo serve --research-fixtures</pre>
      </details>
    </div>}
    {fixtures && <FixtureNote provenance={data.experiments.find(e => e._provenance.kind !== "measured")?._provenance}/>}
    {status.value?.controller && <p className="controller-line">{status.value.controller.connected
      ? <><Icon name="check"/>Контроллер экспериментов подключён</>
      : <><Icon name="help"/>Контроллер экспериментов не подключён: доступен просмотр сохранённых результатов</>}</p>}
    {status.value?.roots_configured !== false && !data.experiments.length && <div className="workspace-empty">
      <h2>Экспериментов нет</h2><p>В подключённом каталоге нет экспериментов в поддерживаемом формате.</p></div>}
    <ul className="experiment-list">{data.experiments.map(experiment =>
      <li key={experiment.artifact_id}><a href={rlink("experiments", experiment.artifact_id)} className="experiment-card">
        <span className="experiment-title"><strong>{experiment.title ?? "Эксперимент без названия"}</strong>
          <Origin provenance={experiment._provenance} compact/></span>
        <span className="experiment-meta">{experimentStatus(experiment.status)} · {share(experiment.progress?.completed, experiment.progress?.planned)} попыток
          · найдено слабостей: {count(experiment.failures)}</span>
        <span className="experiment-meta">{modelText(experiment)}</span>
        <span className="text-link">Открыть эксперимент<Icon name="arrow"/></span>
      </a></li>)}</ul>
    {(data.curricula.length > 0 || data.comparisons.length > 0) && <section className="research-panel">
      <h2>Учебные наборы и сравнения</h2>
      <ul className="choice-list">
        {data.curricula.map(item => <li key={item.artifact_id}><a href={rlink("curricula", item.artifact_id)}>
          <Icon name="folder"/><span><strong>{item.title ?? "Учебный набор"}</strong><small>Учебный набор</small></span><Icon name="arrow"/></a></li>)}
        {data.comparisons.map(item => <li key={item.artifact_id}><a href={rlink("comparisons", item.artifact_id)}>
          <Icon name="compare"/><span><strong>{item.title ?? "Сравнение версий"}</strong><small>Сравнение версий</small></span><Icon name="arrow"/></a></li>)}
      </ul>
    </section>}
    {data.incompatible.length > 0 && <section className="research-panel" aria-labelledby="incompatible-title">
      <h2 id="incompatible-title">Несовместимые результаты: {data.incompatible.length}</h2>
      <p className="muted">Эти файлы не показаны: их формат не поддерживается консолью или они повреждены. Они не учитываются ни в одном экране.</p>
      <TechDetails title="Список файлов и причины" data={data.incompatible}/>
    </section>}
  </>;
}

export function ExperimentScreen({ id }: { id: string }) {
  const view = useResearch(`experiment:${id}`, signal => research.experiment(id, signal));
  const live = view.value?.controller.connected === true && view.value.experiment.status === "running";
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(view.retry, 10000);
    return () => clearInterval(timer);
  }, [live]);
  if (view.loading) return <><WorkspaceHeader title="Эксперимент"/><Loading label="Загружаем эксперимент"/></>;
  if (!view.value) return <><WorkspaceHeader title="Эксперимент"/><Failed error={view.error} retry={view.retry} back={backToResearch}/></>;
  const { experiment, attempts, failures, controller, missing_attempts } = view.value;
  const links = stageLinks(view.value.links);
  const stop = experiment.stop_reason;
  return <>
    <WorkspaceHeader title={experiment.title ?? "Эксперимент"} subtitle={`${experimentStatus(experiment.status)} · ${modelText(experiment)}`}
      action={<div className="heading-tools"><Origin provenance={experiment._provenance}/>
        <button className="secondary-action" type="button" onClick={view.retry}>Обновить</button></div>}/>
    <Stale at={view.staleAt} retry={view.retry}/>
    <ResearchPath current="experiment" links={links}/>
    <FixtureNote provenance={experiment._provenance}/>
    {stop && <p className="inline-error" role="alert">{researchError(stop.code)} {experiment.status === "stopped" &&
      "Выполненные попытки сохранены; оставшиеся не запускались."}</p>}
    <div className="research-grid">
      <section className="research-panel" aria-labelledby="setup-title">
        <h2 id="setup-title">Конфигурация</h2>
        <dl className="facts">
          <div><dt>Модель</dt><dd>{modelText(experiment)}</dd></div>
          <div><dt>Варианты агента</dt><dd>{(experiment.agents ?? []).map(a => a.label ?? "Вариант").join(", ") || "—"}</dd></div>
          <div><dt>Режим проверки</dt><dd>{experiment.mode?.official ? "Официальная приёмка" : "Исследовательский протокол (не официальная приёмка)"}</dd></div>
          <div><dt>Лимиты</dt><dd>{count(experiment.mode?.action_limit)} действий · {count(experiment.mode?.time_limit_s)} с на попытку</dd></div>
          <div><dt>Повторы</dt><dd>{count(experiment.repetitions)}</dd></div>
          <div><dt>Бюджет</dt><dd>{experiment.budget?.limit != null ? `${usd(experiment.budget.spent)} из ${usd(experiment.budget.limit)}`
            : experiment.budget?.note ?? "Не задан: платный запуск невозможен"}</dd></div>
        </dl>
      </section>
      <ControllerPanel experiment={experiment} controller={controller}/>
    </div>
    <ProgressBlock experiment={experiment}/>
    <section className="research-panel" aria-labelledby="failures-title">
      <h2 id="failures-title">Найденные слабости</h2>
      {!failures.length ? <p className="muted">Подтверждённых слабостей нет. Это не означает, что агент не ошибается: смотрите попытки ниже.</p>
        : <ul className="choice-list">{failures.map(item => <li key={item.artifact_id}><a href={rlink("failures", item.artifact_id)}>
          <Icon name="search"/><span><strong>{item.title ?? findingName(item.finding_type ?? "")}</strong>
            <small>{findingName(item.finding_type ?? "")} · ошибка в {share(item.frequency?.failed, item.frequency?.total)} попыток</small></span>
          <Icon name="arrow"/></a></li>)}</ul>}
    </section>
    <AttemptsTable attempts={attempts} experiment={experiment}/>
    {missing_attempts.length > 0 && <p className="inline-error">Не найдены файлы {missing_attempts.length} попыток, перечисленных в эксперименте.</p>}
    <NextStep href={links.failure} label="Разобрать слабость" missing="Слабостей для разбора пока нет."/>
    <TechDetails data={{ artifact_id: experiment.artifact_id, protocol_id: experiment.protocol_id, source_sha: experiment.source_sha, split: experiment.split }}/>
  </>;
}

function AttemptsTable({ attempts, experiment }: { attempts: AttemptSummary[]; experiment: Experiment }) {
  const variant = (id?: string) => experiment.agents?.find(a => a.variant_id === id)?.label ?? "Вариант агента";
  return <section className="research-panel" aria-labelledby="attempts-title">
    <h2 id="attempts-title">Попытки: {attempts.length}</h2>
    {!attempts.length ? <p className="muted">Сохранённых попыток нет.</p> :
      <table className="research-table">
        <thead><tr><th scope="col">Компания</th><th scope="col">Вариант</th><th scope="col">Повтор</th><th scope="col">Состояние</th><th scope="col">Итог оценки</th><th scope="col"><span className="sr-only">Действие</span></th></tr></thead>
        <tbody>{attempts.map(a => <tr key={a.artifact_id}>
          <td data-label="Компания">{a.company_label ?? "Компания"}</td>
          <td data-label="Вариант">{variant(a.variant_id)}</td>
          <td data-label="Повтор">{count(a.repetition)}</td>
          <td data-label="Состояние">{attemptStatus(a.status)}</td>
          <td data-label="Итог оценки"><Verdict attempt={a}/></td>
          <td><a className="text-link" href={rlink("attempts", a.artifact_id)}>Траектория<Icon name="arrow"/></a></td>
        </tr>)}</tbody>
      </table>}
  </section>;
}

function NeededDocs({ attempt, base, labels, current }: {
  attempt: AttemptDetail; base: string; labels: Record<string, string>; current?: string;
}) {
  const rows = attempt.needed_received ?? [];
  if (!rows.length) return <p className="muted">Расчёт по документам недоступен, поэтому список необходимых документов не построен.</p>;
  const steps = new Map(attempt.received.map(row => [row.key, row.step]));
  return <ul className="needed-list">{rows.map(row => <li key={row.key} className={row.received ? "got" : "missing"}>
    <span className="needed-mark">{row.received ? <><Icon name="check"/><span className="sr-only">получен</span></> : <span aria-hidden="true">✕</span>}</span>
    <DocButton label={labels[row.key] ?? recordName(row.key)} href={docLink(base, row.key)} current={current === row.key}/>
    <small>{row.received ? `получен на шаге ${steps.get(row.key)}` : "агент не получал"}</small>
  </li>)}</ul>;
}

function Submission({ attempt, base, labels, current }: { attempt: AttemptDetail; base: string; labels: Record<string, string>; current?: string }) {
  const findings = attempt.submission?.findings ?? [];
  if (attempt.status && attempt.status !== "completed") return <p className="muted">Ответа нет: попытка не завершена ({attemptStatus(attempt.status).toLocaleLowerCase("ru")}). Финансовая оценка не выставляется.</p>;
  if (attempt.evaluation_hidden) return <p className="muted">Ответ скрыт до окончания контроля.</p>;
  if (attempt.submission === null) return <p className="muted">Ответ не сохранён.</p>;
  if (!findings.length) return <p className="muted">Агент сдал пустой ответ: выводов нет.</p>;
  return <ul className="submitted">{findings.map((finding, index) => <li key={index}>
    <strong>{claimText(finding, attempt.currency_exponents)}</strong>
    <div className="doc-row">{(finding.evidence_ids ?? []).map(key =>
      <DocButton key={key} label={labels[key] ?? recordName(key)} href={docLink(base, key)} current={current === key}/>)}</div>
  </li>)}</ul>;
}

export function FailureScreen({ id, attemptId, docKey }: { id: string; attemptId?: string; docKey?: string }) {
  const view = useResearch(`failure:${id}`, signal => research.failure(id, signal));
  if (view.loading) return <><WorkspaceHeader title="Слабость агента"/><Loading label="Загружаем слабость"/></>;
  if (!view.value) return <><WorkspaceHeader title="Слабость агента"/><Failed error={view.error} retry={view.retry} back={backToResearch}/></>;
  const { failure, attempts, situation, independent, needed, snapshot_id } = view.value;
  const live = attempts.filter(a => !a.missing);
  const chosen = live.find(a => a.artifact_id === attemptId) ?? live.find(a => a.evaluation?.tp === 0) ?? live[0];
  const base = chosen ? rlink("failures", id, "attempt", chosen.artifact_id) : rlink("failures", id);
  const allKeys = [...needed, ...live.flatMap(a => (a.submission?.findings ?? []).flatMap(f => f.evidence_ids ?? []))];
  const labels = labelsFor(allKeys, situation);
  const calc = situation?.calculations[0];
  const s = calc?.summary;
  const exponents = situation?.currency_exponents ?? {};
  const links = stageLinks(view.value.links);
  const missing = chosen?.needed_received?.filter(row => !row.received) ?? [];
  return <>
    <WorkspaceHeader title={failure.title ?? "Слабость агента"}
      subtitle={`${findingName(failure.finding_type ?? "")} · ошибка в ${share(failure.frequency?.failed, failure.frequency?.total)} попыток`}
      action={<Origin provenance={failure._provenance}/>}/>
    <Stale at={view.staleAt} retry={view.retry}/>
    <ResearchPath current="failure" links={links}/>
    <FixtureNote provenance={failure._provenance}/>
    <div className="research-grid">
      <section className="research-panel situation" aria-labelledby="situation-title">
        <h2 id="situation-title">Финансовая ситуация</h2>
        {s ? <dl className="calculation situation-calc">
          <div><dt>Поступило по платежу</dt><dd>{money(s.received_minor, s.currency, exponents)}</dd></div>
          <div><dt>Распределено до границы</dt><dd>{money(s.allocated_minor, s.currency, exponents)}</dd></div>
          <div><dt>Граница периода</dt><dd>{s.boundary ? `${when(s.boundary)}${s.boundary_exclusive ? " (не включительно)" : ""}` : "—"}</dd></div>
          {s.after_boundary.length > 0 && <div><dt>Зачтено после границы</dt><dd>{s.after_boundary.map(row =>
            money(row.value_minor, s.currency, exponents)).join(", ")}</dd></div>}
          <div className="balance"><dt>Нераспределено на отчётную дату</dt><dd>{money(s.residual_minor, s.currency, exponents)}</dd></div>
        </dl> : <p className="muted">Расчёт консоли для этого типа недоступен.</p>}
        <p className="muted small">Расчёт консоли по публичным документам. Правило: {recordName(calc?.rule.key ?? "policy/allocation").toLocaleLowerCase("ru")}.</p>
      </section>
      <section className="research-panel" aria-labelledby="check-title">
        <h2 id="check-title">Проверка ответа</h2>
        <dl className="facts">
          <div><dt>Независимая проверка</dt><dd>{independent?.findings.length
            ? independent.findings.map(f => money(f.claimed_amount_minor, f.currency, exponents)).join(", ")
            : independent ? "Расхождений этого типа не найдено" : "Не выполнена"}</dd></div>
          <div><dt>Ответ выбранной попытки</dt><dd>{chosen?.claims.length ? chosen.claims.map(c => money(c.claimed_amount_minor, c.currency, exponents)).join(", ") : "Выводов нет"}</dd></div>
          <div><dt>Оценщик</dt><dd>{chosen ? <Verdict attempt={chosen}/> : "—"}</dd></div>
        </dl>
        {independent && <p className="muted small">Независимая проверка: {independent.engine ?? "проверяющий"}; результат «{checkStatus(independent.status)}».</p>}
        {failure.criterion?.text && <p className="criterion"><b>Критерий слабости.</b> {failure.criterion.text}</p>}
      </section>
    </div>
    <section className="research-panel" aria-labelledby="repeats-title">
      <h2 id="repeats-title">Повторы</h2>
      <ul className="attempt-tabs" role="list">{live.map(a => <li key={a.artifact_id}>
        <a href={rlink("failures", id, "attempt", a.artifact_id)} aria-current={a.artifact_id === chosen?.artifact_id ? "true" : undefined}>
          <span>Повтор {count(a.repetition)}</span><Verdict attempt={a}/></a></li>)}</ul>
      <p className="muted small">{failure.interval?.low != null && failure.interval.high != null
        ? `Интервал частоты: ${decimal(failure.interval.low)}–${decimal(failure.interval.high)} (${failure.interval.method ?? "метод не указан"}).`
        : failure.interval?.reason ?? "Интервал неопределённости не рассчитан."}</p>
    </section>
    {chosen && <div className="investigation-grid">
      <section className="research-panel" aria-labelledby="actions-title">
        <h2 id="actions-title">Действия агента</h2>
        <p className="run-kind"><Icon name="document"/>Сохранённая траектория · {attemptStatus(chosen.status)}</p>
        <ActionList steps={chosen.steps_detail} received={chosen.received} labels={labels}/>
      </section>
      <section className="research-panel" aria-labelledby="docs-title">
        <h2 id="docs-title">Документы, нужные для расчёта</h2>
        <NeededDocs attempt={chosen} base={base} labels={labels} current={docKey}/>
        <h3>Отправленный вывод</h3>
        <Submission attempt={chosen} base={base} labels={labels} current={docKey}/>
        <ScoreLine evaluation={chosen.evaluation}/>
        <h3>Что видно из наблюдений</h3>
        <p>{missing.length
          ? `Агент не получил ${missing.length} из ${needed.length} документов, нужных для расчёта: ${missing.map(row => labels[row.key] ?? recordName(row.key)).join(", ")}.`
          : "Агент получил все документы, нужные для расчёта."} Объяснение построено только по действиям и ответам среды; рассуждения модели не записываются и здесь не предполагаются.</p>
        {(failure.alternatives ?? []).length > 0 && <><h3>Альтернативные объяснения</h3>
          <ul>{failure.alternatives!.map(text => <li key={text}>{text}</li>)}</ul></>}
      </section>
    </div>}
    {docKey && <DocumentPanel snapshotId={chosen?.snapshot_id ?? snapshot_id} docKey={docKey} label={labels[docKey]} closeHref={base}/>}
    <NextStep href={links.reduction} label="Сокращённый случай" missing="Сокращённый случай для этой слабости не опубликован."/>
    {chosen && <TechDetails data={chosen.raw}/>}
  </>;
}

export function AttemptScreen({ id, docKey }: { id: string; docKey?: string }) {
  const view = useResearch(`attempt:${id}`, signal => research.attempt(id, signal));
  if (view.loading) return <><WorkspaceHeader title="Траектория попытки"/><Loading label="Загружаем попытку"/></>;
  if (!view.value) return <><WorkspaceHeader title="Траектория попытки"/><Failed error={view.error} retry={view.retry} back={backToResearch}/></>;
  const attempt = view.value;
  const base = rlink("attempts", id);
  const keys = [...attempt.received.map(r => r.key), ...(attempt.submission?.findings ?? []).flatMap(f => f.evidence_ids ?? [])];
  const labels = labelsFor(keys);
  const experiment = typeof attempt.raw.experiment_id === "string" ? attempt.raw.experiment_id : undefined;
  return <>
    <WorkspaceHeader title={attempt.company_label ?? "Траектория попытки"} subtitle={`Повтор ${count(attempt.repetition)} · ${attemptStatus(attempt.status)}`}
      action={<Origin provenance={attempt._provenance}/>}/>
    <FixtureNote provenance={attempt._provenance}/>
    {attempt.status !== "completed" && <p className="inline-error" role="status">{attemptStatus(attempt.status)}. {attempt.error?.message ?? "Причина не указана."} Финансовой оценки у этой попытки нет.</p>}
    <div className="investigation-grid">
      <section className="research-panel"><h2>Действия агента</h2>
        <p className="run-kind"><Icon name="document"/>Сохранённая траектория</p>
        <ActionList steps={attempt.steps_detail} received={attempt.received} labels={labels}/></section>
      <section className="research-panel"><h2>Полученные документы: {attempt.received.length}</h2>
        <div className="doc-row">{attempt.received.filter(r => !r.key.startsWith("ledger/")).slice(0, 40).map(row =>
          <DocButton key={row.key} label={labels[row.key] ?? recordName(row.key)} href={docLink(base, row.key)} current={docKey === row.key}/>)}</div>
        <p className="muted small">Проводок получено: {attempt.received.filter(r => r.key.startsWith("ledger/")).length}.</p>
        <h3>Отправленный вывод</h3>
        <Submission attempt={attempt} base={base} labels={labels} current={docKey}/>
        <p><Verdict attempt={attempt}/> <ScoreLine evaluation={attempt.evaluation}/></p>
      </section>
    </div>
    {docKey && <DocumentPanel snapshotId={attempt.snapshot_id} docKey={docKey} label={labels[docKey]} closeHref={base}/>}
    {experiment && <NextStep href={rlink("experiments", experiment)} label="Вернуться к эксперименту"/>}
    <TechDetails data={attempt.raw}/>
  </>;
}

function SideCard({ title, name, side, failedText, base, current, findingType }: {
  title: string; name: "original" | "reduced"; side: ReductionSide; failedText: string; base: string; current?: string;
  findingType?: string | null;
}) {
  const findings = side.validation?.independent?.findings ?? [];
  const focus = findings.find(f => f.type === findingType) ?? findings[0];
  const exponents = side.snapshot?.currency_exponents ?? {};
  const labels = labelsFor(findings.flatMap(f => f.evidence_ids ?? []));
  return <section className="research-panel reduction-side">
    <h2>{title}</h2>
    <dl className="facts">
      <div><dt>Проводок</dt><dd>{count(side.ledger_rows)}</dd></div>
      <div><dt>Документов</dt><dd>{count(side.documents)}</dd></div>
      <div><dt>Независимая проверка</dt><dd>{side.validation?.missing || !side.validation ? "Не выполнена"
        : findings.length ? findings.map(f => `${findingName(f.type)} · ${money(f.claimed_amount_minor, f.currency, exponents)}`).join("; ")
        : "Расхождений нет"}</dd></div>
      <div><dt>Ошибка агента</dt><dd>{failedText}</dd></div>
    </dl>
    {focus && <p className="muted small">Доказательства независимой проверки: {findingName(focus.type).toLocaleLowerCase("ru")}</p>}
    {focus && <div className="doc-row">{focus.evidence_ids?.map(key =>
      <DocButton key={key} label={labels[key] ?? recordName(key)} href={docLink(`${base}/side/${name}`, key)}
        current={current === key}/>)}</div>}
  </section>;
}

function expectedText(family: Family): string {
  if (!family.expected.length) return "Вывод о нераспределённом платеже не нужен";
  return family.expected.map(([type, amount, currency]) => `${findingName(type)} · ${money(amount, currency, family.currency_exponents)}`).join("; ");
}

function actualVerdict(family: Family, attempt: AttemptSummary): { text: string; tone: string } {
  if (attempt.missing) return { text: "Попытка не найдена", tone: "unknown" };
  if (!attempt.evaluation) return { text: "Оценка недоступна", tone: "unknown" };
  const claimed = attempt.claims.filter(c => family.expected.length ? true : c.type === "payment_allocation");
  if (!family.expected.length) return claimed.length ? { text: "Ложное обвинение", tone: "fail" } : { text: "Ответ изменился верно", tone: "pass" };
  return attempt.evaluation.tp > 0 ? { text: "Ошибка найдена", tone: "pass" } : { text: "Ошибка сохранилась в ответе агента", tone: "fail" };
}

export function ReductionScreen({ id, side, docKey }: { id: string; side?: string; docKey?: string }) {
  const view = useResearch(`reduction:${id}`, signal => research.reduction(id, signal));
  if (view.loading) return <><WorkspaceHeader title="Сокращённый случай"/><Loading label="Загружаем сокращённый случай"/></>;
  if (!view.value) return <><WorkspaceHeader title="Сокращённый случай"/><Failed error={view.error} retry={view.retry} back={backToResearch}/></>;
  const { reduction, original, reduced, repeats, families } = view.value;
  const base = rlink("reductions", id);
  const links = stageLinks(view.value.links);
  const r = (name: string) => repeats[name] ? share(repeats[name].failed, repeats[name].total) + " попыток" : "нет повторов";
  const activeSide = side === "original" ? original : side === "reduced" ? reduced : undefined;
  return <>
    <WorkspaceHeader title="Сокращённый случай" subtitle="Тот же финансовый дефект на сокращённом наборе документов."
      action={<Origin provenance={reduction._provenance}/>}/>
    <Stale at={view.staleAt} retry={view.retry}/>
    <ResearchPath current="reduction" links={links}/>
    <FixtureNote provenance={reduction._provenance}/>
    <div className="research-grid equal">
      <SideCard title="Исходный случай" name="original" side={original} findingType={view.value.finding_type} failedText={r("original")} base={base} current={side === "original" ? docKey : undefined}/>
      <SideCard title="Сокращённый случай" name="reduced" side={reduced} findingType={view.value.finding_type} failedText={r("reduced")} base={base} current={side === "reduced" ? docKey : undefined}/>
    </div>
    {activeSide && docKey && <DocumentPanel snapshotId={activeSide.snapshot_id} docKey={docKey} closeHref={base}/>}
    {(reduction.transformations ?? []).length > 0 && <section className="research-panel"><h2>Что изменено при сокращении</h2>
      <ul>{reduction.transformations!.map(text => <li key={text}>{text}</li>)}</ul>
      <p className="muted small">Сохранение ошибки подтверждено независимой проверкой сокращённого случая и повторными попытками.</p></section>}
    <section className="research-panel" aria-labelledby="families-title">
      <h2 id="families-title">Семейство случая: ожидаемое и фактическое изменение ответа</h2>
      {!families.length ? <p className="muted">Варианты семейства не опубликованы.</p> :
        <table className="research-table">
          <thead><tr><th scope="col">Вариант</th><th scope="col">Что изменилось</th><th scope="col">Ожидаемый ответ</th><th scope="col">Фактический ответ агента</th></tr></thead>
          <tbody>{families.map(family => <tr key={family.relation + family.title}>
            <td data-label="Вариант"><strong>{relationName(family.relation)}</strong><br/><small className="muted">{family.title}</small></td>
            <td data-label="Что изменилось">{family.expected_change ?? "—"}</td>
            <td data-label="Ожидаемый ответ">{expectedText(family)}</td>
            <td data-label="Фактический ответ агента"><ul className="plain family-actual">{family.actual.map(attempt => {
              const verdict = actualVerdict(family, attempt);
              return <li key={attempt.artifact_id}>
                <small className="muted">{view.value!.variants[attempt.variant_id ?? ""] ?? "Вариант агента"}</small>
                <span className={`verdict verdict-${verdict.tone}`}>{verdict.text}</span>
                <span className="small">{attempt.claims.length ? attempt.claims.map(c => claimText(c, family.currency_exponents)).join("; ") : "Выводов нет"}</span>
                {!attempt.missing && <a className="text-link" href={rlink("attempts", attempt.artifact_id)}>Траектория<Icon name="arrow"/></a>}
              </li>;
            })}</ul></td>
          </tr>)}</tbody>
        </table>}
      <p className="muted small">Для переименованных документов траектория агента может отличаться; сравнивается финансовый ответ.</p>
    </section>
    <NextStep href={links.curriculum} label="Учебный набор" missing="Учебный набор для этого случая не опубликован."/>
    <TechDetails data={{ reduction: reduction.artifact_id, method: reduction.method, original: original.snapshot_ref, reduced: reduced.snapshot_ref }}/>
  </>;
}

function TrainingCard({ training }: { training: Training }) {
  const tone = training.state === "completed" ? "pass" : training.state === "failed" || training.state === "not_available" ? "fail" : "unknown";
  const result = training.result;
  return <li className="training-card">
    <div className="training-head"><strong>{trainingKind(training.kind)}</strong><span className={`verdict verdict-${tone}`}>{trainingState(training.state)}</span></div>
    <p className="muted small">{TRAINING_KIND_NOTE[training.kind ?? ""] ?? ""}</p>
    {training.state === "completed" && result ? <p>{typeof result.summary === "string" ? result.summary : "Получена новая версия."}
      {training.kind === "weights" && typeof result.model_id === "string" && <> Версия модели: {result.model_id}.</>}</p>
      : <p>{training.error?.message ?? (training.state === "running" || training.state === "queued" ? "Задание ещё не завершено; результата пока нет." : "Результата нет.")}</p>}
    <dl className="facts compact">
      <div><dt>Исходная модель</dt><dd>{[training.base_model?.provider, training.base_model?.model_id].filter(Boolean).join(" · ") || "—"}</dd></div>
      <div><dt>Бюджет</dt><dd>{training.budget?.limit != null ? `${usd(training.budget.spent)} из ${usd(training.budget.limit)}` : "не применяется"}</dd></div>
    </dl>
    <TechDetails data={{ artifact_id: training.artifact_id, job: training.job, result: training.result, error: training.error }}/>
  </li>;
}

export function CurriculumScreen({ id }: { id: string }) {
  const view = useResearch(`curriculum:${id}`, signal => research.curriculum(id, signal));
  if (view.loading) return <><WorkspaceHeader title="Учебный набор"/><Loading label="Загружаем учебный набор"/></>;
  if (!view.value) return <><WorkspaceHeader title="Учебный набор"/><Failed error={view.error} retry={view.retry} back={backToResearch}/></>;
  const { curriculum, training } = view.value;
  const links = stageLinks(view.value.links);
  const items = curriculum.items ?? [];
  return <>
    <WorkspaceHeader title={curriculum.title ?? "Учебный набор"} subtitle="Происхождение задач, проверенность и полученные версии агента."
      action={<Origin provenance={curriculum._provenance}/>}/>
    <ResearchPath current="curriculum" links={links}/>
    <FixtureNote provenance={curriculum._provenance}/>
    <div className="research-grid">
      <section className="research-panel"><h2>Состав и разделение</h2>
        <dl className="facts">{Object.entries(curriculum.splits ?? {}).map(([split, n]) =>
          <div key={split}><dt>{splitName(split)}</dt><dd>{count(n)}</dd></div>)}</dl>
        <p className="muted small">Варианты одной экономической семьи находятся в одном разделе. Новые компании для сравнения в набор не входят.</p>
      </section>
      <section className="research-panel"><h2>Отбор</h2>
        <p>Принято задач: {count(items.length)}; независимо проверено: {count(items.filter(i => i.validation === "passed").length)}.</p>
        {(curriculum.filtered ?? []).length > 0 && <><h3>Отклонено</h3><ul>{curriculum.filtered!.map((row, i) => <li key={i}>{row.title ?? "Задача"}</li>)}</ul></>}
        <p className="muted small">Форматы выгрузки: {(curriculum.export_formats ?? []).length ? curriculum.export_formats!.length : "не указаны"}.</p>
      </section>
    </div>
    <section className="research-panel" aria-labelledby="items-title">
      <h2 id="items-title">Задачи</h2>
      {!items.length ? <p className="muted">Задач нет.</p> : <table className="research-table">
        <thead><tr><th scope="col">Задача</th><th scope="col">Происхождение</th><th scope="col">Раздел</th><th scope="col">Независимая проверка</th></tr></thead>
        <tbody>{items.map(item => <tr key={item.item_id}>
          <td data-label="Задача">{item.title ?? "Задача"}</td><td data-label="Происхождение">{originName(item.origin)}</td>
          <td data-label="Раздел">{splitName(item.split)}</td><td data-label="Независимая проверка">{checkStatus(item.validation)}</td>
        </tr>)}</tbody></table>}
    </section>
    <section className="research-panel" aria-labelledby="training-title">
      <h2 id="training-title">Обучение и изменения агента</h2>
      {!training.length ? <p className="muted">Заданий обучения нет: результата обучения пока нет.</p>
        : <ul className="training-list">{training.map(t => <TrainingCard key={t.artifact_id} training={t}/>)}</ul>}
      <p className="muted small">Обучение весов, изменение инструкции и примеры в запросе — разные способы; только первый создаёт новую версию модели.</p>
    </section>
    <NextStep href={links.comparison} label="Сравнить версии" missing="Сравнение версий пока не опубликовано."/>
    <TechDetails data={{ artifact_id: curriculum.artifact_id, split_hash: curriculum.split_hash, items: items.map(i => ({ id: i.item_id, family: i.family_id, hash: i.hash })), export_formats: curriculum.export_formats }}/>
  </>;
}

function intervalText(metric: string, crosses: boolean | null, difference: number): string {
  if (crosses === null) return "Неопределённость не оценена.";
  if (crosses) return "Интервал включает ноль: различие не установлено. Это нормальный результат, а не ошибка.";
  const better = HIGHER_IS_BETTER[metric];
  if (better === undefined) return difference > 0 ? "Интервал целиком выше нуля." : "Интервал целиком ниже нуля.";
  return (difference > 0) === better ? "Интервал не включает ноль: показатель улучшился." : "Интервал не включает ноль: показатель ухудшился.";
}

function MetricCells({ metrics }: { metrics: VariantMetrics }) {
  return <>
    <td data-label="micro F1">{decimal(metrics.micro_f1)}</td>
    <td data-label="Средняя награда">{decimal(metrics.mean_reward)}</td>
    <td data-label="FP на чистых">{metrics.clean_fp ? share(metrics.clean_fp.count, metrics.clean_fp.denominator) : "нет данных"}</td>
    <td data-label="Завершено">{metrics.completion ? share(metrics.completion.completed, metrics.completion.total) : "нет данных"}</td>
    <td data-label="Timeout / ошибки">{count(metrics.timeouts)} / {count(metrics.errors)}</td>
    <td data-label="Действий в среднем">{decimal(metrics.actions_mean, 1)}</td>
    <td data-label="Время">{duration(metrics.time_s)}</td>
    <td data-label="Стоимость">{usd(metrics.cost_usd)}</td>
  </>;
}

export function ComparisonScreen({ id }: { id: string }) {
  const view = useResearch(`comparison:${id}`, signal => research.comparison(id, signal));
  if (view.loading) return <><WorkspaceHeader title="Сравнение версий"/><Loading label="Загружаем сравнение"/></>;
  if (!view.value) return <><WorkspaceHeader title="Сравнение версий"/><Failed error={view.error} retry={view.retry} back={backToResearch}/></>;
  const c = view.value.comparison;
  const label = (variant: string) => c.variants?.find(v => v.variant_id === variant)?.label ?? "Вариант";
  const links = stageLinks(view.value.links);
  return <>
    <WorkspaceHeader title={c.title ?? "Сравнение версий"} subtitle="Сопоставление сохранённых попыток на одной выборке."
      action={<Origin provenance={c._provenance}/>}/>
    <ResearchPath current="comparison" links={links}/>
    <FixtureNote provenance={c._provenance}/>
    {c.verification?.status !== "passed" && <p className="inline-warning" role="note">
      Независимая проверка метрик: {checkStatus(c.verification?.status).toLocaleLowerCase("ru")}. {c.verification?.reason}</p>}
    <section className="research-panel"><h2>Выборка</h2>
      <dl className="facts">
        <div><dt>Независимых семейств</dt><dd>{count(c.sample?.families)}</dd></div>
        <div><dt>Компаний</dt><dd>{count(c.sample?.episodes)}</dd></div>
        <div><dt>Повторов на компанию</dt><dd>{count(c.sample?.repetitions)}</dd></div>
        <div><dt>Раздел</dt><dd>{splitName(c.sample?.split)}</dd></div>
      </dl>
      {c.sample?.note && <p className="muted small">{c.sample.note}</p>}
    </section>
    <section className="research-panel" aria-labelledby="variants-title">
      <h2 id="variants-title">Показатели вариантов</h2>
      <table className="research-table metrics-table">
        <thead><tr><th scope="col">Вариант</th><th scope="col">micro F1</th><th scope="col">Средняя награда</th><th scope="col">FP на чистых</th>
          <th scope="col">Завершено</th><th scope="col">Timeout / ошибки</th><th scope="col">Действий в среднем</th><th scope="col">Время</th><th scope="col">Стоимость</th></tr></thead>
        <tbody>{(c.variants ?? []).map(v => <tr key={v.variant_id}>
          <th scope="row" data-label="Вариант">{v.label ?? "Вариант"}</th>
          {v.metrics ? <MetricCells metrics={v.metrics}/> : <td colSpan={8} data-label="Результат">Нет результата: {v.missing_reason ?? "причина не указана"}</td>}
        </tr>)}</tbody>
      </table>
      <p className="muted small">Значения взяты из опубликованного сравнения как есть; консоль не пересчитывает и не усредняет их.</p>
    </section>
    <section className="research-panel" aria-labelledby="paired-title">
      <h2 id="paired-title">Парные различия по семействам</h2>
      {!(c.paired ?? []).length ? <p className="muted">Парные различия не опубликованы.</p> : <ul className="paired">{c.paired!.map(row => {
        const crosses = row.interval ? row.interval[0] <= 0 && row.interval[1] >= 0 : null;
        return <li key={row.metric + row.a + row.b}>
          <strong>{metricName(row.metric)}: {label(row.b)} − {label(row.a)} = {signed(row.difference, 3)}</strong>
          <span>{row.interval ? `интервал [${signed(row.interval[0], 3)}; ${signed(row.interval[1], 3)}]` : "интервал не рассчитан"} · {row.method ?? "метод не указан"}</span>
          <span>{intervalText(row.metric, crosses, row.difference)}</span>
        </li>;
      })}</ul>}
    </section>
    {(c.missing ?? []).length > 0 && <section className="research-panel"><h2>Нет результата</h2>
      <ul>{c.missing!.map((row, index) => <li key={row.variant_id + index}>{label(row.variant_id)}: {row.message ?? (researchError(row.reason) === researchError(undefined)
        ? "результат не получен" : researchError(row.reason))}</li>)}</ul></section>}
    <NextStep href="#/research" label="К экспериментам"/>
    <TechDetails data={c}/>
  </>;
}
