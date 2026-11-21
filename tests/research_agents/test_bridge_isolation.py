"""Network-isolation layer of the LLM bridge: egress proxy, launch environment/argv, JSONL transport.

All network traffic stays on 127.0.0.1. The live Docker test is skipped unless AUDITLAB_BRIDGE_IMAGE
names a locally loaded image and `docker version` succeeds; it never contacts a provider.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from auditlab.research.agents import egress_proxy, sandbox
from auditlab.runner.isolation import PolicyFailure, PolicyTimeout

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "sk-test-SENTINEL-must-never-appear-0123456789"
SECRET = "TEST_BRIDGE_PROVIDER_KEY"


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class _Log(io.StringIO):
    def records(self):
        return [json.loads(line) for line in self.getvalue().splitlines()]


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class EgressProxyTests(unittest.TestCase):
    def setUp(self):
        self.echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
        self.echo.daemon_threads = True
        threading.Thread(target=self.echo.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.echo_port = self.echo.server_address[1]
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.echo.shutdown()
        self.echo.server_close()

    def proxy(self, allow=None, **options):
        log = _Log()
        server, port = egress_proxy.start_in_thread(allow or [f"127.0.0.1:{self.echo_port}"], log=log, **options)
        self.servers.append(server)
        return port, log

    def open(self, port, request: bytes):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(request)
        return sock

    @staticmethod
    def status(sock) -> bytes:
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        return data.split(b"\r\n", 1)[0]

    def request(self, port, request: bytes) -> bytes:
        with self.open(port, request) as sock:
            return self.status(sock)

    def connect_line(self, target):
        return (f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                f"Proxy-Authorization: Basic {SENTINEL}\r\n\r\n").encode()

    def test_allowlisted_connect_tunnels_both_ways_and_logs_counts_only(self):
        port, log = self.proxy()
        sock = self.open(port, self.connect_line(f"127.0.0.1:{self.echo_port}"))
        self.assertEqual(self.status(sock), b"HTTP/1.1 200 Connection Established")
        sock.sendall(b"ping-1234")
        self.assertEqual(sock.recv(100), b"ping-1234")
        sock.close()
        self.assertTrue(_wait(lambda: log.records()))
        [record] = log.records()
        self.assertTrue(record["allowed"])
        self.assertEqual(record["target"], f"127.0.0.1:{self.echo_port}")
        self.assertEqual((record["bytes_up"], record["bytes_down"]), (9, 9))
        self.assertTrue({"time", "client", "duration_s"} <= set(record))
        self.assertNotIn(SENTINEL, log.getvalue())
        self.assertNotIn("Proxy-Authorization", log.getvalue())

    def test_early_bytes_after_connect_are_forwarded(self):
        port, _ = self.proxy()
        sock = self.open(port, self.connect_line(f"127.0.0.1:{self.echo_port}") + b"hello")
        self.assertIn(b"200", self.status(sock))
        data = b""
        while len(data) < 5:
            data += sock.recv(100)
        self.assertEqual(data, b"hello")
        sock.close()

    def test_denies_other_host_other_port_and_unlisted_ip_literal(self):
        port, log = self.proxy(allow=[f"localhost:{self.echo_port}"])
        cases = {"api.openai.com:443": "not_allowlisted", f"localhost:{self.echo_port + 1}": "not_allowlisted",
                 f"127.0.0.1:{self.echo_port}": "not_allowlisted",
                 # allowlisted name that resolves to loopback is still refused
                 f"localhost:{self.echo_port}": "resolved_to_non_public_address"}
        for target in cases:
            self.assertEqual(self.request(port, self.connect_line(target)), b"HTTP/1.1 403 Forbidden", target)
        self.assertTrue(_wait(lambda: len(log.records()) == len(cases)))
        self.assertEqual({r["target"]: r["reason"] for r in log.records()}, cases)
        self.assertFalse(any(r["allowed"] for r in log.records()))
        self.assertNotIn(SENTINEL, log.getvalue())

    def test_wrong_port_on_allowlisted_ip(self):
        port, _ = self.proxy()
        self.assertIn(b"403", self.request(port, self.connect_line(f"127.0.0.1:{self.echo_port + 1}")))

    def test_plain_http_methods_are_rejected_without_logging_target_or_headers(self):
        port, log = self.proxy()
        for method in ("GET", "POST"):
            request = (f"{method} http://127.0.0.1:{self.echo_port}/v1/messages?key={SENTINEL} HTTP/1.1\r\n"
                       f"Host: 127.0.0.1\r\nAuthorization: Bearer {SENTINEL}\r\nContent-Length: 0\r\n\r\n")
            self.assertEqual(self.request(port, request.encode()), b"HTTP/1.1 403 Forbidden")
        self.assertTrue(_wait(lambda: len(log.records()) == 2))
        self.assertEqual({(r["reason"], r["target"]) for r in log.records()}, {("method_not_allowed", None)})
        self.assertNotIn(SENTINEL, log.getvalue())
        self.assertNotIn("Authorization", log.getvalue())

    def test_malformed_requests_get_400(self):
        port, log = self.proxy()
        bad = [b"CONNECT\r\n\r\n", b"garbage\r\n\r\n", b"CONNECT 127.0.0.1 HTTP/1.1\r\n\r\n",
               f"CONNECT 127.0.0.1:{self.echo_port} HTTP/9.9\r\n\r\n".encode(),
               "CONNECT hé.example:443 HTTP/1.1\r\n\r\n".encode(),
               b"CONNECT user@host:443 HTTP/1.1\r\n\r\n", b"CONNECT *:443 HTTP/1.1\r\n\r\n",
               b"CONNECT 127.0.0.1:99999 HTTP/1.1\r\n\r\n"]
        for request in bad:
            self.assertEqual(self.request(port, request), b"HTTP/1.1 400 Bad Request", request)
        self.assertTrue(_wait(lambda: len(log.records()) == len(bad)))

    def test_oversized_headers_get_400(self):
        port, log = self.proxy(max_header_bytes=1024)
        request = self.connect_line(f"127.0.0.1:{self.echo_port}")[:-2] + b"X-Pad: " + b"a" * 20000 + b"\r\n\r\n"
        self.assertEqual(self.request(port, request), b"HTTP/1.1 400 Bad Request")
        self.assertTrue(_wait(lambda: log.records()))
        self.assertEqual(log.records()[0]["reason"], "header_too_large")
        self.assertNotIn(SENTINEL, log.getvalue())

    def test_concurrency_cap(self):
        port, log = self.proxy(max_tunnels=1)
        first = self.open(port, self.connect_line(f"127.0.0.1:{self.echo_port}"))
        self.assertIn(b"200", self.status(first))
        self.assertEqual(self.request(port, self.connect_line(f"127.0.0.1:{self.echo_port}")),
                         b"HTTP/1.1 503 Service Unavailable")
        first.close()
        self.assertTrue(_wait(lambda: sum(r["allowed"] for r in log.records()) == 1))
        third = self.open(port, self.connect_line(f"127.0.0.1:{self.echo_port}"))
        self.assertIn(b"200", self.status(third))
        third.close()
        self.assertIn("capacity", [r["reason"] for r in log.records()])

    def test_idle_tunnel_is_closed(self):
        port, log = self.proxy(idle_timeout_s=0.3)
        sock = self.open(port, self.connect_line(f"127.0.0.1:{self.echo_port}"))
        self.assertIn(b"200", self.status(sock))
        self.assertEqual(sock.recv(10), b"")
        sock.close()
        self.assertTrue(_wait(lambda: log.records()))
        self.assertEqual(log.records()[0]["reason"], "idle_timeout")

    def test_allowlist_parsing(self):
        self.assertEqual(egress_proxy.normalise_allowlist(["API.Anthropic.com:443", "[::1]:8443"]),
                         {"api.anthropic.com:443", "[::1]:8443"})
        for entry in ("api.anthropic.com", "*:443", "http://api.anthropic.com:443", "host:0", "a b:443", "::1:443"):
            with self.assertRaises(ValueError, msg=entry):
                egress_proxy.normalise_allowlist([entry])
        with self.assertRaises(ValueError):
            egress_proxy.normalise_allowlist([])


class SandboxTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {SECRET: SENTINEL, "LEAKY_UNRELATED_VAR": "leak"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bridge_env_contains_only_allowed_keys_and_secret_by_name(self):
        env = sandbox.bridge_env(SECRET)
        self.assertLessEqual(set(env) - {SECRET}, set(sandbox.BASE_ENV_KEYS))
        self.assertEqual(env[SECRET], SENTINEL)
        self.assertEqual(env["PYTHONPATH"], str(ROOT))
        self.assertNotIn("LEAKY_UNRELATED_VAR", env)
        self.assertNotIn(SECRET, sandbox.bridge_env(None))
        with self.assertRaises(ValueError):
            sandbox.bridge_env("TEST_MISSING_PROVIDER_KEY_XYZ")
        for name in ("lower_key", "PATH", "AUDITLAB_LAUNCH_ID", "PYTHONSTARTUP", "HTTPS_PROXY", "AB", "A-B"):
            with self.assertRaises(ValueError, msg=name):
                sandbox.bridge_env(name)
        with self.assertRaises(ValueError):
            sandbox.bridge_env(None, {"LD_PRELOAD": "/x.so"})
        with self.assertRaises(ValueError):
            sandbox.bridge_env(None, {"PYTHONPATH": "/elsewhere"})

    def test_bridge_env_rejects_forbidden_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            episode = Path(tmp, "episode")
            episode.mkdir()
            ok = sandbox.bridge_env(None, {"PATH": os.environ.get("PATH", "")}, forbidden_roots=[episode])
            self.assertNotIn(str(episode), ok["PATH"])
            with self.assertRaises(ValueError):
                sandbox.bridge_env(None, {"PATH": str(episode / "bin") + os.pathsep + "x"}, forbidden_roots=[episode])
            with self.assertRaises(ValueError):
                sandbox.bridge_env(None, {"HTTPS_PROXY": "http://h/" + str(episode)}, forbidden_roots=[episode])
            with self.assertRaises(ValueError):  # the package parent itself is forbidden here
                sandbox.bridge_env(None, forbidden_roots=[ROOT])

    def test_docker_argv_internal_network_hardening_and_no_secret_value(self):
        plan = sandbox.docker_bridge_commands("auditlab-llm-bridge:dev", None, "auditlab-bridge-t1", SECRET,
                                              ["api.anthropic.com:443", "api.openai.com:443"],
                                              cpus="1", memory="1g")
        every = [arg for argv in plan.all_argv() for arg in argv]
        self.assertFalse([arg for arg in every if SENTINEL in arg])
        self.assertFalse({"-v", "--volume", "--mount", "--privileged", "--network=host"} & set(every))
        self.assertFalse([arg for arg in every if arg.startswith(("--volume=", "--mount=", "-v="))])
        self.assertIn(("docker", "network", "create", "--internal", "auditlab-bridge-t1"), plan.setup)
        create = list(plan.bridge_create)
        self.assertEqual(create[:2], ["docker", "create"])
        self.assertEqual(create[create.index("--network") + 1], "auditlab-bridge-t1")
        self.assertEqual(create.count("--network"), 1)
        for flag in ("--read-only", "--pull=never", "--pids-limit", "--cpus", "--memory", "--tmpfs"):
            self.assertIn(flag, create)
        self.assertEqual(create[create.index("--cap-drop") + 1], "ALL")
        self.assertEqual(create[create.index("--user") + 1], "65534:65534")
        self.assertEqual(create[create.index("--security-opt") + 1], "no-new-privileges")
        envs = [create[i + 1] for i, arg in enumerate(create) if arg == "-e"]
        self.assertIn(SECRET, envs)  # name only: Docker copies the value from the client environment
        self.assertFalse([e for e in envs if e.startswith(SECRET + "=")])
        self.assertIn(f"HTTPS_PROXY=http://{plan.proxy_name}:3128", envs)
        self.assertEqual(create[-1], "auditlab-llm-bridge:dev")
        proxy_create = next(argv for argv in plan.setup if argv[:2] == ("docker", "create"))
        self.assertNotIn(SECRET, proxy_create)
        self.assertNotIn("-e", proxy_create)
        self.assertEqual(proxy_create[proxy_create.index("--network") + 1], plan.network)
        self.assertIn(("docker", "network", "connect", plan.egress_network, plan.proxy_name), plan.setup)
        self.assertNotIn("--internal", next(a for a in plan.setup if a[-1] == plan.egress_network))
        allowed = [proxy_create[i + 1] for i, arg in enumerate(proxy_create) if arg == "--allow"]
        self.assertEqual(allowed, ["api.anthropic.com:443", "api.openai.com:443"])
        self.assertEqual({argv[-1] for argv in plan.teardown},
                         {plan.bridge_name, plan.proxy_name, plan.network, plan.egress_network})
        self.assertEqual(sandbox.docker_client_env(SECRET)[SECRET], SENTINEL)
        with self.assertRaises(ValueError):
            sandbox.docker_bridge_commands("img", allow=["*:443"])
        with self.assertRaises(ValueError):
            sandbox.docker_bridge_commands("--privileged")

    def test_launch_label_is_applied(self):
        with mock.patch.dict(os.environ, {"AUDITLAB_LAUNCH_ID": "0" * 32}):
            plan = sandbox.docker_bridge_commands("img", network_name="auditlab-bridge-t2")
        self.assertIn("org.auditlab.launch=" + "0" * 32, plan.bridge_create)

    def test_dev_command_refuses_official_protocol(self):
        with self.assertRaises(ValueError):
            sandbox.dev_bridge_command({"protocol_id": "genesis-official-60s"})
        with self.assertRaises(ValueError):
            sandbox.dev_bridge_command({"protocol_id": "made-up"})
        with self.assertRaises(ValueError):
            sandbox.dev_bridge_command({})
        command = sandbox.dev_bridge_command({"protocol": {"protocol_id": "auditlab-research-600s"}})
        self.assertEqual(command[1:], ["-m", "auditlab.research.agents.bridge"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            path.write_text(json.dumps({"protocol_id": "genesis-official-60s"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                sandbox.dev_bridge_command(path)
            path.write_text(json.dumps({"protocol_id": "auditlab-research-600s"}), encoding="utf-8")
            self.assertNotIn(str(path), sandbox.dev_bridge_command(path))

    def test_dockerfile_copies_only_public_modules(self):
        text = (ROOT / "docker/bridge/Dockerfile").read_text(encoding="utf-8")
        runtime = (ROOT / "docker/runtime/Dockerfile").read_text(encoding="utf-8")
        base = re.search(r"^FROM (\S+)", text, re.M)[1]
        self.assertEqual(base, re.search(r"^FROM (\S+)", runtime, re.M)[1])
        self.assertIn("@sha256:", base)
        sources = [src for line in re.findall(r"^COPY (.+)$", text, re.M) for src in line.split()[:-1]]
        self.assertEqual(sorted(sources), sorted([
            "docker/bridge/requirements.lock", "docker/wheels/bridge/", "auditlab/__init__.py",
            "auditlab/contracts.py", "auditlab/research/__init__.py", "auditlab/research/agents"]))
        self.assertIn("--require-hashes", text)
        code = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
        self.assertNotRegex(code, r"evaluator|runner|generator|fixtures|datasets")


STAND_IN = textwrap.dedent("""
    import json, sys, time
    first = json.loads(sys.stdin.readline())
    mode = first["config"]["mode"]
    if mode == "reject":
        print(json.dumps({"type": "error", "error": "bad config"}), flush=True)
        sys.exit(0)
    print(json.dumps({"type": "ready", "init": first}), flush=True)
    for line in sys.stdin:
        message = json.loads(line)
        if mode == "hang":
            time.sleep(60)
        elif mode == "huge":
            sys.stdout.write(json.dumps({"action": "x" * (300 * 1024), "telemetry": {}}) + "\\n")
            sys.stdout.flush()
        elif mode == "crash":
            sys.exit(3)
        elif mode == "extra":
            print(json.dumps({"action": {}, "truth": 1}), flush=True)
        else:
            print(json.dumps({"action": {"tool": "noop", "echo": message}, "telemetry": {"tokens": 7}}), flush=True)
