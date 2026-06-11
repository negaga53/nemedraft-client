"""Regression guard: client.overlay.main must import cleanly without any
of the heavy deps that the PyInstaller spec excludes from release builds.

When this test fails, look at nemedraft_overlay.spec's ``excludes`` list,
find the module mentioned in the ModuleNotFoundError, and lazy-import it
inside the function(s) that actually need it (build-time tools never run
inside the frozen binary)."""
from __future__ import annotations

import importlib
import sys

import pytest

# Modules listed in nemedraft_overlay.spec's excludes. Keep this list in
# sync with the spec — any module here that's eagerly imported from
# overlay-reachable code will break the release binary.
_SPEC_EXCLUDED = [
    "torch",
    "torch_geometric",
    "sentence_transformers",
    "transformers",
    "fastapi",
    "uvicorn",
    "scipy",
    "matplotlib",
    "pandas",
    "polars",
    "pyarrow",
    "IPython",
    "notebook",
    "jupyter",
    "tensorboard",
    "pygments",
    "psutil",
    "tqdm",
]

# Overlay/common modules that may be cached in sys.modules from prior
# tests in the same session. We evict them so the freshly-blocked
# environment actually re-runs their top-level imports.
_OVERLAY_PREFIXES = ("client", "common.data", "common.inference")


def test_overlay_main_imports_without_excluded_modules() -> None:
    saved_modules = {m: sys.modules.get(m, _MISSING) for m in _SPEC_EXCLUDED}
    evicted: dict[str, object] = {}
    for name in list(sys.modules):
        if name.startswith(_OVERLAY_PREFIXES) or name in _SPEC_EXCLUDED:
            evicted[name] = sys.modules.pop(name)

    # Block each excluded module: setting to None makes ``import X`` raise
    # ModuleNotFoundError, exactly like the PyInstaller frozen env does.
    for name in _SPEC_EXCLUDED:
        sys.modules[name] = None  # type: ignore[assignment]

    try:
        importlib.import_module("client.overlay.main")
        importlib.import_module("client.overlay.boot")
        importlib.import_module("client.overlay.events")
        importlib.import_module("client.overlay.managers.worker_pool")
        importlib.import_module("client.overlay.managers.workers")
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"client.overlay.main triggered a forbidden import at startup: "
            f"{exc.name!r} is in nemedraft_overlay.spec's excludes. Lazy-"
            f"import it inside the function that actually uses it."
        )
    finally:
        for name in _SPEC_EXCLUDED:
            if saved_modules[name] is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]
        for name, mod in evicted.items():
            sys.modules.setdefault(name, mod)


_MISSING = object()
