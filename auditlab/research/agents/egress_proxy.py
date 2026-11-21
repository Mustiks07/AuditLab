"""Allowlisting HTTP CONNECT egress proxy for the LLM network bridge (stdlib only).

The bridge container sits on an internal Docker network whose only route out is this proxy.
The proxy accepts nothing but ``CONNECT host:port`` to exact allowlisted pairs and then tunnels
opaque bytes (TLS is never inspected or terminated). Every other request -- other hosts or ports,
plain HTTP methods, IP literals that are not themselves allowlisted, host names resolving to
non-public addresses, malformed or oversized requests, connections over capacity -- receives an
error status and is closed.

One JSON line is logged per decision: time, client, target, allowed, reason, bytes up/down and
duration. Request headers and bodies are never logged (plain-HTTP requests are rejected without
logging their target, which could carry a query string).

    python -m auditlab.research.agents.egress_proxy --listen 0.0.0.0:3128 --allow api.anthropic.com:443
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import selectors
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

DEFAULT_ALLOW = ("api.anthropic.com:443",)
MAX_HEADER_BYTES = 8192
HEADER_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 120.0
MAX_TUNNEL_S = 900.0
MAX_TUNNELS = 8
_CHUNK = 65536
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_REQUEST_LINE = re.compile(r"([A-Z]{1,16}) (\S{1,300}) HTTP/1\.[01]")


def parse_target(text: str) -> tuple[str, int]:
    """Normalise ``host:port`` / ``[v6]:port``. Raises ValueError for anything else."""
    if not isinstance(text, str) or not text.isascii():
        raise ValueError("target must be ASCII host:port")
    match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([^:\[\]/@\s]+):(\d{1,5})", text)
    if not match:
        raise ValueError("target must be host:port")
    host = (match[1] or match[3]).lower()
    port = int(match[2] or match[4])
    if not 0 < port < 65536:
        raise ValueError("port out of range")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if match[1] or not _HOST.fullmatch(host):
            raise ValueError("invalid host") from None
    return host, port


def format_target(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def normalise_allowlist(allow) -> frozenset[str]:
    entries = frozenset(format_target(*parse_target(entry)) for entry in allow)
    if not entries:
        raise ValueError("allowlist is empty")
    return entries


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def upstream_address(url: str | None):
    """An explicit trusted CONNECT proxy, never credentials or a client-supplied URL."""
    if url is None:
        return None
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError('upstream proxy must be http://host:port without credentials or a path')
    return parse_target(format_target(parsed.hostname, parsed.port))


class _Reject(Exception):
    def __init__(self, status: str, reason: str, target: str | None = None):
        super().__init__(reason)
        self.status, self.reason, self.target = status, reason, target


class _Handler(socketserver.BaseRequestHandler):
    server: "EgressProxy"

    def handle(self) -> None:
        sock, server = self.request, self.server
        client = format_target(*self.client_address[:2])
        record = {"client": client, "target": None, "allowed": False, "reason": None,
                  "bytes_up": 0, "bytes_down": 0}
        started = time.monotonic()
        if not server.slots.acquire(blocking=False):
            record["reason"] = "capacity"
            self._reject(sock, "503 Service Unavailable")
            server.log_decision(record, started)
            return
        upstream = None
        try:
            target, early = self._read_connect(sock)
            record["target"] = target
            upstream = self._open_upstream(target)
            sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            record.update(allowed=True, reason="allowlisted")
            if early:
                upstream.sendall(early)
                record["bytes_up"] += len(early)
            self._tunnel(sock, upstream, record)
        except _Reject as rejection:
            record.update(target=rejection.target or record["target"], reason=rejection.reason)
            self._reject(sock, rejection.status)
        except OSError:
            record["reason"] = record["reason"] or "client_error"
        finally:
            if upstream is not None:
                upstream.close()
            server.slots.release()
            server.log_decision(record, started)

    def _read_connect(self, sock) -> tuple[str, bytes]:
        server = self.server
        deadline = time.monotonic() + server.header_timeout_s
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            if len(buffer) > server.max_header_bytes:
                raise _Reject("400 Bad Request", "header_too_large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Reject("408 Request Timeout", "header_timeout")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(server.max_header_bytes + 1 - len(buffer) + 4)
            except socket.timeout:
                raise _Reject("408 Request Timeout", "header_timeout") from None
            if not chunk:
                raise _Reject("400 Bad Request", "incomplete_request")
            buffer += chunk
        head, _, early = buffer.partition(b"\r\n\r\n")
        if len(head) + 4 > server.max_header_bytes:
            raise _Reject("400 Bad Request", "header_too_large")
        try:
            lines = head.decode("ascii").split("\r\n")
        except UnicodeDecodeError:
            raise _Reject("400 Bad Request", "malformed_request") from None
        match = _REQUEST_LINE.fullmatch(lines[0])
        if not match or any(not line or "\n" in line or "\r" in line for line in lines[1:]):
            raise _Reject("400 Bad Request", "malformed_request")
        if match[1] != "CONNECT":
            # Never log a plain-HTTP target: its path or query may carry data.
            raise _Reject("403 Forbidden", "method_not_allowed")
        try:
            target = format_target(*parse_target(match[2]))
        except ValueError:
            raise _Reject("400 Bad Request", "malformed_target") from None
        if target not in server.allow:
            raise _Reject("403 Forbidden", "not_allowlisted", target)
        return target, early

    def _open_upstream(self, target: str) -> socket.socket:
        host, port = parse_target(target)
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise _Reject("502 Bad Gateway", "resolve_failed", target) from None
        addresses = [info for info in infos if info[0] in (socket.AF_INET, socket.AF_INET6)]
        if not _is_ip(host):
            # A name must not reach loopback/private/link-local space (e.g. the Docker host).
            # IP literals are only reachable when allowlisted verbatim.
            if not addresses or any(not ipaddress.ip_address(info[4][0].split("%")[0]).is_global
                                    for info in addresses):
                raise _Reject("403 Forbidden", "resolved_to_non_public_address", target)
        if self.server.upstream_proxy:
            connection = http.client.HTTPConnection(*self.server.upstream_proxy,
                                                    timeout=self.server.connect_timeout_s)
            try:
                connection.set_tunnel(host, port)
                connection.connect()
                upstream, connection.sock = connection.sock, None
                return upstream
            except (OSError, http.client.HTTPException):
                connection.close()
                raise _Reject('502 Bad Gateway', 'upstream_proxy_failed', target) from None
        for family, kind, proto, _, address in addresses:
            upstream = socket.socket(family, kind, proto)
            upstream.settimeout(self.server.connect_timeout_s)
            try:
                upstream.connect(address)  # the resolved address, so no second lookup
                return upstream
            except OSError:
                upstream.close()
        raise _Reject("502 Bad Gateway", "upstream_connect_failed", target)

    def _tunnel(self, client, upstream, record) -> None:
        server = self.server
        hard_deadline = time.monotonic() + server.max_tunnel_s
        peers = {client: (upstream, "bytes_up"), upstream: (client, "bytes_down")}
        with selectors.DefaultSelector() as selector:
            for sock in peers:
                sock.setblocking(False)
                selector.register(sock, selectors.EVENT_READ)
            while True:
                wait = min(server.idle_timeout_s, hard_deadline - time.monotonic())
                if wait <= 0:
                    record["reason"] = "max_duration"
                    return
                events = selector.select(wait)
                if not events:
                    record["reason"] = "idle_timeout" if time.monotonic() < hard_deadline else "max_duration"
                    return
                for key, _ in events:
                    target, counter = peers[key.fileobj]
                    try:
                        data = key.fileobj.recv(_CHUNK)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not data:
                        return
                    target.setblocking(True)
                    target.settimeout(server.idle_timeout_s)
                    target.sendall(data)
                    target.setblocking(False)
                    record[counter] += len(data)

    @staticmethod
    def _reject(sock, status: str) -> None:
        try:
            sock.settimeout(2)
            sock.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            sock.shutdown(socket.SHUT_WR)
            # Drain briefly so unread request bytes do not turn the close into a reset.
            deadline, drained = time.monotonic() + 1, 0
            while drained < 1 << 20 and time.monotonic() < deadline:
                chunk = sock.recv(_CHUNK)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass


class EgressProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, address, allow=DEFAULT_ALLOW, *, log=None, max_tunnels: int = MAX_TUNNELS,
                 max_header_bytes: int = MAX_HEADER_BYTES, header_timeout_s: float = HEADER_TIMEOUT_S,
                 connect_timeout_s: float = CONNECT_TIMEOUT_S, idle_timeout_s: float = IDLE_TIMEOUT_S,
                 max_tunnel_s: float = MAX_TUNNEL_S, upstream_proxy: str | None = None):
        if max_tunnels < 1 or max_header_bytes < 64:
            raise ValueError("invalid proxy limits")
        self.allow = normalise_allowlist(allow)
        self.upstream_proxy = upstream_address(upstream_proxy)
        self.slots = threading.BoundedSemaphore(max_tunnels)
        self.max_header_bytes, self.header_timeout_s = max_header_bytes, header_timeout_s
        self.connect_timeout_s, self.idle_timeout_s = connect_timeout_s, idle_timeout_s
        self.max_tunnel_s = max_tunnel_s
        self._log = log if log is not None else sys.stderr
        self._log_lock = threading.Lock()
        host = address[0]
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def log_decision(self, record: dict, started: float) -> None:
        line = json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                           **record, "duration_s": round(time.monotonic() - started, 3)},
                          sort_keys=True)
        with self._log_lock:
            try:
                self._log.write(line + "\n")
                self._log.flush()
            except (OSError, ValueError):
                pass


def start_in_thread(allow, host: str = "127.0.0.1", port: int = 0, **options):
    """Serve in a daemon thread; returns (server, bound_port). Call server.shutdown(); server.server_close()."""
    server = EgressProxy((host, port), allow, **options)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server, server.server_address[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.research.agents.egress_proxy",
                                     description="CONNECT-only allowlisting egress proxy")
    parser.add_argument("--listen", default="0.0.0.0:3128", help="HOST:PORT to bind")
    parser.add_argument("--allow", action="append", help="exact host:port; repeatable "
                        f"(default {', '.join(DEFAULT_ALLOW)})")
    parser.add_argument("--log", help="append decision JSON lines to FILE (default stderr)")
    parser.add_argument("--max-tunnels", type=int, default=MAX_TUNNELS)
    parser.add_argument("--max-header-bytes", type=int, default=MAX_HEADER_BYTES)
    parser.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_S)
    parser.add_argument("--max-tunnel-seconds", type=float, default=MAX_TUNNEL_S)
    parser.add_argument("--upstream-proxy", help="trusted http://host:port CONNECT proxy; no credentials")
    args = parser.parse_args(argv)
    host, _, port = args.listen.rpartition(":")
    if not host or not port.isdigit():
        parser.error("--listen must be HOST:PORT")
    log = open(args.log, "a", encoding="utf-8", newline="\n") if args.log else sys.stderr
    try:
        server = EgressProxy((host.strip("[]"), int(port)), args.allow or DEFAULT_ALLOW, log=log,
                             max_tunnels=args.max_tunnels, max_header_bytes=args.max_header_bytes,
                             idle_timeout_s=args.idle_timeout, max_tunnel_s=args.max_tunnel_seconds,
                             upstream_proxy=args.upstream_proxy)
    except ValueError as error:
        parser.error(str(error))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if log is not sys.stderr:
            log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
