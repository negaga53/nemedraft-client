"""In-process Mono memory reader for MTG Arena.

This package replaces the external mtga-tracker-daemon sidecar. It uses
``pymem`` (``OpenProcess`` + ``ReadProcessMemory``, no DLL injection) to read
MTG Arena's Mono runtime state directly from the overlay process. The walker
is a minimal port of HackF5.UnitySpy targeting only the field paths the
overlay actually consumes (player identity, current event lobby, draft
state).

The package is Windows-only. Public callers should go through
:mod:`client.overlay.arena_memory`, which guards every entry point with
:func:`platform.is_memory_supported`.
"""
