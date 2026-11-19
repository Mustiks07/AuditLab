import { useEffect, useState } from "react";
import { api, CompareSide, CompareView, Derivation } from "../api";
import { ErrorBox, LabelBadge, Panel, StatusPill } from "../components";
import { minor, money, reward, REASONS, when } from "../format";

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function Compare({ left, right }: { left: string; right: string }) {
  const [view, setView] = useState<CompareView | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api.compare(left, right).then(setView).catch(setError);
  }, [left, right]);

  if (error) return <ErrorBox error={error} />;
  if (!view) return <div className="loading">Сравниваю {left} и {right}…</div>;
  const ch = view.change_request;
  const delta = view.calculations.reduce((acc, c) => acc + c.difference_minor, 0);
  const currency = view.calculations[0]?.right.currency ?? "KZT";

  return (
    <div className="compare">
      <div className="page-head">
        <a href="#/" className="back">← Запуски</a>
        <h1>Сравнение пары</h1>
      </div>

      <div className="change-summary">
        {ch && (
          <div className="cs-block">
            <div className="cs-label">Изменённый факт</div>
            <div className="cs-main mono">{ch.target}.{ch.field}</div>
            <div className="cs-arrow">
              <span className="mono">{fmt(view.changes.find((c) => c.key === ch.target && c.field === ch.field)?.before)}</span>
              <span> → </span>
              <span className="mono strong">{ch.value}</span>
            </div>
            {ch.moved && <div className="small muted">согласованно перенесены: {ch.moved.join(", ")}</div>}
          </div>
        )}
        {view.derived_from && (
          <div className="cs-block">
            <div className="cs-label">Изменено доказательство в копии сдачи</div>
            <div className="cs-main mono">вывод #{view.derived_from.finding_index}: {view.derived_from.replaced.from} → {view.derived_from.replaced.to}</div>
            <div className="small muted">Снимок данных тот же; изменилась только сдача.</div>
          </div>
        )}
        {!ch && !view.derived_from && view.changes.length > 0 && (
          <div className="cs-block">
            <div className="cs-label">Открытая пара</div>
            <div className="cs-main">Различий в документах: {view.changes.length}</div>
          </div>
        )}
        <div className="cs-block">
          <div className="cs-label">Прогноз судьи</div>
          <div className="cs-main">{view.prediction ? `«${view.prediction}»` : <span className="muted">не записан</span>}</div>
        </div>
        {view.calculations.length > 0 && (
          <div className="cs-block cs-effect">
            <div className="cs-label">Фактический эффект по открытым данным</div>
            <div className="cs-main">Δ = <b>{money(delta, currency)}</b> <span className="muted mono">({minor(delta)} мин. ед.)</span></div>
          </div>
        )}
      </div>

      <div className="two">
        <Side side={view.left} title="Исходная" />
        <Side side={view.right} title={view.derived_from ? "Копия" : ch ? "Новый опыт" : "Вторая"} />
      </div>

      {view.calculations.length > 0 && (
        <Panel title="Обе суммы: расчёт по исходным документам">
          {view.calculations.map((c) => (
            <div key={c.type + c.subject} className="calc-pair">
              <CalcCell calc={c.left} runId={view.left.run.run_id} />
              <div className="calc-diff">
                <div className="small muted">разница</div>
                <div className="strong">{money(c.difference_minor, c.right.currency)}</div>
              </div>
              <CalcCell calc={c.right} runId={view.right.run.run_id} />
            </div>
          ))}
        </Panel>
      )}

      <Panel title={`Различия в документах · ${view.changes.length}`}>
        {view.changes.length === 0 ? (
          <div className="muted">Открытые документы совпадают.</div>
        ) : (
          <table className="diff">
            <thead><tr><th>запись</th><th>поле</th><th>{view.left.run.run_id}</th><th>{view.right.run.run_id}</th></tr></thead>
            <tbody>
              {view.changes.map((c, i) => (
                <tr key={i}>
                  <td>
                    <a className="mono" href={`#/runs/${view.right.run.run_id}/record/${c.key}`}>{c.key}</a>
                    {c.line_after && <span className="small muted"> стр. {c.line_after}</span>}
                  </td>
                  <td className="mono">{c.field ?? "запись"}</td>
                  <td className="mono before">{fmt(c.before)}</td>
                  <td className="mono after">{fmt(c.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

function Side({ side, title }: { side: CompareSide; title: string }) {
  const r = side.run;
  return (
    <Panel title={<>{title}: <a href={`#/runs/${r.run_id}`} className="mono">{r.run_id}</a></>} extra={<LabelBadge run={r} />}>
      <div className="small muted">{r.company} · эпизод <span className="mono">{r.episode_id}</span> · <StatusPill status={r.status} /></div>
      <div className="side-claims">
        <div className="line-label">Фактический ответ аудитора</div>
        {side.empty && <div className="no-findings-inline">Ошибок не заявлено</div>}
        {side.claims.map((c, i) => (
          <div key={i} className="claim">
            <div><b>{money(c.amount_minor, c.currency)}</b> · {c.type} · <span className="mono small">{c.affected_ids.join(", ")}</span></div>
            <div className="mono small muted">{c.evidence_ids.join("  ")}</div>
            {c.verdict && (
              <div className={c.verdict.matched ? "ok-text" : "bad-text"}>
                {c.verdict.matched ? "✓ засчитан" : `✗ ${REASONS[c.verdict.reason] ?? c.verdict.reason}`}
              </div>
            )}
            {c.unconfirmed.length > 0 && (
              <div className="warn">
                Больше не подтверждено ссылкой:{" "}
                {c.unconfirmed.map((u) => <div key={u.key}><a className="mono" href={`#/runs/${r.run_id}/record/${u.key}`}>{u.key}</a> — {u.label}</div>)}
              </div>
            )}
          </div>
        ))}
      </div>
      {side.score && (
        <div className="score-inline mono">
          TP {side.score.tp} · FP {side.score.fp} · FN {side.score.fn} · F1 {side.score.f1.toFixed(3)} · награда <b>{reward(side.score.reward)}</b>
        </div>
      )}
      <a className="btn btn-small" href={`#/runs/${r.run_id}`}>Открыть расследование</a>
    </Panel>
  );
}

function CalcCell({ calc, runId }: { calc: Derivation; runId: string }) {
  return (
    <div className="calc-cell">
      <div className="small">{calc.title}</div>
      {calc.operands.map((op) => (
        <div key={op.key + op.field} className="mono small">
          <a href={`#/runs/${runId}/record/${op.key}`}>{op.key}</a> {op.field}={op.value ?? op.value_minor}
          {op.timestamp && <span className="muted"> · {when(op.timestamp)}</span>}
        </div>
      ))}
      {(calc.context ?? []).map((op) => (
        <div key={"c" + op.key} className="mono small muted">
          <a href={`#/runs/${runId}/record/${op.key}`}>{op.key}</a> после границы · {when(op.timestamp)}
        </div>
      ))}
      <div className="mono small formula">{calc.formula}</div>
      <div className="strong">{money(calc.result_minor, calc.currency)}</div>
    </div>
  );
}
