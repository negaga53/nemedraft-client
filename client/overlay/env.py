"""Client environment configuration — loads .env.client + .env.client.local."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _project_root() -> Path:
    """Return the project root (directory containing .env.client).

    When running as a PyInstaller bundle, returns the directory containing
    the executable so that ``.env.client`` and ``.env.client.local`` live
    alongside the binary.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Walk up from this file: client/overlay/env.py → project root
    return Path(__file__).resolve().parent.parent.parent


def bundle_root() -> Path:
    """Return the root directory for bundled data files.

    In a frozen PyInstaller build, returns ``sys._MEIPASS`` (the temp
    directory where bundled data is extracted).  In development, returns
    the project root so that relative data paths like
    ``data/processed/card_id_map.json`` resolve correctly.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent.parent


@dataclass
class ClientEnv:
    """Typed client environment variables."""

    server_url: str
    server_port: int
    supabase_url: str
    supabase_anon_key: str

    # Persisted tokens (from .env.client.local)
    saved_server_token: str
    saved_supabase_refresh_token: str
    saved_user_email: str


def load_client_env() -> ClientEnv:
    """Load client env from .env.client then .env.client.local (overrides)."""
    # Base config: in a frozen build, .env.client is bundled inside the
    # executable (extracted to _MEIPASS).  In dev it lives at project root.
    load_dotenv(bundle_root() / ".env.client", override=False)

    # Local overrides (gitignored — stored tokens, custom server URL).
    # Also load a .env.client next to the exe so users can customise.
    exe_root = _project_root()
    load_dotenv(exe_root / ".env.client", override=True)
    load_dotenv(exe_root / ".env.client.local", override=True)

    return ClientEnv(
        server_url=os.getenv("SERVER_URL", "http://localhost"),
        server_port=int(os.getenv("SERVER_PORT", "9000")),
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY", ""),
        saved_server_token=os.getenv("SAVED_SERVER_TOKEN", ""),
        saved_supabase_refresh_token=os.getenv("SAVED_SUPABASE_REFRESH_TOKEN", ""),
        saved_user_email=os.getenv("SAVED_USER_EMAIL", ""),
    )


def _upsert_env_keys(updates: dict[str, str | None]) -> None:
    """Read .env.client.local, merge *updates*, and write back.

    Keys whose value is ``None`` are removed; all others are set.
    """
    path = _project_root() / ".env.client.local"
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, _, value = stripped.partition("=")
            if key:
                existing[key] = value
    for k, v in updates.items():
        if v is None:
            existing.pop(k, None)
        else:
            existing[k] = v
    lines = ["# Auto-populated by the overlay — do not edit manually\n"]
    for k, v in existing.items():
        lines.append(f"{k}={v}\n")
    path.write_text("".join(lines), encoding="utf-8")


def save_client_tokens(
    server_token: str,
    refresh_token: str,
    email: str,
) -> None:
    """Persist tokens to .env.client.local."""
    _upsert_env_keys({
        "SAVED_SERVER_TOKEN": server_token,
        "SAVED_SUPABASE_REFRESH_TOKEN": refresh_token,
        "SAVED_USER_EMAIL": email,
    })


def save_arena_player_id(player_id: str) -> None:
    """Persist the Arena player ID to .env.client.local."""
    _upsert_env_keys({"ARENA_PLAYER_ID": player_id})


def clear_client_tokens() -> None:
    """Remove stored tokens from .env.client.local."""
    _upsert_env_keys({
        "SAVED_SERVER_TOKEN": None,
        "SAVED_SUPABASE_REFRESH_TOKEN": None,
        "SAVED_USER_EMAIL": None,
    })
