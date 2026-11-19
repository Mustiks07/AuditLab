import { useState } from "react";
import { api, newOpKey, OpStatus, waitForOp } from "../api";
import { go } from "../App";
import { ErrorBox } from "../components";
import { OpLine } from "./RecordPane";

export function RescoreForm({ runId, findingIndex, evidence, keys, onCancel }: {
  runId: string; findingIndex: number; evidence: string[]; keys: string[]; onCancel: () => void;
}) {
  const [from, setFrom] = useState(evidence[0] ?? "");
  const candidates = keys.filter((k) => !evidence.includes(k));
  const [to, setTo] = useState(candidates.find((k) => k.startsWith(from.split("/")[0] + "/")) ?? candidates[0] ?? "");
  const [status, setStatus] = useState<OpStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const busy = status !== null && (status.status === "pending" || status.status === "running");

  const run = async () => {
    setError(null);
    const key = newOpKey("copy");
    try {
      const first = await api.rescore(runId, { finding_index: findingIndex, replace: from, with: to, operation_key: key });
      setStatus(first);
      const done = first.status === "completed" ? first : await waitForOp(key, setStatus);
      setStatus(done);
      if (done.status === "completed" && done.run_id) go(`/compare/${runId}/${done.run_id}`);
    } catch (e) {
      setError(e);
      setStatus(null);
    }
  };

  return (
    <div className="change-form">
      <div className="change-title">Копия сдачи с заменой доказательства</div>
      <div className="small muted">Создаётся отдельная попытка с новым ID; оценщик пересчитывает копию. Исходный результат остаётся доступен.</div>
      <label className="field">
        <span>убрать</span>
        <select value={from} onChange={(e) => setFrom(e.target.value)} disabled={busy}>
          {evidence.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
      </label>
      <label className="field">
        <span>вместо</span>
        <select value={to} onChange={(e) => setTo(e.target.value)} disabled={busy}>
          {candidates.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
      </label>
      <div className="change-actions">
        <button type="button" className="btn btn-primary" onClick={run} disabled={busy || !from || !to}>{busy ? "Пересчёт…" : "Пересчитать копию"}</button>
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>Отмена</button>
      </div>
      {status && <OpLine status={status} />}
      {error !== null && <ErrorBox error={error} />}
    </div>
  );
}
