"""Targeted dump of objects suspected to own draft pack/pick state.

Writes UTF-8 directly to disk so unicode characters from MTGA strings
don't crash on Windows' default cp1252 stdout.

Usage::

    py -3 scripts/diag_draft_targeted.py [output_path]

Default output: diag_targeted.txt next to the cwd.
"""

from __future__ import annotations

import logging
import struct
import sys
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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


def _tn(c: int) -> str:
    return _TYPE_NAMES.get(c, f"0x{c:02x}")


def _dump_int_array_maybe(image, obj, out: TextIO, label: str, indent: int) -> bool:
    from client.overlay.memory.mono import ObjectInstance
    if not isinstance(obj, ObjectInstance):
        return False
    klass = obj.runtime_class()
    if klass is None or klass.full_name not in {
        "System.Int32[]", "Int32[]", "uint[]", "UInt32[]"
    }:
        return False
    reader = image.reader
    sp = reader.size_of_ptr
    try:
        length = reader.read_int32(obj.address + sp * 3)
    except Exception:
        return True
    pad = "  " * indent
    if length < 0 or length > 1024:
        out.write(f"{pad}* {label}: int[] bogus length={length}\n")
        return True
    if length == 0:
        out.write(f"{pad}* {label}: int[] empty\n")
        return True
    try:
        data = reader.read_bytes(obj.address + sp * 4, 4 * length)
        elements = struct.unpack(f"<{length}i", data)
    except Exception:
        out.write(f"{pad}* {label}: int[] read failed (length={length})\n")
        return True
    out.write(f"{pad}* {label}: int[] length={length} values={list(elements)}\n")
    return True


def _dump_list_int_maybe(image, obj, out: TextIO, label: str, indent: int) -> bool:
    """Dump if obj is a List<int> / List<uint>."""
    from client.overlay.memory.mono import ObjectInstance
    if not isinstance(obj, ObjectInstance):
        return False
    klass = obj.runtime_class()
    if klass is None or "List`1" not in klass.full_name:
        return False
    reader = image.reader
    sp = reader.size_of_ptr
    try:
        items_ptr = reader.read_ptr(obj.address + sp * 2)
        size = reader.read_int32(obj.address + sp * 3)
    except Exception:
        return True
    pad = "  " * indent
    if items_ptr == 0:
        out.write(f"{pad}* {label}: List<?> empty/null items\n")
        return True
    if size <= 0 or size > 1024:
        out.write(f"{pad}* {label}: List<?> size={size}\n")
        return True
    # array length is at +sp*3
    try:
        arr_len = reader.read_int32(items_ptr + sp * 3)
    except Exception:
        out.write(f"{pad}* {label}: List<?> size={size} (arr_len read failed)\n")
        return True
    try:
        n = min(size, arr_len)
        data = reader.read_bytes(items_ptr + sp * 4, 4 * n)
        ints = struct.unpack(f"<{n}i", data)
    except Exception:
        out.write(f"{pad}* {label}: List<?> size={size} arr_len={arr_len} (read failed)\n")
        return True
    out.write(f"{pad}* {label}: List<int?> size={size} values={list(ints)}\n")
    return True


def _dump_dict_int_int_maybe(image, obj, out: TextIO, label: str, indent: int) -> bool:
    """Dump Dictionary<int,int> if obj is one — useful for pack→pickedGrpId."""
    from client.overlay.memory.mono import ObjectInstance
    if not isinstance(obj, ObjectInstance):
        return False
    klass = obj.runtime_class()
    if klass is None or "Dictionary`2" not in klass.full_name:
        return False
    reader = image.reader
    sp = reader.size_of_ptr
    pad = "  " * indent
    # Dictionary<TKey,TValue> layout (NetCore Mono):
    #   header(2 ptrs) + buckets_arr_ptr + entries_arr_ptr + ... + count(int32)
    # We try a permissive scan: read potential count at sp*7
    try:
        count = reader.read_int32(obj.address + sp * 7)
    except Exception:
        return True
    if count < 0 or count > 256:
        out.write(f"{pad}* {label}: Dictionary count(@{sp*7:#x})={count} (suspect)\n")
        return True
    out.write(f"{pad}* {label}: Dictionary count={count}\n")
    return True


