// Typed client for the console API (auditlab/demo/server.py). Same-origin, no external calls.

export interface RunMeta {
  run_id: string;
  episode_id: string;
  policy: string;
  mode: string;
  status: string;
  label?: string;
  label_text: string;
  company?: string;
  period?: { start?: string; end?: string };
  created_at?: string;
  engine?: Record<string, string>;
  steps?: number;
  inspection_steps?: number;
  reward?: number | null;
  findings?: number | null;
  pair_id?: string;
  parent_run_id?: string;
  children?: string[];
  change?: { target: string; field: string; value: string; moved?: string[] };
  prediction?: string;
  note?: string;
  reason?: string;
  journal_available?: boolean;
  derived_from?: { run_id: string; finding_index: number; replaced: { from: string; to: string } };
}

export interface Action { action: string; args: Record<string, unknown> }

export interface Step {
  step: number;
  action: Action;
  text: string;
  ok: boolean;
  error?: { code: string; message: string } | null;
  observation: Record<string, unknown>;
  remaining_steps: number;
  reward: number;
  terminated: boolean;
  truncated: boolean;
  opened_key: string | null;
  listed_keys: string[];
}

export interface Operand {
  key: string;
  file: string;
  line: number;
  field: string;
  value_minor?: number;
  value?: string;
  sign: number;
  label: string;
  timestamp?: string;
}

export interface Derivation {
  type: string;
  title: string;
  subject: string;
  operands: Operand[];
  context?: Operand[];
  rule: Record<string, unknown> & { key: string; text?: unknown };
  formula: string;
  result_minor: number | null;
  currency: string;
  affected_ids: string[];
  evidence: string[];
  expected_minor?: number;
  recorded_minor?: number;
  index_check?: { published: string[]; grouped: string[]; complete: boolean };
}

export interface Finding {
  type: string;
  affected_ids: string[];
  claimed_amount_minor: number;
  currency: string;
  evidence_ids: string[];
  explanation: string;
}

export interface FindingView {
  index: number;
  finding: Finding;
  type_name: string;
  evidence: { key: string; exists: boolean; file: string | null; line: number | null; opened_at_step: number | null; listed_at_step: number | null }[];
  affected: { id: string; key: string | null }[];
  derivation: Derivation | null;
  uncited_operands: string[];
  console_agrees: boolean;
  verdict: { index: number; matched: boolean; reason: string; source?: "per_finding" | "aggregate" } | null;
  verifier: { connected: boolean; text?: string; engine?: string; amount_minor?: number | null; currency?: string; agrees?: boolean };
}

export interface Score {
  tp: number; fp: number; fn: number;
  precision: number; recall: number; f1: number;
  reward: number; inspection_steps?: number;
}

export interface RunDetail {
  run: RunMeta;
  snapshot: { manifest: Record<string, unknown>; tables: Record<string, number> };
  journal: { available: boolean; lines: number };
  steps: Step[];
  submission: { episode_id: string; findings: Finding[] } | null;
  empty_submission: boolean;
  findings: FindingView[];
  score: Score | null;
  verifier: { connected: boolean; engine?: string };
  console_scan: { type: string; subject: string; result_minor: number; currency: string; affected_ids: string[] }[];
  children: string[];
  keys: string[];
}

export interface RecordView {
  key: string;
  table: string;
  local_id: string;
  file: string;
  line: number;
  raw: string;
  fields: Record<string, unknown>;
  context: { line: number; text: string; key: string | null }[];
  header: string | null;
  total_lines: number;
}

export interface Change { key: string; field: string | null; before: unknown; after: unknown; line_before?: number; line_after?: number }

export interface CompareSide {
  run: RunMeta;
  claims: { type: string; affected_ids: string[]; amount_minor: number; currency: string; evidence_ids: string[]; verdict: FindingView["verdict"]; unconfirmed: { key: string; label: string }[] }[];
  score: Score | null;
  empty: boolean;
}

export interface CompareView {
  left: CompareSide;
  right: CompareSide;
  changes: Change[];
  calculations: { type: string; subject: string; left: Derivation; right: Derivation; difference_minor: number }[];
  change_request?: RunMeta["change"];
  prediction?: string;
  derived_from?: RunMeta["derived_from"];
}

export interface OpStatus {
  op_key: string;
  status: "pending" | "running" | "completed" | "failed";
  run_id?: string;
  error?: { code: string; message: string };
}

export interface ReplayCheck {
  run_id: string;
  steps_checked: number;
  mismatches: { step: number }[];
  observations_match: boolean;
  session: string;
  score_match?: boolean | null;
  score_again?: Record<string, number>;
  evaluator?: string;
  score_note?: string;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, "offline", "Сервер консоли недоступен. Запустите: python -m auditlab.demo serve");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 202) {
    const detail = (body && (body.detail ?? body.error)) || {};
    const code = typeof detail === "object" && detail.code ? detail.code : `http_${res.status}`;
    const message = typeof detail === "object" && detail.message ? detail.message : JSON.stringify(detail);
    throw new ApiError(res.status, code, message);
  }
  return body as T;
}

export const api = {
  runs: () => call<{ runs: RunMeta[] }>("/demo/runs"),
  run: (id: string) => call<RunDetail>(`/demo/runs/${encodeURIComponent(id)}`),
  record: (id: string, key: string) =>
    call<RecordView>(`/demo/runs/${encodeURIComponent(id)}/records/${key.split("/").map(encodeURIComponent).join("/")}`),
  compare: (left: string, right: string) =>
    call<CompareView>(`/demo/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`),
  createPair: (body: { source_run_id: string; change: { target: string; field: string; value: string }; prediction?: string; operation_key: string }) =>
    call<OpStatus>("/demo/pairs", { method: "POST", body: JSON.stringify(body) }),
  rescore: (id: string, body: { finding_index: number; replace: string; with: string; operation_key: string }) =>
    call<OpStatus>(`/demo/runs/${encodeURIComponent(id)}/rescore`, { method: "POST", body: JSON.stringify(body) }),
  replay: (id: string) => call<ReplayCheck>(`/demo/runs/${encodeURIComponent(id)}/replay`, { method: "POST" }),
  op: (key: string) => call<OpStatus>(`/demo/ops/${encodeURIComponent(key)}`),
};

export function newOpKey(prefix: string): string {
  const rand = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");
  return `${prefix}-${rand}`;
}

export async function waitForOp(key: string, onUpdate: (s: OpStatus) => void, timeoutMs = 60000): Promise<OpStatus> {
  const started = Date.now();
  for (;;) {
    let status: OpStatus;
    try {
      status = await api.op(key);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) status = { op_key: key, status: "pending" };
      else throw e;
    }
    onUpdate(status);
    if (status.status === "completed" || status.status === "failed") return status;
    if (Date.now() - started > timeoutMs) {
      return { op_key: key, status: "failed", error: { code: "timeout", message: "Опыт не завершился за 60 с" } };
    }
    await new Promise((r) => setTimeout(r, 300));
  }
}
