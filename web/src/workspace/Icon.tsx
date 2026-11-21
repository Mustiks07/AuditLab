import type { ReactNode } from "react";

const shapes: Record<string, ReactNode> = {
  workspace: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
  play: <path d="m7 4 13 8-13 8Z"/>,
  compare: <path d="M3 7h17m-5-5 5 5-5 5M21 17H4m5-5-5 5 5 5"/>,
  document: <path d="M14 3H5v18h14V8Zm0 0v6h5M8 13h8M8 17h6"/>,
  bookmark: <path d="M6 3h12v18l-6-4-6 4Z"/>,
  help: <><circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5M12 17h.01"/></>,
  search: <><circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/></>,
  arrow: <path d="M3 12h17m-6-6 6 6-6 6"/>,
  folder: <path d="M3 8V5h7l2 3h9v12H3Zm0 4h18"/>,
  building: <path d="M6 21V6l12-3v18M3 21h18M9 8h1m4-1h1M9 12h1m4-1h1M9 16h1m4-1h1M11 21v-3h3v3"/>,
  check: <path d="m4 12 5 5L20 6"/>,
  filter: <path d="M3 4h18l-7 8v8l-4-2v-6Z"/>,
  menu: <path d="M3 6h18M3 12h18M3 18h18"/>,
  close: <path d="m5 5 14 14M19 5 5 19"/>,
  research: <path d="M9 3h6M10 3v6l-5.5 9.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7.5 15h9"/>,
};

export function Icon({ name }: { name: keyof typeof shapes }) {
  return <svg className="c3-icon" viewBox="0 0 24 24" aria-hidden="true">{shapes[name]}</svg>;
}

export function Logo() {
  return <svg className="c3-logo" viewBox="0 0 32 34" aria-hidden="true" fill="currentColor">
    <path d="M12 1H5a3 3 0 0 0-3 3v26a3 3 0 0 0 3 3h7v-6H8V7h4ZM20 1h8a2 2 0 0 1 2 2v3h-4v24a3 3 0 0 1-3 3h-7v-6h4Z"/>
  </svg>;
}
