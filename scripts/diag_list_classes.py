"""List every class our walker can see in Assembly-CSharp."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO)


def main() -> int:
    from client.overlay.memory.session import MemorySession

    session = MemorySession.instance()
    if not session.ensure_attached():
        print("not attached", file=sys.stderr)
        return 1
    image = session.image
    classes = sorted(image.classes(), key=lambda c: c.full_name)
    print(f"total classes: {len(classes)}")
    has_wrapper = False
    has_account = False
    has_event = False
    for klass in classes:
        if klass.name in ("WrapperController", "AccountClient",
                          "AccountInformation", "SceneLoader",
                          "EventPageContentController",
                          "PlayerEvent", "EventInfo", "CourseData"):
            print(f"  found: {klass.full_name!r} @ {klass.address:#x}")
        if klass.name == "WrapperController":
            has_wrapper = True
        if klass.name == "AccountClient":
            has_account = True
        if klass.name == "EventPageContentController":
            has_event = True
    print()
    print(f"has WrapperController: {has_wrapper}")
    print(f"has AccountClient: {has_account}")
    print(f"has EventPageContentController: {has_event}")

    print("\n-- first 50 class full names --")
    for klass in classes[:50]:
        print(f"  {klass.full_name}")
    print("\n-- last 50 class full names --")
    for klass in classes[-50:]:
        print(f"  {klass.full_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
