"""Deeper diagnostic — list assemblies, hash table size, sample class names."""

from __future__ import annotations

import logging
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO)


def main() -> int:
    import pymem
    from pymem.process import module_from_name

    from client.overlay.memory.offsets import select_offsets
    from client.overlay.memory.pe import find_export_rva, find_root_domain_pointer
    from client.overlay.memory.reader import ProcessReader

    pm = pymem.Pymem("MTGA.exe")
    print(f"pid = {pm.process_id}")

    module = module_from_name(pm.process_handle, "mono-2.0-bdwgc.dll")
    print(f"mono base = {int(module.lpBaseOfDll):#x}")
    print(f"mono path = {module.filename}")
    print(f"mono file_version = {getattr(module, 'file_version', '<n/a>')!r}")

    offsets = select_offsets(getattr(module, "file_version", None))
    print(f"offsets selected = {offsets is not None}")

    reader = ProcessReader(pm, is_64bits=True)
    rva = find_export_rva(module.filename, "mono_get_root_domain")
    print(f"mono_get_root_domain RVA = {rva:#x}")
    func_addr = int(module.lpBaseOfDll) + rva
    domain_var_addr = find_root_domain_pointer(reader, func_addr)
    domain = reader.read_ptr(domain_var_addr)
    print(f"MonoDomain* = {domain:#x}")

    head = reader.read_ptr(domain + offsets.referenced_assemblies)
    print(f"\nWalking domain assemblies (head = {head:#x}):")
    node = head
    seen = 0
    csharp_image = None
    csharp_image_for_strict = None
    while node != 0 and seen < 1024:
        assembly = reader.read_ptr(node)
        if assembly == 0:
            node = reader.read_ptr(node + 8)
            continue
        name_ptr = reader.read_ptr(assembly + 16)
        # NUL-terminated read
        if name_ptr != 0:
            buf = pm.read_bytes(name_ptr, 256)
            nul = buf.find(b"\x00")
            name = buf[:nul if nul >= 0 else len(buf)].decode("ascii", errors="replace")
        else:
            name = "<null>"
        image_addr = reader.read_ptr(assembly + offsets.assembly_image)
        print(f"  assembly @ {assembly:#x}  image @ {image_addr:#x}  name = {name!r}")
        if name == "Assembly-CSharp" and csharp_image_for_strict is None:
            csharp_image_for_strict = image_addr
        if name.startswith("Assembly-CSharp") and csharp_image is None:
            csharp_image = image_addr
        node = reader.read_ptr(node + 8)
        seen += 1

    print(f"\nseen {seen} assemblies")
    print(f"  first 'Assembly-CSharp*'       image = {csharp_image and hex(csharp_image)}")
    print(f"  strict 'Assembly-CSharp'       image = {csharp_image_for_strict and hex(csharp_image_for_strict)}")

    # Hash table at the strict image
    if csharp_image_for_strict:
        size = reader.read_uint32(csharp_image_for_strict + offsets.image_class_cache + offsets.hash_table_size)
        table = reader.read_ptr(csharp_image_for_strict + offsets.image_class_cache + offsets.hash_table_table)
        print(f"\n[strict Assembly-CSharp] class_cache: size={size}  table={table:#x}")
        # Sample first 30 buckets, list first class name in each
        named = 0
        for b in range(min(size, 1024)):
            ptr = reader.read_ptr(table + b * 8)
            chain_len = 0
            head_class = ptr
            while ptr != 0 and chain_len < 256:
                chain_len += 1
                ptr = reader.read_ptr(ptr + offsets.type_definition_next_class_cache)
            if chain_len > 0 and named < 10:
                # Read name of first class
                name_ptr = reader.read_ptr(head_class + offsets.type_definition_name)
                if name_ptr:
                    buf = pm.read_bytes(name_ptr, 128)
                    nul = buf.find(b"\x00")
                    cname = buf[:nul if nul >= 0 else 0].decode("ascii", errors="replace")
                    ns_ptr = reader.read_ptr(head_class + offsets.type_definition_namespace)
                    nbuf = pm.read_bytes(ns_ptr, 128) if ns_ptr else b""
                    nnul = nbuf.find(b"\x00")
                    cns = nbuf[:nnul if nnul >= 0 else 0].decode("ascii", errors="replace")
                    print(f"    bucket[{b}] chain_len={chain_len} first = {cns!r}.{cname!r}")
                    named += 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
