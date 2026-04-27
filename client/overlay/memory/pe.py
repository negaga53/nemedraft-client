"""PE export resolution + epilogue disassembly to recover ``mono_root_domain``.

We only need to bootstrap a single function — ``mono_get_root_domain`` — and
follow its return-value alias to the global ``mono_root_domain`` pointer. We
do NOT call into MTGA: this is read-only memory inspection, identical to what
HackF5.UnitySpy's :class:`AssemblyImageFactory` does (see
``AssemblyImageFactory.cs:123``).

The disassembly assumes Mono's standard codegen for x64 PE builds:

.. code-block:: text

    48 8B 05 XX XX XX XX    ; mov rax, [rip + disp32]
    C3                      ; ret

If MSVC re-orders or inlines this, ``find_root_domain_pointer`` returns
``None`` and the caller falls back to the log watcher.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

from .exceptions import MonoBootstrapFailed
from .reader import ProcessReader

logger = logging.getLogger(__name__)


# Expected prologue: `mov rax, [rip+disp32]` -- 48 8B 05 XX XX XX XX
_RIP_RELATIVE_LOAD_PREFIX = b"\x48\x8b\x05"
_RIP_PLUS_OFFSET_OFFSET = 3   # disp32 starts 3 bytes into the function
_RIP_VALUE_OFFSET = 7         # RIP after the mov instruction = func + 7


def find_export_rva(dll_path: str | Path, export_name: str) -> int | None:
    """Return the RVA (relative virtual address) of an exported function.

    Args:
        dll_path: Filesystem path to the on-disk DLL (the same one MTGA
            loaded — read from ``pymem.process.module_from_name``'s
            ``filename`` attribute).
        export_name: ASCII name of the export, e.g. ``mono_get_root_domain``.

    Returns:
        Export RVA, or ``None`` when the export is not found or the PE is
        unparseable.
    """
    import pefile

    try:
        pe = pefile.PE(str(dll_path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
        )
    except Exception:
        logger.debug("Failed to parse PE %s", dll_path, exc_info=True)
        return None

    export_dir = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
    if export_dir is None:
        logger.debug("PE %s has no export directory", dll_path)
        return None

    target = export_name.encode("ascii")
    for symbol in export_dir.symbols:
        if symbol.name == target:
            return int(symbol.address)
    return None


def find_root_domain_pointer(reader: ProcessReader, function_address: int) -> int:
    """Return the address of the global ``mono_root_domain`` variable.

    Args:
        reader: Active :class:`ProcessReader` attached to MTGA.
        function_address: Absolute address of ``mono_get_root_domain`` in
            MTGA's address space.

    Returns:
        Address of the global ``MonoDomain*`` variable. Dereference it to get
        the actual ``MonoDomain*``.

    Raises:
        MonoBootstrapFailed: When the function prologue does not match the
            expected ``mov rax, [rip+disp32]`` codegen.
    """
    if not reader.is_64bits:
        raise MonoBootstrapFailed("32-bit Mono is not supported")

    try:
        prefix = reader.read_bytes(function_address, 3)
    except Exception as exc:
        raise MonoBootstrapFailed(
            f"Cannot read mono_get_root_domain prologue: {exc}"
        ) from exc

    if prefix != _RIP_RELATIVE_LOAD_PREFIX:
        raise MonoBootstrapFailed(
            f"Unexpected mono_get_root_domain prologue {prefix.hex()}; "
            "Mono codegen may have changed."
        )

    disp_bytes = reader.read_bytes(function_address + _RIP_PLUS_OFFSET_OFFSET, 4)
    disp32 = struct.unpack_from("<i", disp_bytes)[0]
    return function_address + disp32 + _RIP_VALUE_OFFSET
