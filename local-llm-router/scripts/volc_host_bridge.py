#!/usr/bin/env python3
"""Host-side Volc Coding TLS bridge for Docker Desktop on this Windows network.

The Linux LiteLLM container resolves arkcn-beijing.volces.com and then hits
SSLEOF on TLS (same class of failure as MiniMax). Windows host Python can
complete TLS using the host resolver. This process listens on loopback HTTP
and forwards Anthropic Messages POSTs to Volc over that host path.

  F:\\anaconda\\envs\\py312\\python.exe scripts\\volc_host_bridge.py

Then point the container at:
  VOLC_CODING_ANTHROPIC_BASE_URL=http://host.docker.internal:18444/api/coding

Never logs API keys or request bodies.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "arkcn-beijing.volces.com"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18444
# Cloudflare DoH NXDOMAINs this name; Aliyun / DNSPod see the China A records.
_DOH = (
    "https://dns.alidns.com/resolve?name=ark.cn-beijing.volces.com&type=A",
    "https://doh.pub/dns-query?name=ark.cn-beijing.volces.com&type=A",
)
_ip = ""
_ip_lock = threading.Lock()
_ip_at = 0.0
_TTL = 30.0
_FORWARD_HEADERS = (
    "content-type",
    "x-api-key",
    "anthropic-version",
    "anthropic-beta",
    "user-agent",
    "authorization",
)


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _tls_ok(ip: str) -> bool:
    try:
        raw = socket.create_connection((ip, 443), timeout=5)
        try:
            sock = _tls_context().wrap_socket(raw, server_hostname=HOST)
            sock.close()
        finally:
            try:
                raw.close()
            except OSError:
                pass
        return True
    except Exception:
        return False


def _doh_ips() -> list[str]:
    from urllib.request import Request, urlopen

    out: list[str] = []
    for url in _DOH:
        try:
            req = Request(url, headers={"accept": "application/dns-json"})
            with urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
        answers = payload.get("Answer") or []
        if isinstance(answers, dict):
            answers = [answers]
        for item in answers:
            if not isinstance(item, dict):
                continue
            if int(item.get("type") or 0) != 1:
                continue
            ip = str(item.get("data") or "").strip()
            if ip and not ip.startswith("28.") and ":" not in ip and ip not in out:
                out.append(ip)
    return out


def resolve_ip() -> str:
    global _ip, _ip_at
    now = time.monotonic()
    with _ip_lock:
        if _ip and (now - _ip_at) < _TTL:
            return _ip
    for ip in _doh_ips():
        if _tls_ok(ip):
            with _ip_lock:
                _ip = ip
                _ip_at = time.monotonic()
            return ip
    with _ip_lock:
        if _ip:
            return _ip
    raise OSError("volc DoH A record with working TLS unavailable")


def forward_post(path: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes, str]:
    ip = resolve_ip()
    if not path.startswith("/"):
        path = "/" + path
    req_headers = [
        f"POST {path} HTTP/1.1",
        f"Host: {HOST}",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    for key in _FORWARD_HEADERS:
        val = headers.get(key) or headers.get(key.title())
        if val:
            wire = "User-Agent" if key == "user-agent" else key
            req_headers.append(f"{wire}: {val}")
    blob = ("\r\n".join(req_headers) + "\r\n\r\n").encode("ascii") + body
    raw = socket.create_connection((ip, 443), timeout=timeout)
    try:
        sock = _tls_context().wrap_socket(raw, server_hostname=HOST)
        sock.settimeout(timeout)
        sock.sendall(blob)
        chunks: list[bytes] = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
    finally:
        try:
            raw.close()
        except OSError:
            pass
    data = b"".join(chunks)
    sep = data.find(b"\r\n\r\n")
    if sep < 0:
        return 502, b'{"type":"error","error":{"message":"bridge: bad upstream framing"}}', "application/json"
    head, rest = data[:sep], data[sep + 4 :]
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = status_line.split(" ")
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        status = 502
    ctype = "application/json"
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-type:"):
            ctype = line.split(b":", 1)[1].decode("ascii", errors="replace").strip()
            break
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"transfer-encoding:") and b"chunked" in line.lower():
            rest = _dechunk(rest)
            break
    return status, rest, ctype


def _dechunk(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        nl = raw.find(b"\r\n", i)
        if nl < 0:
            break
        try:
            size = int(raw[i:nl], 16)
        except ValueError:
            break
        i = nl + 2
        if size == 0:
            break
        out.extend(raw[i : i + size])
        i += size + 2
    return bytes(out)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[volc-bridge] {self.command} {urlparse(self.path).path}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path.rstrip("/") in {"/health", ""}:
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.endswith("/messages"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16 * 1024 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else b""
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        try:
            status, payload, ctype = forward_post(path, hdrs, body, timeout=90.0)
        except Exception as exc:  # noqa: BLE001
            payload = json.dumps(
                {"type": "error", "error": {"message": f"bridge: {type(exc).__name__}"}}
            ).encode()
            status = 502
            ctype = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    ip = resolve_ip()
    print(f"volc host bridge http://{LISTEN_HOST}:{LISTEN_PORT} -> {HOST} ({ip})", flush=True)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
