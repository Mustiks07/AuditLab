// Read-only research projection (auditlab/demo/research.py). Same-origin; the server never returns secrets.
import { ApiError } from "../api";

export type ProvenanceKind = "dev_fixture" | "measured" | "test_double" | "unknown";
export interface Provenance { kind: ProvenanceKind; label: string; note: string | null }
export interface StageLinks {
  experiment: string | null; failure: string | null; reduction: string | null; curriculum: string | null; comparison: string | null;
}
export interface Money { minor: number | null; currency: string }

export interface ControllerStatus {
  connected: boolean;
  reason?: string | null;
  budget_configured: boolean | null;
  api_key_configured: boolean | null;
  error?: string;
}
export interface AgentVariant {
  variant_id: string; label?: string; kind?: string; notes?: boolean;
  model?: { provider?: string | null; model_id?: string | null }; prompt_hash?: string | null;
}
export interface Progress {
  planned?: number; completed?: number; running?: number;
  failed?: { timeout?: number; provider_error?: number; invalid_response?: number; budget_stop?: number };
}
export interface Experiment {
  artifact_id: string; title?: string; status?: string; stop_reason?: { code?: string; message?: string } | null;
  agents?: AgentVariant[]; mode?: { protocol_id?: string; official?: boolean; action_limit?: number; time_limit_s?: number; note?: string };
  progress?: Progress; budget?: { currency?: string; limit?: number | null; spent?: number | null; note?: string };
  timing?: { started_at?: string; finished_at?: string; elapsed_s?: number | null };
  cost?: { input_tokens?: number | null; output_tokens?: number | null; usd?: number | null; note?: string };
  repetitions?: number; split?: { hash?: string; families?: Record<string, number>; rule?: string };
  protocol_id?: string; source_sha?: string; failures?: number; _provenance: Provenance;
}
export interface Evaluation { tp: number; fp: number; fn: number; reward: number; inspection_steps?: number }
export interface Claim { type: string; claimed_amount_minor: number; currency: string }
export interface AttemptSummary {
  artifact_id: string; variant_id?: string; repetition?: number; status?: string; company_label?: string;
  split?: string; family_id?: string; evaluation: Evaluation | null; evaluation_hidden: boolean;
  claims: Claim[]; usage?: { input_tokens?: number | null; output_tokens?: number | null; cost_usd?: number | null; cost_note?: string; elapsed_s?: number | null };
  steps: number; missing?: boolean; _provenance?: Provenance;
}
export interface StepView {
  step: number; action: string; ok: boolean | null; remaining_steps: number | null; table?: string;
  cursor?: boolean; records?: number; more?: boolean; opened_key?: string; findings?: number; error?: string;
}
export interface SubmittedFinding extends Claim { affected_ids?: string[]; evidence_ids?: string[]; explanation?: string }
export interface AttemptDetail extends AttemptSummary {
  snapshot_id: string | null; currency_exponents: Record<string, number>;
  steps_detail: StepView[]; received: { key: string; step: number; exists: boolean }[];
  submission: { findings?: SubmittedFinding[] } | null; started_at?: string; finished_at?: string;
  needed_received?: { key: string; received: boolean }[]; raw: Record<string, unknown>;
  error?: { code?: string; message?: string } | null;
}
export interface Situation {
  currency_exponents: Record<string, number>;
  period: { start?: string; end?: string; timezone?: string };
  calculations: {
    type: string; subject: string; currency: string; result_minor: number | null; formula?: string;
    operands: { key: string; value_minor?: number; sign: number; label: string; field: string }[];
    rule: { key: string; boundary?: string; boundary_exclusive?: boolean };
    summary: null | {
      currency: string; received_minor: number; allocated_minor: number; residual_minor: number | null;
      boundary?: string; boundary_exclusive?: boolean; after_boundary: { key: string; value_minor?: number }[];
    };
  }[];
}
export interface IndependentFinding { type: string; claimed_amount_minor: number; currency: string; evidence_ids?: string[] }
export interface Validation {
  artifact_id: string; missing?: boolean; snapshot_id?: string | null; company_label?: string;
  independent?: { engine?: string; status?: string; findings?: IndependentFinding[] };
  source_book?: { status?: string; reason?: string }; witness?: { status?: string; reason?: string };
}
export interface Failure {
  artifact_id: string; title?: string; experiment_id?: string; finding_type?: string; family_id?: string;
  variant_id?: string; criterion?: { text?: string; check?: string };
  frequency?: { failed?: number; total?: number }; interval?: { low?: number | null; high?: number | null; method?: string | null; reason?: string };
  alternatives?: string[]; reduction_id?: string; validation_id?: string; _provenance: Provenance;
}
export interface FailureView {
  links: StageLinks;
  failure: Failure; attempts: AttemptDetail[]; validation: Validation | null;
  independent: { engine?: string; status?: string; findings: IndependentFinding[] } | null;
  situation: Situation | null; snapshot_id: string | null; needed: string[];
}
export interface ExperimentView {
  links: StageLinks;
  experiment: Experiment; attempts: AttemptSummary[]; missing_attempts: string[];
  failures: (Failure & { finding_type?: string })[]; reductions: { artifact_id: string }[];
  curricula: { artifact_id: string; title?: string }[]; comparisons: { artifact_id: string; title?: string }[];
  controller: ControllerStatus;
}
export interface SnapshotSummary {
  snapshot_id: string; tables: Record<string, number>; currency_exponents: Record<string, number>;
  period: { start?: string; end?: string; timezone?: string };
}
export interface ReductionSide {
  snapshot_ref?: string; snapshot_id: string | null; validation: Validation | null; snapshot: SnapshotSummary | null;
  ledger_rows?: number; documents?: number;
}
export interface Family {
  relation: string; title?: string; expected_change?: string; expected: [string, number, string][];
  actual: AttemptSummary[]; snapshot_id: string | null; validation: Validation | null; currency_exponents: Record<string, number>;
}
export interface ReductionView {
  links: StageLinks;
  reduction: { artifact_id: string; failure_id?: string; experiment_id?: string; transformations?: string[]; method?: string; _provenance: Provenance };
  finding_type?: string | null; variants: Record<string, string>;
  original: ReductionSide; reduced: ReductionSide;
  repeats: Record<string, { failed?: number; total?: number; attempts: AttemptSummary[] }>;
  families: Family[];
}
export interface CurriculumItem { item_id: string; title?: string; split?: string; origin?: string; family_id?: string; validation?: string; hash?: string }
export interface Training {
  artifact_id: string; kind?: string; state?: string; curriculum_id?: string;
  base_model?: { provider?: string | null; model_id?: string | null }; settings?: Record<string, unknown>;
  budget?: { currency?: string; limit?: number | null; spent?: number | null };
  job?: { job_id?: string | null; checkpoint?: string | null }; result?: Record<string, unknown> | null;
  error?: { code?: string; message?: string } | null; _provenance: Provenance;
}
export interface CurriculumView {
  links: StageLinks;
  curriculum: {
    artifact_id: string; title?: string; experiment_id?: string; failure_ids?: string[]; reduction_ids?: string[];
    split_hash?: string; splits?: Record<string, number>; items?: CurriculumItem[];
    filtered?: { title?: string; reason?: string }[]; export_formats?: string[]; _provenance: Provenance;
  };
  training: Training[];
}
export interface VariantMetrics {
  micro_f1?: number | null; mean_reward?: number | null; clean_fp?: { count: number; denominator: number } | null;
  completion?: { completed: number; total: number } | null; timeouts?: number | null; errors?: number | null;
  actions_mean?: number | null; tokens?: number | null; time_s?: number | null; cost_usd?: number | null;
}
export interface ComparisonView {
  links: StageLinks;
  comparison: {
    artifact_id: string; title?: string; experiment_id?: string; status?: string;
    verification?: { status?: string; reason?: string }; verified_by?: string | null;
    sample?: { families?: number; episodes?: number; repetitions?: number; split?: string; note?: string };
    variants?: { variant_id: string; label?: string; training_id?: string | null; metrics: VariantMetrics | null; missing_reason?: string }[];
    paired?: { a: string; b: string; metric: string; difference: number; interval: [number, number] | null; method?: string; unit?: string }[];
    missing?: { variant_id: string; reason: string; message?: string }[]; _provenance: Provenance;
  };
}
export interface Overview {
  experiments: Experiment[];
  curricula: { artifact_id: string; title?: string; _provenance: Provenance }[];
  training: { artifact_id: string; kind?: string; state?: string; curriculum_id?: string; _provenance: Provenance }[];
  comparisons: { artifact_id: string; title?: string; status?: string; _provenance: Provenance }[];
  incompatible: { file: string; kind: string | null; reason: string }[];
}
export interface ResearchStatus {
  roots_configured: boolean; counts: Record<string, number>; incompatible: number;
  provenance: ProvenanceKind[]; controller: ControllerStatus;
}
export interface RecordView {
  key: string; table: string; file: string; line: number; fields: Record<string, unknown>;
  currency_exponents: Record<string, number>; context: { line: number; text: string; key: string | null }[];
  header: string | null; total_lines: number;
}

