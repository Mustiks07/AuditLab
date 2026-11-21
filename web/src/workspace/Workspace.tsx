import { api, type CompareView, type RunDetail, type RunMeta } from "../api";
import { compareLink, runLink } from "../navigation";
import { WorkspaceHeader } from "./AppShell";
import { ContinueReview, PairPreview } from "./Cards";
import { Icon } from "./Icon";
import { periodName, realPairs } from "./model";
import { useResource } from "./resources";
import { RunsTable } from "./RunsTable";

type CatalogProps = { runs: RunMeta[]; names: Map<string, string>; search: string; clearSearch: () => void };

const details = new Map<string, RunDetail>();
const comparisons = new Map<string, CompareView>();

async function cached<T>(cache: Map<string, T>, key: string, load: () => Promise<T>, signal: AbortSignal): Promise<T> {
  const saved = cache.get(key);
  if (saved !== undefined) return saved;
  const value = await load();
  if (!signal.aborted) {
    if (cache.size >= 4) {
      const oldest = cache.keys().next().value;
      if (oldest !== undefined) cache.delete(oldest);
    }
    cache.set(key, value);
  }
  return value;
}

export function Workspace({ runs, names, search, clearSearch, lastViewed, onChoose }: CatalogProps & {
  lastViewed: string | null; onChoose: () => void;
}) {
  const selected = runs.find(run => run.run_id === lastViewed) ?? runs.find(run => run.status === "completed") ?? runs[0];
  const pairs = realPairs(runs);
  const pair = pairs.find(item => item.directed && item.right.run_id === selected?.run_id) ??
    pairs.find(item => item.directed && item.left.run_id === selected?.run_id) ??
    pairs.find(item => item.directed) ?? pairs[0];
  const selectionKey = selected?.run_id ?? null;
  const pairKey = pair ? JSON.stringify([pair.left.run_id, pair.right.run_id]) : null;
  const detail = useResource(selectionKey, signal =>
    cached(details, selectionKey!, () => api.run(selectionKey!, signal), signal));
  const comparison = useResource(pairKey, signal =>
    cached(comparisons, pairKey!, () => api.compare(pair.left.run_id, pair.right.run_id, signal), signal));

  return <>
    <WorkspaceHeader title="Рабочая область" subtitle="Проверки, к которым стоит вернуться."
      action={<button className="primary-action" type="button" disabled={!runs.length} onClick={onChoose}>
        <Icon name="folder"/>Открыть запуск</button>}/>
    {runs.length > 0 && <div className="feature-grid">
      <ContinueReview run={selected} title={names.get(selected.run_id)} resumed={lastViewed === selected.run_id}
        detail={detail?.value} error={detail?.error}/>
      <PairPreview pair={pair} names={names} details={comparison?.value}/>
    </div>}
    <section className="recent-runs" aria-labelledby="recent-runs-title">
      <div className="section-title"><h2 id="recent-runs-title">Последние запуски</h2>
        <a className="text-link" href="#/runs">Все запуски<Icon name="arrow"/></a>
      </div>
      <RunsTable runs={runs} names={names} search={search} clearSearch={clearSearch} selected={selected?.run_id} recent/>
    </section>
  </>;
}

export function Catalog({ title, runs, names, search, clearSearch, documents, saved }: CatalogProps & {
  title: string; documents?: boolean; saved?: boolean;
}) {
  return <>
    <WorkspaceHeader title={title} subtitle={documents ? "Выберите проверку, чтобы открыть её документы." : undefined}/>
    {saved && !runs.length ? <div className="workspace-empty">
      <h2>Пока ничего не сохранено</h2><p>Откройте проверку и сохраните её для быстрого доступа.</p>
      <a className="text-link" href="#/runs">Все запуски<Icon name="arrow"/></a>
    </div> : <RunsTable runs={runs} names={names} search={search} clearSearch={clearSearch}
      target={documents ? id => `${runLink(id)}/documents` : undefined}/>}
  </>;
}

export function PairCatalog({ runs, names }: Pick<CatalogProps, "runs" | "names">) {
  const pairs = realPairs(runs);
  return <>
    <WorkspaceHeader title="Сравнения" subtitle="Связанные проверки и результаты изменения условий."/>
    {!pairs.length ? <div className="workspace-empty"><h2>Пока нет сравнений</h2>
      <p>Связанные запуски появятся здесь после изменения условия проверки.</p></div> :
      <ul className="choice-list pair-list">{pairs.map(({left, right, directed}) =>
        <li key={compareLink(left.run_id, right.run_id)}><a href={compareLink(left.run_id, right.run_id)}>
          <Icon name="compare"/><span><strong>{names.get(left.run_id)} {directed ? "→" : "·"} {names.get(right.run_id)}</strong>
            <small>{periodName(right)}</small></span><Icon name="arrow"/>
        </a></li>)}</ul>}
  </>;
}

export function RunChooser({ runs, names }: Pick<CatalogProps, "runs" | "names">) {
  return <ul className="choice-list">{runs.map(run =>
    <li key={run.run_id}><a href={runLink(run.run_id)}>
      <Icon name="building"/><span><strong>{names.get(run.run_id)}</strong><small>{periodName(run)}</small></span><Icon name="arrow"/>
    </a></li>)}</ul>;
}

export function WorkspaceSkeleton({ title }: { title: string }) {
  return <div aria-busy="true" role="status" aria-label="Загружаем запуски">
    <WorkspaceHeader title={title}/>
    <div className="feature-grid skeleton-grid"><div className="skeleton"/><div className="skeleton"/></div>
    <div className="skeleton skeleton-list"/>
  </div>;
}
