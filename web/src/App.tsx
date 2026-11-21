import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { go, runLink } from "./navigation";
import { Investigation } from "./views/Investigation";
import { Compare } from "./views/Compare";
import { Replay } from "./views/Replay";
import { AppShell, Dialog, Sidebar, WorkspaceHeader } from "./workspace/AppShell";
import { newestFirst, runNames } from "./workspace/model";
import { useLocal, useResource } from "./workspace/resources";
import { Catalog, PairCatalog, RunChooser, Workspace, WorkspaceSkeleton } from "./workspace/Workspace";
import { AttemptScreen, ComparisonScreen, CurriculumScreen, ExperimentScreen, FailureScreen, ReductionScreen, ResearchHome } from "./research/screens";

export { go } from "./navigation";

type Route =
  | { view: "home" | "runs" | "comparisons" | "documents" | "saved" }
  | { view: "run"; id: string; key?: string; documents?: boolean }
  | { view: "replay"; id: string }
  | { view: "compare"; left: string; right: string }
  | { view: "research"; page: string; id?: string; attempt?: string; side?: string; doc?: string };

function researchRoute(parts: string[]): Route {
  const [, page = "", id, ...rest] = parts;
  if (!id || !["experiments", "failures", "attempts", "reductions", "curricula", "comparisons"].includes(page)) {
    return { view: "research", page: "home" };
  }
  const docAt = rest.indexOf("doc");
  const value = (name: string) => { const at = rest.indexOf(name); return at >= 0 && at < (docAt < 0 ? rest.length : docAt) ? rest[at + 1] : undefined; };
  return { view: "research", page, id, attempt: value("attempt"), side: value("side"),
    doc: docAt >= 0 && rest.length > docAt + 1 ? rest.slice(docAt + 1).join("/") : undefined };
}

function parse(hash: string): Route {
  let parts: string[];
  try { parts = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent); }
  catch { return { view: "home" }; }
  if (parts[0] === "runs" && parts[1]) {
    if (parts[2] === "replay") return { view: "replay", id: parts[1] };
    if (parts[2] === "record" && parts[3]) return { view: "run", id: parts[1], key: parts.slice(3).join("/") };
    return { view: "run", id: parts[1], documents: parts[2] === "documents" };
  }
  if (parts[0] === "compare" && parts[1] && parts[2]) return { view: "compare", left: parts[1], right: parts[2] };
  if (parts[0] === "research") return researchRoute(parts);
  const page = parts[0];
  if (page === "runs" || page === "comparisons" || page === "documents" || page === "saved") return { view: page };
  return { view: "home" };
}

function section(route: Route): [string, string] {
  switch (route.view) {
    case "compare": case "comparisons": return ["/comparisons", "Сравнения"];
    case "documents": return ["/documents", "Документы"];
    case "saved": return ["/saved", "Сохранённое"];
    case "run": return route.documents || route.key ? ["/documents", "Документы"] : ["/runs", "Проверка"];
    case "replay": return ["/runs", "Повтор проверки"];
    case "research": return ["/research", "Исследование"];
    case "runs": return ["/runs", "Запуски"];
    default: return ["/", "Рабочая область"];
  }
}

const EMPTY: never[] = [];
const stringValue = (value: unknown): value is string => typeof value === "string";
const savedValue = (value: unknown): value is string[] => Array.isArray(value) && value.every(stringValue);

