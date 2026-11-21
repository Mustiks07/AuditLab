import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, newOpKey, waitForOp } from "../api";
import type { OpStatus, RecordView } from "../api";
import { DiagnosticDownload, ErrorBox, KeyChip } from "../components";
import { money } from "../format";
import { compareLink, go, recordLink, runLink } from "../navigation";
import { fieldName, humanFact, named, recordName } from "../workspace/copy";
import { Icon } from "../workspace/Icon";
import { useResource } from "../workspace/resources";
import "./record.css";

const ACCOUNT_PURPOSE: Record<string, string> = {
  AR: "Задолженность по выставленным счетам в валюте счёта.", REVENUE: "Выручка в валюте операции.",
  CASH: "Поступление или возврат денег.", UNAPPLIED: "Платежи, ожидающие зачёта или возврата.",
  FX_VALUATION: "Оценка валютной операции в основной валюте.", FX_OFFSET: "Парная запись к валютной оценке.",
  CASH_RESERVE: "Перевод между денежными счетами внутри компании сохраняет общий остаток.",
};
const MONEY_FIELDS = ["debit_minor", "credit_minor", "amount_minor", "total_minor"];
const DATE_FIELDS = ["timestamp", "issued_at", "due_at", "date", "deadline"];
const CURRENCY_FIELDS = ["currency", "source_currency", "target_currency"];
const knownPolicy = (value: string) => recordName(`policy/${value}`);
const stringList = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
const plainObject = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const currencyCode = (value: unknown): string | undefined =>
  typeof value === "string" && /^[A-Z]{3}$/.test(value) ? value : undefined;

function sourceDate(value: unknown) {
  if (typeof value !== "string") return "Дата не указана";
  const date = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (date) return `${date[3]}.${date[2]}.${date[1]}`;
  const time = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!time || !Number.isFinite(Date.parse(value))) return "Дата недоступна";
  const seconds = time[5] && (time[5] !== "00" || time[6]) ? `:${time[5]}${time[6]?.replace(".", ",") ?? ""}` : "";
  const zone = time[7] === "Z" ? "UTC" : `UTC${time[7].replace("-", "−")}`;
  return `${time[3]}.${time[2]}.${time[1]}, ${time[4]}${seconds} · ${zone}`;
}

function amount(value: unknown, currency: string | undefined, exponents: Record<string, number>): string {
  if (value === undefined || value === null) return "Сумма не указана";
  if (typeof value !== "number" || !Number.isSafeInteger(value)) return "Точная сумма недоступна";
  if (!currency) return "Валюта не указана";
  const exponent = exponents[currency];
  if (!Number.isInteger(exponent) || exponent < 0 || exponent > 9) return "Разрядность валюты не указана";
  return money(value, currency, exponents);
}

function RecordLink({ runId, recordKey, onOpen, label }: {
  runId: string; recordKey: string; onOpen?: (key: string) => void; label: string;
}) {
  if (onOpen) return <KeyChip k={recordKey} onOpen={onOpen} label={label} />;
  return <a className="chip record-link" href={recordLink(runId, recordKey)}>{label}</a>;
}

type RecordProps = {
  runId: string;
  recordKey: string;
  onOpen?: (key: string) => void;
  highlightFields: string[];
  openedAt: number | null;
  listedAt?: number | null;
  cited: boolean;
  canChange: boolean;
  generator?: string;
  periodEnd?: string;
  relatedKeys?: Record<string, string>;
};

export function RecordPane(props: RecordProps) {
  const { runId, recordKey } = props;
  const [revision, revise] = useState(0);
  const result = useResource(JSON.stringify([runId, recordKey]), () => api.record(runId, recordKey), revision);
  if (result?.error !== undefined) {
    return <>
      <ErrorBox error={result.error} title="Документ недоступен" />
      <div className="record-actions">
        <button type="button" className="btn" onClick={() => revise(value => value + 1)}>Повторить загрузку</button>
        <a className="btn btn-ghost" href={runLink(runId)}>К проверке</a>
      </div>
    </>;
  }
  if (!result?.value) return <div className="loading" role="status" aria-busy="true">Открываем документ…</div>;
  return <RecordContents key={JSON.stringify([runId, recordKey])} {...props} record={result.value} />;
}

