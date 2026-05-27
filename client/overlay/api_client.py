"""HTTP client for the NemeDraft prediction server API."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from client.overlay.auth_client import AuthClient
from client.overlay.env import ClientEnv

logger = logging.getLogger(__name__)


@dataclass
class Pick:
    """A single card pick result (mirrors server PickResult)."""

    card: str
    card_id: int = 0
    rank: int = 0
    score: float = 0.0
    gihwr: float = 0.0
    ata: float = 0.0
    iwd: float = 0.0
    mana_cost: str = ""
    colors: list[str] = field(default_factory=list)
    type_line: str = ""
    is_elite: bool = False
    stats_loaded: bool = True
    # Empty when no usable 17Lands data was found. Set to the format
    # whose bundle supplied gihwr/ata — may differ from the requested
    # format when the fallback ladder kicked in.
    stats_format: str = ""


@dataclass
class UserInfo:
    """User profile information from the server."""

    user_id: str
    email: str
    is_vip: bool


class NemeDraftClient:
    """Synchronous HTTP client for the NemeDraft server API.

    All calls attach the server JWT from :class:`AuthClient`.  On a 401
    response the client auto-refreshes the token and retries once.
    """

    def __init__(self, env: ClientEnv, auth: AuthClient) -> None:
        self._base = f"{env.server_url}:{env.server_port}"
        self._auth = auth
        self._http = httpx.Client(timeout=httpx.Timeout(10, connect=5))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(
        self,
        pack_cards: list[str],
        pool_cards: list[str],
        set_code: str,
        pack_number: int = 0,
        pick_number: int = 0,
        *,
        draft_format: str = "",
        arena_format: str = "",
        last_pick: str | None = None,
    ) -> list[Pick]:
        """Call ``POST /api/predict`` and return ranked picks.

        ``last_pick`` is the card the player chose from the *previous*
        pack/pick (or ``None`` at P1P1 / after a fresh login). The server
        uses it to backfill draft-history rows with the actual pick.
        """
        body: dict = {
            "pack_cards": pack_cards,
            "pool_cards": pool_cards,
            "set_code": set_code,
            "pack_number": pack_number,
            "pick_number": pick_number,
        }
        if draft_format:
            body["draft_format"] = draft_format
        if arena_format:
            body["arena_format"] = arena_format
        if last_pick is not None:
            body["last_pick"] = last_pick
        data = self._authed_request("POST", "/api/predict", json=body, timeout=5)
        if data is None:
            return []

        picks: list[Pick] = []
        for p in data.get("picks", []):
            picks.append(Pick(
                card=p["card"],
                card_id=p.get("card_id", 0),
                rank=p.get("rank", 0),
                score=p.get("score", 0.0),
                gihwr=p.get("gihwr", 0.0),
                ata=p.get("ata", 0.0),
                iwd=p.get("iwd", 0.0),
                mana_cost=p.get("mana_cost", ""),
                colors=p.get("colors", []),
                type_line=p.get("type_line", ""),
                is_elite=p.get("is_elite", False),
                stats_loaded=p.get("stats_loaded", True),
                stats_format=p.get("stats_format", ""),
            ))
        return picks

    def compute_signals(
        self,
        seen_cards: list[dict],
        set_code: str,
        *,
        draft_format: str = "",
    ) -> dict[str, float] | None:
        """Call ``POST /api/signals`` and return colour scores."""
        body: dict = {"seen_cards": seen_cards, "set_code": set_code}
        if draft_format:
            body["draft_format"] = draft_format
        data = self._authed_request("POST", "/api/signals", json=body, timeout=3)
        if data is None:
            return None
        return data.get("scores")

    def deck_suggestions(
        self,
        pool_cards: list[str],
        set_code: str,
        *,
        draft_format: str = "",
    ) -> dict | None:
        """Call ``POST /api/deck-suggestions`` and return archetype suggestions."""
        body: dict = {"pool_cards": pool_cards, "set_code": set_code}
        if draft_format:
            body["draft_format"] = draft_format
        data = self._authed_request("POST", "/api/deck-suggestions", json=body, timeout=5)
        if data is None:
            return None
        return data.get("suggestions")

    def health(self) -> dict | None:
        """Call ``GET /api/health`` (no auth required)."""
        try:
            resp = self._http.get(f"{self._base}/api/health", timeout=2)
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        return None

    def fetch_user_info(self) -> UserInfo | None:
        """Call ``GET /api/me`` and return the user's profile info."""
        # Outer timeout must exceed the server's upstream Supabase
        # timeout (5s, see server/routes/user_info.py) plus network
        # overhead — otherwise the client times out before the server
        # can return its 502 on a slow Supabase call.
        data = self._authed_request("GET", "/api/me", timeout=10)
        if data is None:
            return None
        return UserInfo(
            user_id=data.get("user_id", ""),
            email=data.get("email", ""),
            is_vip=data.get("is_vip", False),
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _authed_request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        timeout: float = 5,
    ) -> dict | None:
        """Make an authenticated request with automatic 401 retry."""
        token = self._auth.get_token()
        if not token:
            logger.warning("No auth token available — skipping %s %s", method, path)
            return None

        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp = self._http.request(
                method, url, json=json, headers=headers, timeout=timeout,
            )
        except httpx.HTTPError:
            logger.warning("Request failed: %s %s", method, path, exc_info=True)
            return None

        if resp.status_code == 401:
            # Token expired — refresh and retry once
            logger.info("Got 401 — refreshing token and retrying")
            session = self._auth.refresh()
            if not session:
                logger.warning("Token refresh failed — cannot retry %s %s", method, path)
                return None
            headers["Authorization"] = f"Bearer {session.token}"
            try:
                resp = self._http.request(
                    method, url, json=json, headers=headers, timeout=timeout,
                )
            except httpx.HTTPError:
                logger.warning("Retry failed: %s %s", method, path, exc_info=True)
                return None

        if resp.status_code != 200:
            logger.warning(
                "Server returned %d for %s %s: %s",
                resp.status_code, method, path, resp.text[:200],
            )
            return None

        return resp.json()
