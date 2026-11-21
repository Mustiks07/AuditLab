import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import { ErrorBox } from "../components";
import { humanText, recordName } from "../workspace/copy";
import { OpLine, RecordPreview, useReviewOperation } from "./RecordPane";

type RescoreProps = {
  runId: string;
  findingIndex: number;
  evidence: string[];
  keys: string[];
  onCancel: () => void;
  recordLabels?: ReadonlyMap<string, string>;
};

const sameCategory = (a: string, b: string) => a.split("/", 1)[0] === b.split("/", 1)[0];

function displayLabels(keys: string[], recordLabels?: ReadonlyMap<string, string>) {
  const labels = new Map<string, string>();
  const counts = new Map<string, number>();
  for (const key of keys) {
    const name = recordName(key);
    const count = (counts.get(name) ?? 0) + 1;
    counts.set(name, count);
    labels.set(key, humanText(recordLabels?.get(key)) ?? `${name} · вариант ${count}`);
  }
  return labels;
}

export function RescoreForm(props: RescoreProps) {
  return <RescoreFields key={JSON.stringify([props.runId, props.findingIndex, props.evidence])} {...props} />;
}

function RescoreFields({ runId, findingIndex, evidence, keys, onCancel, recordLabels }: RescoreProps) {
  const cited = [...new Set(evidence)];
  const unique = useMemo(() => [...new Set([...keys, ...evidence])], [keys, evidence]);
  const labels = useMemo(() => displayLabels(unique, recordLabels), [unique, recordLabels]);
  const candidates = unique.filter(key => !evidence.includes(key));
  const [from, setFrom] = useState(evidence[0] ?? "");
  const [to, setTo] = useState(candidates.find(key => sameCategory(key, from)) ?? candidates[0] ?? "");
  const title = useRef<HTMLHeadingElement>(null);
  useEffect(() => { title.current?.focus(); }, []);
  const { submit, status, error, busy } = useReviewOperation(runId);

  function changeEvidence(key: string) {
    setFrom(key);
    if (!sameCategory(key, to)) {
      const similar = candidates.find(candidate => sameCategory(candidate, key));
      if (similar) setTo(similar);
    }
  }

  const canSubmit = evidence.includes(from) && candidates.includes(to);
  function rescore(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    void submit("copy", JSON.stringify([findingIndex, from, to]), key => api.rescore(runId, {
      finding_index: findingIndex, replace: from, with: to, operation_key: key,
    }));
  }

  return (
    <form className="change-form" onSubmit={rescore} aria-label="Проверка с другим доказательством">
      <h3 ref={title} tabIndex={-1} className="change-title">Пересчитать с другим доказательством</h3>
      <p className="small muted">Пересчёт создаст отдельную копию. Исходный результат сохранится.</p>
      <label className="field field-col">
        <span>Доказательство для замены</span>
        <select value={from} onChange={ev => changeEvidence(ev.target.value)} disabled={busy || !cited.length} required>
          {!cited.length && <option value="">Нет доказательств</option>}
          {cited.map(key => <option key={key} value={key}>{labels.get(key)}</option>)}
        </select>
      </label>
      <RecordPreview runId={runId} recordKey={from} />
      <label className="field field-col">
        <span>Новая запись</span>
        <select value={to} onChange={ev => setTo(ev.target.value)} disabled={busy || !candidates.length} required>
          {!candidates.length && <option value="">Нет доступных записей</option>}
          {candidates.map(key => <option key={key} value={key}>{labels.get(key)}</option>)}
        </select>
      </label>
      <RecordPreview runId={runId} recordKey={to} />
      <div className="change-actions">
        <button type="submit" className="btn btn-primary" disabled={busy || !canSubmit}>{busy ? "Пересчитываем…" : "Пересчитать копию"}</button>
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>Отмена</button>
      </div>
      {status && <OpLine status={status} />}
      {error !== undefined && <ErrorBox error={error} title="Не удалось пересчитать копию" />}
    </form>
  );
}