function RecordContents({ record, ...props }: RecordProps & { record: RecordView }) {
  const { runId, onOpen, highlightFields, openedAt, listedAt, cited, canChange, generator, periodEnd, relatedKeys } = props;
  const [changing, setChanging] = useState(false);
  const changeButton = useRef<HTMLButtonElement>(null);
  const focusOnReturn = useRef(false);
  useEffect(() => {
    if (!changing && focusOnReturn.current) {
      focusOnReturn.current = false;
      changeButton.current?.focus();
    }
  }, [changing]);

  const fields = record.fields;
  const currency = currencyCode(fields.currency);
  const date = typeof fields.timestamp === "string" ? fields.timestamp : undefined;
  const generated = (generator ?? "").startsWith("auditlab.generator");
  const boundary = Date.parse(periodEnd ?? "");
  const lastSettlement = typeof fields.payment_id === "string"
    && (fields.account === "UNAPPLIED" && Number(fields.debit_minor) > 0
      || fields.account === "AR" && Number(fields.credit_minor) > 0)
    && (!Number.isFinite(boundary) || Math.abs(Date.parse(date ?? "") - boundary) <= 60000);
  const canEditTime = canChange && date && Number.isFinite(Date.parse(date))
    && (generated ? record.table === "ledger" && lastSettlement : ["ledger", "payments"].includes(record.table));
  const nearby = record.context.filter(line => line.key !== null && line.key !== record.key);
  const rows: Array<{ key: string; label: string; value: ReactNode }> = [];
  const push = (key: string, value: ReactNode, label = fieldName(key)) => rows.push({ key, label, value });

  if (record.table === "policy") {
    for (const [key, value] of Object.entries(fields)) {
      if (key === "rule" || key === "recognition") {
        push(key, policyText(record.local_id, value), key === "recognition" ? "Дата признания" : "Условие");
      } else if (key === "deadline") {
        push(key, sourceDate(value), "Граница периода");
      } else if (key === "end_exclusive" && typeof value === "boolean") {
        push(key, value ? "Не входит в период" : "Входит в период", "Последний момент");
      } else if (key === "version") {
        push(key, typeof value === "string" && /^\d+(\.\d+)*$/.test(value) ? value : "Версия не указана", "Версия правила");
      } else if (key === "rules") {
        const rules = plainObject(value);
        if (rules) for (const [name, version] of Object.entries(rules)) {
          if (typeof version === "string" && /^\d+(\.\d+)*$/.test(version)) push(name, version, knownPolicy(name));
        }
      } else {
        const purpose = named(ACCOUNT_PURPOSE, key);
        if (purpose) push(key, purpose, humanFact(key, "account"));
      }
    }
  } else {
    for (const key of DATE_FIELDS) if (key in fields) push(key, sourceDate(fields[key]));
    if ("account" in fields) push("account", humanFact(fields.account, "account"));
    for (const key of MONEY_FIELDS) if (key in fields) push(key, amount(fields[key], currency, record.currency_exponents));
    for (const key of CURRENCY_FIELDS) if (key in fields) push(key, currencyCode(fields[key]) ?? "Валюта не указана");
    if ("rate" in fields) {
      const rate = String(fields.rate);
      push("rate", /^\d+(\.\d+)?$/.test(rate) ? rate.replace(".", ",") : "Курс недоступен");
    }
    if ("line_items" in fields && Array.isArray(fields.line_items)) {
      const items = (fields.line_items as unknown[]).map(plainObject).filter((item): item is Record<string, unknown> => item !== null);
      push("line_items", items.length > 0 ? <ul className="record-positions">
        {items.map((item, index) => {
          const description = item.description === "Commercial services" ? "Коммерческие услуги"
            : item.description === "Synthetic service" ? "Услуга" : humanFact(item.description, "description");
          return <li key={index}><span>{description || "Позиция счёта"}</span><strong>{amount(item.amount_minor, currency, record.currency_exponents)}</strong></li>;
        })}
      </ul> : "Позиции не указаны");
    }
  }

  const related = [
    ...[fields.invoice_id, ...stringList(fields.invoice_refs)].map(value => typeof value === "string" ? relatedKeys?.[`invoice/${value}`] : undefined),
    typeof fields.payment_id === "string" ? relatedKeys?.[`payment/${fields.payment_id}`] : undefined,
  ].filter((key): key is string => Boolean(key) && key !== record.key);
  const relationships = [...new Set(related)];
  const settlement = plainObject(fields.settlement_index);
  const settledRows = settlement ? stringList(settlement.unapplied_row_ids) : [];

  return (
    <article className="record c3-record">
      <header className="record-head">
        <span className="company-icon"><Icon name="document" /></span>
        <div>
          <h3>{recordName(record.key)}</h3>
          <p className="small muted">Запись {record.line} · {cited ? "Указана в доказательствах" : "Не указана в доказательствах"}</p>
          <p className="small muted">{openedAt !== null ? `Аудитор открыл на шаге ${openedAt}`
            : listedAt != null ? `Аудитор получил на шаге ${listedAt}` : "Аудитор не открывал эту запись"}</p>
        </div>
      </header>

      {rows.length > 0 ? <table className="fields">
        <tbody>
          {rows.map(({ key, label, value }) => {
            const hot = highlightFields.includes(key);
            return <tr key={key} className={hot ? "field-hot" : undefined}>
              <th scope="row">{label}</th>
              <td>{value}{hot && <span className="hot-tag record-highlight">В расчёте</span>}</td>
            </tr>;
          })}
        </tbody>
      </table> : <p className="muted">Подробности доступны в исходной записи.</p>}

      {settlement && <section className="record-related">
        <h4>Движения по платежу</h4>
        <p className="small muted">До {sourceDate(settlement.before)}, без учёта момента границы.</p>
        {settledRows.length ? <div className="record-links">
          {settledRows.map((id, index) => <RecordLink key={id} runId={runId} recordKey={`ledger/${id}`}
            onOpen={onOpen} label={`Движение ${index + 1}`} />)}
        </div> : <p className="muted">До границы движения отсутствуют.</p>}
      </section>}

      {relationships.length > 0 && <section className="record-related">
        <h4>Связанные документы</h4>
        <div className="record-links">
          {relationships.map((key, index) => <RecordLink key={key} runId={runId} recordKey={key} onOpen={onOpen}
            label={relationships.length === 1 ? recordName(key) : `${recordName(key)} · запись ${index + 1}`} />)}
        </div>
      </section>}
      {record.table === "payments" && relationships.length === 0 && stringList(fields.invoice_refs).length > 0
        && <p className="small muted">В назначении платежа указан счёт.</p>}

      <div className="record-actions">
        {canEditTime && !changing && <button ref={changeButton} type="button" className="btn"
          onClick={() => setChanging(true)}>Изменить дату и время</button>}
        <DiagnosticDownload data={record} label="Скачать исходную запись" />
      </div>
      {canEditTime && changing && <ChangeFact runId={runId} target={record.key} current={date}
        generator={generator} periodEnd={periodEnd} onCancel={() => { focusOnReturn.current = true; setChanging(false); }} />}

      {nearby.length > 0 && <details className="record-nearby">
        <summary>Соседние записи</summary>
        <div className="record-links">
          {nearby.map(line => <RecordLink key={line.line} runId={runId} recordKey={line.key!} onOpen={onOpen}
            label={`${recordName(line.key!)} · запись ${line.line}`} />)}
        </div>
      </details>}
    </article>
  );
}

