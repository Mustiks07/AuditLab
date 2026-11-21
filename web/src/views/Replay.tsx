import { useEffect, useMemo, useState } from "react";
import { api, type ReplayCheck } from "../api";
import { DiagnosticDownload, Empty, ErrorBox, Panel } from "../components";
import { money, reward } from "../format";
import { recordLink, runLink } from "../navigation";
import { WorkspaceHeader } from "../workspace/AppShell";
import { humanFact, recordName, referenceLabels, stepName } from "../workspace/copy";
import { Icon } from "../workspace/Icon";
import { auditorName, companyName, findingName, periodName, runStatus } from "../workspace/model";
import { useResource } from "../workspace/resources";
import { StepView } from "./Investigation";
import { RecordPane } from "./RecordPane";

function LiveCheck({ check }: { check: ReplayCheck }) {
  const fullMatch = check.observations_match && check.score_match === true;
  const mismatch = !check.observations_match || check.score_match === false;
  return <div className={`banner ${fullMatch ? "banner-ok" : mismatch ? "banner-error" : "banner-replay"}`} role="status">
    <b>{fullMatch ? "Повтор подтверждён" : mismatch ? "Есть различия при повторе" : "Повтор выполнен частично"}</b>
    <div>{check.executed_at && <>Проверено {humanFact(check.executed_at, "timestamp")}. </>}
      {Number.isSafeInteger(check.steps_checked) && <>Проверено шагов: {check.steps_checked}. </>}
      {check.observations_match ? "Наблюдения совпали." : "Наблюдения разошлись."}
      {check.mismatches.length > 0 && <> Шаги: {check.mismatches.map(item => item.step).join(", ")}.</>}
      {check.score_match === true ? " Оценка совпала." : check.score_match === false ? " Оценка разошлась."
        : " Сопоставление оценки недоступно."}
      {check.score_match != null && Number.isFinite(check.score_again?.reward) &&
        <> Награда: {reward(check.score_again?.reward)}.</>}
      {check.session === "demo-standin" && <> Повтор выполнен в резервной среде.</>}
    </div>
  </div>;
}