""")


class BridgeProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.script = Path(cls.tmp.name, "stand_in_bridge.py")
        cls.script.write_text(STAND_IN, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def bridge(self, mode, timeout_s=10.0, journal=None):
        bridge = sandbox.BridgeProcess([sys.executable, str(self.script)], env=sandbox.bridge_env(None),
                                       timeout_s=timeout_s, init={"mode": mode}, journal=journal)
        self.addCleanup(bridge.close)
        return bridge

    def test_init_line_and_round_trip(self):
        bridge = self.bridge("echo", journal=[{"turn": 1}])
        self.assertEqual(bridge.ready["init"], {"type": "init", "config": {"mode": "echo"}, "journal": [{"turn": 1}]})
        observation = {"step": 0, "tools": ["list_files"]}
        reply = bridge.act(observation)
        self.assertEqual(reply, {"action": {"tool": "noop", "echo": {"type": "observation", "observation": observation}},
                                 "telemetry": {"tokens": 7}})
        self.assertEqual(bridge.act({"step": 1})["telemetry"], {"tokens": 7})

    def test_rejected_init_is_policy_failure(self):
        with self.assertRaises(PolicyFailure):
            self.bridge("reject")

    def test_oversized_output_is_policy_failure(self):
        bridge = self.bridge("huge")
        with self.assertRaises(PolicyFailure) as caught:
            bridge.act({"step": 0})
        self.assertNotIsInstance(caught.exception, PolicyTimeout)

    def test_hang_is_policy_timeout(self):
        bridge = self.bridge("hang", timeout_s=1.0)
        started = time.monotonic()
        with self.assertRaises(PolicyTimeout):
            bridge.act({"step": 0})
        self.assertLess(time.monotonic() - started, 8)

    def test_crash_and_unexpected_fields_are_policy_failures(self):
        with self.assertRaises(PolicyFailure):
            self.bridge("crash").act({"step": 0})
        with self.assertRaises(PolicyFailure):
            self.bridge("extra").act({"step": 0})

    def test_oversized_observation_is_refused(self):
        bridge = self.bridge("echo")
        with self.assertRaises(PolicyFailure):
            bridge.act({"blob": "x" * (1024 * 1024 + 1)})


def _docker_ready() -> bool:
    if not os.environ.get("AUDITLAB_BRIDGE_IMAGE") or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


PROBE = textwrap.dedent("""
    import json, socket, sys
    proxy = sys.argv[1]
    result = {}
    try:
        socket.getaddrinfo("example.com", 443); result["dns"] = "resolved"
    except OSError:
        result["dns"] = "failed"
    s = socket.create_connection((proxy, 3128), timeout=10)
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\\r\\n\\r\\n")
    result["proxy"] = s.recv(100).split(b"\\r\\n")[0].decode()
    print(json.dumps(result))
""")


@unittest.skipUnless(_docker_ready(), "set AUDITLAB_BRIDGE_IMAGE to a loaded image and run where Docker works")
class LiveDockerTopologyTest(unittest.TestCase):
    def test_bridge_network_has_no_direct_route_and_proxy_refuses_unlisted(self):
        image = os.environ["AUDITLAB_BRIDGE_IMAGE"]
        plan = sandbox.docker_bridge_commands(image, secret_env=None)
        try:
            for argv in plan.setup:
                subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, timeout=60)
            probe = subprocess.run(["docker", "run", "--rm", "--network", plan.network, "--read-only",
                                    "--cap-drop", "ALL", "--entrypoint", "python", image, "-c", PROBE,
                                    plan.proxy_name], capture_output=True, text=True, timeout=60)
            result = json.loads(probe.stdout)
            self.assertEqual(result, {"dns": "failed", "proxy": "HTTP/1.1 403 Forbidden"})
        finally:
            for argv in plan.teardown:
                subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    unittest.main()