function policyText(key: string, value: unknown) {
  if (typeof value === "string") {
    if (key === "allocation" && value.startsWith("Apply the entire designated payment before period_end (exclusive).")) {
      return "Платёж нужно полностью распределить до границы периода. Остаток равен сумме поступлений за вычетом зачётов. Список движений содержит все проводки платежа до границы."
        + (value.includes("Unassigned deposits can be refunded") ? " Платёж без назначения можно вернуть." : "");
    }
    if (key === "period" && /^invoice issue date for invoice/i.test(value)) {
      return "Счёт признаётся по дате выставления. Поступление и зачёт определяются датой операции.";
    }
    if (key === "fx_date" && value.startsWith("Local date of the source event")) {
      return value.includes("Quotes are target units per source unit")
        ? "Курс берётся на местную дату поступления платежа или выставления счёта. Он показывает, сколько единиц основной валюты приходится на одну единицу исходной."
        : "Курс берётся на местную дату исходной операции.";
    }
    if (key === "rounding" && value === "ROUND_HALF_UP") {
      return "Округление до ближайшей младшей единицы валюты. Ровно половина округляется в сторону увеличения абсолютной суммы.";
    }
  }
  return "Точное правило доступно в исходной записи.";
}

export function RecordPreview({ runId, recordKey }: { runId: string; recordKey: string }) {
  const result = useResource(recordKey ? JSON.stringify([runId, recordKey]) : null, () => api.record(runId, recordKey));
  if (!recordKey) return null;
  if (result?.error !== undefined) return <p className="small muted">Запись недоступна для просмотра.</p>;
  const record = result?.value;
  if (!record) return <p className="small muted" role="status">Загружаем запись…</p>;
  const fields = record.fields;
  const value = record.table === "ledger" ? (Number(fields.debit_minor) > 0 ? fields.debit_minor : fields.credit_minor)
    : fields.total_minor ?? fields.amount_minor;
  const timestamp = fields.timestamp ?? fields.issued_at;
  return <p className="small muted record-preview">
    {recordName(record.key)} · запись {record.line}
    {typeof fields.account === "string" && <> · {humanFact(fields.account, "account")}</>}
    {value !== undefined && <> · {amount(value, currencyCode(fields.currency), record.currency_exponents)}</>}
    {typeof timestamp === "string" && <> · {sourceDate(timestamp)}</>}
  </p>;
}

