"""Live smoke: tick the MemoryWatcher once against current MTGA state
and print what it would emit. With MTGA in the deck-builder for a
finished draft, we expect a single DeckPoolDetectedEvent."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from client.overlay.memory_watcher import MemoryWatcher
    from client.overlay.memory.session import MemorySession

    mw = MemoryWatcher()
    events: list = []
    mw.add_callback(lambda e: events.append(e))

    session = MemorySession.instance()
    # Two ticks: the first should emit, the second should be a no-op
    # (fingerprint matches).
    for n in range(2):
        mw._tick(session)
        print(f"after tick {n + 1}: {len(events)} event(s)")
        for e in events:
            print(f"  -> {type(e).__name__} {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