const TIMEOUT_MS = 15000;

async function call<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  const timer = new AbortController();
  const timeout = setTimeout(() => timer.abort(), TIMEOUT_MS);
  const abort = () => timer.abort();
  signal?.addEventListener("abort", abort);
  let res: Response;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init, signal: timer.signal });
  } catch {
    if (signal?.aborted) throw new ApiError(0, "aborted", "Запрос отменён");
    throw new ApiError(0, timer.signal.aborted ? "timeout" : "offline", "Сервис исследований недоступен");
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && typeof body === "object" ? (body.detail ?? {}) : {};
    const code = typeof detail === "object" && typeof detail.code === "string" ? detail.code : `http_${res.status}`;
    throw new ApiError(res.status, code, typeof detail?.message === "string" ? detail.message : "");
  }
  if (body === null || typeof body !== "object") throw new ApiError(res.status, "bad_response", "Ответ не распознан");
  return body as T;
}

const id = encodeURIComponent;
export const research = {
  status: (signal?: AbortSignal) => call<ResearchStatus>("/research/status", undefined, signal),
  overview: (signal?: AbortSignal) => call<Overview>("/research/overview", undefined, signal),
  experiment: (key: string, signal?: AbortSignal) => call<ExperimentView>(`/research/experiments/${id(key)}`, undefined, signal),
  failure: (key: string, signal?: AbortSignal) => call<FailureView>(`/research/failures/${id(key)}`, undefined, signal),
  attempt: (key: string, signal?: AbortSignal) => call<AttemptDetail>(`/research/attempts/${id(key)}`, undefined, signal),
  reduction: (key: string, signal?: AbortSignal) => call<ReductionView>(`/research/reductions/${id(key)}`, undefined, signal),
  curriculum: (key: string, signal?: AbortSignal) => call<CurriculumView>(`/research/curricula/${id(key)}`, undefined, signal),
  comparison: (key: string, signal?: AbortSignal) => call<ComparisonView>(`/research/comparisons/${id(key)}`, undefined, signal),
  record: (snapshot: string, key: string, signal?: AbortSignal) =>
    call<RecordView>(`/research/snapshots/${id(snapshot)}/records/${key.split("/").map(id).join("/")}`, undefined, signal),
  control: (experiment: string, action: "start" | "stop") =>
    call<Record<string, unknown>>(`/research/experiments/${id(experiment)}/${action}`, { method: "POST" }),
};
