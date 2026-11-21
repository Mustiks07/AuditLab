import type { ReactNode } from "react";
import { ApiError, type Derivation, type Operand, type RunMeta } from "./api";
import { money } from "./format";
import { fieldName, friendlyError, humanFact, operandName, recordName } from "./workspace/copy";
import { findingName, runStatus } from "./workspace/model";

export function LabelBadge({ run }: { run: RunMeta }) {
  if (run.label === "live_experiment") return <span className="pill">Новая проверка</span>;
  if (run.label === "rescore_copy") return <span className="pill">Пересчитанная копия</span>;
  return null;
}

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status}`}>{runStatus(status)}</span>;
}

export function KeyChip({ k, onOpen, active, missing, hint, label }: {
  k: string; onOpen?: (key: string) => void; active?: boolean; missing?: boolean; hint?: string; label?: string;
}) {
  const className = `chip ${active ? "chip-active" : ""} ${missing ? "chip-missing" : ""}`;
  const title = label ?? recordName(k);
  if (!onOpen) return <span className={className} title={hint}>{title}</span>;
  return <button type="button" className={className} onClick={() => onOpen(k)}
    aria-pressed={active === undefined ? undefined : active} title={hint}>{title}</button>;
}

export function Panel({ title, extra, children, className = "" }: {
  title: ReactNode; extra?: ReactNode; children: ReactNode; className?: string;
}) {
  return <section className={`panel ${className}`}>
    <div className="panel-head"><h2>{title}</h2>{extra}</div>
    <div className="panel-body">{children}</div>
  </section>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty">
    <div className="empty-title">{title}</div>
    {children && <div className="empty-body">{children}</div>}
  </div>;
}

export function DiagnosticDownload({ data, label = "Скачать диагностику" }: { data: unknown; label?: string }) {
  function download() {
    const file = new Blob([JSON.stringify(data, null, 2) ?? ""], { type: "application/json" });
    const url = URL.createObjectURL(file);
    const link = document.createElement("a");
    link.href = url;
    link.download = "auditlab-report.json";
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <button className="text-link" type="button" onClick={download}>{label}</button>;
}

export function ErrorBox({ error, title }: { error: unknown; title?: string }) {
  const message = friendlyError(error instanceof ApiError ? error.code : undefined);
  return <div className="error-box" role="alert">
    {title && <strong>{title}</strong>}
    <div>{message}</div>
    <DiagnosticDownload data={error instanceof ApiError ?
      { status: error.status, code: error.code, message: error.message } :
      { message: error instanceof Error ? error.message : String(error) }}/>
  </div>;
}

function valueOf(op: Operand, exponents: Record<string, number>) {
  if (op.value_minor !== undefined && op.currency) return money(op.value_minor, op.currency, exponents);
  return humanFact(op.value, op.field);
}

export function DerivationView({ calc, onOpen, cited, activeKey, exponents }: {
  exponents: Record<string, number>; calc: Derivation; onOpen: (key: string) => void;
  cited?: string[]; activeKey?: string | null;
}) {
  const citedSet = new Set(cited ?? []);
  const rule = calc.rule;
  return <div className="derivation">
    <h3 className="derivation-title">Расчёт по документам</h3>
    <div className="operands-scroll" role="region" aria-label={`Расчёт: ${findingName(calc.type)}`} tabIndex={0}>
      <table className="operands">
        <thead><tr><th scope="col">Операция</th><th scope="col">Значение</th><th scope="col">Источник</th></tr></thead>
        <tbody>
          {[...calc.operands, ...(calc.context ?? [])].map((op, index) => {
            const context = index >= calc.operands.length;
            const label = operandName(op, calc.type, context);
            return <tr key={`${op.key}:${op.field}`} className={activeKey === op.key ? "row-active" : context ? "row-context" : undefined}>
              <td>{label}{op.timestamp && <small className="operand-date">{humanFact(op.timestamp)}</small>}</td>
              <td className="money-cell">{valueOf(op, exponents)}</td>
              <td><KeyChip k={op.key} label={`${recordName(op.key)} · ${fieldName(op.field).toLocaleLowerCase("ru")}`}
                onOpen={onOpen} active={activeKey === op.key} missing={!context && cited !== undefined && !citedSet.has(op.key)}/></td>
            </tr>;
          })}
        </tbody>
      </table>
    </div>
    {calc.type === "fx_conversion" && calc.expected_minor != null && calc.recorded_minor != null && <p>
      По курсу: <b>{money(calc.expected_minor, calc.currency, exponents)}</b> · В учёте: <b>{money(calc.recorded_minor, calc.currency, exponents)}</b>
    </p>}
    <div className="rule">
      <KeyChip k={rule.key} onOpen={onOpen} active={activeKey === rule.key}
        missing={cited !== undefined && !citedSet.has(rule.key)}/>
      {typeof rule.boundary === "string" && <span>Граница: {humanFact(rule.boundary)}{rule.boundary_exclusive === true ? ", не включается" : ""}</span>}
      {typeof rule.local_date === "string" && <span>Местная дата курса: {humanFact(rule.local_date)}</span>}
    </div>
    {calc.index_check && !calc.index_check.complete && <p className="warn">Перечень движений платежа расходится с проводками.</p>}
    <div className="derivation-result">Итог: <b>{money(calc.result_minor, calc.currency, exponents)}</b></div>
  </div>;
}
