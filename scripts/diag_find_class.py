"""Search every loaded assembly for WrapperController."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)


TARGETS = {
    "WrapperController",
    "AccountClient",
    "AccountInformation",
    "EventPageContentController",
    "PlayerEvent",
    "EventInfo",
    "CourseData",
    "SceneLoader",
    "PAPA",
}


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

    head = reader.read_ptr(domain + offsets.referenced_assemblies)
    seen_total = 0
    matches = {t: [] for t in TARGETS}
    node = head
    for _ in range(1024):
        if node == 0:
            break
        assembly = reader.read_ptr(node)
        if not assembly:
            node = reader.read_ptr(node + 8)
            continue
        name_addr = reader.read_ptr(assembly + 16)
        asm_name = reader.read_ascii_string(name_addr, 128)
        image_addr = reader.read_ptr(assembly + offsets.assembly_image)
        if image_addr == 0:
            node = reader.read_ptr(node + 8)
            continue
        try:
            cache_size = reader.read_uint32(
                image_addr + offsets.image_class_cache + offsets.hash_table_size
            )
            cache_table = reader.read_ptr(
                image_addr + offsets.image_class_cache + offsets.hash_table_table
            )
        except Exception:
            node = reader.read_ptr(node + 8)
            continue
        if cache_size == 0 or cache_size > 1 << 24 or cache_table == 0:
            node = reader.read_ptr(node + 8)
            continue
        # Walk every chain in this hash table — single-step using offset 0x108
        klass_count = 0
        for b in range(cache_size):
            ptr = reader.read_ptr(cache_table + b * 8)
            depth = 0
            while ptr != 0 and depth < 1000:
                klass_count += 1
                try:
                    name_p = reader.read_ptr(ptr + offsets.type_definition_name)
                    cname = reader.read_ascii_string(name_p, 128) if name_p else ""
                except Exception:
                    cname = ""
                if cname in TARGETS:
                    matches[cname].append(asm_name)
                try:
                    ptr = reader.read_ptr(ptr + offsets.type_definition_next_class_cache)
                except Exception:
                    break
                depth += 1
        seen_total += 1
        if klass_count > 100:
            print(f"  asm {asm_name!r}: {klass_count} classes (size={cache_size})")
        node = reader.read_ptr(node + 8)

    print(f"\nscanned {seen_total} assemblies\n")
    for target, asms in matches.items():
        if asms:
            print(f"  {target!r} found in: {asms}")
        else:
            print(f"  {target!r} NOT FOUND")
    return 0


if __name__ == "__main__":
    sys.exit(main())
