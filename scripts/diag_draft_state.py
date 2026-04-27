"""Investigation diagnostic for live draft state — run when in a pod.

This script walks the Mono object graph from ``WrapperController.<Instance>``
along plausible paths to draft state and dumps every field at each level so
the right field names can be identified for
:func:`client.overlay.memory.walker.read_draft_state`.

See ``docs/draft-state-investigation.md`` for the procedure.

Usage::

    py -3 scripts\\diag_draft_state.py            > diag_p1p1.txt
    # ... make pick, wait for next pack ...
    py -3 scripts\\diag_draft_state.py            > diag_p1p2.txt
    diff diag_p1p1.txt diag_p1p2.txt              # find what changed

Output dumps per level:
  * field name, MonoType code (decoded), instance offset
  * value if scalar (int, string)
  * for object fields, the runtime class name
  * for any candidate ``int[]`` array, its length and first/last few values
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# Mono type code names for human-readable dump output. Keep this minimal.
_TYPE_NAMES = {
    0x02: "BOOL", 0x03: "CHAR",
    0x04: "I1", 0x05: "U1", 0x06: "I2", 0x07: "U2",
    0x08: "I4", 0x09: "U4", 0x0A: "I8", 0x0B: "U8",
    0x0C: "R4", 0x0D: "R8",
    0x0E: "STRING", 0x11: "VALUETYPE", 0x12: "CLASS",
    0x13: "VAR", 0x14: "ARRAY", 0x15: "GENERICINST",
    0x18: "I", 0x19: "U", 0x1C: "OBJECT", 0x1D: "SZARRAY",
    0x55: "ENUM",
}


def _type_name(code: int) -> str:
    return _TYPE_NAMES.get(code, f"0x{code:02x}")


def _dump_obj(label: str, obj: Any, indent: int = 0, *, max_depth: int = 1) -> None:
    """Print every field of an ObjectInstance / ClassDefinition."""
    from client.overlay.memory.mono import ClassDefinition, ObjectInstance

    pad = "  " * indent
    if obj is None:
        print(f"{pad}{label}: <None>")
        return
    if isinstance(obj, ObjectInstance):
        klass = obj.runtime_class()
        if klass is None:
            print(f"{pad}{label}: <ObjectInstance @ {obj.address:#x}, runtime_class=None>")
            return
        print(f"{pad}{label}: {klass.full_name} @ {obj.address:#x}")
        for field in sorted(klass.fields(), key=lambda f: f.offset):
            _dump_field(obj, field, indent + 1, max_depth=max_depth)
    elif isinstance(obj, ClassDefinition):
        print(f"{pad}{label}: <ClassDefinition {obj.full_name} @ {obj.address:#x}>")
        for field in sorted(obj.fields(), key=lambda f: f.offset):
            tag = "static" if field.is_static else "inst"
            print(f"{pad}  +{field.offset:#04x} [{tag}] {field.name}: {_type_name(field.type_code)}")
    else:
        print(f"{pad}{label}: {obj!r}")


def _dump_field(obj: Any, field, indent: int, *, max_depth: int) -> None:
    """Print one field with its decoded value (recurses one level for objects)."""
    from client.overlay.memory.mono import ObjectInstance

    pad = "  " * indent
    name = field.name
    tcode = _type_name(field.type_code)
    tag = "S" if field.is_static else "I"

    # Try to read the value via the high-level API.
    try:
        value = obj.get(name)
    except Exception as exc:
        print(f"{pad}+{field.offset:#04x} [{tag}] {name}: {tcode}  <read-error: {exc}>")
        return

    # Object pointer — descend if depth allows.
    if isinstance(value, ObjectInstance):
        klass = value.runtime_class()
        type_label = klass.full_name if klass else "?"
        print(f"{pad}+{field.offset:#04x} [{tag}] {name}: {tcode} -> {type_label} @ {value.address:#x}")
        # Try the int[] heuristic — pull array length & first elements.
        _maybe_dump_int_array(obj.image, value, name, indent + 1)
        if max_depth > 0 and klass is not None:
            for sub in sorted(klass.fields(), key=lambda f: f.offset)[:32]:
                if sub.is_static:
                    continue
                _dump_field(value, sub, indent + 1, max_depth=max_depth - 1)
        return

    # Primitive: int / string / None
    print(f"{pad}+{field.offset:#04x} [{tag}] {name}: {tcode} = {value!r}")


def _maybe_dump_int_array(image, obj, label: str, indent: int) -> None:
    """If ``obj`` looks like a Mono int[] array, print its contents."""
    from client.overlay.memory.mono import ObjectInstance

    if not isinstance(obj, ObjectInstance):
        return
    klass = obj.runtime_class()
    if klass is None:
        return
    if klass.full_name not in {"System.Int32[]", "Int32[]", "uint[]", "UInt32[]"}:
        return
    pad = "  " * indent
    # Build a fake "field" at offset 0 to use ProcessReader.read_int32_array.
    # The address of the ObjectInstance IS the array pointer here, so we
    # need to read the length directly from offset 0x18 (3 * size_of_ptr).
    reader = image.reader
    size_of_ptr = reader.size_of_ptr
    try:
        length = reader.read_int32(obj.address + size_of_ptr * 3)
    except Exception:
        return
    if length < 0 or length > 256:
        print(f"{pad}* {label} int[]: bogus length={length}")
        return
    if length == 0:
        print(f"{pad}* {label} int[]: empty")
        return
    try:
        import struct
        data = reader.read_bytes(obj.address + size_of_ptr * 4, 4 * length)
    except Exception:
        print(f"{pad}* {label} int[]: failed to read body (length={length})")
        return
    elements = struct.unpack(f"<{length}i", data)
    head = list(elements[:6])
    tail = list(elements[-3:]) if length > 9 else []
    print(f"{pad}* {label} int[]: length={length} head={head} tail={tail}")


def main() -> int:
    from client.overlay.memory.session import MemorySession
    from client.overlay.memory.mono import ObjectInstance

    session = MemorySession.instance()
    if not session.ensure_attached():
        print("Could not attach to MTGA. Is the game running?", file=sys.stderr)
        return 1
    image = session.image

    # ---- 1. WrapperController instance + every field on it -------------------
    print("=" * 78)
    print("WrapperController.<Instance>k__BackingField — every field, depth 0")
    print("=" * 78)
    wrapper_class = image.get_class("WrapperController")
    if wrapper_class is None:
        print("ERROR: WrapperController not found", file=sys.stderr)
        return 2
    wrapper = wrapper_class.get_static("<Instance>k__BackingField")
    _dump_obj("WrapperController", wrapper, max_depth=0)

    # ---- 2. EventPageContentController.PlayerEvent.CourseData ----------------
    print("\n" + "=" * 78)
    print("Event lobby chain — CourseData (most likely site of draft state)")
    print("=" * 78)
    if isinstance(wrapper, ObjectInstance):
        scene_loader = wrapper.get("<SceneLoader>k__BackingField")
        if isinstance(scene_loader, ObjectInstance):
            current = scene_loader.get("<CurrentNavContent>k__BackingField")
            if isinstance(current, ObjectInstance):
                klass = current.runtime_class()
                ftype = klass.full_name if klass else "?"
                print(f"CurrentNavContent runtime type = {ftype}")
                if klass and klass.full_name == "EventPage.EventPageContentController":
                    ctx = current.get("_currentEventContext")
                    if isinstance(ctx, ObjectInstance):
                        _dump_obj("_currentEventContext", ctx, max_depth=0)
                        player_event = ctx.get("PlayerEvent")
                        if isinstance(player_event, ObjectInstance):
                            print()
                            _dump_obj("PlayerEvent", player_event, max_depth=2)
                            course_data = player_event.get("<CourseData>k__BackingField")
                            print()
                            _dump_obj("CourseData", course_data, max_depth=2)

    # ---- 3. PAPA + event manager dump ---------------------------------------
    print("\n" + "=" * 78)
    print("PAPA singleton — every field, depth 1")
    print("=" * 78)
    papa_class = image.get_class("PAPA")
    if papa_class is not None:
        # PAPA is typically also a static singleton.
        for fname in ("<Instance>k__BackingField", "_instance", "Instance"):
            try:
                papa = papa_class.get_static(fname)
                if papa is not None:
                    print(f"(located via static field {fname!r})")
                    _dump_obj("PAPA", papa, max_depth=1)
                    break
            except Exception:
                continue
        else:
            print("Could not access a PAPA static instance via known field names")
    else:
        print("PAPA class not registered (yet)")

    # ---- 4. Search for any draft manager-like classes -----------------------
    print("\n" + "=" * 78)
    print("Class registry: classes whose name contains 'Draft'")
    print("=" * 78)
    for klass in sorted(image.classes(), key=lambda c: c.full_name):
        if "draft" in klass.name.lower() and "<>" not in klass.name:
            print(f"  {klass.full_name}")

    print("\n" + "=" * 78)
    print("Class registry: classes whose name contains 'Pack' / 'Pick'")
    print("=" * 78)
    for klass in sorted(image.classes(), key=lambda c: c.full_name):
        n = klass.name.lower()
        if ("pack" in n or "pick" in n) and "<>" not in klass.name:
            print(f"  {klass.full_name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
