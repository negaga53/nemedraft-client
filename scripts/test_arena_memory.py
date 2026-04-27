"""Smoke-test the new pymem-based Arena memory reader.

Run this on Windows (where MTG Arena is) with MTGA.exe at the home screen
or in a draft lobby:

    py -3 scripts\\test_arena_memory.py

Reports:
  * is_memory_supported() / attach success
  * Player identity (account ID, display name, persona ID)
  * Current event landing state (lobby / type / event name / draft id)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make sibling 'client' package importable when run from the scripts dir.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> int:
    from client.overlay.arena_memory import (
        get_arena_current_event,
        get_arena_player_identity,
        is_memory_supported,
    )
    from client.overlay.memory.session import MemorySession

    print(f"sys.platform   = {sys.platform}")
    print(f"is_memory_supported() = {is_memory_supported()}")

    if not is_memory_supported():
        print("Memory access not supported on this platform.", file=sys.stderr)
        return 1

    session = MemorySession.instance()
    attached = session.ensure_attached()
    print(f"MemorySession.ensure_attached() = {attached}")
    if not attached:
        print(
            "Could not attach to MTGA. Is MTG Arena running and signed in?",
            file=sys.stderr,
        )
        return 2
    print(f"  pid       = {session.pid}")
    print(f"  image @   = {session.image.address:#x}")

    print("\n-- /playerId --")
    identity = get_arena_player_identity()
    if identity is None:
        print("  no identity available (Arena may still be loading)")
    else:
        print(f"  player_id    = {identity.player_id!r}")
        print(f"  display_name = {identity.display_name!r}")
        print(f"  elapsed_ms   = {identity.elapsed_ms}")

    print("\n-- /currentEvent --")
    event = get_arena_current_event()
    if event is None:
        print("  no current event payload (Arena scene tree not ready)")
    else:
        print(f"  is_in_event_lobby   = {event.is_in_event_lobby}")
        print(f"  content_type        = {event.content_type!r}")
        print(f"  internal_event_name = {event.internal_event_name!r}")
        print(f"  event_state         = {event.event_state}")
        print(f"  format_type         = {event.format_type}")
        print(f"  draft_id            = {event.draft_id!r}")
        print(f"  current_event_state = {event.current_event_state}")
        print(f"  current_module      = {event.current_module}")
        print(f"  is_draft_lobby      = {event.is_draft_lobby}")
        print(f"  elapsed_ms          = {event.elapsed_ms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
