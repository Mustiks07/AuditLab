import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import { Icon, Logo } from "./Icon";
import { NAV } from "./model";

export function Dialog({ title, onClose, children, drawer = false }: {
  title: string; onClose: () => void; children: ReactNode; drawer?: boolean;
}) {
  const modal = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = modal.current;
    if (!element) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    element.showModal();
    if (drawer) element.querySelector<HTMLElement>("[aria-current]")?.focus();
    return () => {
      element.close();
      if (opener?.isConnected) opener.focus();
    };
  }, [drawer]);
  return <dialog className={drawer ? "c3-dialog c3-drawer" : "c3-dialog"} aria-label={title} ref={modal}
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className="dialog-title"><h2>{title}</h2>
      <button className="icon-button" type="button" onClick={onClose} aria-label="Закрыть"><Icon name="close"/></button>
    </div>
    {children}
  </dialog>;
}

export function Sidebar({ active, onAbout, onNavigate, documentsHref }: {
  active: string; onAbout: () => void; onNavigate?: () => void; documentsHref?: string;
}) {
  return <>
    <a href="#/" className="c3-brand" onClick={onNavigate}><Logo/><span>AuditLab</span></a>
    <p className="environment">Локальная среда</p>
    <nav className="side-nav" aria-label="Основная навигация">
      {NAV.map(([href, label, icon]) =>
        <a key={href} href={href === "/documents" && documentsHref ? documentsHref : `#${href}`}
          aria-current={active === href ? "page" : undefined} onClick={onNavigate}>
          <Icon name={icon}/>{label}
        </a>)}
    </nav>
    <div className="sidebar-bottom">
      <button type="button" onClick={onAbout}><Icon name="help"/>О данных</button>
    </div>
  </>;
}

export function AppShell({ active, title, search, onSearch, searchRef, onDialog, documentsHref, children }: {
  active: string; title: string; search: string; onSearch: (text: string) => void;
  searchRef: RefObject<HTMLInputElement>; onDialog: (mode: "menu" | "about") => void; documentsHref?: string; children: ReactNode;
}) {
  return <div className="c3-app">
    <a href="#main-content" className="skip-link" onClick={event => {
      event.preventDefault();
      document.getElementById("main-content")?.focus();
    }}>К содержимому</a>
    <aside className="c3-sidebar" aria-label="Навигация">
      <Sidebar active={active} onAbout={() => onDialog("about")} documentsHref={documentsHref}/>
    </aside>
    <main className="c3-main" id="main-content" tabIndex={-1}>
      <div className="utility">
        <button className="icon-button mobile-menu" type="button" onClick={() => onDialog("menu")} aria-label="Открыть меню">
          <Icon name="menu"/>
        </button>
        <div className="breadcrumb"><a href="#/">AuditLab</a><span aria-hidden="true">/</span><b>{title}</b></div>
        <label className="workspace-search">
          <Icon name="search"/>
          <input ref={searchRef} type="search" placeholder="Поиск запусков" aria-label="Поиск запусков"
            value={search} onChange={event => onSearch(event.target.value)}/>
          <kbd aria-hidden="true">Ctrl K</kbd>
        </label>
      </div>
      {children}
    </main>
  </div>;
}

export function WorkspaceHeader({ title, subtitle, action }: {
  title: string; subtitle?: string; action?: ReactNode;
}) {
  return <header className="workspace-heading">
    <div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>
    {action}
  </header>;
}
