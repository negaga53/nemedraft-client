"""Exception types for the Mono memory reader."""

from __future__ import annotations


class MemoryReaderError(Exception):
    """Base class for memory-reader errors."""


class ProcessNotAttached(MemoryReaderError):
    """Raised when MTGA is not running or cannot be opened."""


class MonoBootstrapFailed(MemoryReaderError):
    """Raised when the Mono root domain or Assembly-CSharp image cannot be located."""


class OffsetsUnsupported(MemoryReaderError):
    """Raised when the running Unity version has no offset table."""


class MonoFieldMissing(MemoryReaderError):
    """Raised when a class or field cannot be found in the Mono image."""
