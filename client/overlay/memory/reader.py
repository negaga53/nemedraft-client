"""Typed memory readers backed by ``pymem.Pymem``.

Wraps ``ReadProcessMemory`` calls with helpers for the Mono types we care
about: pointers, primitive ints, ASCII C-strings, UTF-16 Mono strings, and
managed arrays.
"""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING, Optional

from .exceptions import ProcessNotAttached

if TYPE_CHECKING:  # pragma: no cover
    from pymem import Pymem


logger = logging.getLogger(__name__)


class ProcessReader:
    """Thin pymem wrapper that mirrors HackF5.UnitySpy's ProcessFacade.

    Args:
        pm: A live :class:`pymem.Pymem` instance attached to MTGA.
        is_64bits: Whether the target process is 64-bit. MTG Arena is x64
            on Windows; this is parameterised only for clarity.
    """

    __slots__ = ("_pm", "_is_64bits", "_size_of_ptr")

    def __init__(self, pm: "Pymem", is_64bits: bool = True) -> None:
        self._pm = pm
        self._is_64bits = is_64bits
        self._size_of_ptr = 8 if is_64bits else 4

    @property
    def is_64bits(self) -> bool:
        return self._is_64bits

    @property
    def size_of_ptr(self) -> int:
        return self._size_of_ptr

    @property
    def pm(self) -> "Pymem":
        return self._pm

    # -------- raw reads ----------------------------------------------------

    def read_bytes(self, address: int, size: int) -> bytes:
        if address == 0:
            raise ProcessNotAttached("Refusing to dereference NULL pointer")
        return self._pm.read_bytes(address, size)

    def read_int8(self, address: int) -> int:
        return struct.unpack_from("<b", self.read_bytes(address, 1))[0]

    def read_uint8(self, address: int) -> int:
        return self.read_bytes(address, 1)[0]

    def read_int32(self, address: int) -> int:
        return struct.unpack_from("<i", self.read_bytes(address, 4))[0]

    def read_uint32(self, address: int) -> int:
        return struct.unpack_from("<I", self.read_bytes(address, 4))[0]

    def read_uint64(self, address: int) -> int:
        return struct.unpack_from("<Q", self.read_bytes(address, 8))[0]

    def read_ptr(self, address: int) -> int:
        if self._is_64bits:
            return self.read_uint64(address)
        return self.read_uint32(address)

    # -------- string reads -------------------------------------------------

    def read_ascii_string(self, address: int, max_size: int = 1024) -> str:
        """Read a null-terminated ASCII C-string at ``address``.

        Returns an empty string when ``address`` is NULL.
        """
        if address == 0:
            return ""
        buf = self._pm.read_bytes(address, max_size)
        nul = buf.find(b"\x00")
        if nul >= 0:
            buf = buf[:nul]
        try:
            return buf.decode("ascii", errors="replace")
        except UnicodeDecodeError:
            return ""

    def read_ascii_string_at_ptr(self, address: int, max_size: int = 1024) -> str:
        """Dereference a pointer at ``address`` and read an ASCII string."""
        ptr = self.read_ptr(address)
        return self.read_ascii_string(ptr, max_size=max_size)

    # -------- array reads --------------------------------------------------

    def read_int32_array(self, address: int, max_elements: int = 256) -> Optional[list[int]]:
        """Dereference a Mono ``int[]`` at ``address`` and return its elements.

        ``MonoArray`` x64 layout:

        * MonoObject header (vtable + sync block): 2 pointers
        * bounds pointer: 1 pointer
        * length: int32 (immediately after the bounds pointer)
        * vector data: ``length`` elements of ``int32`` each, starting at
          offset ``4 * size_of_ptr``

        Args:
            address: Address holding the pointer to the MonoArray.
            max_elements: Sanity cap on the array length to avoid runaway
                reads on a corrupt pointer.

        Returns:
            Element list, or ``None`` when the pointer is NULL or the
            length looks corrupt.
        """
        ptr = self.read_ptr(address)
        if ptr == 0:
            return None
        length_offset = self._size_of_ptr * 3
        try:
            length = self.read_int32(ptr + length_offset)
        except Exception:
            return None
        if length < 0 or length > max_elements:
            logger.debug(
                "Refusing int32[] read at %#x — length=%d out of range",
                ptr, length,
            )
            return None
        if length == 0:
            return []
        data_offset = self._size_of_ptr * 4
        try:
            buf = self._pm.read_bytes(ptr + data_offset, 4 * length)
        except Exception:
            return None
        return list(struct.unpack_from(f"<{length}i", buf))

    def read_managed_string(self, address: int) -> Optional[str]:
        """Dereference a Mono ``System.String*`` and return its UTF-16 value.

        Layout (struct ``_MonoString``):

        * MonoObject header: 2 pointers
        * length: int32
        * chars: UTF-16 code units, ``length`` of them

        Args:
            address: Address holding a pointer to the Mono string.

        Returns:
            The decoded string, or ``None`` when the pointer is NULL.
        """
        ptr = self.read_ptr(address)
        if ptr == 0:
            return None
        header = self._size_of_ptr * 2
        length = self.read_int32(ptr + header)
        if length <= 0:
            return ""
        if length > 1 << 20:  # sanity cap: 1M chars
            logger.debug("Refusing to read absurdly long Mono string at %x", ptr)
            return None
        try:
            data = self._pm.read_bytes(ptr + header + 4, 2 * length)
        except Exception:
            logger.debug("Failed to read Mono string body at %x", ptr, exc_info=True)
            return None
        try:
            return data.decode("utf-16-le", errors="replace")
        except UnicodeDecodeError:
            return None