export function Replay({ runId, names }: { runId: string; names?: ReadonlyMap<string, string> }) {
  const [revision, setRevision] = useState(0);
  const result = useResource(runId, signal => api.run(runId, signal), revision);
  const detail = result?.value;
  const labels = useMemo(() => referenceLabels(detail?.keys ?? []), [detail]);
  const relatedKeys = useMemo(() => Object.fromEntries(
    (detail?.findings ?? []).flatMap(finding => finding.affected.flatMap(item => item.key ? [[item.id, item.key]] : [])),
  ), [detail]);
  const [pos, setPos] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [visibleRecord, setVisibleRecord] = useState<string | null>(null);
  const [check, setCheck] = useState<ReplayCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<unknown>(null);
  const total = detail?.steps.length ?? 0;

  useEffect(() => {
    if (!playing || pos >= total) return;
    const timer = setTimeout(() => {
      const next = pos + 1;
      setPos(next);
      setVisibleRecord(null);
      if (next === total) setPlaying(false);
    }, 1100);
    return () => clearTimeout(timer);
  }, [playing, pos, total]);

  function selectPosition(next: number) {
    setPlaying(false);
    setVisibleRecord(null);
    setPos(next);
  }

  function togglePlayback() {
    if (playing) setPlaying(false);
    else {
      if (pos >= total) setPos(0);
      setVisibleRecord(null);
      setPlaying(true);
    }
  }

  function openRecord(record: string) {
    setPlaying(false);
    setVisibleRecord(record);
  }

  async function verify() {
    setChecking(true);
    setCheck(null);
    setCheckError(null);
    try { setCheck(await api.replay(runId)); }
    catch (error) { setCheckError(error); }
    finally { setChecking(false); }
  }

  const status = detail?.run.status;
  const subtitle = detail ? `${names?.get(runId) ?? companyName(detail.run)} · ${periodName(detail.run)}` : undefined;
  const current = detail && pos > 0 ? detail.steps[pos - 1] : null;
  const record = visibleRecord ?? current?.opened_key;
  const finished = total > 0 && pos >= total;
  return <div className="replay">
    <WorkspaceHeader title="Повтор проверки" subtitle={subtitle}
      action={<a className="btn" href={runLink(runId)}>К проверке</a>}/>
    {result?.error ? <>
      <ErrorBox error={result.error}/>
      <button className="btn" type="button" onClick={() => setRevision(value => value + 1)}>Повторить загрузку</button>
    </> : !detail ? <div className="loading" role="status">Загружаем запись проверки…</div>
    : !detail.journal.available || total === 0 ? <Panel title="Запись проверки">
      <Empty title="Запись проверки недоступна">Откройте исходную проверку, чтобы посмотреть её результат.</Empty>
    </Panel> : <>
      <p className="small muted">Запись действий · {auditorName(detail.run.policy)}{status && ` · ${runStatus(status)}`}</p>
      <div className="player">
        <button className="btn btn-small" type="button" onClick={() => selectPosition(0)} disabled={pos === 0}>В начало</button>
        <button className="btn btn-small" type="button" onClick={() => selectPosition(pos - 1)} disabled={pos === 0}>Назад</button>
        <button className="btn btn-small btn-primary" type="button" onClick={togglePlayback}>
          {!playing && <Icon name="play"/>}{playing ? "Пауза" : finished ? "С начала" : "Воспроизвести"}
        </button>
        <button className="btn btn-small" type="button" onClick={() => selectPosition(pos + 1)} disabled={finished}>Вперёд</button>
        <input type="range" min={0} max={total} value={pos} aria-label="Шаг воспроизведения"
          aria-valuetext={`Шаг ${pos} из ${total}`} onChange={event => selectPosition(Number(event.target.value))}/>
        <span>Шаг {pos} из {total}</span>
        <span className="spacer"/>
        <button className="btn btn-small btn-ghost" type="button" onClick={verify} disabled={checking}>
          {checking ? "Повторяем…" : "Выполнить повтор заново"}
        </button>
      </div>
      {check && <LiveCheck check={check}/>}
      {checkError != null && <ErrorBox error={checkError}/>}
      <div className="three">
        <Panel title="Действия проверки" className="col-steps">
          <ol className="steps">
            {detail.steps.map((step, index) => <li key={step.step}>
              <button className={`step ${index < pos ? "step-done" : "step-future"} ${index === pos - 1 ? "step-active" : ""}`}
                type="button" aria-current={index === pos - 1 ? "step" : undefined}
                onClick={() => selectPosition(index + 1)}>
                <span className="step-n">{step.step}</span><span className="step-text">{stepName(step)}</span>
                <span className="step-rest">{step.remaining_steps}</span>
              </button>
            </li>)}
          </ol>
        </Panel>
        <Panel title={record ? labels[record] ?? recordName(record) : current ? stepName(current) : "Начало записи"} className="col-record">
          {record ? <>
            <div className="actions">
              <a className="btn btn-small" href={recordLink(runId, record)}>Открыть в проверке</a>
              {visibleRecord && <button className="btn btn-small" type="button" onClick={() => setVisibleRecord(null)}>К текущему шагу</button>}
            </div>
            <RecordPane key={record} runId={runId} recordKey={record} onOpen={openRecord} highlightFields={[]}
              openedAt={current?.opened_key === record ? current.step : null}
              cited={(detail.submission?.findings ?? []).some(finding => finding.evidence_ids.includes(record))}
              relatedKeys={relatedKeys} canChange={false}/>
          </> : current ? <StepView step={current} onOpen={openRecord} labels={labels}/>
          : <Empty title="Запись готова к воспроизведению">Выберите шаг или запустите автоматический повтор.</Empty>}
        </Panel>
        <Panel title="Ответ аудитора" className="col-findings"
          extra={finished && <DiagnosticDownload data={{ record: detail, replay: check }}/>}>
          {!finished ? <p className="muted">Ответ появится на последнем шаге.</p>
            : !detail.submission ? <p className="muted">Ответ недоступен.</p>
            : detail.empty_submission ? <p className="muted">Без выводов</p>
            : detail.findings.map(finding => <div key={finding.index} className="claim">
              <b>{findingName(finding.finding.type)}</b>
              <div>{/^[A-Z]{3}$/.test(finding.finding.currency)
                ? money(finding.finding.claimed_amount_minor, finding.finding.currency, detail.snapshot.manifest.currency_exponents ?? {})
                : "Валюта не указана"}</div>
              <ul className="small">{finding.evidence.map(item => <li key={item.key}>
                {item.exists ? <button className="chip" type="button" onClick={() => openRecord(item.key)}>
                  {labels[item.key] ?? recordName(item.key)}</button> : "Доказательство недоступно"}
              </li>)}</ul>
              {finding.verdict && <div className={finding.verdict.matched ? "ok-text" : "bad-text"}>
                {finding.verdict.matched ? "Засчитан оценщиком" : "Не засчитан оценщиком"}</div>}
            </div>)}
          {finished && detail.score && <div className="score-inline">
            Засчитано: {detail.score.tp} · Не засчитано: {detail.score.fp} · Пропущено: {detail.score.fn}
            {" · "}Награда: <b>{reward(detail.score.reward)}</b>
            {Number.isFinite(detail.score.f1) && <div className="small">Мера точности и полноты: {reward(detail.score.f1)}.</div>}
          </div>}
        </Panel>
      </div>
    </>}
  </div>;
}