function splitDate(value: string) {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(Z|[+-]\d{2}:\d{2})$/.exec(value);
  return { local: match?.[1] ?? "", offset: match?.[2] && match[2] !== "Z" ? match[2] : "+00:00" };
}

function shiftDate(iso: string, minutes: number) {
  const { offset } = splitDate(iso);
  const direction = offset.startsWith("-") ? -1 : 1;
  const zoneMinutes = direction * (Number(offset.slice(1, 3)) * 60 + Number(offset.slice(4, 6)));
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return "";
  return new Date(time + (minutes + zoneMinutes) * 60000).toISOString().slice(0, 19) + offset;
}

type Start = (key: string) => Promise<OpStatus>;
export function useReviewOperation(runId: string) {
  const [status, setStatus] = useState<OpStatus | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const active = useRef(true);
  const running = useRef(false);
  const previous = useRef<{ signature: string; key: string }>();
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  async function submit(kind: "change" | "copy", signature: string, start: Start) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setStatus(null);
    setError(undefined);
    try {
      if (previous.current?.signature !== signature) previous.current = { signature, key: newOpKey(kind) };
      const key = previous.current.key;
      const first = await start(key);
      if (!active.current) return;
      setStatus(first);
      const done = first.status === "pending" || first.status === "running"
        ? await waitForOp(key, status => { if (active.current) setStatus(status); }) : first;
      if (!active.current) return;
      if (done.status === "completed" && !done.run_id) {
        setStatus(null);
        setError(new Error("Отсутствует ссылка на завершённую проверку."));
        return;
      }
      setStatus(done);
      if (done.status === "completed" && done.run_id) go(compareLink(runId, done.run_id));
    } catch (reason) {
      if (active.current) {
        setError(reason);
        setStatus(null);
      }
    } finally {
      running.current = false;
      if (active.current) setBusy(false);
    }
  }

  return { submit, status, error, busy };
}

