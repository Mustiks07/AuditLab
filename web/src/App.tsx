import { useEffect, useState } from "react";
import { RunsList } from "./views/RunsList";
import { Investigation } from "./views/Investigation";
import { Compare } from "./views/Compare";
import { Replay } from "./views/Replay";

type Route =
  | { view: "runs" }
  | { view: "run"; id: string; key?: string }
  | { view: "replay"; id: string }
  | { view: "compare"; left: string; right: string };

function parse(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] === "runs" && parts[1]) {
    if (parts[2] === "replay") return { view: "replay", id: parts[1] };
    if (parts[2] === "record" && parts[3]) return { view: "run", id: parts[1], key: parts.slice(3).join("/") };
    return { view: "run", id: parts[1] };
  }
  if (parts[0] === "compare" && parts[1] && parts[2]) return { view: "compare", left: parts[1], right: parts[2] };
  return { view: "runs" };
}

export function go(path: string) {
  window.location.hash = path;
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/">
          <span className="brand-mark">AL</span>
          <span>AuditLab <span className="muted">· консоль жюри</span></span>
        </a>
        <span className="topbar-note">локально · только открытые завершённые прогоны · все суммы синтетические</span>
      </header>
      <main className="content">
        {route.view === "runs" && <RunsList />}
        {route.view === "run" && <Investigation key={route.id} runId={route.id} initialKey={route.key} />}
        {route.view === "replay" && <Replay key={route.id} runId={route.id} />}
        {route.view === "compare" && <Compare key={`${route.left}|${route.right}`} left={route.left} right={route.right} />}
      </main>
    </div>
  );
}
