"""TEST DOUBLE: a local fake OpenAI-compatible HTTP server (127.0.0.1 only, background thread).

It implements just the endpoints the stdlib clients call, so the real request/response code is
exercised end to end. Nothing it returns is ever a real model output or a real training result.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeOpenAIServer:
    def __init__(self, expected_key: str):
        self.expected_key = expected_key
        self.requests: list[dict] = []
        self.chat_script: list[dict] = []      # {"status", "body" (obj or bytes), "headers", "delay"}
        self.files: dict[str, bytes] = {}
        self.jobs: dict[str, dict] = {}
        self.job_statuses: list[str] = ["running", "succeeded"]   # applied one per GET, last one sticks
        self.create_error: tuple[int, dict] | None = None
        self.lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def start(self) -> "FakeOpenAIServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r["method"] == method and r["path"] == path)

    # ------------------------------------------------------------------ handler
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def _send(self, status: int, body, headers: dict | None = None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("x-request-id", "req_fake")
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _dispatch(self, method: str):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                with fake.lock:
                    fake.requests.append({"method": method, "path": url.path, "query": parse_qs(url.query),
                                          "body": raw, "headers": dict(self.headers)})
                if self.headers.get("Authorization") != f"Bearer {fake.expected_key}":
                    return self._send(401, {"error": {"message": f"Incorrect API key provided: {self.headers.get('Authorization')}"}})
                path = url.path.removeprefix("/v1")
                if method == "POST" and path == "/chat/completions":
                    return self._chat()
                if method == "POST" and path == "/files":
                    return self._upload(raw)
                if path == "/fine_tuning/jobs":
                    return self._create(raw) if method == "POST" else self._send(200, {
                        "object": "list", "data": list(reversed(list(fake.jobs.values()))), "has_more": False})
                parts = path.split("/")
                if len(parts) >= 4 and parts[1:3] == ["fine_tuning", "jobs"]:
                    job = fake.jobs.get(parts[3])
                    if job is None:
                        return self._send(404, {"error": {"message": "no such job"}})
                    if len(parts) == 4:
                        return self._send(200, self._advance(job))
                    if parts[4] == "events":
                        return self._send(200, {"object": "list", "has_more": False, "data": [
                            {"object": "fine_tuning.job.event", "id": "ev-1", "created_at": 1, "level": "info",
                             "message": f"status {job['status']}", "type": "message"}]})
                    if parts[4] == "checkpoints":
                        data = [] if job["status"] != "succeeded" else [
                            {"object": "fine_tuning.job.checkpoint", "id": "ftckpt-1", "created_at": 2,
                             "fine_tuned_model_checkpoint": job["fine_tuned_model"] + ":ckpt-step-10",
                             "fine_tuning_job_id": job["id"], "step_number": 10, "metrics": {"train_loss": 0.5}}]
                        return self._send(200, {"object": "list", "has_more": False, "data": data})
                return self._send(404, {"error": {"message": "unknown endpoint"}})

            def _chat(self):
                if not fake.chat_script:
                    return self._send(500, {"error": {"message": "fake script exhausted"}})
                item = fake.chat_script.pop(0)
                time.sleep(item.get("delay", 0))
                return self._send(item.get("status", 200), item["body"], item.get("headers"))

            def _upload(self, raw: bytes):
                if b'name="purpose"' not in raw or b"fine-tune" not in raw or b'name="file"' not in raw:
                    return self._send(400, {"error": {"message": "multipart fields missing"}})
                file_id = f"file-{len(fake.files) + 1}"
                fake.files[file_id] = raw
                return self._send(200, {"id": file_id, "object": "file", "purpose": "fine-tune", "bytes": len(raw)})

            def _create(self, raw: bytes):
                if fake.create_error:
                    return self._send(*fake.create_error)
                body = json.loads(raw)
                if body.get("training_file") not in fake.files:
                    return self._send(400, {"error": {"message": "unknown training_file"}})
                job_id = f"ftjob-{len(fake.jobs) + 1}"
                job = {"object": "fine_tuning.job", "id": job_id, "status": "validating_files", "model": body["model"],
                       "training_file": body["training_file"], "validation_file": body.get("validation_file"),
                       "method": body.get("method"), "metadata": body.get("metadata"), "seed": body.get("seed"),
                       "suffix": body.get("suffix"), "fine_tuned_model": None, "trained_tokens": None, "error": None,
                       "_polls": 0}
                fake.jobs[job_id] = job
                return self._send(200, self._public(job))

            def _advance(self, job: dict) -> dict:
                with fake.lock:
                    index = min(job["_polls"], len(fake.job_statuses) - 1)
                    job["_polls"] += 1
                    job["status"] = fake.job_statuses[index]
                    if job["status"] == "succeeded":
                        job["fine_tuned_model"] = f"ft:{job['model']}:auditlab:{job['id']}"
                        job["trained_tokens"] = 12_000
                    elif job["status"] == "failed":
                        job["error"] = {"code": "invalid_training_file", "message": "line 3 is invalid",
                                        "param": "training_file"}
                    return self._public(job)

            @staticmethod
            def _public(job: dict) -> dict:
                return {k: v for k, v in job.items() if not k.startswith("_")}

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        return Handler
