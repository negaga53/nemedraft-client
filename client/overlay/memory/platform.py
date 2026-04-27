"""Platform gating for the Mono memory reader."""

from __future__ import annotations

import sys

IS_SUPPORTED: bool = sys.platform == "win32"

_pymem_import_checked = False
_pymem_import_ok = False


def pymem_importable() -> bool:
    """Return whether the optional ``pymem`` dependency is installed.

    Returns:
        ``True`` once pymem has been imported successfully at least once.
    """
    global _pymem_import_checked, _pymem_import_ok
    if _pymem_import_checked:
        return _pymem_import_ok
    _pymem_import_checked = True
    if not IS_SUPPORTED:
        return False
    try:
        import pymem  # noqa: F401
        import pefile  # noqa: F401
    except ImportError:
        _pymem_import_ok = False
    else:
        _pymem_import_ok = True
    return _pymem_import_ok


def is_memory_supported() -> bool:
    """Return whether memory reads are usable in this environment."""
    return IS_SUPPORTED and pymem_importable()
