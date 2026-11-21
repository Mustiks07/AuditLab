import { useState } from "react";
import type { RunMeta } from "../api";
import { runLink } from "../navigation";
import { Icon } from "./Icon";
import { auditorName, periodName, runResult, runStatus } from "./model";

type Segment = "all" | "findings" | "empty";

export function RunsTable({ runs, names, search = "", clearSearch, selected, recent = false, target = runLink }: {
  runs: RunMeta[]; names: Map<string, string>; search?: string; clearSearch?: () => void;
  selected?: string; recent?: boolean; target?: (id: string) => string;
}) {
  const [segment, setSegment] = useState<Segment>("all");
  const [policy, setPolicy] = useState("");
  const [period, setPeriod] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<"newest" | "asc" | "desc">("newest");
  const filtered = segment !== "all" || policy !== "" || period !== "" || status !== "" || search.trim() !== "";
  const query = search.trim().toLocaleLowerCase("ru");
  const title = (run: RunMeta) => names.get(run.run_id) ?? "Проверка";
  const matches = runs.filter(run =>
    (!query || [title(run), periodName(run), auditorName(run.policy), runResult(run).text]
      .some(value => value.toLocaleLowerCase("ru").includes(query))) &&
    (!policy || policy === auditorName(run.policy)) && (!period || period === periodName(run)) &&
    (!status || status === run.status) &&
    (segment === "all" || (run.status === "completed" && Number.isInteger(run.findings) &&
      (segment === "findings" ? run.findings! > 0 : run.findings === 0))));
  const sorted = sort === "newest" ? matches : [...matches].sort((a, b) =>
    (sort === "asc" ? 1 : -1) * title(a).localeCompare(title(b), "ru"));
  const shown = recent && !filtered && sort === "newest" ? sorted.slice(0, 5) : sorted;
  const values = (fn: (run: RunMeta) => string) => [...new Set(runs.map(fn))];
  function reset() {
    setSegment("all");
    setPolicy("");
    setPeriod("");
    setStatus("");
    clearSearch?.();
  }
  return <>
    <div className="table-toolbar">
      <div className="segments" role="group" aria-label="Результат проверки">
        {([["all", "Все"], ["findings", "С выводами"], ["empty", "Без выводов"]] as const)
          .map(([value, label]) => <button type="button" key={value} aria-pressed={segment === value} onClick={() => setSegment(value)}>{label}</button>)}
      </div>
      <details className="run-filters">
        <summary><Icon name="filter"/>Фильтры{(policy || period || status) && <span className="filter-on" aria-label="Применены"/>}</summary>
        <div className="filter-popover">
          <label>Аудитор<select aria-label="Аудитор" value={policy} onChange={e => setPolicy(e.target.value)}>
            <option value="">Все аудиторы</option>{values(run => auditorName(run.policy)).map(text => <option key={text}>{text}</option>)}
          </select></label>
          <label>Период<select aria-label="Период" value={period} onChange={e => setPeriod(e.target.value)}>
            <option value="">Все периоды</option>{values(periodName).map(text => <option key={text}>{text}</option>)}
          </select></label>
          <label>Ход проверки<select aria-label="Ход проверки" value={status} onChange={e => setStatus(e.target.value)}>
            <option value="">Все состояния</option>{values(run => run.status).map(value => <option key={value} value={value}>{runStatus(value)}</option>)}
          </select></label>
          {filtered && <button className="text-link" type="button" onClick={reset}>Очистить фильтры</button>}
        </div>
      </details>
    </div>
    {shown.length > 0 ? <table className="workspace-table" role="table" aria-label="Запуски проверок">
      <thead role="rowgroup"><tr role="row">
        <th scope="col" role="columnheader" aria-sort={sort === "newest" ? "none" : sort === "asc" ? "ascending" : "descending"}>
          <button className="sort-runs" type="button" onClick={() => setSort(sort === "newest" ? "asc" : sort === "asc" ? "desc" : "newest")}>
            Запуск{sort !== "newest" && <span aria-hidden="true">{sort === "asc" ? " ↑" : " ↓"}</span>}</button></th>
        <th scope="col" role="columnheader">Аудитор</th><th scope="col" role="columnheader">Результат</th>
        <th scope="col" role="columnheader"><span className="sr-only">Открытие запуска</span></th>
      </tr></thead>
      <tbody role="rowgroup">{shown.map(run => {
        const result = runResult(run);
        return <tr key={run.run_id} role="row" className={run.run_id === selected ? "selected" : undefined}>
          <td role="cell"><a className="run-name" href={target(run.run_id)}>
            <Icon name="building"/><span><b>{title(run)}</b><small>{periodName(run)}</small></span>
          </a></td>
          <td role="cell">{auditorName(run.policy)}</td>
          <td role="cell"><span className={`run-result ${result.tone}`}><i aria-hidden="true"/>{result.text}</span></td>
          <td role="cell"><span className="row-arrow"><Icon name="arrow"/></span></td>
        </tr>;
      })}</tbody></table> : <div className="workspace-empty" role="status">
      <h3>{runs.length > 0 ? "Ничего не найдено" : "Пока нет проверок"}</h3>
      {runs.length > 0 ? <button type="button" className="text-link" onClick={reset}>Очистить фильтры</button>
        : <p>Когда появится открытый запуск, его можно будет выбрать здесь.</p>}
    </div>}
  </>;
}
