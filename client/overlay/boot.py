"""Startup workers — auto-update check and background boot."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from client.overlay.api_client import NemeDraftClient
from client.overlay.arena_memory import get_arena_player_identity
from client.overlay.auth_client import AuthClient
from client.overlay.card_art import CardArtCache
from client.overlay.card_mapper import ArenaCardMapper
from client.overlay.config import OverlayConfig
from client.overlay.env import ClientEnv, save_arena_player_id
from client.overlay.i18n import Translator
from client.overlay.log_watcher import LogWatcher, extract_arena_player_id
from client.overlay.memory.platform import is_memory_supported
from client.overlay.memory_watcher import MemoryWatcher

logger = logging.getLogger("overlay")


class UpdateWorker(QThread):
    """Checks for a newer release on GitHub and downloads it if found.

    Emits ``progress`` with human-readable status strings, then either
    ``update_ready`` (with the path to the new binary) or ``no_update``
    when no action is needed.
    """

    progress = Signal(str)
    update_ready = Signal(object)   # Path to downloaded binary
    no_update = Signal()
    update_failed = Signal(str)

    def run(self) -> None:  # noqa: D401
        from client.overlay.updater import check_for_update, download_update
        from client.overlay.i18n import tr

        self.progress.emit(tr("update_checking"))
        try:
            result = check_for_update()
        except Exception as exc:
            logger.warning("Update check failed: %s", exc)
            self.no_update.emit()
            return

        if result is None:
            self.no_update.emit()
            return

        latest_version, download_url = result
        self.progress.emit(tr("update_downloading", version=latest_version))

        try:
            new_binary = download_update(
                download_url,
                progress_callback=self._on_download_progress,
            )
        except Exception as exc:
            logger.error("Update download failed: %s", exc, exc_info=True)
            self.update_failed.emit(str(exc))
            return

        self.update_ready.emit(new_binary)

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        from client.overlay.i18n import tr
        pct = int(downloaded / total * 100)
        self.progress.emit(tr("update_download_progress", pct=pct))


@dataclass
class BootResult:
    """Components built during background startup."""

    mapper: ArenaCardMapper
    auth_client: AuthClient
    api_client: NemeDraftClient
    art_cache: CardArtCache
    watcher: LogWatcher
    memory_watcher: MemoryWatcher | None
    server_supported_sets: list[str]
    has_arena_player_id: bool


@dataclass(frozen=True)
class ArenaIdentityResolution:
    """Arena player identity resolved from memory or Player.log."""

    player_id: str
    source: str
    display_name: str = ""


def resolve_arena_identity() -> ArenaIdentityResolution | None:
    """Resolve the Arena player ID from the best available local source."""
    arena_identity = get_arena_player_identity()
    if arena_identity is not None:
        player_id = arena_identity.player_id.strip()
        if player_id:
            return ArenaIdentityResolution(
                player_id=player_id,
                source="memory",
                display_name=arena_identity.display_name.strip(),
            )

    log_player_id = (extract_arena_player_id() or "").strip()
    if log_player_id:
        return ArenaIdentityResolution(player_id=log_player_id, source="log")

    return None


class BootWorker(QThread):
    """Loads minimal resources off the UI thread.

    Card mappings and Scryfall data are **not** loaded here — they are
    deferred to ``SetDataWorker`` once a draft set is detected.
    """

    progress = Signal(str)          # status message for the home tab
    finished_ok = Signal(object)    # BootResult
    finished_err = Signal(str)      # error message

    def __init__(
        self,
        args: argparse.Namespace,
        config: OverlayConfig,
        env: ClientEnv,
    ) -> None:
        super().__init__()
        self._args = args
        self._config = config
        self._env = env

    def run(self) -> None:  # noqa: D401 — Qt override
        try:
            args = self._args

            # 1. Scryfall refresh (optional) --------------------------------
            if args.update_scryfall:
                self.progress.emit("Updating Scryfall data...")
                from client.overlay.card_mapper import update_scryfall
                update_scryfall(Path(args.scryfall_dir))

            # 2. Card ID map only (lightweight) -----------------------------
            self.progress.emit("Loading card ID map...")
            mapper = ArenaCardMapper(
                scryfall_dir=Path(args.scryfall_dir),
                card_id_map_path=Path(args.card_id_map),
                lazy=True,
            )

            # Card translations for the active language.
            translator = Translator.instance()
            translator.load_card_translations(Path(args.scryfall_dir))

            # 3. Auth + API client ------------------------------------------
            self.progress.emit("Connecting to server...")
            arena_player_id = ""
            arena_identity = resolve_arena_identity()
            if arena_identity:
                arena_player_id = arena_identity.player_id
                if arena_identity.source == "memory":
                    logger.info(
                        "Arena player ID from memory: %s (display=%s)",
                        arena_player_id,
                        arena_identity.display_name or "unknown",
                    )
                else:
                    logger.info("Arena player ID from Player.log: %s", arena_player_id)
                save_arena_player_id(arena_player_id)
            else:
                arena_player_id = os.getenv("ARENA_PLAYER_ID", "") or ""
                if arena_player_id:
                    logger.warning(
                        "Using cached Arena player ID because memory reader is unavailable: %s",
                        arena_player_id,
                    )
                else:
                    logger.warning(
                        "Arena player ID not found via MTGA memory or Player.log "
                        "- sign-in will be unavailable"
                    )
            auth_client = AuthClient(self._env, arena_player_id=arena_player_id)
            api_client = NemeDraftClient(self._env, auth_client)

            # Only attempt auto-login when we have a player ID to send
            if arena_player_id and auth_client.try_auto_login():
                logger.info("Auto-login succeeded for %s", auth_client.user_email)
            elif arena_player_id and auth_client.session:
                logger.info(
                    "Session restored but expired — will retry refresh "
                    "(email=%s)", auth_client.user_email,
                )
            elif not arena_player_id:
                logger.info("Skipping auto-login — no arena player ID")
            else:
                logger.info("No stored session — user will need to sign in")

            # Query server for supported sets.
            server_supported_sets: list[str] = []
            health = api_client.health()
            if health:
                server_supported_sets = health.get("supported_sets", [])
                logger.info("Server supported sets: %s", server_supported_sets)

            # 4. Remaining light components ----------------------------------
            art_cache = CardArtCache(enabled=self._config.overlay.show_art)
            log_path = Path(args.log_file) if args.log_file else None
            watcher = LogWatcher(log_path=log_path)
            memory_watcher = MemoryWatcher() if is_memory_supported() else None

            self.finished_ok.emit(BootResult(
                mapper=mapper,
                auth_client=auth_client,
                api_client=api_client,
                art_cache=art_cache,
                watcher=watcher,
                memory_watcher=memory_watcher,
                server_supported_sets=server_supported_sets,
                has_arena_player_id=bool(arena_player_id),
            ))

        except Exception as exc:
            logger.exception("Boot worker failed")
            self.finished_err.emit(str(exc))
