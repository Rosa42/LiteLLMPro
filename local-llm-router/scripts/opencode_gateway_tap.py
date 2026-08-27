"""Dump-and-forward tap: OpenCode → this process → LiteLLM gateway.

Listens on 127.0.0.1:18777 by default, forwards to 127.0.0.1:4000, and writes
one redacted Anthropic Messages dump (no original pixels, no secrets).
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from shared_quota_router.vision_agents.capture import redact_capture  # noqa: E402


class TapHandler(BaseHTTPRequestHandler):
    target_base = "http://127.0.0.1:4000"
    dump_path = Path("opencode-live.json")
    dumped = 0
    max_dumps = 8

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._maybe_dump(body)
        url = self.target_base.rstrip("/") + self.path
        headers = {
            str(key): str(value)
            for key, value in self.headers.items()
            if str(key).lower() not in {"host", "content-length"}
        }
        req = Request(url, data=body if body else None, headers=headers, method=self.command)
        try:
            with urlopen(req, timeout=120) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if str(key).lower() in {"transfer-encoding", "connection"}:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)
        except HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            for key, value in exc.headers.items() if exc.headers else []:
                if str(key).lower() in {"transfer-encoding", "connection"}:
                    continue
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)
        except URLError as exc:
            msg = str(exc.reason or exc).encode("utf-8")
            self.send_response(502)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _maybe_dump(self, body: bytes) -> None:
        if TapHandler.dumped >= TapHandler.max_dumps:
            return
        if self.command != "POST":
            return
        path = (self.path or "").split("?", 1)[0]
        if not path.endswith("/messages"):
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        dump = redact_capture(
            {
                "headers": {str(k): str(v) for k, v in self.headers.items()},
                "messages": payload.get("messages"),
            }
        )
        n = TapHandler.dumped + 1
        if dump.get("headers"):
            dump["headers"]["host"] = "127.0.0.1:4000"
        dump["provenance"] = {
            "kind": "live-gateway",
            "live_gateway": True,
            "source": "opencode_gateway_tap",
            "path": path,
            "model": payload.get("model"),
            "seq": n,
        }
        dest = TapHandler.dump_path
        if n == 1:
            out = dest
        else:
            out = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dump, indent=2) + "\n", encoding="utf-8")
        TapHandler.dumped = n
        sys.stderr.write("wrote %s\n" % out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:18777")
    parser.add_argument("--target", default="http://127.0.0.1:4000")
    parser.add_argument(
        "-o",
        "--output",
        default=str(
            ROOT / "tests" / "fixtures" / "vision_agents" / "opencode" / "live.json"
        ),
    )
    args = parser.parse_args()
    host, port_s = args.listen.rsplit(":", 1)
    TapHandler.target_base = args.target
    TapHandler.dump_path = Path(args.output)
    TapHandler.dumped = 0
    httpd = ThreadingHTTPServer((host, int(port_s)), TapHandler)
    sys.stderr.write("tap %s -> %s dump=%s\n" % (args.listen, args.target, args.output))
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
