import { useEffect, useState } from "react";
import { api, ReplayCheck, RunDetail } from "../api";
import { ErrorBox, LabelBadge, Panel } from "../components";
import { money, reward } from "../format";
import { StepView } from "./Investigation";
import { RecordPane } from "./RecordPane";

export function Replay({ runId }: { runId: string }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pos, setPos] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [check, setCheck] = useState<ReplayCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<unknown>(null);

  useEffect(() => {
    api.run(runId).then(setDetail).catch(setError);
  }, [runId]);

  useEffect(() => {
    if (!playing || !detail) return;
    if (pos >= detail.steps.length) {
      setPlaying(false);
      return;
    }
    const t = setTimeout(() => setPos((p) => p + 1), 1100);
    return () => clearTimeout(t);
  }, [playing, pos, detail]);

  if (error) return <ErrorBox error={error} />;
  if (!detail) return <div className="loading">Загрузка журнала {runId}…</div>;
  const total = detail.steps.length;
  const current = pos > 0 ? detail.steps[pos - 1] : null;
  const finished = pos >= total;

  const verify = async () => {
    setChecking(true);
    setCheckError(null);
    try {
      setCheck(await api.replay(runId));
    } catch (e) {
      setCheckError(e);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="replay">
      <div className="banner banner-replay">
        ПОВТОР ЗАПИСИ · <span className="mono">{runId}</span> — воспроизводится сохранённый журнал завершённой попытки, а не новый прогон.
      </div>
      <div className="run-title">
        <a href={`#/runs/${runId}`} className="back">← Расследование</a>
        <LabelBadge run={detail.run} />
        <h1>{detail.run.company ?? detail.run.episode_id}</h1>
      </div>
      <div className="player">
        <button type="button" className="btn btn-small" onClick={() => { setPos(0); setPlaying(false); }}>⏮</button>
        <button type="button" className="btn btn-small" onClick={() => setPos((p) => Math.max(0, p - 1))} disabled={pos === 0}>◀</button>
        <button type="button" className="btn btn-small btn-primary" onClick={() => { if (finished) setPos(0); setPlaying(!playing); }}>
          {playing ? "⏸ пауза" : finished ? "▶ сначала" : "▶ играть"}
        </button>
        <button type="button" className="btn btn-small" onClick={() => setPos((p) => Math.min(total, p + 1))} disabled={finished}>▶</button>
        <input type="range" min={0} max={total} value={pos} onChange={(e) => { setPos(Number(e.target.value)); setPlaying(false); }} />
        <span className="mono">шаг {pos} / {total}</span>
        <span className="spacer" />
        <button type="button" className="btn btn-small btn-ghost" onClick={verify} disabled={checking}>
          {checking ? "Проверяю…" : "Повторить действия и оценку"}
        </button>
      </div>
      {check && (
        <div className={`banner ${check.observations_match && check.score_match !== false ? "banner-ok" : "banner-error"}`}>
          <b>ЖИВОЙ ПЕРЕСЧЁТ</b>{check.executed_at && <> · выполнен сейчас: <span className="mono">{check.executed_at}</span>, {check.elapsed_ms} мс</>}.{" "}
          Повтор {check.steps_checked} инспекций через {check.session === "demo-standin" ? "учебную среду" : check.session}: наблюдения {check.observations_match ? "совпали" : `разошлись на шагах ${check.mismatches.map((m) => m.step).join(", ")}`}.
          {check.score_match !== undefined && check.score_match !== null && <> Повтор оценки: {check.score_match ? "совпала" : "разошлась"} (награда {reward(check.score_again?.reward)}).</>}
          {check.score_note && <> {check.score_note}</>}
        </div>
      )}
      {checkError !== null && <ErrorBox error={checkError} />}

      <div className="three">
        <Panel title="Журнал" className="col-steps">
          <ol className="steps">
            {detail.steps.map((s, i) => (
              <li key={s.step}>
                <button type="button" className={`step ${i < pos ? "step-done" : "step-future"} ${i === pos - 1 ? "step-active" : ""}`}
                  onClick={() => { setPos(i + 1); setPlaying(false); }}>
                  <span className="step-n">{s.step}</span>
                  <span className="step-text mono">{s.text}</span>
                  <span className="step-rest">{s.remaining_steps}</span>
                </button>
              </li>
            ))}
          </ol>
        </Panel>
        <Panel title={current ? `Шаг ${current.step}: ${current.text}` : "Начало эпизода"} className="col-record">
          {!current && <div className="muted">Первое наблюдение: постановка задачи, манифест, 100 шагов. Нажмите «играть».</div>}
          {current && current.opened_key ? (
            <RecordPane runId={runId} recordKey={current.opened_key} onOpen={() => undefined} highlightFields={[]} openedAt={current.step}
              cited={(detail.submission?.findings ?? []).some((f) => f.evidence_ids.includes(current.opened_key!))} canChange={false} />
          ) : current ? <StepView step={current} onOpen={() => undefined} /> : null}
        </Panel>
        <Panel title="Итог" className="col-findings">
          {!finished && <div className="muted">Итог появится после последнего шага журнала.</div>}
          {finished && detail.submission === null && <div className="bad-text">Сдачи нет — попытка оборвана, итог не засчитан.</div>}
          {finished && detail.empty_submission && <div className="no-findings-title">Ошибок не заявлено</div>}
          {finished && detail.findings.map((f) => (
            <div key={f.index} className="claim">
              <div><b>{money(f.finding.claimed_amount_minor, f.finding.currency, detail.snapshot.manifest.currency_exponents ?? {})}</b> · {f.type_name}</div>
              <div className="mono small muted">{f.finding.evidence_ids.join("  ")}</div>
              {f.verdict && <div className={f.verdict.matched ? "ok-text" : "bad-text"}>{f.verdict.matched ? "✓ засчитан" : "✗ не засчитан"}</div>}
            </div>
          ))}
          {finished && detail.score && (
            <div className="score-inline mono">TP {detail.score.tp} · FP {detail.score.fp} · FN {detail.score.fn} · награда <b>{reward(detail.score.reward)}</b></div>
          )}
        </Panel>
      </div>
    </div>
  );
}