export function ChangeFact({ runId, target, current, generator, periodEnd, onCancel }: {
  runId: string; target: string; current: string; generator?: string; periodEnd?: string; onCancel: () => void;
}) {
  const generated = (generator ?? "").startsWith("auditlab.generator");
  const options = generated && periodEnd && Number.isFinite(Date.parse(periodEnd)) ? [
    { name: "До границы периода", value: shiftDate(periodEnd, -1) },
    { name: "На границе периода", value: shiftDate(periodEnd, 0) },
    { name: "После границы периода", value: shiftDate(periodEnd, 1) },
  ] : [];
  const original = Date.parse(current);
  const [option, setOption] = useState(() => [...options].reverse().find(item => Date.parse(item.value) !== original)?.value ?? "");
  const [draft, setDraft] = useState(() => splitDate(shiftDate(current, 2)));
  const [prediction, setPrediction] = useState("");
  const title = useRef<HTMLHeadingElement>(null);
  useEffect(() => { title.current?.focus(); }, []);
  const { submit, status, error, busy } = useReviewOperation(runId);
  const value = options.length ? option : `${draft.local}${draft.offset}`;
  const canSubmit = (options.length > 0 || Boolean(draft.local) && /^[+-]\d{2}:\d{2}$/.test(draft.offset))
    && Number.isFinite(Date.parse(value)) && Date.parse(value) !== original;
  const submitChange = (ev: FormEvent) => {
    ev.preventDefault();
    if (!canSubmit) return;
    void submit("change", JSON.stringify([target, value, prediction]), key => api.createPair({
      source_run_id: runId, change: { target, field: "timestamp", value },
      prediction: prediction.trim() || undefined, operation_key: key,
    }));
  };

  return (
    <form className="change-form" onSubmit={submitChange} aria-label="Изменение даты и времени">
      <h3 ref={title} tabIndex={-1} className="change-title">Изменить время операции</h3>
      <p className="small muted">{generated
        ? "Для этой проверки можно изменить время последнего зачёта относительно границы периода."
        : "Изменение создаст отдельную проверку и покажет результат рядом с исходным."}</p>
      <p className="record-current"><span className="small muted">Текущее время</span> <strong>{sourceDate(current)}</strong></p>
      {options.length ? <fieldset className="record-timing" disabled={busy}>
        <legend>Новое время зачёта</legend>
        {options.map(item => <label key={item.name} className="record-timing-option">
          <input type="radio" name="settlement-time" value={item.value} checked={option === item.value}
            disabled={Date.parse(item.value) === original} onChange={() => setOption(item.value)} />
          <span>{item.name}<small>{sourceDate(item.value)}</small></span>
        </label>)}
      </fieldset> : <>
        <label className="field field-col">
          <span>Новая дата и время</span>
          <input type="datetime-local" step="1" required value={draft.local} disabled={busy}
            onChange={ev => setDraft({ ...draft, local: ev.target.value })} />
        </label>
        <label className="field field-col">
          <span>Смещение от UTC</span>
          <input required pattern="([+]|-)[0-9]{2}:[0-9]{2}" placeholder="+05:00" autoComplete="off"
            maxLength={6} value={draft.offset} onChange={ev => setDraft({ ...draft, offset: ev.target.value.replace("−", "-") })} disabled={busy} />
        </label>
        <div className="quick">
          <button type="button" className="btn btn-small btn-ghost" disabled={busy} onClick={() => setDraft(splitDate(shiftDate(current, 2)))}>+2 минуты</button>
          <button type="button" className="btn btn-small btn-ghost" disabled={busy} onClick={() => setDraft(splitDate(shiftDate(current, -2)))}>−2 минуты</button>
          <button type="button" className="btn btn-small btn-ghost" disabled={busy} onClick={() => setDraft(splitDate(shiftDate(current, 24 * 60)))}>+1 день</button>
        </div>
      </>}
      <label className="field field-col">
        <span>Ваш прогноз (необязательно)</span>
        <textarea rows={2} value={prediction} onChange={ev => setPrediction(ev.target.value)} disabled={busy} maxLength={500} />
      </label>
      <div className="change-actions">
        <button type="submit" className="btn btn-primary" disabled={busy || !canSubmit}>{busy ? "Проверка выполняется…" : "Запустить проверку"}</button>
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>Отмена</button>
      </div>
      {status && <OpLine status={status} />}
      {error !== undefined && <ErrorBox error={error} title="Не удалось запустить проверку" />}
    </form>
  );
}

export function OpLine({ status }: { status: OpStatus }) {
  if (status.status === "failed") return <ErrorBox error={status.error} title="Операция не завершилась" />;
  const labels = { pending: "Готовим проверку…", running: "Проверяем результат…", completed: "Проверка завершена" };
  return <p className={`op op-${status.status}`} role="status">{labels[status.status]}</p>;
}
