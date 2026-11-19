import { useEffect, useRef, useState } from "react";
import { api, ApiError, newOpKey, OpStatus, RecordView, waitForOp } from "../api";
import { go } from "../App";
import { ErrorBox, KeyChip } from "../components";
import { money, when } from "../format";

const MONEY_FIELDS = new Set(["debit_minor", "credit_minor", "amount_minor", "total_minor"]);

export function RecordPane({ runId, recordKey, onOpen, highlightFields, openedAt, cited, canChange }: {
  runId: string; recordKey: string; onOpen: (k: string) => void; highlightFields: string[];
  openedAt: number | null; cited: boolean; canChange: boolean;
}) {
  const [rec, setRec] = useState<RecordView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [changing, setChanging] = useState(false);
  const lineRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setRec(null);
    setError(null);
    setChanging(false);
    api.record(runId, recordKey).then(setRec).catch(setError);
  }, [runId, recordKey]);

  useEffect(() => {
    lineRef.current?.scrollIntoView({ block: "nearest" });
  }, [rec]);

  if (error) return <ErrorBox error={error} />;
  if (!rec) return <div className="loading">Открываю {recordKey}…</div>;
  const currency = String(rec.fields.currency ?? "KZT");
  const canEditTime = canChange && "timestamp" in rec.fields && (rec.table === "ledger" || rec.table === "payments");

  return (
    <div className="record">
      <div className="record-head">
        <KeyChip k={rec.key} active />
        <span className="mono file-ref">{rec.file} · строка {rec.line}</span>
        <span className={`small ${cited ? "ok-text" : "muted"}`}>{cited ? "процитирована в выводе" : "не процитирована"}</span>
        <span className="small muted">{openedAt ? `аудитор открыл на шаге ${openedAt}` : "аудитор не открывал отдельно"}</span>
      </div>

      <div className="file" aria-label={`Фрагмент файла ${rec.file}`}>
        {rec.header && (
          <div className="file-line file-header"><span className="ln">1</span><span className="txt">{rec.header}</span></div>
        )}
        {rec.context.filter((c) => !(rec.header && c.line === 1)).map((c) => (
          <div key={c.line} ref={c.line === rec.line ? lineRef : undefined}
            className={`file-line ${c.line === rec.line ? "file-line-active" : ""} ${c.key ? "file-line-link" : ""}`}
            onClick={() => c.key && c.key !== rec.key && onOpen(c.key)}>
            <span className="ln">{c.line}</span>
            <span className="txt">{c.text}</span>
          </div>
        ))}
        {rec.total_lines > rec.context.length + 1 && <div className="small muted file-more">всего строк в файле: {rec.total_lines}</div>}
      </div>

      <table className="fields">
        <tbody>
          {Object.entries(rec.fields).map(([name, value]) => {
            const hot = highlightFields.includes(name);
            const isMoney = MONEY_FIELDS.has(name) && typeof value === "number";
            return (
              <tr key={name} className={hot ? "field-hot" : ""}>
                <th className="mono">{name}</th>
                <td className="mono">
                  {value === null ? <span className="muted">null</span> : typeof value === "object" ? <ObjectValue value={value} onOpen={onOpen} table={rec.table} /> : String(value)}
                  {isMoney && <span className="muted"> = {money(value as number, currency)}</span>}
                  {name === "timestamp" && <span className="muted"> · {when(String(value))}</span>}
                </td>
                <td className="small">{hot && <span className="hot-tag">операнд суммы</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {canEditTime && !changing && (
        <button type="button" className="btn" onClick={() => setChanging(true)}>Изменить факт и запустить новый опыт…</button>
      )}
      {canEditTime && changing && (
        <ChangeFact runId={runId} target={rec.key} current={String(rec.fields.timestamp)} onCancel={() => setChanging(false)} />
      )}
    </div>
  );
}

function ObjectValue({ value, onOpen }: { value: object; onOpen: (k: string) => void; table: string }) {
  const v = value as Record<string, unknown>;
  if (Array.isArray(value)) return <span>{JSON.stringify(value)}</span>;
  if ("unapplied_row_ids" in v) {
    return (
      <span>
        до {when(String(v.before))} (искл.):{" "}
        {(v.unapplied_row_ids as string[]).map((id) => <KeyChip key={id} k={`ledger/${id}`} onOpen={onOpen} />)}
      </span>
    );
  }
  return <span>{JSON.stringify(value)}</span>;
}

function shiftIso(iso: string, minutes: number): string {
  const m = /^(.*)([+-]\d{2}:\d{2}|Z)$/.exec(iso);
  const offset = m ? m[2] : "+05:00";
  const sign = offset.startsWith("-") ? -1 : 1;
  const offMin = offset === "Z" ? 0 : sign * (parseInt(offset.slice(1, 3)) * 60 + parseInt(offset.slice(4, 6)));
  const t = new Date(new Date(iso).getTime() + minutes * 60000 + offMin * 60000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}T${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}:${pad(t.getUTCSeconds())}${offset}`;
}

export function ChangeFact({ runId, target, current, onCancel }: { runId: string; target: string; current: string; onCancel: () => void }) {
  const [value, setValue] = useState(shiftIso(current, 2));
  const [prediction, setPrediction] = useState("");
  const [status, setStatus] = useState<OpStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const busy = status !== null && (status.status === "pending" || status.status === "running");

  const run = async () => {
    setError(null);
    const key = newOpKey("change");
    try {
      const first = await api.createPair({ source_run_id: runId, change: { target, field: "timestamp", value }, prediction: prediction || undefined, operation_key: key });
      setStatus(first);
      const done = first.status === "completed" ? first : await waitForOp(key, setStatus);
      setStatus(done);
      if (done.status === "completed" && done.run_id) go(`/compare/${runId}/${done.run_id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e : String(e));
      setStatus(null);
    }
  };

  return (
    <div className="change-form">
      <div className="change-title">Изменить один факт: <span className="mono">{target}.timestamp</span></div>
      <div className="small muted">Будет создана новая неизменяемая компания с согласованными датами и прежними записанными суммами, по ней заново пройдёт аудитор и оценщик. Исходная попытка не меняется.</div>
      <label className="field">
        <span>было</span><code>{current}</code>
      </label>
      <label className="field">
        <span>станет</span>
        <input className="mono" value={value} onChange={(e) => setValue(e.target.value)} disabled={busy} spellCheck={false} />
      </label>
      <div className="quick">
        <button type="button" className="btn btn-small btn-ghost" disabled={busy} onClick={() => setValue(shiftIso(current, 2))}>+2 минуты</button>
        <button type="button" className="btn btn-small btn-ghost" disabled={busy} onClick={() => setValue(shiftIso(current, -2))}>−2 минуты</button>
        <button type="button" className="btn btn-small btn-ghost" disabled={busy} onClick={() => setValue(shiftIso(current, 24 * 60))}>+1 день</button>
      </div>
      <label className="field field-col">
        <span>Ваш прогноз до запуска (необязательно)</span>
        <textarea rows={2} value={prediction} onChange={(e) => setPrediction(e.target.value)} disabled={busy}
          placeholder="Например: появится расхождение 1 000 ₸ по курсу 510" />
      </label>
      <div className="change-actions">
        <button type="button" className="btn btn-primary" onClick={run} disabled={busy}>{busy ? "Опыт выполняется…" : "Запустить новый опыт"}</button>
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>Отмена</button>
      </div>
      {status && <OpLine status={status} />}
      {error !== null && <ErrorBox error={error} />}
    </div>
  );
}

export function OpLine({ status }: { status: OpStatus }) {
  const text: Record<string, string> = {
    pending: "ожидание запуска доверенного сервиса…",
    running: "генерация компании, прогон аудитора и оценка…",
    completed: `готово: ${status.run_id}`,
    failed: `ошибка запуска: ${status.error?.code} — ${status.error?.message}`,
  };
  return <div className={`op op-${status.status}`} role="status">{text[status.status]} <span className="muted mono small">{status.op_key}</span></div>;
}