def _walk(obj: Any, out: TextIO, indent: int = 0, max_depth: int = 2, prefix: str = "") -> None:
    from client.overlay.memory.mono import ObjectInstance
    pad = "  " * indent
    if obj is None:
        out.write(f"{pad}{prefix}<None>\n")
        return
    if not isinstance(obj, ObjectInstance):
        out.write(f"{pad}{prefix}{obj!r}\n")
        return
    klass = obj.runtime_class()
    if klass is None:
        out.write(f"{pad}{prefix}<ObjectInstance @ {obj.address:#x}>\n")
        return
    out.write(f"{pad}{prefix}{klass.full_name} @ {obj.address:#x}\n")
    fields = sorted(klass.fields(), key=lambda f: f.offset)
    for f in fields:
        if f.is_static or f.is_literal:
            continue
        try:
            v = obj.get(f.name)
        except Exception as exc:
            out.write(f"{pad}  +{f.offset:#04x} {f.name}: {_tn(f.type_code)} <err {exc}>\n")
            continue
        if isinstance(v, ObjectInstance):
            sub = v.runtime_class()
            sub_name = sub.full_name if sub else "?"
            out.write(f"{pad}  +{f.offset:#04x} {f.name}: {_tn(f.type_code)} -> {sub_name} @ {v.address:#x}\n")
            if _dump_int_array_maybe(obj.image, v, out, f.name, indent + 2):
                continue
            if _dump_list_int_maybe(obj.image, v, out, f.name, indent + 2):
                continue
            if _dump_dict_int_int_maybe(obj.image, v, out, f.name, indent + 2):
                pass  # also recurse for objects
            if max_depth > 0 and sub is not None and "List`1" not in sub_name:
                _walk(v, out, indent + 2, max_depth - 1)
        else:
            try:
                # Sanitize non-ASCII chars in strings
                if isinstance(v, str):
                    v_repr = repr(v)
                else:
                    v_repr = repr(v)
            except Exception:
                v_repr = "<unrepr>"
            out.write(f"{pad}  +{f.offset:#04x} {f.name}: {_tn(f.type_code)} = {v_repr}\n")


def main(argv: list[str]) -> int:
    from client.overlay.memory.session import MemorySession
    from client.overlay.memory.mono import ObjectInstance

    out_path = Path(argv[1]) if len(argv) > 1 else Path.cwd() / "diag_targeted.txt"

    session = MemorySession.instance()
    if not session.ensure_attached():
        sys.stderr.write("Could not attach to MTGA.\n")
        return 1
    image = session.image

    wc = image.get_class("WrapperController")
    wrapper = wc.get_static("<Instance>k__BackingField") if wc else None
    if not isinstance(wrapper, ObjectInstance):
        sys.stderr.write("WrapperController.Instance not available\n")
        return 2

    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        scene_loader = wrapper.get("<SceneLoader>k__BackingField")
        current = scene_loader.get("<CurrentNavContent>k__BackingField") if isinstance(scene_loader, ObjectInstance) else None
        klass = current.runtime_class() if isinstance(current, ObjectInstance) else None
        out.write(f"CurrentNavContent runtime type = {klass.full_name if klass else '?'}\n\n")

        if not isinstance(current, ObjectInstance):
            out.write("CurrentNavContent is null — not in a draft view.\n")
            return 0

        out.write("=" * 78 + "\n")
        out.write("_packCollection (depth 3)\n")
        out.write("=" * 78 + "\n")
        pc = current.get("_packCollection")
        _walk(pc, out, max_depth=3)

        out.write("\n" + "=" * 78 + "\n")
        out.write("_limitedEvent (depth 3)\n")
        out.write("=" * 78 + "\n")
        le = current.get("_limitedEvent")
        _walk(le, out, max_depth=3)

        out.write("\n" + "=" * 78 + "\n")
        out.write("_eventContext (depth 3)\n")
        out.write("=" * 78 + "\n")
        ec = current.get("_eventContext")
        _walk(ec, out, max_depth=3)

        out.write("\n" + "=" * 78 + "\n")
        out.write("_draftPackIndexToDraftPickGrpId (depth 4)\n")
        out.write("=" * 78 + "\n")
        d = current.get("_draftPackIndexToDraftPickGrpId")
        _walk(d, out, max_depth=4)

        out.write("\n" + "=" * 78 + "\n")
        out.write("EventManager (depth 2)\n")
        out.write("=" * 78 + "\n")
        em = wrapper.get("<EventManager>k__BackingField")
        _walk(em, out, max_depth=2)

        out.write("\n" + "=" * 78 + "\n")
        out.write("DraftContentController flags + small fields\n")
        out.write("=" * 78 + "\n")
        for fname in ("_active", "_isGettingInitialStatus", "_okToPickCard",
                      "AutoPickCards", "_isForceVertical"):
            try:
                v = current.get(fname)
            except Exception as exc:
                v = f"<err {exc}>"
            out.write(f"  {fname} = {v!r}\n")

    sys.stderr.write(f"Wrote {out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
