"""Development fixtures for the research console, built from real open data — never measurements.

    python -m auditlab.demo.research_fixtures OUT_DIR

Until the integrator's controller and the shared artifact schemas exist, the research screens need
coherent artifacts in the field layout of docs/plans/agent-improvement/CONTRACTS.md. This builder
makes them honestly:

* companies are real snapshots of ``datasets/open`` (and one generator variant with new document IDs);
* every attempt is executed by the real runner (``auditlab.runner.make_env("train")``) and scored by
  the real evaluator; the observations are what the public environment really returned;
* the *agent* is a scripted fixture policy, not an API model. Its actions are fixed in this file so
  that one strategy reproduces a documented weakness. Nothing here is a model's answer;
* the independent check is the SQL verifier (``auditlab.verifier``) on the public files only;
* repetition counts, intervals, training jobs and comparison metrics are illustrative values for the
  interface. They are marked ``provenance.kind = "dev_fixture"`` and the console shows that label.

Truth stays under ``OUT_DIR/trusted`` and is never served by the console.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .paths import REPO

OPEN = REPO / "datasets" / "open"
COMPANY_B = OPEN / "train" / "E651B6A63DDA2448A91DC825714D7732E"   # settlement after the boundary: residual 20 000 ₸
COMPANY_A = OPEN / "train" / "E6476455CA5F6F7CF4A34F78E68AD54C0"   # same family, settlement before: defect fixed
CLEAN = OPEN / "train" / "E04F4ED6D6765D816DF94C62477B6EC4C"       # clean company of another family
PUBLIC_FILES = ("manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json")
SCHEMA = "1.0"
SOURCE_SHA = "dev-fixture"
PROTOCOL = "research-dev-fixture-1"
PROVENANCE = {
    "kind": "dev_fixture",
    "note": ("Фикстура разработки интерфейса. Компании и документы — открытый синтетический набор AuditLab; "
             "действия задаёт скрипт фикстуры, их исполняет настоящий раннер, оценивает настоящий оценщик. "
             "Это не ответы модели и не измерения эксперимента."),
}
FIXED_TIME = "2026-11-21T10:00:00+05:00"


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _hash_dir(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return "sha256:" + digest.hexdigest()


def _common(artifact_kind: str, artifact_id: str, **fields) -> dict:
    return {"schema_version": SCHEMA, "artifact_kind": artifact_kind, "artifact_id": artifact_id,
            "source_sha": SOURCE_SHA, "protocol_id": PROTOCOL, "created_at": FIXED_TIME,
            "provenance": PROVENANCE, **fields}


# ------------------------------------------------------------------ snapshots
def _copy_public(src: Path, dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for name in PUBLIC_FILES:
        shutil.copyfile(src / name, dst / name)
    return dst


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _payment_case(public: Path) -> dict:
    """The designated payment whose last settlement is closest to the period boundary.

    In the boundary family this is the payment the generator moves across the boundary; in a clean
    company it is simply the most recently settled designated payment. Chosen from public files only.
    """
    payments = _read_jsonl(public / "payments.jsonl")
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as handle:
        ledger = list(csv.DictReader(handle))
    best = None
    for doc in payments:
        index = doc.get("settlement_index") or {}
        rows = [r for r in ledger if r["payment_id"] == doc["payment_id"] and r["account"] == "UNAPPLIED"]
        debits = [r["timestamp"] for r in rows if int(r["debit_minor"]) > 0]
        if index and len(doc.get("invoice_refs") or []) == 1 and debits and any(int(r["credit_minor"]) > 0 for r in rows):
            key = max(debits)
            if best is None or key > best[0]:
                best = (key, {"doc": doc, "rows": rows})
    if best is None:
        raise RuntimeError(f"no designated payment with a settlement index in {public}")
    return best[1]


def reduce_company(src_public: Path, dst_public: Path, payment_id: str) -> dict:
    """Keep only the documents and ledger rows of one payment and its invoice (a fixture reduction)."""
    _copy_public(src_public, dst_public)
    payments = [d for d in _read_jsonl(src_public / "payments.jsonl") if d["payment_id"] == payment_id]
    invoice_ids = {ref for d in payments for ref in d.get("invoice_refs") or []}
    invoices = [d for d in _read_jsonl(src_public / "invoices.jsonl") if d["invoice_id"] in invoice_ids]
    with (src_public / "ledger.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header, ledger = reader.fieldnames, list(reader)
    journals = {r["journal_id"] for r in ledger if r["payment_id"] == payment_id or r["invoice_id"] in invoice_ids}
    kept = [r for r in ledger if r["journal_id"] in journals]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(kept)
    (dst_public / "ledger.csv").write_text(out.getvalue(), encoding="utf-8")
    for name, docs in (("payments.jsonl", payments), ("invoices.jsonl", invoices)):
        (dst_public / name).write_text("".join(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n"
                                               for d in docs), encoding="utf-8")
    manifest = json.loads((src_public / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(ledger_rows=len(kept), document_count=len(payments) + len(invoices),
                    episode_id=manifest["episode_id"] + "-R")
    (dst_public / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ledger_rows": [len(ledger), len(kept)],
            "documents": [len(_read_jsonl(src_public / "payments.jsonl")) + len(_read_jsonl(src_public / "invoices.jsonl")),
                          len(payments) + len(invoices)]}


def renamed_company(dst: Path) -> Path:
    """Company B regenerated with a new snapshot nonce: same economics, new document IDs and order."""
    from auditlab.generator import generate_episode
    provenance = json.loads((COMPANY_B / "private" / "provenance.json").read_text(encoding="utf-8"))
    config = {**provenance["config"], "snapshot_nonce": "fixture-renamed-documents"}
    generate_episode(provenance["seed"], config, dst)
    return dst


# ------------------------------------------------------------------ scripted fixture agent
class ScriptedAgent:
    """Fixed action plan of a fixture. ``read_ledger`` decides whether ledger pages are requested."""

    def __init__(self, public: Path, read_ledger: bool, claim: str):
        self.case = _payment_case(public)
        self.read_ledger, self.claim = read_ledger, claim
        self.queue: list = [("list", "policy"), ("list", "payments"),
                            ("get_document", self.case["doc"]["doc_id"])]
        if read_ledger:
            self.queue.append(("list_all", "ledger"))
        self.queue.append(("submit", None))
        self.cursor = None
        self.episode_id = None
        self.seen: dict[str, dict] = {}

    def _finding(self) -> dict:
        doc = self.case["doc"]
        invoice = doc["invoice_refs"][0]
        rows = self.case["rows"]
        credit = next(r for r in rows if int(r["credit_minor"]) > 0)
        before = doc["settlement_index"]["before"]
        debits = [r for r in rows if int(r["debit_minor"]) > 0 and r["timestamp"] < before and r["row_id"] in self.seen]
        if self.claim == "full_payment":
            amount, evidence = int(doc["amount_minor"]), [f"payments/{doc['doc_id']}", "policy/allocation"]
        else:
            amount = int(credit["credit_minor"]) - sum(int(r["debit_minor"]) for r in debits)
            if amount <= 0:
                return None
            evidence = [f"payments/{doc['doc_id']}", f"ledger/{credit['row_id']}",
                        *[f"ledger/{r['row_id']}" for r in debits], "policy/allocation"]
        return {"type": "payment_allocation", "affected_ids": sorted([f"payment/{doc['payment_id']}", f"invoice/{invoice}"]),
                "claimed_amount_minor": amount, "currency": doc["currency"], "evidence_ids": evidence,
                "explanation": "Остаток назначенного платежа не распределён на отчётную дату."}

    def act(self, observation: dict) -> dict:
        if self.episode_id is None:
            self.episode_id = (observation.get("manifest") or {}).get("episode_id")
        result = observation.get("result") if isinstance(observation, dict) else None
        if isinstance(result, dict):
            for record in result.get("records") or []:
                if isinstance(record, dict) and record.get("row_id"):
                    self.seen[record["row_id"]] = record
        while self.queue:
            kind, arg = self.queue[0]
            if kind == "list":
                self.queue.pop(0)
                return {"action": "list_records", "args": {"table": arg, "cursor": None, "limit": 50}}
            if kind == "get_document":
                self.queue.pop(0)
                return {"action": "get_document", "args": {"doc_id": arg}}
            if kind == "list_all":
                if self.cursor == "done":
                    self.queue.pop(0)
                    self.cursor = None
                    continue
                cursor = None if self.cursor is None else (result or {}).get("next_cursor")
                if self.cursor is not None and cursor is None:
                    self.queue.pop(0)
                    self.cursor = None
                    continue
                self.cursor = cursor or "started"
                return {"action": "list_records", "args": {"table": arg, "cursor": cursor, "limit": 50}}
            self.queue.pop(0)
            finding = self._finding()
            return {"action": "submit", "args": {"episode_id": self.episode_id, "findings": [finding] if finding else []}}
        raise RuntimeError("plan exhausted")


def run_attempt(public: Path, truth: dict, agent: ScriptedAgent, work: Path) -> dict:
    """Execute the scripted plan through the real runner and return its export files."""
    from auditlab.runner import make_env
    episode = work / "episode"
    _copy_public(public, episode / "public")
    _dump(episode / "private" / "truth.json", truth)
    export = work / "export"
    env = make_env("train")
    observation, _ = env.reset(0, {"snapshot_dir": str(episode), "export_dir": str(export),
                                   "policy_name": "fixture-scripted-agent"})
    for _ in range(100):
        observation, _, terminated, truncated, _ = env.step(agent.act(observation))
        if terminated or truncated:
            break
    env.close()
    lines = [json.loads(line) for line in (export / "trajectories.jsonl").read_text(encoding="utf-8").splitlines() if line]
    return {"run": json.loads((export / "run.json").read_text(encoding="utf-8")),
            "score": json.loads((export / "score.json").read_text(encoding="utf-8")),
            "submission": json.loads((export / "submission.json").read_text(encoding="utf-8")),
            "steps": lines}


# ------------------------------------------------------------------ artifacts
def _truth(company_dir: Path) -> dict:
    return json.loads((company_dir / "private" / "truth.json").read_text(encoding="utf-8"))


def _validation(vid: str, public: Path, label: str, snapshot_ref: str) -> dict:
    from auditlab.verifier import verify_snapshot
    findings = [dict(f) for f in verify_snapshot(public)]
    return _common("validation", vid, snapshot_ref=snapshot_ref, company_label=label,
                   source_book={"status": "not_checked", "reason": "Проверка исходной книги выполняется генератором; "
                                "в фикстуре не повторялась."},
                   independent={"engine": "SQL-сверка AuditLab по публичным файлам", "status": "passed",
                                "findings": findings},
                   evidence_available={"status": "passed"},
                   witness={"status": "not_checked", "reason": "Траектория свидетеля появится у независимого проверяющего."})


def _attempt(aid: str, experiment_id: str, variant: str, repetition: int, episode: dict, snapshot_ref: str,
             result: dict, *, status: str = "completed", seconds: float = 0.0, tokens: tuple = (0, 0)) -> dict:
    inspections = sum(1 for s in result["steps"] if (s.get("action") or {}).get("action") != "submit")
    return _common("attempt", aid, experiment_id=experiment_id, variant_id=variant, repetition=repetition,
                   episode=episode, snapshot_ref=snapshot_ref, status=status,
                   started_at=FIXED_TIME, finished_at=FIXED_TIME,
                   steps=[{"step": s["step"], "action": s["action"], "observation": s["observation"]} for s in result["steps"]],
                   submission=result["submission"],
                   evaluation={"visibility": "open", "evaluator": "auditlab.evaluator",
                               "tp": result["score"]["tp"], "fp": result["score"]["fp"], "fn": result["score"]["fn"],
                               "reward": result["score"]["reward"], "inspection_steps": inspections},
                   usage={"input_tokens": tokens[0], "output_tokens": tokens[1], "cost_usd": None,
                          "cost_note": "Скриптовая политика фикстуры не вызывает модель: стоимости нет.",
                          "elapsed_s": seconds})


def build(out: Path) -> dict:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; fixtures are written to a new directory")
    out.mkdir(parents=True, exist_ok=True)
    snaps = out / "snapshots"
    companies = {
        "company-b": ("Компания B · зачёт после границы", COMPANY_B / "public", _truth(COMPANY_B), "train", "family-boundary"),
        "company-a": ("Компания A · зачёт до границы", COMPANY_A / "public", _truth(COMPANY_A), "train", "family-boundary"),
        "clean": ("Чистая компания другой семьи", CLEAN / "public", _truth(CLEAN), "train", "family-clean"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        renamed = renamed_company(Path(tmp) / "renamed")
        companies["company-b-renamed"] = ("Компания B · новые номера документов", renamed / "public",
                                          json.loads((renamed / "private" / "truth.json").read_text()), "train",
                                          "family-boundary")
        for name, (_, public, truth, _, _) in companies.items():
            _copy_public(public, snaps / name / "public")
            _dump(out / "trusted" / name / "truth.json", truth)
    case = _payment_case(snaps / "company-b" / "public")
    sizes = reduce_company(snaps / "company-b" / "public", snaps / "company-b-reduced" / "public", case["doc"]["payment_id"])
    reduced_truth = {**companies["company-b"][2], "episode_id": companies["company-b"][2]["episode_id"] + "-R",
                     "issues": [i for i in companies["company-b"][2]["issues"] if i["type"] == "payment_allocation"]}
    companies["company-b-reduced"] = ("Компания B · сокращённый случай", snaps / "company-b-reduced" / "public",
                                      reduced_truth, "train", "family-boundary")
    _dump(out / "trusted" / "company-b-reduced" / "truth.json", reduced_truth)

    exp = "exp-memory-notes"
    attempts: dict[str, dict] = {}

    def attempt(aid, company, variant, repetition, read_ledger, claim, seconds, tokens):
        label, _, truth, split, family = companies[company]
        public = snaps / company / "public"
        with tempfile.TemporaryDirectory() as work:
            result = run_attempt(public, truth, ScriptedAgent(public, read_ledger, claim), Path(work))
        episode = {"company_label": label, "family_id": family, "split": split,
                   "snapshot_hash": _hash_dir(public)}
        art = _attempt(aid, exp, variant, repetition, episode, f"snapshots/{company}/public", result,
                       seconds=seconds, tokens=tokens)
        attempts[aid] = art
        _dump(out / "experiments" / exp / "attempts" / f"{aid}.json", art)
        return art

    # Plain variant: does not request ledger pages and claims the whole payment; notes variant reads them.
    attempt("att-b-plain-1", "company-b", "plain", 1, False, "full_payment", 21.4, (18200, 640))
    attempt("att-b-plain-2", "company-b", "plain", 2, False, "full_payment", 19.8, (17950, 610))
    attempt("att-b-plain-3", "company-b", "plain", 3, True, "residual", 34.1, (29400, 820))
    attempt("att-b-notes-1", "company-b", "notes", 1, True, "residual", 38.7, (31200, 1430))
    attempt("att-r-plain-1", "company-b-reduced", "plain", 1, False, "full_payment", 9.2, (6100, 520))
    attempt("att-r-plain-2", "company-b-reduced", "plain", 2, False, "full_payment", 8.7, (6050, 515))
    attempt("att-a-plain-1", "company-a", "plain", 1, False, "full_payment", 20.3, (18100, 630))
    attempt("att-a-notes-1", "company-a", "notes", 1, True, "residual", 37.9, (30800, 1390))
    attempt("att-n-plain-1", "company-b-renamed", "plain", 1, False, "full_payment", 20.9, (18300, 650))
    attempt("att-c-plain-1", "clean", "plain", 1, False, "full_payment", 18.6, (16900, 600))

    for name in ("company-b", "company-b-reduced", "company-a", "clean", "company-b-renamed"):
        _dump(out / "validations" / f"val-{name}.json",
              _validation(f"val-{name}", snaps / name / "public", companies[name][0], f"snapshots/{name}/public"))

    _dump(out / "experiments" / exp / "experiment.json", _common(
        "experiment", exp, title="Агент без памяти и агент с заметками", status="completed",
        split={"hash": "sha256:fixture-split", "families": {"train": 2, "dev": 0, "holdout": 0},
               "rule": "Чистые и ошибочные варианты одной экономической семьи в одном разделе."},
        agents=[{"variant_id": "plain", "label": "Без памяти", "kind": "scripted_fixture",
                 "model": {"provider": None, "model_id": None}, "notes": False, "prompt_hash": None},
                {"variant_id": "notes", "label": "Со структурированными заметками", "kind": "scripted_fixture",
                 "model": {"provider": None, "model_id": None}, "notes": True, "prompt_hash": None}],
        mode={"protocol_id": PROTOCOL, "official": False, "action_limit": 100, "time_limit_s": 60,
              "note": "Исследовательский протокол фикстуры, не официальная приёмка."},
        repetitions=3, budget={"currency": "USD", "limit": None, "spent": None,
                               "note": "Фикстура не вызывает платную модель."},
        progress={"planned": len(attempts), "completed": len(attempts), "running": 0,
                  "failed": {"timeout": 0, "provider_error": 0, "invalid_response": 0, "budget_stop": 0}},
        timing={"started_at": FIXED_TIME, "finished_at": FIXED_TIME, "elapsed_s": sum(a["usage"]["elapsed_s"] for a in attempts.values())},
        cost={"input_tokens": None, "output_tokens": None, "usd": None,
              "note": "Токены в попытках фикстуры условные; модель не вызывалась."},
        attempt_ids=sorted(attempts)))

    # A stopped experiment showing operational failures (all values illustrative).
    _dump(out / "experiments" / "exp-budget-stop" / "experiment.json", _common(
        "experiment", "exp-budget-stop", title="Повтор на dev-семействах · остановлен по бюджету", status="stopped",
        stop_reason={"code": "budget_exceeded", "message": "Достигнут лимит расходов эксперимента."},
        split={"hash": "sha256:fixture-split", "families": {"train": 0, "dev": 12, "holdout": 0}},
        agents=[{"variant_id": "plain", "label": "Без памяти", "kind": "api_model",
                 "model": {"provider": "Провайдер модели (фикстура)", "model_id": "модель для отладки интерфейса"}, "notes": False,
                 "prompt_hash": "sha256:fixture-prompt"}],
        mode={"protocol_id": "research-dev-1", "official": False, "action_limit": 100, "time_limit_s": 60},
        repetitions=3, budget={"currency": "USD", "limit": 25.0, "spent": 25.0},
        progress={"planned": 72, "completed": 41, "running": 0,
                  "failed": {"timeout": 3, "provider_error": 2, "invalid_response": 1, "budget_stop": 25}},
        timing={"started_at": FIXED_TIME, "finished_at": FIXED_TIME, "elapsed_s": 2894.0},
        cost={"input_tokens": 1830000, "output_tokens": 61000, "usd": 25.0},
        attempt_ids=["att-stop-timeout", "att-stop-provider-error", "att-stop-invalid-response", "att-stop-budget-stop"]))

    # Operational outcomes without a financial result: no evaluation is published for them.
    stopped_ids = []
    for status, note in (("timeout", "Попытка не уложилась в 60 секунд, включая задержки сервиса."),
                         ("provider_error", "Сервис модели вернул ошибку; ответа нет."),
                         ("invalid_response", "Ответ модели не является допустимым действием."),
                         ("budget_stop", "Попытка не начата: исчерпан бюджет эксперимента.")):
        aid = f"att-stop-{status.replace('_', '-')}"
        stopped_ids.append(aid)
        _dump(out / "experiments" / "exp-budget-stop" / "attempts" / f"{aid}.json", _common(
            "attempt", aid, experiment_id="exp-budget-stop", variant_id="plain", repetition=1,
            episode={"company_label": "Компания B · зачёт после границы", "family_id": "family-boundary", "split": "dev",
                     "snapshot_hash": _hash_dir(snaps / "company-b" / "public")},
            snapshot_ref="snapshots/company-b/public", status=status, started_at=FIXED_TIME, finished_at=FIXED_TIME,
            steps=[], submission=None, evaluation=None, error={"code": status, "message": note},
            usage={"input_tokens": None, "output_tokens": None, "cost_usd": None, "elapsed_s": 60.0 if status == "timeout" else None}))
    _dump(out / "experiments" / "exp-model-unavailable" / "experiment.json", _common(
        "experiment", "exp-model-unavailable", title="Проверка второй модели · модель недоступна", status="failed",
        stop_reason={"code": "model_unavailable", "message": "Провайдер сообщил, что модель недоступна."},
        split={"hash": "sha256:fixture-split", "families": {"train": 0, "dev": 12, "holdout": 0}},
        agents=[{"variant_id": "plain", "label": "Без памяти", "kind": "api_model",
                 "model": {"provider": "Провайдер модели (фикстура)", "model_id": "недоступная модель"}, "notes": False}],
        mode={"protocol_id": "research-dev-1", "official": False, "action_limit": 100, "time_limit_s": 60},
        repetitions=3, budget={"currency": "USD", "limit": 10.0, "spent": 0.0},
        progress={"planned": 36, "completed": 0, "running": 0,
                  "failed": {"timeout": 0, "provider_error": 0, "invalid_response": 0, "budget_stop": 0}},
        timing={"started_at": FIXED_TIME, "finished_at": FIXED_TIME, "elapsed_s": 2.0},
        cost={"input_tokens": 0, "output_tokens": 0, "usd": 0.0}, attempt_ids=[]))

    plain_b = [attempts[a] for a in ("att-b-plain-1", "att-b-plain-2", "att-b-plain-3")]
    failed = sum(1 for a in plain_b if a["evaluation"]["tp"] == 0)
    _dump(out / "experiments" / exp / "failures" / "fail-boundary-residual.json", _common(
        "failure", "fail-boundary-residual", experiment_id=exp,
        title="Остаток платежа на границе периода",
        criterion={"text": "Агент заявляет нераспределённой всю сумму назначенного платежа, хотя часть зачтена до "
                           "границы периода; вывод не засчитан оценщиком.",
                   "check": "evaluation.tp == 0 при заявленной сумме, равной сумме платежа"},
        finding_type="payment_allocation", family_id="family-boundary", variant_id="plain",
        attempt_ids=[a["artifact_id"] for a in plain_b], validation_id="val-company-b",
        frequency={"failed": failed, "total": len(plain_b)},
        interval={"low": None, "high": None, "method": None,
                  "reason": "Три повтора одной компании не дают интервала по семействам."},
        alternatives=["Попытка не получила страницы проводок; по наблюдениям нельзя отличить пропуск чтения от "
                      "неверного правила зачёта."],
        reduction_id="red-boundary-residual"))

    def outcome(aid):
        a = attempts[aid]
        claims = [(f["type"], f["claimed_amount_minor"], f["currency"]) for f in a["submission"]["findings"]]
        return {"attempt_id": aid, "claims": claims, "tp": a["evaluation"]["tp"], "fp": a["evaluation"]["fp"],
                "fn": a["evaluation"]["fn"]}

    def expected(company):
        return [(i["type"], i["expected_amount_minor"], i["currency"]) for i in companies[company][2]["issues"]
                if i["type"] == "payment_allocation"]

    _dump(out / "reductions" / "red-boundary-residual.json", _common(
        "reduction", "red-boundary-residual", failure_id="fail-boundary-residual", experiment_id=exp,
        original={"snapshot_ref": "snapshots/company-b/public", "validation_id": "val-company-b",
                  "ledger_rows": sizes["ledger_rows"][0], "documents": sizes["documents"][0]},
        reduced={"snapshot_ref": "snapshots/company-b-reduced/public", "validation_id": "val-company-b-reduced",
                 "ledger_rows": sizes["ledger_rows"][1], "documents": sizes["documents"][1]},
        method="fixture_filter",
        transformations=["Оставлены только платёж, назначенный счёт и проводки их журналов.",
                         "Остальные счета, платежи и проводки удалены; правила и курсы сохранены."],
        repeats={"original": {"failed": failed, "total": len(plain_b), "attempt_ids": [a["artifact_id"] for a in plain_b]},
                 "reduced": {"failed": sum(1 for a in ("att-r-plain-1", "att-r-plain-2") if attempts[a]["evaluation"]["tp"] == 0),
                             "total": 2, "attempt_ids": ["att-r-plain-1", "att-r-plain-2"]}},
        families=[
            {"relation": "defect_fixed", "title": "Последний зачёт до границы периода",
             "snapshot_ref": "snapshots/company-a/public", "validation_id": "val-company-a",
             "expected_change": "Остатка на отчётную дату нет: вывод о нераспределённом платеже должен исчезнуть.",
             "expected": expected("company-a"), "actual": [outcome("att-a-plain-1"), outcome("att-a-notes-1")]},
            {"relation": "clean_analog", "title": "Компания другой семьи без ошибок",
             "snapshot_ref": "snapshots/clean/public", "validation_id": "val-clean",
             "expected_change": "Ошибок нет: правильный ответ пуст.",
             "expected": expected("clean"), "actual": [outcome("att-c-plain-1")]},
            {"relation": "renamed", "title": "Те же операции, новые номера документов",
             "snapshot_ref": "snapshots/company-b-renamed/public", "validation_id": "val-company-b-renamed",
             "expected_change": "Финансовое основание прежнее: остаток должен сохраниться.",
             "expected": expected("company-b-renamed"), "actual": [outcome("att-n-plain-1")]},
        ]))

    _dump(out / "curricula" / "cur-boundary-v1.json", _common(
        "curriculum", "cur-boundary-v1", title="Учебный набор: остатки на границе периода",
        experiment_id=exp, failure_ids=["fail-boundary-residual"], reduction_ids=["red-boundary-residual"],
        split_hash="sha256:fixture-split",
        splits={"train": 3, "dev": 1, "holdout": 0},
        items=[{"item_id": "item-1", "title": "Сокращённый случай · зачёт после границы", "split": "train",
                "origin": "reduction", "family_id": "family-boundary", "validation": "passed", "hash": _hash_dir(snaps / "company-b-reduced" / "public")},
               {"item_id": "item-2", "title": "Исправленный дефект · зачёт до границы", "split": "train",
                "origin": "family_transform", "family_id": "family-boundary", "validation": "passed", "hash": _hash_dir(snaps / "company-a" / "public")},
               {"item_id": "item-3", "title": "Те же операции, новые номера документов", "split": "train",
                "origin": "family_transform", "family_id": "family-boundary", "validation": "passed", "hash": _hash_dir(snaps / "company-b-renamed" / "public")},
               {"item_id": "item-4", "title": "Компания другой семьи без ошибок", "split": "dev",
                "origin": "clean_control", "family_id": "family-clean", "validation": "passed", "hash": _hash_dir(snaps / "clean" / "public")}],
        filtered=[{"title": "Случай без доступных доказательств в бюджете действий", "reason": "evidence_unreachable"}],
        export_formats=["chat_jsonl", "few_shot_markdown"]))

    trainings = [
        ("trn-weights", "weights", "not_available", None,
         {"code": "training_unavailable", "message": "Для выбранной модели у провайдера нет доступа к обучению весов."}),
        ("trn-weights-small", "weights", "failed", None,
         {"code": "job_failed", "message": "Задание обучения завершилось ошибкой провайдера до первого checkpoint."}),
        ("trn-prompt", "prompt", "completed", {"prompt_hash": "sha256:fixture-instruction-v2",
                                               "summary": "Инструкция дополнена требованием открыть все проводки платежа до границы."}, None),
        ("trn-few-shot", "few_shot", "interrupted", None,
         {"code": "interrupted", "message": "Сборка примеров прервана оператором; результата нет."}),
    ]
    for tid, kind, state, result, error in trainings:
        _dump(out / "training" / f"{tid}.json", _common(
            "training", tid, kind=kind, state=state, curriculum_id="cur-boundary-v1",
            base_model={"provider": "Провайдер модели (фикстура)", "model_id": "модель для отладки интерфейса"},
            settings={"epochs": 2 if kind == "weights" else None, "examples": 3},
            budget={"currency": "USD", "limit": 40.0 if kind == "weights" else None, "spent": 0.0 if kind == "weights" else None},
            job={"job_id": None if state == "not_available" else f"job-{tid}", "checkpoint": None},
            result=result, error=error))

    _dump(out / "comparisons" / "cmp-holdout-v1.json", _common(
        "comparison", "cmp-holdout-v1", title="Версии агента на новых компаниях", experiment_id=exp,
        status="partial", verified_by=None,
        verification={"status": "not_checked", "reason": "Независимые метрики ещё не опубликованы; значения — фикстура для проверки интерфейса."},
        sample={"families": 20, "episodes": 40, "repetitions": 3, "split": "holdout",
                "note": "Каждая семья — чистый и ошибочный вариант; повторы не считаются независимыми компаниями."},
        variants=[
            {"variant_id": "base", "label": "Исходный агент", "training_id": None,
             "metrics": {"micro_f1": 0.42, "mean_reward": -0.11, "clean_fp": {"count": 9, "denominator": 60},
                         "completion": {"completed": 117, "total": 120}, "timeouts": 2, "errors": 1,
                         "actions_mean": 11.4, "tokens": 2210000, "time_s": 3110.0, "cost_usd": 31.6}},
            {"variant_id": "prompt-v2", "label": "Изменённая инструкция", "training_id": "trn-prompt",
             "metrics": {"micro_f1": 0.47, "mean_reward": -0.04, "clean_fp": {"count": 11, "denominator": 60},
                         "completion": {"completed": 118, "total": 120}, "timeouts": 1, "errors": 1,
                         "actions_mean": 14.9, "tokens": 2860000, "time_s": 3940.0, "cost_usd": 40.2}},
            {"variant_id": "weights", "label": "Обучение весов", "training_id": "trn-weights",
             "metrics": None, "missing_reason": "Обучение весов недоступно: результата нет."},
        ],
        paired=[{"a": "base", "b": "prompt-v2", "metric": "micro_f1", "difference": 0.05,
                 "interval": [-0.04, 0.13], "method": "bootstrap по семействам", "unit": "family"},
                {"a": "base", "b": "prompt-v2", "metric": "clean_fp_rate", "difference": 0.033,
                 "interval": [-0.02, 0.09], "method": "bootstrap по семействам", "unit": "family"}],
        missing=[{"variant_id": "weights", "reason": "training_unavailable"}]))

    _dump(out / "incompatible" / "experiment-v2.json",
          {"schema_version": "2.0", "artifact_kind": "experiment", "artifact_id": "exp-future-schema",
           "source_sha": SOURCE_SHA, "protocol_id": PROTOCOL, "provenance": PROVENANCE,
           "title": "Эксперимент в будущем формате"})
    (out / "README.txt").write_text(PROVENANCE["note"] + "\n", encoding="utf-8")
    return {"attempts": len(attempts), "out": str(out)}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m auditlab.demo.research_fixtures OUT_DIR", file=sys.stderr)
        return 2
    print(json.dumps(build(Path(argv[0])), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
