#!/usr/bin/env python3
"""C1-05: client disconnect must release shared-quota inflight lease.

Starts a streaming curl, polls Redis while it runs, then kills curl mid-flight.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def load_env(path: str = ".env") -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v.strip().strip('"').strip("'")
    return out


def redis_inflight(pwd: str) -> int:
    r = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "deploy/docker-compose.yaml",
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "-a",
            pwd,
            "GET",
            "sq:inflight:opencode-a",
        ],
        capture_output=True,
        text=True,
    )
    for ln in r.stdout.strip().splitlines():
        if ln.isdigit():
            return int(ln)
    return 0


def main() -> int:
    if shutil.which("curl") is None and shutil.which("curl.exe") is None:
        print("SKIP: curl not found")
        return 0

    curl = "curl.exe" if shutil.which("curl.exe") else "curl"
    env = load_env()
    mk = env["LITELLM_MASTER_KEY"]
    pwd = env["REDIS_PASSWORD"]
    body = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "stream": True,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Count slowly from 1 to 50"}],
        }
    )

    base = redis_inflight(pwd)
    print(f"baseline inflight={base}")

    proc = subprocess.Popen(
        [
            curl,
            "-N",
            "-s",
            "-H",
            f"Authorization: Bearer {mk}",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            "http://127.0.0.1:4000/v1/chat/completions",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    saw_inflight = False
    try:
        for _ in range(30):
            time.sleep(0.5)
            cur = redis_inflight(pwd)
            if cur > base:
                saw_inflight = True
            if saw_inflight:
                proc.terminate()
                break
            if proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    print(f"curl exit={proc.returncode} saw_inflight={saw_inflight}")

    for i in range(15):
        time.sleep(1)
        cur = redis_inflight(pwd)
        print(f" t+{i + 1}s inflight={cur}")
        if cur == base:
            if saw_inflight:
                print("C1-05 DISCONNECT SMOKE PASS")
                return 0
            print("C1-05 DISCONNECT SMOKE FAIL (lease never acquired)")
            return 1

    print("C1-05 DISCONNECT SMOKE FAIL (inflight not released)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
