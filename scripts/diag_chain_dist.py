"""Histogram chain lengths and probe candidate next_class_cache offsets."""

from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)


def main() -> int:
    import pymem
    from pymem.process import module_from_name

    from client.overlay.memory.offsets import select_offsets
    from client.overlay.memory.pe import find_export_rva, find_root_domain_pointer
    from client.overlay.memory.reader import ProcessReader

    pm = pymem.Pymem("MTGA.exe")
    module = module_from_name(pm.process_handle, "mono-2.0-bdwgc.dll")
    offsets = select_offsets(None)
    reader = ProcessReader(pm, is_64bits=True)

    rva = find_export_rva(module.filename, "mono_get_root_domain")
    domain_var = find_root_domain_pointer(reader, int(module.lpBaseOfDll) + rva)
    domain = reader.read_ptr(domain_var)

    # Walk to Assembly-CSharp image
    head = reader.read_ptr(domain + offsets.referenced_assemblies)
    image_addr = 0
    node = head
    for _ in range(1024):
        if node == 0:
            break
        assembly = reader.read_ptr(node)
        if assembly:
            name_addr = reader.read_ptr(assembly + 16)
            name = reader.read_ascii_string(name_addr, 128)
            if name == "Assembly-CSharp":
                image_addr = reader.read_ptr(assembly + offsets.assembly_image)
                break
        node = reader.read_ptr(node + 8)
    print(f"Assembly-CSharp image @ {image_addr:#x}")

    cache_size = reader.read_uint32(image_addr + offsets.image_class_cache + offsets.hash_table_size)
    cache_table = reader.read_ptr(image_addr + offsets.image_class_cache + offsets.hash_table_table)
    print(f"cache size = {cache_size}, table = {cache_table:#x}")

    # Find a known live class first to inspect its bytes
    known_class = 0
    for b in range(cache_size):
        ptr = reader.read_ptr(cache_table + b * 8)
        if ptr:
            known_class = ptr
            break
    print(f"first class @ {known_class:#x}")

    # Dump 80 bytes starting at name offset to inspect layout
    print("\nbytes at known_class[0xE0..0x130] (8-byte chunks as ptrs):")
    base = known_class
    for off in range(0xE0, 0x130, 8):
        val = reader.read_uint64(base + off)
        print(f"  +{off:#04x} = {val:#018x}")

    # Try all candidate next_class_cache offsets and report total class count
    candidates = [0xE4, 0xE8, 0xF0, 0xF8, 0x100, 0x108, 0x110, 0x118, 0x120]
    print("\nChain-walk totals per candidate next_class_cache offset:")
    for cand in candidates:
        total = 0
        chains = Counter()
        seen = set()
        for b in range(cache_size):
            ptr = reader.read_ptr(cache_table + b * 8)
            chain_len = 0
            while ptr != 0 and chain_len < 1000 and ptr not in seen:
                seen.add(ptr)
                chain_len += 1
                try:
                    ptr = reader.read_ptr(ptr + cand)
                except Exception:
                    break
            chains[chain_len] += 1
            total += chain_len
        print(
            f"  next_class_cache=+{cand:#x}: total classes={total}, "
            f"chain-len histogram (top 5): {chains.most_common(5)}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
