"""Turn a dumped vision request into a redacted fixture (no original pixels).

Usage:
  python scripts/redact_vision_agent_capture.py dump.json -o tests/fixtures/vision_agents/opencode/live.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Allow running from local-llm-router without installing the plugin.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from shared_quota_router.vision_agents.capture import redact_capture  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON dump with messages and optional headers")
    parser.add_argument("-o", "--output", required=True, help="Destination fixture path")
    args = parser.parse_args()
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    redacted = redact_capture(raw)
    redacted["provenance"] = {
        "kind": "live-gateway",
        "live_gateway": True,
        "source": Path(args.input).name,
    }
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(redacted, indent=2) + "\n", encoding="utf-8")
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
