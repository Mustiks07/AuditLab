import { useEffect, useState } from "react";
import { api, RunMeta } from "../api";
import { Empty, ErrorBox, LabelBadge, StatusPill } from "../components";
import { reward, when } from "../format";

export function RunsList() {
  const [runs, setRuns] = useState<RunMeta[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api.runs().then((r) => setRuns(r.runs)).catch(setError);
  }, []);

  if (error) return <ErrorBox error={error} />;
  if (!runs) return <div className="loading">Загрузка списка запусков…</div>;
  if (runs.length === 0)
    return (
      <Empty title="Нет завершённых открытых запусков">
        Консоль показывает только завершённые открытые экспорты. Создайте учебные:
        <pre>python -m auditlab.demo samples</pre>
        или подключите каталог раннера: <code>python -m auditlab.demo --exports runs/exports serve</code>
      </Empty>
    );

  const pairs = new Map<string, RunMeta[]>();
  runs.forEach((r) => {
    if (r.pair_id && r.label === "training_sample") pairs.set(r.pair_id, [...(pairs.get(r.pair_id) ?? []), r]);
  });

  return (
    <div className="runs">
      <div className="page-head">
        <h1>Открытые запуски</h1>
        <p className="muted">
          Каждая попытка показана отдельно. Живые опыты и копии сдачи хранятся рядом с исходной попыткой, исходная не меняется.
        </p>
      </div>
      {[...pairs.entries()].filter(([, rs]) => rs.length === 2).map(([pid, rs]) => (
        <div className="pair-hint" key={pid}>
          Открытая пара <b>{pid}</b>: {rs[0].episode_id} и {rs[1].episode_id} отличаются одним фактом.
          <a className="btn btn-small" href={`#/compare/${rs[0].run_id}/${rs[1].run_id}`}>Сравнить пару</a>
        </div>
      ))}
      <table className="runs-table">
        <thead>
          <tr>
            <th>Метка</th><th>Компания</th><th>Период</th><th>Политика</th><th>Статус</th>
            <th className="num">Выводы</th><th className="num">Награда</th><th>Связи</th><th></th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.run_id} className={r.status !== "completed" ? "row-warn" : ""}>
              <td><LabelBadge run={r} /></td>
              <td>
                <div className="strong">{r.company ?? r.episode_id}</div>
                <div className="mono small muted">{r.run_id} · эпизод {r.episode_id}</div>
              </td>
              <td className="small">{when(r.period?.start).slice(0, 10)} – {when(r.period?.end).slice(0, 10)}<div className="muted">конец искл.</div></td>
              <td className="mono">{r.policy}</td>
              <td>
                <StatusPill status={r.status} />
                {r.reason && <div className="small warn-text">{r.reason}</div>}
                {r.status !== "completed" && <div className="small muted">журнал {r.journal_available ? "доступен (неполный)" : "недоступен"}</div>}
              </td>
              <td className="num">{r.findings ?? "—"}</td>
              <td className="num mono">{reward(r.reward)}</td>
              <td className="small">
                {r.parent_run_id && <div>от <a href={`#/runs/${r.parent_run_id}`}>{r.parent_run_id}</a> · <a href={`#/compare/${r.parent_run_id}/${r.run_id}`}>сравнить</a></div>}
                {(r.children ?? []).map((c) => <div key={c}>→ <a href={`#/runs/${c}`}>{c}</a></div>)}
              </td>
              <td>
                <div className="actions">
                  <a className="btn btn-small" href={`#/runs/${r.run_id}`}>Расследование</a>
                  {r.journal_available !== false && <a className="btn btn-small btn-ghost" href={`#/runs/${r.run_id}/replay`}>Повтор</a>}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
