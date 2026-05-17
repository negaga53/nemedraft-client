"""Smoke test: call read_draft_state and pretty-print its payload."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.overlay.memory.session import MemorySession
from client.overlay.memory.walker import read_draft_state


def main() -> int:
    session = MemorySession.instance()
    if not session.ensure_attached():
        print("Could not attach to MTGA", file=sys.stderr)
        return 1
    payload = read_draft_state(session)
    if payload is None:
        print("read_draft_state returned None (no active draft view)")
        return 0
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
