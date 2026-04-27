"""Sanity check the assembly we found is really Assembly-CSharp + locate class_cache."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)


def main() -> int:
    import pefile
    import pymem
    from pymem.process import module_from_name

    from client.overlay.memory.offsets import select_offsets
    from client.overlay.memory.pe import find_export_rva, find_root_domain_pointer
    from client.overlay.memory.reader import ProcessReader

    pm = pymem.Pymem("MTGA.exe")
    module = module_from_name(pm.process_handle, "mono-2.0-bdwgc.dll")
    print(f"mono dll = {module.filename}")

    # Read full PE version info from disk
    pe = pefile.PE(module.filename, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    if hasattr(pe, "VS_VERSIONINFO"):
        for fileinfo in pe.FileInfo[0]:
            if fileinfo.Key.decode() == "StringFileInfo":
                for table in fileinfo.StringTable:
                    for k, v in table.entries.items():
                        if b"Version" in k:
                            print(f"  PE {k.decode()} = {v.decode()!r}")

    offsets = select_offsets(None)
    reader = ProcessReader(pm, is_64bits=True)
    rva = find_export_rva(module.filename, "mono_get_root_domain")
    domain_var = find_root_domain_pointer(reader, int(module.lpBaseOfDll) + rva)
    domain = reader.read_ptr(domain_var)

    head = reader.read_ptr(domain + offsets.referenced_assemblies)
    print(f"\nAssembly walk:")
    node = head
    for _ in range(1024):
        if node == 0:
            break
        assembly = reader.read_ptr(node)
        if assembly:
            name_addr = reader.read_ptr(assembly + 16)
            name = reader.read_ascii_string(name_addr, 128)
            if name == "Assembly-CSharp":
                print(f"  assembly @ {assembly:#x}")
                # Dump all 8-byte values from start of assembly for ~64 bytes
                print("  assembly struct (8-byte slots):")
                for off in range(0, 0x80, 8):
                    val = reader.read_uint64(assembly + off)
                    extra = ""
                    # If it looks like a pointer, try reading a string at it
                    if 0x10000 < val < 0x7FFFFFFFFFFF:
                        try:
                            s = reader.read_ascii_string(val, 64)
                            if s and s.isprintable() and 1 < len(s) < 64:
                                extra = f"  -> str = {s!r}"
                        except Exception:
                            pass
                    print(f"    +{off:#04x} = {val:#018x}{extra}")
                break
        node = reader.read_ptr(node + 8)

    # Now scan a range of offsets after assembly.image (0x60) for hash-table-shaped data
    image_addr = reader.read_ptr(assembly + 0x60)
    print(f"\nimage @ {image_addr:#x} (from assembly+0x60)")
    # Try to dump the first 0x600 bytes of the image as 8-byte slots, looking for a pointer that points to a real array of class pointers
    print("scanning image offsets 0x300..0x500 for hash-table-shaped data:")
    for off in range(0x300, 0x520, 8):
        size_at = reader.read_uint32(image_addr + off)
        table_at = reader.read_ptr(image_addr + off + 8)
        # Hash size should be a small prime, table should look like a heap pointer
        if 100 < size_at < 100000 and 0x10000 < table_at < 0x7FFFFFFFFFFF:
            # Check if table is a valid pointer to pointers
            try:
                first_class = reader.read_ptr(table_at)
                if 0x10000 < first_class < 0x7FFFFFFFFFFF:
                    name_ptr = reader.read_ptr(first_class + 0x48)
                    cname = reader.read_ascii_string(name_ptr, 64)
                    print(
                        f"  image+{off:#x}: size={size_at}, table={table_at:#x}, "
                        f"first[0]={first_class:#x} -> name={cname!r}"
                    )
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
