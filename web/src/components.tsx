import { ReactNode } from "react";
import type { Derivation, Operand, RunMeta } from "./api";
import { minor, money, OP_ERRORS, opError, STATUS, when } from "./format";

export function LabelBadge({ run }: { run: RunMeta }) {
  return <span className={`badge badge-${run.label ?? "export"}`}>{run.label_text}</span>;
}

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status}`}>{STATUS[status] ?? status}</span>;
}

export function EngineLine({ engine }: { engine?: Record<string, string> }) {
  if (!engine) return null;
  const names: Record<string, string> = { session: "среда", policy: "политика", evaluator: "оценщик", generator: "генератор", verifier: "сверка" };
  return (
    <div className="engine-line" title="Какие модули дали этот результат">
      {Object.entries(engine).map(([k, v]) => (
        <span key={k} className={`engine ${v === "demo-standin" ? "engine-standin" : v === "not_connected" ? "engine-off" : ""}`}>
          {names[k] ?? k}: {v === "demo-standin" ? "учебная замена" : v === "not_connected" ? "не подключена" : v}
        </span>
      ))}
    </div>
  );
}

export function KeyChip({ k, onOpen, active, missing, hint }: { k: string; onOpen?: (k: string) => void; active?: boolean; missing?: boolean; hint?: string }) {
  return (
    <button type="button" className={`chip ${active ? "chip-active" : ""} ${missing ? "chip-missing" : ""}`}
      onClick={() => onOpen?.(k)} title={hint ? `${k} · ${hint}` : k} disabled={!onOpen}>
      {k}
    </button>
  );
}

export function Panel({ title, extra, children, className }: { title: ReactNode; extra?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className ?? ""}`}>
      <div className="panel-head">
        <h2>{title}</h2>
        {extra}
      </div>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="empty-body">{children}</div>}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const code = typeof error === "object" && error !== null && "code" in error ? String((error as { code: unknown }).code) : undefined;
  const raw = error instanceof Error ? error.message : String(error);
  const msg = code && OP_ERRORS[code] ? opError(code, raw) : raw;
  return <div className="error-box" role="alert"><b>Ошибка.</b> {msg}</div>;
}

function operandValue(op: Operand): string {
  if (op.value !== undefined) return op.value;
  return minor(op.value_minor);
}

/** Operands → result. Every operand links to the exact file line it was read from. */
export function DerivationView({ calc, onOpen, cited, activeKey, exponents }: { exponents: Record<string, number>; calc: Derivation; onOpen: (k: string) => void; cited?: string[]; activeKey?: string | null }) {
  const citedSet = new Set(cited ?? []);
  const rule = calc.rule;
  return (
    <div className="derivation">
      <div className="derivation-title">{calc.title}</div>
      <div className="operands-hint small muted">← Таблица расчёта прокручивается по горизонтали →</div>
      <div className="operands-scroll" role="region" aria-label={`Расчёт: ${calc.title}`} tabIndex={0}>
      <table className="operands">
        <thead>
          <tr><th></th><th title="Числа в файлах — целые минимальные единицы валюты (тиын, цент); под ними — сумма по разрядности из манифеста">значение, мин. ед.</th><th>источник</th><th>строка</th><th>смысл</th></tr>
        </thead>
        <tbody>
          {calc.operands.map((op) => (
            <tr key={op.key + op.field} className={activeKey === op.key ? "row-active" : ""}>
              <td className="sign">{calc.type === "fx_conversion" ? (op.sign < 0 ? "−" : op.field === "rate" ? "×" : "") : op.sign < 0 ? "−" : "+"}</td>
              <td className="num">{operandValue(op)}{op.value_minor !== undefined && op.currency && <div className="small muted">{money(op.value_minor, op.currency, exponents)}</div>}</td>
              <td>
                <KeyChip k={op.key} onOpen={onOpen} active={activeKey === op.key} missing={cited !== undefined && !citedSet.has(op.key)}
                  hint={cited !== undefined && !citedSet.has(op.key) ? "Этот операнд не процитирован в выводе" : `${op.file}:${op.line}`} />
              </td>
              <td className="mono small">{op.file.replace("public/", "")}:{op.line} · {op.field}</td>
              <td className="small">{op.label}{op.timestamp ? ` · ${when(op.timestamp)}` : ""}</td>
            </tr>
          ))}
          {(calc.context ?? []).map((op) => (
            <tr key={"ctx" + op.key} className="row-context">
              <td className="sign">·</td>
              <td className="num">{operandValue(op)}{op.value_minor !== undefined && op.currency && <div className="small muted">{money(op.value_minor, op.currency, exponents)}</div>}</td>
              <td><KeyChip k={op.key} onOpen={onOpen} active={activeKey === op.key} /></td>
              <td className="mono small">{op.file.replace("public/", "")}:{op.line}</td>
              <td className="small">{op.label}{op.timestamp ? ` · ${when(op.timestamp)}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <div className="formula mono">{calc.formula} <span className="muted small">· в мин. ед.; итог в {calc.currency}</span></div>
      <div className="rule">
        <KeyChip k={rule.key} onOpen={onOpen} active={activeKey === rule.key}
          missing={cited !== undefined && !citedSet.has(rule.key)} />
        {"boundary" in rule && <span> граница {when(String(rule.boundary))} (исключительно)</span>}
        {"local_date" in rule && <span> местная дата {String(rule.local_date)} ({String(rule.timezone)}), округление {String(rule.rounding)}</span>}
      </div>
      {calc.index_check && !calc.index_check.complete && (
        <div className="warn">Опубликованный индекс {calc.index_check.published.join(", ")} не совпадает с группировкой книги {calc.index_check.grouped.join(", ")}</div>
      )}
      <div className="derivation-result">
        Итог: <b>{money(calc.result_minor, calc.currency, exponents)}</b> <span className="muted mono">({minor(calc.result_minor)} мин. ед.)</span>
      </div>
    </div>
  );
}
