"""Local judge console: FastAPI over completed open exports + the built React app.

Handlers read only through ExportStore. New experiments are executed by the
trusted lab in a separate process (``python -m auditlab.demo.lab``); this
process never opens training truth. Not used by the official test run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import research, research_lab, service
from .paths import PACKAGE, REPO, DemoPaths
from .store import OP_KEY_RE, ExportStore, NotFound, Refused

STATIC = PACKAGE / "static"


class ChangeBody(BaseModel):
    target: str = Field(max_length=100)
    field: str = Field(default="timestamp", max_length=40)
    value: str = Field(max_length=64)


class PairRequest(BaseModel):
    source_run_id: str = Field(max_length=96)
    change: ChangeBody
    prediction: str | None = Field(default=None, max_length=500)
    operation_key: str = Field(max_length=64)


class RescoreRequest(BaseModel):
    finding_index: int = Field(ge=0, le=29)
    replace: str = Field(max_length=100)
    with_key: str = Field(alias="with", max_length=100)
    operation_key: str = Field(max_length=64)


def _lab_command(paths: DemoPaths, extra: list[Path], *args: str) -> list[str]:
    cmd = [sys.executable, "-m", "auditlab.demo.lab", "--root", str(paths.root)]
    for root in extra:
        cmd += ["--exports", str(root)]
    return cmd + list(args)


def _run_lab(paths: DemoPaths, extra: list[Path], *args: str, timeout: int = 60) -> dict:
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run(_lab_command(paths, extra, *args), cwd=str(REPO), env=env, capture_output=True,
                          text=True, timeout=timeout)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return {"ok": False, "error": {"code": "infrastructure_error", "message": f"lab exited with code {proc.returncode}"}}
    return json.loads(lines[-1])


def research_roots_from_env() -> list[Path]:
    value = os.environ.get("AUDITLAB_RESEARCH_ROOT", "")
    return [Path(p) for p in value.split(os.pathsep) if p]


def create_app(paths: DemoPaths | None = None, extra_roots: list[Path] | None = None, build_samples: bool = True,
               research_roots: list[Path] | None = None) -> FastAPI:
    paths = (paths or DemoPaths.default()).ensure()
    research_dirs = [Path(p).resolve() for p in (research_roots if research_roots is not None else research_roots_from_env())]
    extra = [Path(p).resolve() for p in (extra_roots or [])]
    store = ExportStore([paths.exports, *extra], paths.ops)
    app = FastAPI(title="AuditLab judge console", docs_url=None, redoc_url=None, openapi_url=None)
    jobs: dict[str, threading.Thread] = {}
    lock = threading.Lock()
    if build_samples and not any(paths.exports.iterdir()):
        _run_lab(paths, extra, "samples")

    def _guard(fn, *args):
        try:
            return fn(*args)
        except NotFound:
            raise HTTPException(404, {"code": "not_found", "message": "No open completed run with this id."})
        except Refused as exc:
            raise HTTPException(403, {"code": "refused", "message": str(exc)})

    def _start(op_key: str, *args: str) -> JSONResponse:
        if not OP_KEY_RE.match(op_key):
            raise HTTPException(422, {"code": "bad_operation_key", "message": "6-64 characters [A-Za-z0-9_-]."})
        status = store.op_status(op_key)
        if status and status.get("status") == "completed":
            return JSONResponse(status, status_code=200)
        with lock:
            thread = jobs.get(op_key)
            if thread is None or not thread.is_alive():
                if status and status.get("status") == "failed":  # explicit retry of a failed key
                    (paths.ops / f"{op_key}.json").unlink(missing_ok=True)
                thread = threading.Thread(target=_run_lab, args=(paths, extra, *args), daemon=True)
                jobs[op_key] = thread
                thread.start()
        return JSONResponse({"op_key": op_key, "status": "pending"}, status_code=202)

    @app.get("/demo/health")
    def health():
        return {"ok": True, "runs": len(store.list_runs()), "static": (STATIC / "index.html").exists()}

    @app.get("/demo/runs")
    def runs():
        return {"runs": service.list_runs(store)}

    @app.get("/demo/runs/{run_id}")
    def run(run_id: str):
        return _guard(service.run_detail, store, run_id)

    @app.get("/demo/runs/{run_id}/records/{key:path}")
    def record(run_id: str, key: str):
        view = _guard(service.record_view, store, run_id, key)
        if view is None:
            raise HTTPException(404, {"code": "unknown_key", "message": "No public record with this key in the run."})
        return view

    @app.get("/demo/compare")
    def compare(left: str, right: str):
        return _guard(service.compare, store, left, right)

    @app.post("/demo/pairs")
    def pairs(body: PairRequest):
        _guard(store.run_dir, body.source_run_id)
        args = ["change", "--source", body.source_run_id, "--target", body.change.target, "--field", body.change.field,
                "--value", body.change.value, "--op", body.operation_key]
        if body.prediction:
            args += ["--prediction", body.prediction]
        return _start(body.operation_key, *args)

    @app.post("/demo/runs/{run_id}/rescore")
    def rescore(run_id: str, body: RescoreRequest):
        _guard(store.run_dir, run_id)
        return _start(body.operation_key, "rescore", "--source", run_id, "--finding", str(body.finding_index),
                      "--replace", body.replace, "--with", body.with_key, "--op", body.operation_key)

    @app.post("/demo/runs/{run_id}/replay")
    def replay(run_id: str):
        _guard(store.run_dir, run_id)
        reply = _run_lab(paths, extra, "replay-check", "--run", run_id)
        if not reply.get("ok"):
            raise HTTPException(409, reply.get("error"))
        return reply["result"]

    @app.get("/demo/ops/{op_key}")
    def op(op_key: str):
        status = store.op_status(op_key)
        if status is None:
            with lock:
                running = op_key in jobs and jobs[op_key].is_alive()
            if not running:
                raise HTTPException(404, {"code": "unknown_operation", "message": "No such operation."})
            return {"op_key": op_key, "status": "pending"}
        return status

    # ------------------------------------------------------------------ research (read-only projection)
    def _research(fn, *args):
        projection = research.Projection(research.ResearchStore(research_dirs))
        try:
            return fn(projection, *args)
        except research.ResearchError as exc:
            raise HTTPException(404 if exc.code == "not_found" else 409, {"code": exc.code, "message": str(exc)})

    @app.get("/research/status")
    def research_status():
        return _research(lambda p: p.status(research.controller_status()))

    @app.get("/research/overview")
    def research_overview():
        return _research(lambda p: p.overview())

    @app.get("/research/experiments/{artifact_id}")
    def research_experiment(artifact_id: str):
        return _research(lambda p: {**p.experiment(artifact_id), "controller": research.controller_status()})

    @app.get("/research/failures/{artifact_id}")
    def research_failure(artifact_id: str):
        return _research(lambda p: p.failure(artifact_id))

    @app.get("/research/attempts/{artifact_id}")
    def research_attempt(artifact_id: str):
        return _research(lambda p: p.attempt(artifact_id))

    @app.get("/research/reductions/{artifact_id}")
    def research_reduction(artifact_id: str):
        return _research(lambda p: p.reduction(artifact_id))

    @app.get("/research/curricula/{artifact_id}")
    def research_curriculum(artifact_id: str):
        return _research(lambda p: p.curriculum(artifact_id))

    @app.get("/research/comparisons/{artifact_id}")
    def research_comparison(artifact_id: str):
        return _research(lambda p: p.comparison(artifact_id))

    @app.get("/research/lab")
    def research_lab_overview():
        return _research(lambda p: research_lab.lab(p).overview())

    @app.get("/research/lab/families/{artifact_id}")
    def research_lab_family(artifact_id: str):
        return _research(lambda p: research_lab.lab(p).family(artifact_id))

    @app.get("/research/lab/dossiers/{artifact_id}")
    def research_lab_dossier(artifact_id: str):
        return _research(lambda p: research_lab.lab(p).dossier(artifact_id))

    @app.get("/research/lab/map")
    def research_lab_map(experiment: str | None = None, before: str | None = None, after: str | None = None):
        return _research(lambda p: research_lab.lab(p).results_map(experiment, before, after))

    @app.get("/research/snapshots/{sid}")
    def research_snapshot(sid: str):
        return _research(lambda p: p.snapshot_view(sid))

    @app.get("/research/snapshots/{sid}/records/{key:path}")
    def research_record(sid: str, key: str):
        view = _research(lambda p: service.snapshot_record_view(p.store.snapshot(sid), key))
        if view is None:
            raise HTTPException(404, {"code": "unknown_key", "message": "No public record with this key."})
        return view

    @app.post("/research/experiments/{artifact_id}/{action}")
    def research_control(artifact_id: str, action: str, request: Request):
        # This endpoint can spend a configured budget. Reject browser cross-site requests
        # and DNS rebinding before looking up an experiment or invoking its controller.
        origin = request.headers.get("origin")
        host = request.url.hostname
        if (host not in {"127.0.0.1", "localhost", "::1", "testserver"}
                or request.headers.get("sec-fetch-site") == "cross-site"
                or (origin is not None and origin != str(request.base_url).rstrip("/"))):
            raise HTTPException(403, {"code": "cross_origin_refused", "message": "Use the local console origin."})
        if action not in ("start", "stop"):
            raise HTTPException(404, {"code": "not_found", "message": "Unknown action."})
        _research(lambda p: p.store.get("experiment", artifact_id))
        try:
            return research.controller_call(action, artifact_id)
        except research.ResearchError as exc:
            status = 503 if exc.code in ("controller_not_connected", "controller_error") else 409
            raise HTTPException(status, {"code": exc.code, "message": str(exc)})

    @app.get("/{path:path}")
    def static(path: str):
        if path.startswith("demo/") or path.startswith("research/"):
            raise HTTPException(404, {"code": "not_found", "message": "Unknown API path."})
        candidate = (STATIC / path).resolve()
        if path and candidate.is_file() and STATIC.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = STATIC / "index.html"
        if not index.exists():
            return JSONResponse({"error": "frontend not built; run `npm --prefix web run build`"}, status_code=503)
        return FileResponse(index)

    return app