export function App() {
  const [hash, setHash] = useState(() => window.location.hash);
  const route = parse(hash);
  const [dialog, setDialog] = useState<"menu" | "about" | "choose" | null>(null);
  const [search, setSearch] = useState("");
  const [reload, setReload] = useState(0);
  const [lastViewed, setLastViewed] = useLocal<string | null>("auditlab:workspace:last:v1", null, stringValue);
  const [saved, setSaved] = useLocal<string[]>("auditlab:workspace:saved:v1", [], savedValue);
  const searchRef = useRef<HTMLInputElement>(null);
  const pendingFocus = useRef(false);
  const catalog = useResource("catalog", signal => api.runs(signal), reload, true);
  const rawRuns = catalog?.value?.runs ?? EMPTY;
  const runs = useMemo(() => newestFirst(rawRuns), [rawRuns]);
  const names = useMemo(() => runNames(runs), [runs]);
  const [active, title] = section(route);
  const runId = route.view === "run" ? route.id : undefined;
  const runAvailable = runId !== undefined && runs.some(item => item.run_id === runId);
  const documentsId = runAvailable ? runId : runs.find(item => item.run_id === lastViewed)?.run_id;
  const documentsHref = documentsId ? `${runLink(documentsId)}/documents` : undefined;
  const searchView = route.view === "home" || route.view === "runs" || route.view === "documents" || route.view === "saved";

  useEffect(() => {
    const changed = () => {
      setHash(window.location.hash);
      setDialog(null);
      setReload(value => value + 1);
    };
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);

  useEffect(() => {
    if (runAvailable && runId) setLastViewed(runId);
  }, [runId, runAvailable, setLastViewed]);

  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if (dialog || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      if (!searchView) {
        pendingFocus.current = true;
        go("/runs");
      } else searchRef.current?.focus();
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [dialog, searchView]);

  useEffect(() => {
    if (pendingFocus.current) {
      pendingFocus.current = false;
      searchRef.current?.focus();
    }
  }, [hash]);

  function changeSearch(value: string) {
    setSearch(value);
    if (!searchView) {
      pendingFocus.current = true;
      go("/runs");
    }
  }
  function toggleSaved(id: string) {
    setSaved(previous => previous.includes(id) ? previous.filter(item => item !== id) : [...previous, id]);
  }

  const listProps = { runs, names, search, clearSearch: () => setSearch("") };
  const childViews = route.view === "run" || route.view === "compare" || route.view === "replay" || route.view === "research";
  return <>
    <AppShell active={active} title={title} search={search} onSearch={changeSearch} searchRef={searchRef}
      onDialog={setDialog} documentsHref={documentsHref}>
      {!childViews && !catalog?.value && (catalog?.error ? <>
        <WorkspaceHeader title={title}/>
        <div className="workspace-empty" role="alert">
          <h2>Не удалось подключиться к сервису проверок</h2>
          <button className="primary-action" type="button" onClick={() => setReload(value => value + 1)}>Повторить</button>
        </div>
      </> : <WorkspaceSkeleton title={title}/>)}
      {catalog?.value && route.view === "home" && <Workspace {...listProps} lastViewed={lastViewed}
        onChoose={() => setDialog("choose")}/>}
      {catalog?.value && route.view === "runs" && <Catalog {...listProps} title="Запуски"/>}
      {catalog?.value && route.view === "documents" && <Catalog {...listProps} title="Документы" documents/>}
      {catalog?.value && route.view === "saved" && <Catalog {...listProps} title="Сохранённое" saved
        runs={runs.filter(item => saved.includes(item.run_id))}/>}
      {catalog?.value && route.view === "comparisons" && <PairCatalog runs={runs} names={names}/>}
      {route.view === "run" && <Investigation key={route.id} runId={route.id} initialKey={route.key}
        documents={route.documents} title={names.get(route.id)} names={names}
        saved={saved.includes(route.id)} onSave={() => toggleSaved(route.id)}/>}
      {route.view === "compare" && <Compare key={`${route.left}|${route.right}`} left={route.left} right={route.right} names={names}/>}
      {route.view === "replay" && <Replay key={route.id} runId={route.id} names={names}/>}
      {route.view === "research" && route.page === "home" && <ResearchHome/>}
      {route.view === "research" && route.page === "experiments" && <ExperimentScreen key={route.id} id={route.id!}/>}
      {route.view === "research" && route.page === "failures" && <FailureScreen key={route.id} id={route.id!} attemptId={route.attempt} docKey={route.doc}/>}
      {route.view === "research" && route.page === "attempts" && <AttemptScreen key={route.id} id={route.id!} docKey={route.doc}/>}
      {route.view === "research" && route.page === "reductions" && <ReductionScreen key={route.id} id={route.id!} side={route.side} docKey={route.doc}/>}
      {route.view === "research" && route.page === "curricula" && <CurriculumScreen key={route.id} id={route.id!}/>}
      {route.view === "research" && route.page === "comparisons" && <ComparisonScreen key={route.id} id={route.id!}/>}
    </AppShell>
    {dialog === "menu" && <Dialog title="Меню" onClose={() => setDialog(null)} drawer>
      <Sidebar active={active} onAbout={() => setDialog("about")} onNavigate={() => setDialog(null)} documentsHref={documentsHref}/>
    </Dialog>}
    {dialog === "choose" && <Dialog title="Открыть запуск" onClose={() => setDialog(null)}>
      <RunChooser runs={runs} names={names}/>
    </Dialog>}
    {dialog === "about" && <Dialog title="О данных" onClose={() => setDialog(null)}>
      <p>AuditLab работает с открытыми результатами проверок. В поставке есть примеры с синтетическими компаниями и документами для воспроизведения расчётов.</p>
      <p>Итоги и оценки относятся к конкретной проверке. Они не описывают аудит реального клиента и не являются оценкой конкурса.</p>
      <h3>Исследование агентов</h3>
      <p>Раздел показывает результаты экспериментов из подключённого каталога. У каждого результата указано происхождение. «Измерение» — запуск эксперимента. «Фикстура разработки» — данные для проверки интерфейса: компании и документы взяты из открытого набора AuditLab, действия задаёт скрипт, их исполняет настоящий раннер и оценивает настоящий оценщик. Фикстуры не являются ответами модели, а показатели в них — измерениями.</p>
      <p>Банки, клиенты и платёжные системы не подключены. Ключ доступа к модели хранится только на сервере и в браузер не передаётся.</p>
    </Dialog>}
  </>;
}
