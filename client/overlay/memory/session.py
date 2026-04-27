"""Process discovery + Mono bootstrap.

:class:`MemorySession` is a singleton that owns the live ``pymem.Pymem``
attachment plus a cached :class:`AssemblyImage`. Public entry points
(``client.overlay.arena_memory``) call :meth:`ensure_attached` before any
read.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .exceptions import (
    MonoBootstrapFailed,
    OffsetsUnsupported,
    ProcessNotAttached,
)
from .mono import AssemblyImage
from .offsets import MonoOffsets, select_offsets
from .pe import find_export_rva, find_root_domain_pointer
from .platform import is_memory_supported
from .reader import ProcessReader

logger = logging.getLogger(__name__)


_PROCESS_NAME = "MTGA.exe"
_MONO_LIBRARY = "mono-2.0-bdwgc.dll"
# Modern MTGA splits gameplay code across multiple assemblies. ``Core`` is
# the primary one (holds WrapperController, PAPA, EventPageContentController,
# CourseData). ``SharedClientCore`` holds AccountInformation. We keep
# ``Assembly-CSharp`` as a fallback because some classes (e.g., the legacy
# SceneLoader chain) still live there.
_ASSEMBLY_PRIORITIES = ("Core", "SharedClientCore", "Assembly-CSharp")
_ATTACH_RETRY_SECONDS = 5.0
_MAX_ASSEMBLIES = 1024


class MemorySession:
    """Singleton holding the live MTGA attachment and Mono image."""

    _instance: Optional["MemorySession"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pm = None
        self._reader: Optional[ProcessReader] = None
        self._offsets: Optional[MonoOffsets] = None
        self._image: Optional[AssemblyImage] = None
        self._pid: Optional[int] = None
        self._next_attach_attempt: float = 0.0
        self._failed_once = False

    @classmethod
    def instance(cls) -> "MemorySession":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def reader(self) -> Optional[ProcessReader]:
        return self._reader

    @property
    def image(self) -> Optional[AssemblyImage]:
        return self._image

    @property
    def pid(self) -> Optional[int]:
        return self._pid

    def is_attached(self) -> bool:
        return self._image is not None and self._pm is not None

    def ensure_attached(self) -> bool:
        """Attach to MTGA and bootstrap Mono if not already done.

        Returns:
            ``True`` on success; ``False`` if MTGA is not running, the
            platform doesn't support memory reads, or bootstrap fails.
        """
        if not is_memory_supported():
            return False
        with self._lock:
            if self.is_attached() and self._still_alive():
                return True
            now = time.monotonic()
            if not self.is_attached() and now < self._next_attach_attempt:
                return False
            self._next_attach_attempt = now + _ATTACH_RETRY_SECONDS
            try:
                self._attach()
            except (ProcessNotAttached, MonoBootstrapFailed, OffsetsUnsupported) as exc:
                if not self._failed_once:
                    logger.info("Arena memory unavailable: %s", exc)
                    self._failed_once = True
                else:
                    logger.debug("Arena memory unavailable: %s", exc)
                self._reset()
                return False
            except Exception:
                logger.warning(
                    "Unexpected error attaching to MTG Arena memory; falling back to logs",
                    exc_info=True,
                )
                self._reset()
                return False
            self._failed_once = False
            return True

    def detach(self) -> None:
        with self._lock:
            self._reset()

    # -------- internals ----------------------------------------------------

    def _still_alive(self) -> bool:
        if self._pm is None or self._reader is None or self._image is None:
            return False
        try:
            # Reading any int at the image base proves the process is still
            # accepting reads. ReadProcessMemory raises on a dead handle.
            self._reader.read_uint32(self._image.address)
            return True
        except Exception:
            return False

    def _reset(self) -> None:
        if self._pm is not None:
            try:
                self._pm.close_process()
            except Exception:
                pass
        self._pm = None
        self._reader = None
        self._offsets = None
        self._image = None
        self._pid = None

    def _attach(self) -> None:
        import pymem
        from pymem.process import module_from_name

        try:
            pm = pymem.Pymem(_PROCESS_NAME)
        except pymem.exception.ProcessNotFound as exc:
            raise ProcessNotAttached(
                f"MTG Arena ({_PROCESS_NAME}) is not running"
            ) from exc
        except Exception as exc:
            raise ProcessNotAttached(
                f"Cannot attach to MTG Arena: {exc}"
            ) from exc

        module = module_from_name(pm.process_handle, _MONO_LIBRARY)
        if module is None:
            raise MonoBootstrapFailed(
                f"MTGA does not have {_MONO_LIBRARY} loaded yet"
            )

        offsets = select_offsets(getattr(module, "file_version", None))
        if offsets is None:
            raise OffsetsUnsupported(
                f"Unsupported {_MONO_LIBRARY} version "
                f"{getattr(module, 'file_version', '<unknown>')}"
            )

        reader = ProcessReader(pm, is_64bits=offsets.is_64bits)

        rva = find_export_rva(module.filename, "mono_get_root_domain")
        if rva is None:
            raise MonoBootstrapFailed(
                "mono_get_root_domain export not found"
            )
        function_address = int(module.lpBaseOfDll) + rva
        domain_variable = find_root_domain_pointer(reader, function_address)
        domain = reader.read_ptr(domain_variable)
        if domain == 0:
            raise MonoBootstrapFailed("mono_root_domain is NULL")

        image_addresses = self._find_assembly_images(reader, offsets, domain)
        if not image_addresses:
            raise MonoBootstrapFailed(
                f"None of {_ASSEMBLY_PRIORITIES!r} loaded yet"
            )
        image = AssemblyImage(reader, offsets, image_addresses)

        self._pm = pm
        self._reader = reader
        self._offsets = offsets
        self._image = image
        self._pid = pm.process_id
        logger.info(
            "Attached to MTG Arena pid=%d; loaded %d assembly image(s)",
            self._pid, len(image_addresses),
        )

    @staticmethod
    def _find_assembly_images(
        reader: ProcessReader, offsets: MonoOffsets, domain: int
    ) -> list[int]:
        """Walk ``MonoDomain.domain_assemblies`` and collect target images.

        ``MonoDomain.domain_assemblies`` is a singly-linked list; each node
        has ``[ptr to MonoAssembly, ptr to next node]``. ``MonoAssembly.
        aname.name`` is at offset ``2 * size_of_ptr`` and is a
        NUL-terminated C-string.

        Returns:
            Image addresses for the assemblies named in ``_ASSEMBLY_PRIORITIES``,
            ordered by priority. Missing assemblies are silently dropped.
        """
        head = reader.read_ptr(domain + offsets.referenced_assemblies)
        if head == 0:
            raise MonoBootstrapFailed("MonoDomain has no referenced assemblies")

        ptr_size = reader.size_of_ptr
        found: dict[str, int] = {}
        wanted = set(_ASSEMBLY_PRIORITIES)
        node = head
        for _ in range(_MAX_ASSEMBLIES):
            if node == 0 or len(found) == len(wanted):
                break
            assembly = reader.read_ptr(node)
            if assembly == 0:
                node = reader.read_ptr(node + ptr_size)
                continue
            name_addr = reader.read_ptr(assembly + (ptr_size * 2))
            name = reader.read_ascii_string(name_addr, max_size=128) if name_addr else ""
            if name in wanted and name not in found:
                image_addr = reader.read_ptr(assembly + offsets.assembly_image)
                if image_addr != 0:
                    found[name] = image_addr
            node = reader.read_ptr(node + ptr_size)

        ordered: list[int] = []
        for asm_name in _ASSEMBLY_PRIORITIES:
            if asm_name in found:
                logger.info(
                    "Located %s assembly image @ %#x",
                    asm_name, found[asm_name],
                )
                ordered.append(found[asm_name])
        return ordered
