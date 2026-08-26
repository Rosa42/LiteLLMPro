#!/usr/bin/env python3
"""Host-side MiniMax TLS bridge for Docker Desktop on this Windows network.

The Linux LiteLLM container cannot complete TLS to api.minimaxi.com (SSLEOF
even with a DoH-pinned A record). The Windows host Python stack can, if it
connects to the real IP with SNI. This process listens on loopback HTTP and
forwards Anthropic Messages POSTs to MiniMax over that host path.

  F:\\anaconda\\envs\\py312\\python.exe scripts\\minimax_host_bridge.py

Then point the container at:
  MINIMAX_ANTHROPIC_BASE_URL=http://host.docker.internal:18443/anthropic

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
from urllib.request import Request, urlopen

HOST = "api.minimaxi.com"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18443
DOH = "https://cloudflare-dns.com/dns-query?name=api.minimaxi.com&type=A"
_ip = ""
_ip_lock = threading.Lock()
_ip_at = 0.0
_TTL = 30.0


def resolve_ip() -> str:
    global _ip, _ip_at
    now = time.monotonic()
    with _ip_lock:
        if _ip and (now - _ip_at) < _TTL:
            return _ip
    try:
        req = Request(DOH, headers={"accept": "application/dns-json"})
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        answers = payload.get("Answer") or []
        for item in answers:
            if int(item.get("type") or 0) == 1 and item.get("data"):
                ip = str(item["data"]).strip()
                if ip and not ip.startswith("28."):
                    with _ip_lock:
                        _ip = ip
                        _ip_at = time.monotonic()
                    return ip
    except Exception:
        pass
    with _ip_lock:
        if _ip:
            return _ip
    raise OSError("minimax DoH A record unavailable")


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


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
    for key in ("content-type", "x-api-key", "anthropic-version", "user-agent"):
        val = headers.get(key) or headers.get(key.title())
        if val:
            wire = {
                "content-type": "Content-Type",
                "x-api-key": "x-api-key",
                "anthropic-version": "anthropic-version",
                "user-agent": "User-Agent",
            }[key]
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
    if rest.lower().startswith(b"chunked"):
        pass
    # Handle Transfer-Encoding: chunked
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
        print(f"[minimax-bridge] {self.command} {urlparse(self.path).path}", flush=True)

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
    print(f"minimax host bridge http://{LISTEN_HOST}:{LISTEN_PORT} -> {HOST} ({ip})", flush=True)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
